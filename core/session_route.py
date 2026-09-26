"""The opencode session route: one turn in the user's own session, or an ack for it.

`core/brain.py` decides which of the two paths a turn gets. This module is the
first one. Alice gives a webhook **4.5 s** and **1024 characters**; the
opencode server gives one persistent session per user, a tool-less voice agent
that answers inside `r2d2_fast_deadline`, and a full agent that does real work.
So the route is: resolve the user's session, try the voice agent, and if the turn
cannot be answered in place, hand it to the agent and acknowledge. The hand-off
itself -- the submit, the transcript sweep and the `opencode_reply` job -- is
`core/session_collector.py`; everything here is the attempt and the decision.

* **C8 -- a cold session costs 15.5-18.6 s.** So a session whose message count is
  zero skips the synchronous voice turn entirely, submits the work to the agent
  and acknowledges in under a second. A warm session (p50 1.667 s, p95 2.247 s)
  answers directly.
* **A deadline is not an abort, and not a loss.** A voice turn that outruns the
  budget keeps running server-side; the user is acknowledged, the work is
  submitted to the agent as well, and an `opencode_reply` job collects the answer
  for Telegram. Aborting would destroy work already paid for; not submitting would
  lose the request outright, because at the deadline nobody can know whether the
  late turn will answer or ask for the agent.
* **C1 is the caller's to handle.** An `info.error` carrying a 403
  `FreeTierError`, or a 402 inside an HTTP 200, is a refusal that raises out of
  here; `Brain._handle` then answers from the provider chain. A refusal text that
  reaches a speaker is worse than no answer, because the user cannot tell it from
  one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Final

from app.config import Config
from core import metrics, routing
from core.async_worker import Worker
from core.backends.config_loader import BackendSpec
from core.backends.opencode_session import OpencodeSessionBackend, current_application_id
from core.memory import Memory
from core.opencode.client import OpencodeClient, OpencodeDeadlineExceeded
from core.opencode.session_store import OcSessionStore
from core.permissions import PermissionBroker, PermissionVerdict
from core.session_collector import SessionCollector

#: The backend kind that answers from a persistent opencode session. It is the
#: opencode ROUTE, never a member of the fallback chain -- `core/brain.py` filters
#: it out of the order, and `app/diagnostics.py` filters it out of the same list.
SESSION_KIND: Final = "opencode_session"
#: What a brokered permission answer is spoken as. The title opencode supplies is
#: deliberately absent: it is server-controlled, and it can be kilobytes long.
PERMISSION_APPROVED: Final = "Принято, выполняю."
PERMISSION_REFUSED: Final = "Отменяю."


@dataclass(frozen=True, slots=True)
class HybridWiring:
    """The opencode route's collaborators, as todo 18's composition root has them.

    Grouped into one value because they are one thing: the route either exists
    completely or is not used at all, and a half-wired route that silently
    answered from the chain instead would be a bug nobody could see. `spec` is
    carried because the brain needs `fast_model` and `summarize_model` from the
    one place they are configured -- changing a model must stay a one-line edit in
    `config/backends.json`. `broker` is optional so a deployment without todo 14
    still routes; without it a pending ask is simply not answered here.
    """

    spec: BackendSpec
    client: OpencodeClient
    store: OcSessionStore
    backend: OpencodeSessionBackend
    broker: PermissionBroker | None = None


class SessionRoute:
    """One turn against the user's persistent opencode session, and its verdict.

    Holds what every branch needs and nothing about the turn that got here: the
    config, the memory that counts the session, the collector that takes the work
    away when this route cannot answer it, and the logger. The wiring is NOT held
    -- it is passed per call, because a deployment without the opencode route
    still runs this one, and `Brain.opencode` is the one place that says whether
    the route exists.
    """

    def __init__(self, cfg: Config, memory: Memory, worker: Worker, logger: logging.Logger) -> None:
        self.cfg = cfg
        self.memory = memory
        self.logger = logger
        self.collector = SessionCollector(cfg, worker, logger)

    async def turn(self, wiring: HybridWiring, app_id: str, command: str) -> tuple[str, bool]:
        """One turn in the user's own opencode session, or the ack for work started.

        `complete()` resolves the session itself through the `ContextVar`, so the
        store is consulted here for the two things it alone knows: which session
        the agent turn belongs to, and whether this session has ever been used.

        Every return below reports itself to the turn recorder, because this is the
        PRIMARY route and the one whose latency decides whether we fit Alice's
        4.5 s: a turn that reached opencode and said nothing is a turn nobody can
        explain. `llm_ms` is measured around `complete()` alone -- `submit_task`
        returns 204 with the work still to come, so timing it would put a worker's
        queue in a field that means "the model answered".

        The three branches that leave for the agent all return through
        `self.collector.hand_to_agent`, which submits the work AND arms the
        collector that ships the answer. Neither half is optional: submitting
        without collecting is the agent working for nobody, and collecting without
        submitting is a collector waiting for a turn that will never come.
        """
        rec = metrics.current()
        rec.agent = wiring.spec.voice_agent
        session_id = await wiring.store.resolve(app_id)
        if not await self._prepare(wiring, app_id, session_id):
            # C8: the first message in a fresh session costs 15.5-18.6s, so it is
            # submitted to the agent and acknowledged rather than waited on.
            self._handoff(rec, wiring)
            return await self.collector.hand_to_agent(wiring, app_id, session_id, command)
        current_application_id.set(app_id)
        try:
            choice = await rec.measure(
                wiring.backend.complete(
                    [{"role": "user", "content": command}],
                    timeout=self.cfg.r2d2_fast_deadline,
                    model=wiring.spec.fast_model,
                )
            )
        except OpencodeDeadlineExceeded:
            # NOT aborted: the turn is still running server-side, and the collector
            # fetches it. The work is submitted anyway, because at the deadline
            # nobody can know whether the late voice turn will answer or ask for the
            # agent -- and guessing that it will is what dropped every request that
            # ran long: on the live run six laptop-control attempts all overran
            # 3.2s and the agent saw none of them. The record stays a `deadline`,
            # because the turn that outran the budget was the voice turn and
            # `llm_ms` is its cost.
            rec.answered(route=metrics.ROUTE_OPENCODE, model=wiring.spec.fast_model, msgs=1, tools=())
            rec.path = metrics.PATH_DEADLINE
            return await self.collector.hand_to_agent(wiring, app_id, session_id, command)
        rec.answered(route=metrics.ROUTE_OPENCODE, model=choice.model, msgs=1, tools=())
        decision = routing.parse_voice_reply(
            choice.content, sentinel=self.cfg.r2d2_needs_agent_sentinel
        )
        if decision.kind == "speak":
            return decision.spoken, False
        self._handoff(rec, wiring)
        return await self.collector.hand_to_agent(
            wiring, app_id, session_id, command, hint=decision.task_hint
        )

    async def answer_permission(
        self, wiring: HybridWiring | None, app_id: str, command: str
    ) -> str | None:
        """What to speak for a brokered opencode ask, or None to carry on.

        `unrelated` means there was no ask of ours, or the text is not an answer at
        all, and it changes nothing -- the question is then answered normally and
        the ask stays pending, which is the only way it can still be answered.
        """
        broker = wiring.broker if wiring is not None else None
        if broker is None:
            return None
        verdict: PermissionVerdict = await broker.resolve_from_text(app_id, command)
        match verdict:
            case "approved":
                return PERMISSION_APPROVED
            case "rejected":
                return PERMISSION_REFUSED
            case _:
                return None

    @staticmethod
    def _handoff(rec: metrics.Turn, wiring: HybridWiring) -> None:
        """Record a turn that moves to the AGENT: the ack's own facts, not the voice's.

        `msgs=1` and no tools are what the session route really sent (C3/U2: the
        session already holds the conversation, and the voice protocol carries no
        tool calls), and `escalated` is what makes the record read `path=escalate`
        rather than `path=voice` -- the user was acknowledged, not answered, and
        those are different facts about a 4.5 s turn.
        """
        rec.answered(route=metrics.ROUTE_OPENCODE, model=wiring.spec.task_model, msgs=1, tools=())
        rec.agent = wiring.spec.task_agent
        rec.escalated = True

    async def _prepare(self, wiring: HybridWiring, app_id: str, session_id: str) -> bool:
        """Count the turn, summarise a long session once, and report whether it is warm.

        The count is read BEFORE it is bumped, because zero is the C8 signal and
        bumping first would make every session look warm.
        """
        count = await wiring.store.message_count(app_id)
        if count > self.cfg.r2d2_session_soft_limit:
            provider, model = routing.split_model(wiring.spec.summarize_model)
            await wiring.client.summarize(session_id, provider=provider, model=model)
            self.logger.info(
                "opencode %r: summarised the session of %s after %d messages", session_id, app_id, count
            )
        await self.memory.touch_oc_session(app_id, message_delta=1)
        return count > 0
