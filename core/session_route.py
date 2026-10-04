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
  submitted to the agent as well -- after that turn has stopped writing, off
  Alice's clock -- and an `opencode_reply` job collects the answer for Telegram.
  Aborting would destroy work already paid for; not submitting would lose the
  request outright, because at the deadline nobody can know whether the late turn
  will answer or ask for the agent. The ordering is the other half: two turns in
  one session at once is what let a tool refusal the sweep could not see become
  the agent's own history (`qa/live-run-v6.md` §D9).
* **The residue sweep still runs, now as a backstop.** `SessionCollector` remembers
  which sessions crossed the deadline, and `SessionRoute` sweeps them at the entry
  of this turn -- which is what catches a residue a hand-off could not see, because
  its wait is bounded and a bound is not a promise.
* **C1 is the caller's to handle.** An `info.error` carrying a 403
  `FreeTierError`, or a 402 inside an HTTP 200, is a refusal that raises out of
  here; `Brain._handle` then answers from the provider chain. A refusal text that
  reaches a speaker is worse than no answer, because the user cannot tell it from
  one.
* **A session the server calls `busy` is refused before the voice turn, not after.**
  The park gate above this one costs Alice nothing because a park is a fact in our
  own database; `busy` is not, and it is the far more common case. Measured fifteen
  times out of fifteen on 1.18.33 (`qa/live-run-v12.md`): question A outstanding,
  question B asked, «Проверяю, пришлю в телеграм», and then nothing at all, because
  when B's submission makes the session busy A's task is never served -- so the
  `TurnSuperseded` that ended it was a correct lease decision resting on a false
  premise, and no answer was on its way from anywhere. The check therefore sits
  HERE, next to the park gate and not inside the collector: by the time a hand-over
  runs, Alice has already spent the voice budget and the user already has a promise.
"""

from __future__ import annotations

import logging
import time
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
from core.opencode.turn_watch import TurnWatch
from core.permissions import PermissionBroker, PermissionVerdict
from core.session_collector import BUSY_REFUSED, PARKED, SessionCollector

#: The backend kind that answers from a persistent opencode session. It is the
#: opencode ROUTE, never a member of the fallback chain -- `core/brain.py` filters
#: it out of the order, and `app/diagnostics.py` filters it out of the same list.
SESSION_KIND: Final = "opencode_session"
#: What a brokered permission answer is spoken as. The title opencode supplies is
#: deliberately absent: it is server-controlled, and it can be kilobytes long.
PERMISSION_APPROVED: Final = "Принято, выполняю."
#: Prepended to what a model sees when the platform flagged the utterance as
#: dangerous context. The fallback chain carries the same constraint as a system
#: message; the opencode route carried none, so on the primary path the flag had
#: no effect at all.
DANGEROUS_PREFIX: Final = (
    "[Платформа пометила этот запрос как потенциально опасный. "
    "Не выполняй никаких действий и не запускай инструменты без явного "
    "подтверждения пользователя.] "
)
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
    still routes; without it a pending ask is simply not answered here. `turns` is
    the per-session memory the opencode event readers fill, and it travels with the
    wiring because the collector that must know whether the agent is parked on a human
    is armed from here rather than from the composition root.
    """

    spec: BackendSpec
    client: OpencodeClient
    store: OcSessionStore
    backend: OpencodeSessionBackend
    broker: PermissionBroker | None = None
    turns: TurnWatch | None = None


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

    async def turn(
        self, wiring: HybridWiring, app_id: str, command: str, *, dangerous: bool = False
    ) -> tuple[str, bool]:
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
        `self.collector`, which submits the work AND arms the collector that ships
        the answer. Neither half is optional: submitting without collecting is the
        agent working for nobody, and collecting without submitting is a collector
        waiting for a turn that will never come. Two of the three branches hand
        over inside the request; the deadline branch cannot, because its voice turn
        is still running, and gets the same acknowledgement from a call that hands
        over in a background continuation instead.

        **`permission_asked` is decided per terminal path, and never on entry.**
        The module docstring of `core/metrics.py` says what the field claims; this
        is where that becomes three derivations rather than one default. The `parked`
        branch already holds the fact, because the ask is why it refused. The
        `deadline` branch is the ONE place an ask can appear while this turn is in
        flight -- the voice turn is still running server-side and it is the turn
        that can be parked right now -- so it re-reads the broker's row after the
        branch's last await. The C8 branch, the sentinel branch and the spoken
        branch cannot see one and are not asked to guess: C8 has submitted nothing,
        so no turn of ours is running in that session for opencode to interrupt; the
        sentinel branch is reached only after the voice turn ANSWERED with the
        marker, and a turn that raised an ask never returns text; and the entry
        check has already refused any session that was parked, or that the server
        calls busy -- and a C8 session is neither, because a session created
        seconds ago has nothing running in it. What the agent turn
        submitted here will ask is not this turn's to claim -- the next turn records
        `path=parked permission_asked=True`.
        """
        spoken = f"{DANGEROUS_PREFIX}{command}" if dangerous else command
        rec = metrics.current()
        rec.agent = wiring.spec.voice_agent
        session_id = await self._session_of(wiring, app_id)
        await self.collector.sweep_residue(wiring, app_id, session_id)
        if await self.collector.parked(wiring, app_id, session_id):
            rec.answered(route=metrics.ROUTE_OPENCODE, model="", msgs=0, tools=())
            rec.path = metrics.PATH_PARKED
            # An ask of ours is outstanding for this session and it is WHY this turn
            # was refused, so the fact is already in hand -- the one branch that does
            # not have to race for it.
            rec.permission_asked = True
            return PARKED, False
        if await self.collector.busy(wiring, app_id, session_id):
            # The same refusal for a cause R2D2 cannot see: the server reports this
            # session `busy` and does not serve a turn submitted into one. `parked`
            # names a pending ask and `permission_asked` agrees with it; neither can
            # be claimed here, because `GET /session/status` says `busy` and nothing
            # else -- so this is its own path rather than `parked` wearing a claim
            # nothing here supports. `SessionCollector.busy` is where the question is
            # asked, and where the silence of an unreadable answer is resolved into
            # `False` rather than into a refusal.
            rec.answered(route=metrics.ROUTE_OPENCODE, model="", msgs=0, tools=())
            rec.path = metrics.PATH_BUSY
            return BUSY_REFUSED, False
        if not await self._prepare(wiring, app_id, session_id):
            # C8: the first message in a fresh session costs 15.5-18.6s, so it is
            # submitted to the agent and acknowledged rather than waited on.
            self._handoff(rec, wiring)
            return await self.collector.hand_to_agent(wiring, app_id, session_id, spoken)
        current_application_id.set(app_id)
        try:
            choice = await rec.measure(
                wiring.backend.complete(
                    [{"role": "user", "content": spoken}],
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
            # the deadline and the agent saw none of them. The record stays a `deadline`,
            # because the turn that outran the budget was the voice turn and
            # `llm_ms` is its cost.
            #
            # It stays a `deadline` even when the hand-over below is about to decline
            # on a park, and that was tried and reverted: `parked` means "refused
            # BEFORE reaching a model", and this turn did reach one -- the voice turn
            # was submitted and simply did not answer in time. The declining hand-over
            # is the collector's event, told to the user in Telegram, not this turn's.
            # `test_a_voice_turn_that_outran_the_budget_on_a_permission_ask_says_so`
            # is what caught it.
            rec.answered(route=metrics.ROUTE_OPENCODE, model=wiring.spec.fast_model, msgs=1, tools=())
            rec.path = metrics.PATH_DEADLINE
            # The one place an ask can appear while THIS turn is in flight. The voice
            # turn is still running server-side and it is the turn that can be parked
            # right now: it reached for a tool, opencode raised `permission.asked`, and
            # this request gave up waiting at the deadline with the question already on
            # its way to the user -- which is a shape the live run logged as
            # `permission_asked=False` because the field was set nowhere at all. So it
            # is read HERE, after the branch's last await, and never on entry: an ask
            # the still-running turn raises after this line belongs to no turn of ours,
            # and the next turn is the one that records `parked` with it.
            if await self.collector.parked(wiring, app_id, session_id):
                rec.permission_asked = True
            # The voice turn is still running, so this branch hands the work over in a
            # background continuation instead of inside the request: two turns in one
            # session at once is what put a tool refusal the sweep could not see into
            # the agent's own history and killed it for the user (`qa/live-run-v6.md`
            # §D9). The session is marked as owing a residue sweep either way, because
            # the wait is bounded and a bound is not a promise.
            self.collector.note_may_leave_signal(session_id)
            return self.collector.hand_to_agent_after_the_voice_turn(
                wiring, app_id, session_id, spoken, time.monotonic()
            )
        rec.answered(route=metrics.ROUTE_OPENCODE, model=choice.model, msgs=1, tools=())
        decision = routing.parse_voice_reply(
            choice.content, sentinel=self.cfg.r2d2_needs_agent_sentinel
        )
        if decision.kind == "speak":
            return decision.spoken, False
        self._handoff(rec, wiring)
        return await self.collector.hand_to_agent(
            wiring, app_id, session_id, spoken, hint=decision.task_hint
        )

    async def answer_permission(
        self, wiring: HybridWiring | None, app_id: str, command: str
    ) -> str | None:
        """What to speak for a brokered opencode ask, or None to carry on.

        `unrelated` means there was no ask of ours, or the text is not an answer at
        all, and it changes nothing -- the question is then answered normally and
        the ask stays pending, which is the only way it can still be answered.

        **A resolved ask is reported to the recorder before this returns**, because
        the whole content of such a turn IS that answer. It used to leave every
        field at its default, and the live run read it as
        `turn route= path=voice model= agent= llm_ms=0 total_ms=35`: `voice` is the
        vocabulary for "the user heard this spoken aloud", and a `да` typed in
        Telegram is delivered as a message nobody speaks. So it is `permission`, and
        the record says no model was involved -- `route=opencode` because this is the
        opencode session route that answered, `model` empty for the same reason
        `parked`'s is, `msgs=0` and no tools because nothing was sent to a model at
        all. `permission_asked` is True because this turn is the answer to an ask,
        which is the other half of what that field claims.
        """
        broker = wiring.broker if wiring is not None else None
        if broker is None:
            return None
        verdict: PermissionVerdict = await broker.resolve_from_text(app_id, command)
        spoken: str
        match verdict:
            case "approved":
                spoken = PERMISSION_APPROVED
            case "rejected":
                spoken = PERMISSION_REFUSED
            case _:
                return None
        rec = metrics.current()
        rec.answered(route=metrics.ROUTE_OPENCODE, model="", msgs=0, tools=())
        rec.path = metrics.PATH_PERMISSION
        rec.permission_asked = True
        return spoken

    # -- internals ---------------------------------------------------------

    async def _session_of(self, wiring: HybridWiring, app_id: str) -> str:
        """The user's opencode session, or the one the database already claims.

        `OcSessionStore.resolve` re-checks the binding against `GET /session` on every
        turn, because a binding the server has never heard of is a wedge. That check is
        also the one request on this path with no deadline of its own, and right after an
        `opencode serve` restart it read-timed out on a server that was perfectly healthy
        -- the health probe in the same turn answered 200, the list of every session the
        server holds is a big answer, and the turn then fell through to the fallback chain
        and spoke `ERROR_TEXT` with a working brain behind it. `core/opencode/transport.py`
        classifies that read timeout as the typed `OpencodeDeadlineExceeded`, and this is
        where the type earns its keep: the binding is a claim the store has already
        verified, so the honest reading of "the server is slow to answer right now" is
        "use the claim", and a session the server really has dropped answers the voice
        turn 404s, which the brain already handles by falling back. With no binding there
        is nothing to fall back on, so the deadline is raised unchanged.
        """
        try:
            return await wiring.store.resolve(app_id)
        except OpencodeDeadlineExceeded as exc:
            bound = await self.memory.get_oc_session(app_id)
            if bound is None:
                raise
            self.logger.warning(
                "opencode: %s is bound to session %s and the server did not re-verify it in time "
                "(%s); the turn continues on the bound session rather than losing the answer",
                app_id, bound.session_id, exc,
            )
            return bound.session_id

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
                "opencode %r: summarised the session of %s after %d messages",
                wiring.spec.name, session_id, count,
            )
        await self.memory.touch_oc_session(app_id, message_delta=1)
        return count > 0
