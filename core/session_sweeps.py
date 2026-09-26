"""The two transcript sweeps that do NOT run on the hand-off's clock, and the anchor a
collector takes when its own read could not be made in time.

`core/session_collector.py` sweeps the routing signal once per escalating turn, before the
agent's turn is submitted, because an agent asked to work in a session whose newest message
is `[[NEEDS_AGENT]]` is an agent that answers with `[[NEEDS_AGENT]]`. Two more moments need
the same read and cannot have it there, and both are defects the live run measured
(`qa/live-run-v3.md`):

* **D6-residue -- the marker a DEADLINE turn leaves behind.** That branch hands the work to
  the agent while the voice turn is still running, so at sweep time the signal does not exist
  yet. It arrives afterwards, the agent reads it as its own history and writes one itself; the
  collector then ships that echo, and the user receives a stripped copy of their own question
  instead of the result. The fix is ordering, not speed: the signal is deleted at the entry
  of the NEXT turn for that session, which is the last moment before any agent reads the
  history again. `note_dead_turn` marks the session and `sweep_residue` does the work; the
  mark is in-process only, because a restart loses it and the next escalating turn's own
  sweep still finds the signal.
* **D15 -- the read that timed out before anything was armed.** That read is bounded by half
  a second on a path that has already spent the whole voice budget, and when it timed out the
  hand-off simply returned: the user was acknowledged and nobody was left to fetch the answer.
  The collector is armed anyway, on an anchor resolved here, off Alice's clock.
* **A read that came back with nothing to point at.** A session created microseconds ago (C8)
  holds no message at all, and one whose every message was a routing signal holds none once
  this sweep has deleted them. **The two are not the same case, and the difference is the
  whole anchor rule.** `""` on an EMPTY session is the truth -- "everything this session ever
  holds is newer" is exactly right when it has never held anything, and that is what the C8
  branch has always shipped, pinned by
  `test_a_cold_first_turn_enqueues_the_collector_that_will_ship_the_answer`. `""` on a session
  that *had* messages and has just had all of them deleted is a lie: it names the whole history
  as newer. So only the second case comes here.

**The trade-off, stated because it is one.** Arming with no anchor at all would mean
`since_message_id=""`, which `collect_reply` reads as "everything in the session is newer" --
the whole conversation replayed into Telegram, which is worse than losing one answer and is
exactly what the one-snapshot rule exists to prevent. A collector armed that way is also armed
for its whole 600 s ceiling, so the damage is not a rare interleaving but every second turn of
every session: the next turn's answer arrives in this turn's delivery. So an anchor is resolved
LATER, and `anchor` recovers what the delay would otherwise cost: the task message is the one
thing known to be written after the hand-off, so a read that contains it is cut there and the
anchor is the newest surviving message before it -- or, when nothing survives before it, the
task message itself, whose own text is a user message and is therefore not collected. A read
taken before the submit is used as it stands. If no boundary appears inside `ANCHOR_TIMEOUT_S`
the loss is stated in the user's chat -- a bounded loss of an answer is acceptable, a silent
one is not, because the ack already promised a message that will not arrive.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING, Final

import httpx

from app.config import Config
from core import routing
from core.opencode.client import MessageRecord, OpencodeError

if TYPE_CHECKING:  # the collector composes this module, so the edge cannot be a runtime one
    from core.opencode.client import OpencodeClient

__all__ = ["ANCHOR_LOST", "ANCHOR_TIMEOUT_S", "MARKER_TIMEOUT_S", "SessionSweeper"]

#: What an escalating turn may spend on the transcript: reading it once, and per routing
#: signal deleting the message the sweep found. It has already spent the whole voice budget,
#: so this is small and absolute -- a server that will not answer here costs the answer, never
#: the acknowledgement. The residue sweep and the deferred arming use the same bound, because
#: they are the same read at a different moment.
MARKER_TIMEOUT_S: Final = 0.5
#: How long the anchor of a collector may take to resolve when the hand-off's own read could
#: not do it inside `MARKER_TIMEOUT_S`. Spent by a task the user has already been released
#: from, and generous, because the server was BUSY rather than absent: the live run's read
#: timed out at 0.5 s while the server worked through a cold agent turn and answered a moment
#: later.
ANCHOR_TIMEOUT_S: Final = 10.0
#: What the user is told when even that could not be read.
ANCHOR_LOST: Final = "Ответ агента потерян: opencode не ответил вовремя. Спросите ещё раз."

#: The two actions this module needs from the hand-off that owns it: write the collector's job,
#: and put one sentence in the user's chat. Both stay in `core/session_collector.py` and
#: `core/tools/telegram_tool.py`; this module owns the retry and the anchor, and nothing else.
Enqueue: Final = Callable[[str, str, str], Awaitable[None]]
Tell: Final = Callable[[str], Awaitable[None]]


class SessionSweeper:
    """The off-clock half of the escalation: a late residue sweep and a late anchor.

    One object per collector, holding the two pieces of state these two sweeps need: the
    sessions whose last turn was a deadline turn, and the anchor-resolution tasks still in
    flight. The tasks are held so one is never garbage collected mid-read, and so a shutdown
    can see what the process is waiting for.
    """

    def __init__(
        self, cfg: Config, logger: logging.Logger, enqueue: Enqueue, tell: Tell | None = None
    ) -> None:
        self._cfg = cfg
        self._log = logger
        self._enqueue = enqueue
        self._tell = tell
        self._dead_turns: set[str] = set()
        self._pending: set[asyncio.Task[None]] = set()
        #: The gap between two attempts at a read that did not answer. The agent's own answer
        #: is seconds away, so a tight loop would only add requests.
        self._retry_s = cfg.r2d2_event_poll_interval

    # -- the marker a dead turn leaves behind (D6-residue) ------------------

    def note_dead_turn(self, session_id: str) -> None:
        """Record that this session's last turn may still be writing a routing signal."""
        self._dead_turns.add(session_id)

    async def sweep_residue(self, client: OpencodeClient, app_id: str, session_id: str) -> bool:
        """Delete an unwritten-at-the-time signal before this turn's agent reads history.

        One `list_messages`, from which `routing.transcript_sweep` decides the deletions
        alone -- no anchor is taken here, so the one-snapshot rule that governs the hand-off
        (one read decides BOTH the deletions and the anchor, then delete, then enqueue) is
        untouched. A second read here would be a second chance to anchor at a message this
        sweep is about to delete, which is how a collector starts replaying the whole
        conversation into Telegram.

        A session that owes no sweep costs one set lookup and no request at all, so the common
        turn is unaffected. A read that cannot be made keeps the debt: an unswept marker is
        survivable, and a wasted voice budget is not.
        """
        if session_id not in self._dead_turns:
            return False
        try:
            records = await asyncio.wait_for(client.list_messages(session_id), MARKER_TIMEOUT_S)
        except (TimeoutError, httpx.HTTPError, OpencodeError) as exc:
            self._log.warning(
                "opencode: the routing signal a deadline turn left in session %s of %s could not be "
                "swept before this turn (%s: %s); it is swept on the next turn.",
                session_id, app_id, type(exc).__name__, exc,
            )
            return False
        for message_id in self._sweep(records).signal_ids:
            await self.erase(client, app_id, session_id, message_id)
        self._dead_turns.discard(session_id)
        return True

    # -- the anchor the hand-off could not read (D15) ------------------------

    def arm_later(self, client: OpencodeClient, app_id: str, session_id: str, task: str) -> None:
        """Start the anchor resolution the hand-off could not wait for.

        Started, not awaited: Alice is waiting for the words on this turn, the read is the one
        thing here with no budget left, and the agent's own answer is seconds away at the
        earliest. The task is held in a set so it cannot be collected mid-read.

        Two callers, one reason each. The read that TIMED OUT has no snapshot at all (D15), and
        the read that came back EMPTY has no boundary in it -- a session created microseconds
        ago, or one whose every message was a routing signal just deleted. Neither can be armed
        on `""`, and neither can wait on Alice's clock to find out what the boundary is.
        """
        armed = asyncio.create_task(
            self._arm_when_readable(client, app_id, session_id, task),
            name=f"r2d2-anchor:{session_id}",
        )
        self._pending.add(armed)
        armed.add_done_callback(self._pending.discard)

    async def _arm_when_readable(
        self, client: OpencodeClient, app_id: str, session_id: str, task: str
    ) -> None:
        """Read the transcript off the clock, sweep it, and arm the collector on it.

        Retried inside `ANCHOR_TIMEOUT_S` for both reasons a read is not yet an answer: the
        server was busy rather than absent, or it answered before the submit that writes the
        task message had been stored. Either way the task message is what makes the boundary
        knowable, so once it is there `anchor` can tell what the session held before this
        hand-off from what it held after -- which is the whole reason the answer is not lost
        here. A boundary that never appears within the bound is a stated loss, not an arming
        on `""`.
        """
        deadline = time.monotonic() + ANCHOR_TIMEOUT_S
        while True:
            try:
                records = await asyncio.wait_for(
                    client.list_messages(session_id), max(deadline - time.monotonic(), 0.1)
                )
            except (TimeoutError, httpx.HTTPError, OpencodeError) as exc:
                if time.monotonic() >= deadline:
                    await self._lost(session_id, app_id, "the transcript never answered", exc)
                    return
                await asyncio.sleep(self._retry_s)
                continue
            sweep = self._sweep(records)
            for message_id in sweep.signal_ids:
                await self.erase(client, app_id, session_id, message_id)
            anchor = self.anchor(records, sweep, task)
            if anchor or time.monotonic() >= deadline:
                await self._enqueue(app_id, session_id, anchor)
                return
            # The read answered and still named no boundary, which is one thing only: the
            # submit that writes the task message has not been stored yet. Retrying is not
            # politeness -- arming here would mean `""`, and `""` reads as "the whole
            # conversation is newer", so this collector would deliver the next turn's answer
            # as this turn's. The wait costs the user nothing: the ack was already spoken.
            await asyncio.sleep(self._retry_s)

    async def _lost(self, session_id: str, app_id: str, why: str, exc: BaseException) -> None:
        """State a lost answer in the user's own chat rather than only in the log.

        The turn was acknowledged, so the user is waiting for a message that will not come;
        a bounded loss is acceptable and a silent one is not, because the only honest place
        to report it is the channel the user is in.
        """
        self._log.error(
            "opencode: the turn in session %s of %s ran %.0fs and %s, so no collector could be "
            "armed on a boundary (%s: %s). The user is told the answer is lost rather than left "
            "waiting for it.",
            session_id, app_id, ANCHOR_TIMEOUT_S, why, type(exc).__name__, exc,
        )
        if self._tell is not None:
            await self._tell(ANCHOR_LOST)

    @staticmethod
    def anchor(
        records: Sequence[MessageRecord], sweep: routing.TranscriptSweep, task: str
    ) -> str:
        """Where a collector starts reading when this read is not the hand-off's own.

        The sweep's own anchor is right whenever the read happened BEFORE the task was
        submitted, and wrong whenever it happened after: the newest message would then be the
        agent's own answer, and anchoring there collects nothing. The task message is the one
        thing known to be written after the hand-off, so a read containing it is cut there and
        the anchor is the newest surviving message before it.

        **The task message is itself the answer when nothing survives before it**, and never
        `""`. `_assistant_text` starts the window AFTER the anchor, so a task message used as
        one bounds the collector to this turn's own work: the task's text is a user message and
        is not collected, and every later turn's answer is outside the window. `""` would mean
        the opposite -- the whole conversation is newer -- and a collector armed with it stays
        armed for its whole ceiling, so the NEXT turn's answer would arrive in THIS turn's
        delivery. That is not a rare interleaving: it is every second turn of every session.
        """
        cut = next(
            (index for index, record in enumerate(records) if record.text.strip() == task),
            len(records),
        )
        if cut == len(records):
            return sweep.since_message_id
        doomed = set(sweep.signal_ids)
        return next(
            (record.id for record in reversed(records[:cut]) if record.id not in doomed),
            records[cut].id,
        )

    # -- internals ---------------------------------------------------------

    def _sweep(self, records: Sequence[MessageRecord]) -> routing.TranscriptSweep:
        """What one snapshot says must be deleted and where a collector may start."""
        return routing.transcript_sweep(records, sentinel=self._cfg.r2d2_needs_agent_sentinel)

    async def erase(
        self, client: OpencodeClient, app_id: str, session_id: str, message_id: str
    ) -> None:
        """Delete one stored routing signal, and say so loudly if it survived.

        A failure here is not a failed turn: the user has already been acknowledged, the
        collector is armed either way, and the agent's own prompt tells it the token is the
        gateway's. What a failure does cost is the guarantee, so it is logged with the reason
        rather than swallowed -- a session that keeps a signal is a session whose agent may
        start copying it, and the operator is the one who can see that happening.
        """
        try:
            removed = await asyncio.wait_for(
                client.delete_message(session_id, message_id), MARKER_TIMEOUT_S
            )
        except (TimeoutError, httpx.HTTPError, OpencodeError) as exc:
            self._log.warning(
                "opencode: the routing signal %s is still stored in session %s of %s and the agent "
                "reads it as its own history (%s: %s)",
                message_id, session_id, app_id, type(exc).__name__, exc,
            )
            return
        if not removed:
            self._log.warning(
                "opencode: asked to delete the routing signal %s from session %s of %s; the server "
                "did not remove it, and the agent reads it as its own history",
                message_id, session_id, app_id,
            )
