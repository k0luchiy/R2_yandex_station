"""The escalation's other half: the agent gets the work, Telegram gets the answer.

`core/session_route.py` decides *that* a turn cannot be answered inside Alice's
4.5 s -- a cold session (C8), a `[[NEEDS_AGENT]]` marker, or a voice turn that
outran `r2d2_fast_deadline`. This module is what happens next, and it is split
out because it is a different job from deciding: three await points against the
opencode wire (`list_messages`, `delete_message`, `prompt_async`), one job
written to the async worker, and nothing else. A turn that escalates does not
answer a user, and the code that takes over has no text of its own to produce.

The properties this module exists to hold:

* **Exactly one collector per escalating turn.** `hand_to_agent` is the only
  call site in the whole server that submits an agent task or enqueues an
  `opencode_reply` job, which is what makes "one collector per turn" a property
  of the code instead of a convention: the three branches that escalate cannot
  each grow a second, and a branch that escalates cannot forget the collector,
  because returning the ack at all IS this call.
* **The anchor is read before the submit.** `_collect_later` reads the
  transcript and deletes the routing signal BEFORE the task is queued, so the
  marker is the last message the session held while this turn was still its own
  business: the collector then returns the agent's answer and can never replay
  the conversation the user already had. It is also what deletes the voice
  agent's routing signal, and that has to happen before the agent's turn is
  queued -- an agent asked to work in a session whose newest message is
  `[[NEEDS_AGENT]]` is an agent that answers with `[[NEEDS_AGENT]]`. Reading it
  after the submit would race opencode's own write of the task message and make
  the marker depend on which of the two arrived first.
* **A refused submit does not turn into a failed turn.** The user has already
  been told a result is coming, the collector is armed either way, and it still
  ships whatever the session produces -- `NO_REPLY`'s "the agent did not answer"
  when that is nothing. The reason is in the log line and in the `jobs` row, not
  in an error spoken to a speaker who cannot act on it.
* **A deadline is not an abort, and not a loss.** The voice turn that outran the
  budget keeps running server-side; the work is submitted anyway, because at the
  deadline nobody can know whether the late turn will answer or ask for the
  agent, and the collector needs a marker -- hence the last message the session
  held before this turn's work was submitted is recorded in the job.
* **A stored permission refusal IS this module's business, and is deleted.** A refusal is a
  `tool` part whose `state.error` enumerates the refused agent's whole effective matrix
  (contract U8), which in the shared one-session-per-human model is not the reader's. It is
  read as `MessageRecord.refused`, and `routing.transcript_sweep` counts it as enforcement
  state rather than conversation, so it goes out in the same pass and from the same snapshot
  that fixes the anchor (`signal_ids` means "messages that must not survive", whatever made
  them a signal). What the user keeps is the request itself and the refused agent's plain-prose
  answer in the next assistant message; what goes is the record that the command was refused.
  The trade is measured in `qa/live-run-v3.md` §D9 and `qa/live-run-v4.md`: keeping it left an
  agent permanently dead for that user, and deleting it did not.
* **A lost anchor race never loses an answer silently (D15).** The transcript read
  that decides the anchor is bounded by half a second, and when it times out the
  turn used to return before arming anything: the user was told a result was
  coming and nobody was left to fetch it. The collector is now armed either way,
  on an anchor resolved off Alice's clock, and if even that cannot be read the
  loss is stated in the user's chat rather than logged and forgotten.
* **A deadline turn's own marker is swept on the NEXT turn (D6-residue).** On that
  branch the voice turn is still running when the sweep runs, so its
  `[[NEEDS_AGENT]]` does not exist yet -- the agent then reads it and writes one
  itself. `sweep_residue` deletes it at the entry of the following turn, which is
  the last moment before any agent reads the history again.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Final

import httpx

from app.config import Config
from core import routing
from core.async_worker import Worker
from core.opencode.client import OpencodeError
from core.session_sweeps import MARKER_TIMEOUT_S, SessionSweeper
from core.tools.telegram_tool import send_message

if TYPE_CHECKING:  # the route composes this module, so the edge cannot be a runtime one
    from core.session_route import HybridWiring

#: The job type `core/async_worker.py` does not dispatch yet; todo 16 adds the
#: branch. Until then the worker logs the failure, which is the sanctioned
#: intermediate state rather than a silent loss.
JOB_OPENCODE_REPLY: Final = "opencode_reply"
#: The collector's ceiling, stated in the job rather than left to the reader. It
#: is a ceiling and not a wait: `collect_reply` ends on the idle condition first.
COLLECT_TIMEOUT_S: Final = 600.0
#: The agent task carries the request in the user's own words, so the agent has
#: it even when the voice model offered no hint.
TASK_PREFIX: Final = "Пользователь попросил голосом: "


class SessionCollector:
    """The hand-off half of the opencode route: submit the work, arm its collector.

    A collaborator rather than a step, because the two halves of an escalation
    fail differently and are paid for differently: this one has already spent the
    whole voice budget, so every await in it is bounded by `MARKER_TIMEOUT_S` and
    a failure is logged rather than spoken. It holds the three collaborators the
    hand-off needs and nothing about the turn that decided to escalate.
    """

    def __init__(self, cfg: Config, worker: Worker, logger: logging.Logger) -> None:
        self.cfg = cfg
        self.worker = worker
        self.logger = logger
        self.sweeper = SessionSweeper(cfg, logger, self._enqueue, self._say)

    async def hand_to_agent(
        self,
        wiring: HybridWiring,
        app_id: str,
        session_id: str,
        command: str,
        *,
        hint: str = "",
    ) -> tuple[str, bool]:
        """Give `r2d2-agent` the request, and arm the collector that ships its answer.

        The one place this module submits a task or enqueues an `opencode_reply` job,
        which is what makes "exactly one collector per turn" a property of the code instead
        of a convention: the three branches that escalate -- C8, the sentinel and the
        deadline -- cannot each grow a second, and a branch that escalates cannot forget
        the collector, because returning the ack at all IS this call.

        `_collect_later` runs first and reads the transcript BEFORE the submit, so the
        marker is the last message the session held while this turn was still its own
        business: the collector then returns the agent's answer and can never replay the
        conversation the user already had. It is also what deletes the voice agent's
        routing signal, and that has to happen before the agent's turn is queued -- an agent
        asked to work in a session whose newest message is `[[NEEDS_AGENT]]` is an agent
        that answers with `[[NEEDS_AGENT]]`. Reading it after the submit would race
        opencode's own write of the task message.

        A refused submit does not turn into a failed turn. The user has already been told
        a result is coming, the collector is armed either way, and it still ships whatever
        the session produces -- `NO_REPLY`'s "the agent did not answer" when that is
        nothing. The reason is in this log line and in the `jobs` row, not in an error
        spoken to a speaker who cannot act on it.
        """
        self.worker.arm_watch(wiring.turns)
        task = f"{TASK_PREFIX}{command}\n{hint}" if hint else f"{TASK_PREFIX}{command}"
        await self._collect_later(wiring, app_id, session_id, task)
        try:
            await wiring.backend.submit_task(session_id, task)
        except (OpencodeError, httpx.HTTPError) as exc:
            self.logger.warning(
                "opencode %r: session %s of %s took no task from R2D2: %r. The collector is armed "
                "and the user was acknowledged all the same, so the turn is not lost -- only the "
                "work behind it is.",
                wiring.spec.name, session_id, app_id, exc,
            )
        return self.cfg.r2d2_task_ack, False

    def note_may_leave_signal(self, session_id: str) -> None:
        """Record that this session's last turn may still be writing a routing signal.

        Called from the deadline branch, where the voice turn is handed to the agent while it
        is still running: the sweep that runs before the agent's turn therefore cannot see
        the `[[NEEDS_AGENT]]` that turn is about to write, and the agent, reading it as its
        own history, writes one itself. The signal is removed at the entry of the NEXT turn
        for this session -- see `sweep_residue` -- because at this point there is nothing to
        remove.
        """
        self.sweeper.note_dead_turn(session_id)

    async def sweep_residue(
        self, wiring: HybridWiring, app_id: str, session_id: str
    ) -> bool:
        """Delete a dead turn's routing signal before this turn's agent reads history.

        Only sessions the deadline branch touched are swept, so the common turn pays nothing,
        and the sweep is `core/session_sweeps.py`'s: one read that decides deletions alone and
        never an anchor, which is what keeps the one-snapshot rule below intact.
        """
        return await self.sweeper.sweep_residue(wiring.client, app_id, session_id)

    async def _say(self, text: str) -> None:
        """One message in the user's own chat, through the tool the whole project uses.

        Only used to STATE a loss: a bounded loss of an answer is the plan's rule, a silent
        one is not, and the only honest way to report it is in the channel the user is in.
        """
        await send_message(self.cfg, text)

    async def _enqueue(self, app_id: str, session_id: str, anchor: str) -> None:
        """Write the one `opencode_reply` job this escalating turn gets."""
        await self.worker.enqueue(
            {
                "type": JOB_OPENCODE_REPLY,
                "application_id": app_id,
                "session_id": session_id,
                "since_message_id": anchor,
                "timeout_s": COLLECT_TIMEOUT_S,
            }
        )

    async def _collect_later(
        self, wiring: HybridWiring, app_id: str, session_id: str, task: str
    ) -> None:
        """Take the routing signal out of the transcript, and hand the still-running
        turn to the worker that ships the answer to Telegram.

        Both halves read the session ONCE, and the order is the whole point. The
        snapshot says which stored messages are routing signals and which is the
        newest message that survives them; the signals are deleted, and only then
        is the collector armed with the surviving id. Anchoring first and
        deleting afterwards would leave the collector pointing at a message the
        server no longer lists, and a marker it cannot position means "everything
        in the session is newer" -- the entire conversation replayed into Telegram.

        Deleting is not a repair of the user's transcript. Only ASSISTANT messages are
        removed, and only when they are opencode's own enforcement state -- carrying the
        sentinel, or holding a tool-permission refusal -- so every utterance the user made
        and every real answer survives. What goes is a control signal the protocol never
        meant to be conversation, and leaving the sentinel is what let one escalation disable
        the agent path for good. The escalation hint is not lost with the message: it is
        already part of the task text submitted above.

        A stored refusal is swept on the same pass and is NOT protected, which is the change
        from the version of this paragraph that argued the opposite. It arrived as
        `MessageRecord.refused` rather than as text, so this sweep can see one; what it
        carries is the refused agent's whole effective matrix spelled out (contract U8), and
        in the shared session that matrix is not the reader's. The prompt rule that used to
        stand in for the deletion is measured and ineffective -- the agent read the refusal as
        a ban on its own tools, reached for `bash` where `webfetch` was allowed and needed no
        human, and stopped working for that user for good (`qa/live-run-v3.md` §D9). The
        price is that the record of the refusal goes with it; the user still learns the
        command did not run, from the request that is never swept and from the prose answer
        the refused agent wrote next, and both live runs measured that surviving
        (`qa/live-run-v4.md`).

        Finding the marker costs one GET on a path that has already spent the whole voice
        budget, and it is bounded: a server that will not answer here must not cost the
        user the acknowledgement. It used to cost the ANSWER as well -- the read timed out,
        the method returned, and no collector was ever armed, so a plain question produced
        an answer nobody was ever going to receive (`qa/live-run-v3.md` §D15: "Париж."
        written to the session at 13:36:59 and never delivered). So the read failing is no
        longer the end of the hand-off: the collector is armed anyway, on an anchor resolved
        off Alice's clock by `_arm_when_readable`, and only a server that cannot answer THAT
        either costs the answer -- and then the user is told, in their own chat.
        """
        try:
            records = await asyncio.wait_for(
                wiring.client.list_messages(session_id), MARKER_TIMEOUT_S
            )
        except (TimeoutError, httpx.HTTPError, OpencodeError) as exc:
            self.logger.warning(
                "opencode %r: the turn in session %s of %s kept running but the transcript did "
                "not answer inside %.1fs (%s: %s); the collector is armed anyway on an anchor "
                "resolved in the background, and the user is told if even that fails",
                wiring.spec.name, session_id, app_id, MARKER_TIMEOUT_S, type(exc).__name__, exc,
            )
            self.sweeper.arm_later(wiring.client, app_id, session_id, task)
            return
        sweep = routing.transcript_sweep(records, sentinel=self.cfg.r2d2_needs_agent_sentinel)
        for message_id in sweep.signal_ids:
            await self.sweeper.erase(wiring.client, app_id, session_id, message_id)
        if not sweep.since_message_id and records:
            # The session held messages and the sweep removed every one, so `""` would name
            # the whole deleted history as newer -- in a collector that lives for its whole
            # 600 s ceiling, so the next turn's answer arrives as this turn's. The only
            # boundary left is the task message, which the submit has not written yet.
            self.sweeper.arm_later(wiring.client, app_id, session_id, task)
            return
        # An EMPTY snapshot is the C8 case, and `""` is the truth rather than a default: this
        # session has held nothing, so "everything it ever holds is newer" is exact. There is
        # no boundary to wait for, and waiting would hand the answer to a collector that does
        # not exist yet -- the D15 shape again.
        await self._enqueue(app_id, session_id, sweep.since_message_id)
