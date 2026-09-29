"""What may be delivered to a human as "the agent's answer", and how long to wait for it.

The delivery path out of the opencode session ends in one place, and this is the gate
in front of it. `collect_reply` (`core/backends/opencode_session.py`) can tell that the
agent stopped writing; it cannot tell that the agent finished, because opencode's own
`session.idle` arrives *while a tool is blocked on a human answer* (C5,
`docs/11-opencode-contract.md`). Two live defects came out of that gap and both are
answered here, from the stream rather than from a clock:

* **D13 -- the plan shipped instead of the result.** The agent wrote
  `"Сделаю. Сначала посмотрю, что доступно в окружении."`, went quiet while the
  broker's own question sat unanswered in Telegram, and the silence heuristic read
  that as the end of the turn. The digest the agent went on to write reached nobody.
  A second live run then showed the ask alone is not the signal either: the agent
  wrote `"Сейчас соберу реальные данные с arXiv…"`, paused for longer than the silence
  rule waits, and only THEN called its tool -- so a collector that ends on the ask
  alone ships the plan in that window too.

  The signal is therefore C5's conjunction, statefully: `TurnWatch.ended_after` is
  `session.idle` seen since the collection started **and** no ask of ours without its
  reply. That is the same rule `core/opencode/sse_frames.turn_is_complete` states over a
  list of events, and it is the only condition under which "the agent stopped writing"
  means "the agent finished". The wait is not a longer timeout: the 2 s silence is
  still what ends a collection in a deployment with no stream, and while the turn is
  parked the anchor moves to the message the blocked tool call came from, which is where
  the result starts.
* **D6-residue -- a routing signal shipped as an answer.** On the `deadline` branch the
  sweep cannot see the voice agent's `[[NEEDS_AGENT]]` (it is still being written), so
  the agent read the marker in its history and emitted one itself; that echo was the
  first message after the anchor and therefore what the collector shipped. The user
  received a stripped echo of their own question. A body that *begins* with the token
  is a routing signal by the protocol and is never an answer -- the honest delivery is
  that the agent did not answer, with the reason on record.

**Why the predicate is the sweep's own.** "This assistant message carries the token" is
already a decision in this project: `core.routing.transcript_sweep` deletes such a message
out of the session, deliberately, and says why -- the protocol makes the token a control
signal and `for_human` already refuses to show such a body unedited. This gate asks that
same function rather than writing a second, looser reading of it, so a collector and a
sweep cannot disagree about what a signal is. A leading-only check would have been enough
for the one shape the run recorded as a job result, and wrong for the shape the user
actually received: the agent narrates FIRST and echoes the token second, so the harm is a
body with the plan in front of the copy of their question, not a body that starts with it.

The cost is stated rather than hidden: an answer that quotes the token in a sentence of its
own is withheld too, and the user is told the agent did not answer -- which is the safe
direction, and recoverable by asking again. `routing.for_human` still guards every other
outbound body (a digest, a job failure, the broker's notices), which is where a mention
still costs only the token.

**Waiting for the event, and when not to.** The rule above needs a reader: without one
there is no `session.idle` coming, and a collector that waited for it would hold a worker
slot for its whole ceiling. `TurnWatch.watched` is the question that decides -- a reader
attaches on every turn, long before the collector starts -- and a session with no frames
falls back to the silence rule, which is the pre-agreed degradation rather than a new one.

**Bounded twice over.** By the job's own ceiling, and by the broker's window: the broker
refuses every ask it could not answer within `r2d2_permission_timeout`, so a parked turn
ends there even if the stream never delivered the `permission.replied` that closed it --
a lost frame must not hold a worker slot for ten minutes. The wait therefore stops at the
window plus one sweep, and reports the same thing an unanswered risky command gets.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Final, Protocol

from app.config import Config
from core import routing
from core.opencode.wire import MessageRecord
from core.opencode.turn_watch import TurnState, TurnWatch
from core.permission_sweep import park_bound_s

__all__ = ["NO_REPLY", "AnswerGate"]

#: What the user is told when the collector ended without an answer. An empty body would
#: be a misleading success: the phone shows nothing at all and the operator sees a
#: delivered job with no reason in it.
NO_REPLY: Final = "Агент не ответил."

#: The id the collected text is filed under to be swept. It never reaches a server: the
#: predicate reads the role and the text, and the id is here only to satisfy the record.
COLLECTED: Final = "msg_collected"
#: The one role that speaks (C7), re-exported rather than re-declared so the record this
#: gate files cannot drift from the one the sweep reads in a session.
ASSISTANT_ROLE: Final = routing.ASSISTANT_ROLE
#: The one method of the opencode backend this module needs. A `Protocol` rather
#: than the `ReplyCollector` protocol so this gate imports no job-pool type and no
#: client: `core.async_worker` hands `backend.collect_reply` in, and a test can hand
#: in a function with no server, no session and no pool at all. `turn_text` is the
#: task the collector was armed for -- `core/turn_lease.py` refuses to speak for a
#: turn that is not it -- and `""` is a collector with no turn to check against.
class Collect(Protocol):
    async def __call__(
        self, session_id: str, since_message_id: str, timeout_s: float, *, turn_text: str = ""
    ) -> str: ...


class AnswerGate:
    """One delivery's gate: ask the collector again until what it returns is an answer.

    `collect` is the backend's `collect_reply`; `turns` is the per-session memory fed by
    the opencode event stream, and `None` for a deployment with no stream, which degrades
    to the silence heuristic alone. Nothing here reads the transcript, sends anything, or
    decides about a permission: the broker owns that, and this module only asks whether
    the agent is parked and where its result begins.
    """

    def __init__(
        self, cfg: Config, logger: logging.Logger, collect: Collect, turns: TurnWatch | None
    ) -> None:
        self._sentinel = cfg.r2d2_needs_agent_sentinel
        self._poll_s = cfg.r2d2_event_poll_interval
        # The broker refuses an unanswered ask at its window, and it only LOOKS every
        # `SWEEP_INTERVAL_S`; past the window plus one sweep the turn is not waiting for
        # anybody. The code added the poll interval instead -- 2 s where the sweep is 30 --
        # which gave up on a turn up to 28 s before the refusal that ends it, and reported
        # a loss for an answer that was seconds from arriving.
        self._parked_s = park_bound_s(cfg.r2d2_permission_timeout)
        self._log = logger
        self._collect = collect
        self._turns = turns

    async def answer(
        self, session_id: str, since_message_id: str, timeout_s: float, *, turn_text: str = ""
    ) -> str:
        """The agent's answer, or `NO_REPLY`. Raises `TimeoutError` on overrun.

        The ceiling belongs to the caller: this returns as soon as it has an answer, and
        a collector that overruns is the caller's `asyncio.wait_for` to bound, which is
        what keeps one stuck session from holding a worker slot.

        `turn_text` is handed to every collection round unchanged: it is what makes the
        collector a collector FOR A TURN (`core/turn_lease.py`). A round that finds the
        session has moved on raises `TurnSuperseded`, which is NOT caught here: the
        right outcome is a delivered nothing, and only the worker owns the delivery,
        so it propagates to `core/async_worker.py`, which records it and sends nothing.
        """
        started = time.monotonic()
        deadline = started + timeout_s
        parked_at: float | None = None
        anchor = since_message_id
        while True:
            text = await self._collect(session_id, anchor, timeout_s, turn_text=turn_text)
            state = self._state(session_id)
            if state is None or not self._turns.watched(session_id) or state.ended_after(started):
                return text if self._is_answer(text) else self._refuse(text, session_id, state)
            if state.blocked:
                if state.unanswerable is not None:
                    return self._unanswerable(session_id, state)
                if parked_at is None:
                    parked_at = time.monotonic()
                anchor = state.blocked_at or anchor
                self._log.info(
                    "opencode reply: the turn of session %s is parked on %d unanswered ask(s) "
                    "(%s); what it wrote so far is the plan, not the result, so the collector "
                    "keeps waiting",
                    session_id, len(state.waiting),
                    self._turns.describe(session_id) if self._turns else "",
                )
                if time.monotonic() - parked_at >= self._parked_s:
                    self._log.warning(
                        "opencode reply: the turn of session %s was still parked on %d ask(s) "
                        "after %.0fs (%s); the broker refuses an unanswered ask at %.0fs, so the "
                        "user is told the agent did not answer rather than left waiting on a slot",
                        session_id, len(state.waiting), time.monotonic() - parked_at,
                        self._turns.describe(session_id) if self._turns else "no event stream",
                        self._parked_s,
                    )
                    return NO_REPLY
            await asyncio.sleep(min(self._poll_s, deadline - time.monotonic()))

    # -- internals ---------------------------------------------------------

    def _state(self, session_id: str) -> TurnState | None:
        """This session's turn state, or `None` when no watch is wired at all.

        `None` is the degraded mode -- `EVENT_MODE = "poll"`, or a deployment with no
        opencode route -- and it degrades to the silence heuristic rather than to a
        different rule. A watch that exists but has seen no frame for THIS session is the
        same degradation, and `watched` is what tells the two apart.
        """
        return self._turns.state(session_id) if self._turns is not None else None

    def _is_answer(self, text: str) -> bool:
        """Whether `text` may be delivered as the agent's answer."""
        return bool(text) and not self._is_routing_signal(text)

    def _is_routing_signal(self, text: str) -> bool:
        """Whether `text` IS a routing signal rather than an answer.

        Asked of `core.routing.transcript_sweep` -- the function that decides which stored
        assistant messages are control signals -- so this gate and the session sweep cannot
        disagree. The collected text is a `MessageRecord` of its own: it came out of a
        session, it is an assistant message, and that is the whole of what the predicate
        looks at.
        """
        record = MessageRecord(id=COLLECTED, role=ASSISTANT_ROLE, text=text)
        return bool(routing.transcript_sweep([record], sentinel=self._sentinel).signal_ids)

    def _unanswerable(self, session_id: str, state: TurnState) -> str:
        """`NO_REPLY` at once for a turn parked on a question nobody can answer.

        This is the F4 shape and the reason the loop above does not simply wait longer.
        The broker's window is the right bound for an ask R2D2 put to Telegram, because
        the user can answer that one and the sweep ends it when they do not. A
        `question.asked` has no such question: the `question` tool is `deny` in R2D2's
        matrix, so the frame can only mean the installed config is stale, and the turn
        will stay blocked for as long as the process lives. Waiting out `self._parked_s`
        and then reporting a loss would be the same delay the rule exists to remove, and
        the reaper's 900 s is the next bound after that.

        So it is stated on the first round. The WARNING says to reinstall, because the
        user's next question will hit exactly the same park and the only repair is
        `scripts/install_r2d2_opencode_config.sh` -- the matrix on disk and the matrix in
        the server are two files, and this is what it looks like when they disagree.
        """
        self._log.warning(
            "opencode reply: the turn of session %s is parked on question %s, which R2D2 cannot "
            "answer: the `question` tool is denied in config/opencode/r2d2.opencode.json, so this "
            "frame means the INSTALLED matrix is stale (%s). The user is told at once rather than "
            "after the %.0fs park bound -- no broker question exists to answer, and the turn will "
            "not end on its own. Re-run scripts/install_r2d2_opencode_config.sh.",
            session_id, state.unanswerable,
            "no event stream" if self._turns is None else self._turns.describe(session_id),
            self._parked_s,
        )
        return NO_REPLY

    def _refuse(self, text: str, session_id: str, state: TurnState | None) -> str:
        """`NO_REPLY` for a body that is not an answer, with the reason on record.

        Stated rather than shipped, because the alternative is the failure the live run
        measured: a message that reads like an answer and is not one. The WARNING names
        the session and what the turn watch believed, so an operator can tell a
        re-escalation from a silent server. A state is passed in rather than re-read so
        the line says what the decision was actually made on.
        """
        if self._is_routing_signal(text):
            self._log.warning(
                "opencode reply: the turn of session %s answered with a routing signal instead of "
                "a result (%s); the user is told the agent did not answer, because the signal's own "
                "text is a copy of their question",
                session_id,
                "no event stream" if state is None else self._turns.describe(session_id),
            )
        return NO_REPLY
