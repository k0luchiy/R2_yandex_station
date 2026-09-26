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

if TYPE_CHECKING:  # the route composes this module, so the edge cannot be a runtime one
    from core.session_route import HybridWiring

#: The job type `core/async_worker.py` does not dispatch yet; todo 16 adds the
#: branch. Until then the worker logs the failure, which is the sanctioned
#: intermediate state rather than a silent loss.
JOB_OPENCODE_REPLY: Final = "opencode_reply"
#: The collector's ceiling, stated in the job rather than left to the reader. It
#: is a ceiling and not a wait: `collect_reply` ends on the idle condition first.
COLLECT_TIMEOUT_S: Final = 600.0
#: What an escalating turn may spend on the transcript: reading it once, and per
#: routing signal deleting the message the sweep found. It has already spent the
#: whole voice budget, so this is small and absolute -- a server that will not
#: answer here costs the answer, never the acknowledgement.
MARKER_TIMEOUT_S: Final = 0.5
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

        The one place this module submits a task or enqueues an `opencode_reply`
        job, which is what makes "exactly one collector per turn" a property of the
        code instead of a convention: the three branches that escalate -- C8, the
        sentinel and the deadline -- cannot each grow a second, and a branch that
        escalates cannot forget the collector, because returning the ack at all IS
        this call.

        `_collect_later` runs first and reads the transcript BEFORE the submit, so
        the marker is the last message the session held while this turn was still
        its own business: the collector then returns the agent's answer and can
        never replay the conversation the user already had. It is also what deletes
        the voice agent's routing signal, and that has to happen before the agent's
        turn is queued -- an agent asked to work in a session whose newest message is
        `[[NEEDS_AGENT]]` is an agent that answers with `[[NEEDS_AGENT]]`. Reading it
        after the submit would race opencode's own write of the task message and
        make the marker depend on which of the two arrived first.

        A refused submit does not turn into a failed turn. The user has already been
        told a result is coming, the collector is armed either way, and it still
        ships whatever the session produces -- `NO_REPLY`'s "the agent did not
        answer" when that is nothing. The reason is in this log line and in the
        `jobs` row, not in an error spoken to a speaker who cannot act on it.
        """
        await self._collect_later(wiring, app_id, session_id)
        suffix = f"\n{hint}" if hint else ""
        try:
            await wiring.backend.submit_task(session_id, f"{TASK_PREFIX}{command}{suffix}")
        except (OpencodeError, httpx.HTTPError) as exc:
            self.logger.warning(
                "opencode %r: session %s of %s took no task from R2D2: %r. The collector is armed "
                "and the user was acknowledged all the same, so the turn is not lost -- only the "
                "work behind it is.",
                wiring.spec.name, session_id, app_id, exc,
            )
        return self.cfg.r2d2_task_ack, False

    async def _collect_later(self, wiring: HybridWiring, app_id: str, session_id: str) -> None:
        """Take the routing signal out of the transcript, and hand the still-running
        turn to the worker that ships the answer to Telegram.

        Both halves read the session ONCE, and the order is the whole point. The
        snapshot says which stored messages are routing signals and which is the
        newest message that survives them; the signals are deleted, and only then
        is the collector armed with the surviving id. Anchoring first and
        deleting afterwards would leave the collector pointing at a message the
        server no longer lists, and a marker it cannot position means "everything
        in the session is newer" -- the entire conversation replayed into Telegram.

        Deleting is not a repair of the user's transcript. Only ASSISTANT messages
        carrying the sentinel are removed, so every utterance the user made and
        every real answer survives; what goes is a control signal that the protocol
        never meant to be conversation, and leaving it is what let one escalation
        disable the agent path for good (the live run's `POST .../summarize`
        answered `true` and compressed nothing, so no summariser could have saved
        it). The escalation hint is not lost with the message: it is already part
        of the task text submitted above.

        Finding the marker costs one GET on a path that has already spent the
        whole voice budget, and it is bounded: a server that will not answer here
        loses the answer, never the acknowledgement. The deletes carry the same
        bound, because they happen between the model's reply and the words Alice
        is waiting for.
        """
        try:
            records = await asyncio.wait_for(
                wiring.client.list_messages(session_id), MARKER_TIMEOUT_S
            )
        except (TimeoutError, httpx.HTTPError, OpencodeError) as exc:
            self.logger.warning(
                "opencode %r: the turn in session %s of %s keeps running but cannot be "
                "collected: %s", wiring.spec.name, session_id, app_id, exc,
            )
            return
        sweep = routing.transcript_sweep(
            records, sentinel=self.cfg.r2d2_needs_agent_sentinel
        )
        for message_id in sweep.signal_ids:
            await self._erase_signal(wiring, app_id, session_id, message_id)
        await self.worker.enqueue(
            {
                "type": JOB_OPENCODE_REPLY,
                "application_id": app_id,
                "session_id": session_id,
                "since_message_id": sweep.since_message_id,
                "timeout_s": COLLECT_TIMEOUT_S,
            }
        )

    async def _erase_signal(
        self, wiring: HybridWiring, app_id: str, session_id: str, message_id: str
    ) -> None:
        """Delete one stored routing signal, and say so loudly if it survived.

        A failure here is not a failed turn: the user has already been
        acknowledged, the collector is armed either way, and the agent's own
        prompt tells it the token is the gateway's. What a failure does cost is
        the guarantee, so it is logged with the reason rather than swallowed --
        a session that keeps a signal is a session whose agent may start copying
        it, and the operator is the one who can see that happening.
        """
        try:
            removed = await asyncio.wait_for(
                wiring.client.delete_message(session_id, message_id), MARKER_TIMEOUT_S
            )
        except (TimeoutError, httpx.HTTPError, OpencodeError) as exc:
            self.logger.warning(
                "opencode %r: the routing signal %s is still stored in session %s of %s and "
                "the agent reads it as its own history: %s",
                wiring.spec.name, message_id, session_id, app_id, exc,
            )
            return
        if not removed:
            self.logger.warning(
                "opencode %r: asked to delete the routing signal %s from session %s of %s; the "
                "server did not remove it, and the agent reads it as its own history",
                wiring.spec.name, message_id, session_id, app_id,
            )
