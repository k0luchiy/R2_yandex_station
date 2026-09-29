"""The opencode route, assembled: the client, the session store, the session
backend, the permission broker and one event reader per session (plan todo 18).

`app/main.py` is the composition root, and this module is the part of it that has a
decision in it. `wire_opencode` answers "may R2D2 route a turn through opencode at
all", and the answer is not always yes -- so the refusal is as much of the design
as the wiring:

* **A model the server does not list is a silent brain swap (C1).** opencode
  substitutes a different model, answers HTTP 200, and the reply reads like a
  normal answer from a brain nobody chose. So `validate_models()` runs at startup
  and a failure REFUSES the route. That is the decision, and it is the same
  decision for a deployment that is MISCONFIGURED and for one that is merely
  UNCONFIGURED -- the shipped `config/backends.json` names a model id measured on
  one machine, and a stranger's `opencode serve` has no reason to offer it.
  Substituting a model to make the route work is the one thing that must not
  happen: a silent swap is exactly the failure C1 was filed about, and the
  alternative is already available -- the fallback chain, which is what the
  design says a dead primary moves to. So the refusal STAYS, and what changed is
  that it is now a *named* state (`RouteStatus.reason`) rather than one sentence
  that reads the same whether the server is down, the model is absent or the
  catalogue could not be read. An operator can only fix what the message
  distinguishes.
* **A server that is merely DOWN does not refuse the route.** The server is
  started by a systemd unit the owner controls, independently of this process, so
  refusing the wiring would mean a server started an hour later is ignored until
  R2D2 is restarted. The route is wired and marked unvalidated; the per-turn
  `client.health()` in `core/brain.py` gates the actual routing. That is
  `REASON_UNVERIFIED`: a *different* state from every refusal, and the one place
  "wired" and "working" are not the same word.
* **One `EventSource` per session, not one for the process.** `GET /event` is a
  GLOBAL stream (C5): it carries every session's events, and a reader that
  answered them all would answer a stranger's permission request on a stranger's
  turn -- the one irreversible act in this project. `EventSource` takes the one
  `session_id` it may see, so a reader per session is the only safe shape, and
  `SessionWatchingStore.resolve` is the one public method through which a new
  session can appear.

allow: SIZE_OK -- 394 pure LOC, over the 250 ceiling because the C1 gate carries the
closed vocabulary that names its outcomes, and most of that is docstrings recording
decisions an operator has to be able to re-derive. The separable half is
`SessionReaders` + `SessionWatchingStore` (the event fleet), and splitting it out
would give `wire_opencode` an import whose only other caller is this module --
while `app/main.py` has already absorbed the composition root itself. The route is
one thing: the collaborators, the C1 gate and the lifecycle of the tasks they own,
and a route split across two modules is a route whose parts can be started apart.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Final

import httpx

from app.config import Config
from app.diagnostics import ClientFactory
from app.route_status import (
    CATALOGUE_UNREADABLE,
    MODEL_UNCONFIGURED,
    MODEL_UNKNOWN,
    NO_BACKEND,
    NO_WORKSPACE,
    REGISTRY_UNREADABLE,
    UNVERIFIED,
    RouteStatus,
    decide,
)
from core.backends.config_loader import BackendConfigError, BackendSpec, load_backend_specs
from core.backends.opencode_session import OpencodeSessionBackend, OpencodeWiring
from core.brain import HybridWiring
from core.memory import Memory
from core.opencode.client import OpencodeClient, OpencodeError, OpencodeHealth
from core.opencode.models import (
    OpencodeModelCheckFailed,
    OpencodeModelUnconfigured,
    OpencodeModelUnknown,
)
from core.opencode.session_store import OcSessionStore
from core.opencode.sse import PERMISSION_ASKED, EventSource, OpencodeEvent
from core.opencode.turn_watch import TurnWatch
from core.permissions import PermissionBroker

#: Re-exported so the wiring decision and its vocabulary stay one import from here:
#: this is the module that makes them, and `app/route_status.py` exists to break a
#: cycle, not to become the name an operator's tooling has to learn.
__all__ = [
    "CATALOGUE_UNREADABLE", "MODEL_UNCONFIGURED", "MODEL_UNKNOWN", "NO_BACKEND",
    "NO_WORKSPACE", "REGISTRY_UNREADABLE", "OpencodeRoute", "RouteStatus", "SessionReaders",
    "SessionWatchingStore", "UNVERIFIED", "decide", "route_status", "wire_opencode",
]

log = logging.getLogger("r2d2.opencode")

#: The registry key of the backend that answers from a persistent opencode session.
#: `core/brain.py` knows the same backend as `SESSION_KIND`; this is CONFIGURATION,
#: and the two are separate on purpose -- the kind is what the code can build, the
#: name is what the operator wrote.
BACKEND_NAME: Final = "opencode"
#: The reaper's cadence, and the plan's. A module constant rather than a config
#: field because nothing about it is a deployment choice: a sweep aborts only
#: sessions the server still calls busy AND that nobody has used for
#: `r2d2_stale_session_seconds`, so running it more often only costs requests.
REAP_INTERVAL_S: Final = 60.0
#: The reaper task's name, so a leaked task is identifiable in a debugger.
REAPER_TASK: Final = "r2d2-opencode-reaper"


class SessionReaders:
    """One `GET /event` reader per opencode session, and the stop signal they share.

    `ensure()` is idempotent per session, so a startup reconciliation, a returning
    user's next turn and a second turn in that session all converge on ONE reader
    rather than one per event. The shared `stop` is what makes shutdown bounded: a
    reader blocked in a silent stream is cancelled, and one between reconnects sees
    the flag and returns without sleeping out the ladder.
    """

    def __init__(self, spec: BackendSpec, directory: str, broker: PermissionBroker,
                 turns: TurnWatch | None = None) -> None:
        self._spec = spec
        self._directory = directory
        self._broker = broker
        self._turns = turns
        self._stop = asyncio.Event()
        self._tasks: dict[str, asyncio.Task[None]] = {}

    @property
    def count(self) -> int:
        """How many sessions are being read. The only thing an operator needs to see."""
        return len(self._tasks)

    def ensure(self, app_id: str, session_id: str) -> None:
        """Start a reader for this session unless one is already running.

        Called from `SessionWatchingStore.resolve` -- every opencode turn -- and once
        per already-bound session at startup, so a returning user's permission asks
        can be heard without waiting for them to ask a question first.
        """
        if session_id in self._tasks:
            return
        self._tasks[session_id] = asyncio.create_task(
            self._read(app_id, session_id), name=f"r2d2-opencode-events:{session_id}"
        )
        log.info(
            "opencode events: watching session %s of %s for permission asks", session_id, app_id
        )

    async def aclose(self) -> None:
        """Stop every reader and wait for it, so shutdown leaves no pending task."""
        self._stop.set()
        tasks = list(self._tasks.values())
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task
        if tasks:
            log.info("opencode events: %d reader(s) stopped", len(tasks))

    # -- internals ---------------------------------------------------------

    async def _read(self, app_id: str, session_id: str) -> None:
        """One reader for the rest of this process's life, reconnecting as needed."""
        events = EventSource(self._spec, self._directory, session_id=session_id)
        try:
            await events.run(self._handler(app_id, session_id), stop=self._stop)
        finally:
            # The reader owns the EventSource it built, so cancelling the task
            # closes the connection instead of leaving a pool behind per session.
            await events.aclose()

    def _handler(
        self, app_id: str, session_id: str
    ) -> Callable[[OpencodeEvent], Awaitable[None]]:
        async def handle(event: OpencodeEvent) -> None:
            if self._turns is not None:
                # Before the broker's branch, and for EVERY frame: a collector has to learn
                # that the turn ended as well as that nothing of ours waits on a human.
                self._turns.note(event)
            if event.type != PERMISSION_ASKED:
                return
            permission_id, session = event.permission_id, event.session_id
            if permission_id is None or session is None:
                log.warning(
                    "opencode events: a %s of session %r carried no id to answer",
                    PERMISSION_ASKED,
                    session,
                )
                return
            await self._broker.on_permission_requested(
                app_id, session, permission_id, event.title, event.always
            )

        return handle


class SessionWatchingStore(OcSessionStore):
    """The session store, plus: a reader is started for every session it touches.

    `resolve()` is the public method every opencode turn already calls and the only
    one that can produce a session id this process has not seen, so it is the one
    place a reader has to hang off. Overriding the public method rather than the
    private `_create` is deliberate: this subclass adds no knowledge of the base
    class's internals, so a change there cannot silently orphan a reader.
    """

    def __init__(
        self, memory: Memory, client: OpencodeClient, cfg: Config, readers: SessionReaders
    ) -> None:
        super().__init__(memory, client, cfg)
        self._readers = readers

    async def resolve(self, application_id: str) -> str:
        session_id = await super().resolve(application_id)
        self._readers.ensure(application_id, session_id)
        return session_id


@dataclass(slots=True)
class OpencodeRoute:
    """One complete opencode route, and the lifecycle of everything it owns.

    `models` is the DISTINCT ids `validate_models()` checked, and it is empty when
    the server did not answer at startup. "verified" and "unverified" is the
    difference the startup line has to say out loud, so it is a value here rather
    than a log line nobody can query.

    A route OWNS its tasks -- the readers and the reaper -- and stops them in
    `stop()`. The alternative is a lifespan that knows how many tasks a route
    started and has to find them again to cancel them, which is exactly how a
    "Task was destroyed but it is pending" warning is born.
    """

    wiring: HybridWiring
    readers: SessionReaders
    models: tuple[str, ...] = ()
    _reaper: asyncio.Task[None] | None = None
    _stopped: bool = False

    @property
    def models_label(self) -> str:
        """The verified model ids for the startup line, or what the operator must know.

        `unverified` is not a detail: an empty `models` means the server did not
        answer at startup, so C1's silent-brain-swap hazard is unproved either way,
        and an operator reading "started" must not take it as a guarantee.
        """
        return ",".join(self.models) if self.models else "unverified"

    async def start(self) -> None:
        """Arm the broker's sweep and the reaper. Once; a second call is a no-op."""
        if self._stopped:
            return
        broker = self.wiring.broker
        if broker is not None:
            await broker.start()
        # The first sweep runs inline, before the reaper exists: a session a crashed
        # run left busy is worth clearing while this process is still deciding
        # whether it can answer at all, and a sweep that only began 60 s later would
        # let a dead turn run straight through a restart.
        await self.wiring.store.reap()
        self._reaper = asyncio.create_task(_reap_forever(self.wiring.store), name=REAPER_TASK)

    async def stop(self) -> None:
        """Stop the reaper, the readers, the sweep and the client, in that order.

        Reverse order because each step depends on the one before: the readers
        answer through the broker, the broker answers through the client a reader
        holds, and the store is the last thing standing. Every step is contained, so
        a failure in one cannot leave the others running, and the `_stopped` guard
        makes a second call the same as the first -- a lifespan and a supervisor may
        both reach shutdown, and the second must not re-close a closed client.
        """
        if self._stopped:
            return
        self._stopped = True
        await self._cancelled(self._reaper)
        self._reaper = None
        with suppress(Exception):
            await self.readers.aclose()
        broker = self.wiring.broker
        if broker is not None:
            with suppress(Exception):
                await broker.stop()
        with suppress(Exception):
            await self.wiring.backend.aclose()

    @staticmethod
    async def _cancelled(task: asyncio.Task[None] | None) -> None:
        """Cancel one task and wait for it, so nothing is left pending."""
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def wire_opencode(
    cfg: Config,
    memory: Memory,
    health: OpencodeHealth | None,
    factory: ClientFactory,
) -> OpencodeRoute | None:
    """Build the opencode route, or record the reason there is none. Never raises.

    `factory` is the composition root's `OpencodeClient` handed in rather than
    imported; `app/diagnostics.py` explains why the seam belongs to the caller.
    `None` means the route is NOT in use and every turn is answered by the chain --
    a supported deployment, not a failure to boot, but never a silent one: the
    reason is in `route_status()` before this returns, and at ERROR in the log.
    """
    spec = _spec(cfg)
    if spec is None:
        return None
    if not os.path.isdir(cfg.r2d2_workspace):
        # C6: the server does not validate `?directory=`, so a workspace that is not
        # there would hand the agent's file tools a root that does not exist, and
        # every session request would fail -- one turn at a time, for ever.
        return _refused(NO_WORKSPACE, f"the workspace {cfg.r2d2_workspace!r} does not exist")
    client = factory(spec, cfg.r2d2_workspace)
    broker = PermissionBroker(memory, client, cfg)
    turns = TurnWatch()
    readers = SessionReaders(spec, cfg.r2d2_workspace, broker, turns)
    store = SessionWatchingStore(memory, client, cfg, readers)
    backend = OpencodeSessionBackend(spec, OpencodeWiring(client=client, store=store, cfg=cfg))
    await _reattach(readers, memory)
    status = await _validated(backend, spec, reachable=bool(health and health.reachable))
    if not status.wired:
        return None
    wiring = HybridWiring(spec=spec, client=client, store=store, backend=backend,
                          broker=broker, turns=turns)
    decide(status)
    return OpencodeRoute(wiring=wiring, readers=readers, models=status.models)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _spec(cfg: Config) -> BackendSpec | None:
    """The opencode backend's spec, or `None` with the cause already recorded.

    A missing one is an ERROR even though the chain-only deployment works: the
    shipped `config/backends.json` declares this backend and `.env.oc.example`
    documents its password, so a registry without it is a misconfiguration whose
    only symptom would otherwise be an assistant that quietly stopped using the
    brain it was built around.
    """
    try:
        _chain, specs = load_backend_specs(cfg)
    except BackendConfigError as exc:
        _refused(REGISTRY_UNREADABLE, f"cannot read {cfg.backends_path}: {exc}")
        return None
    spec = specs.get(BACKEND_NAME)
    if spec is None:
        _refused(NO_BACKEND, f"no {BACKEND_NAME!r} backend in {cfg.backends_path}")
    return spec


def _refused(reason: str, detail: str) -> None:
    """Record and announce that the route is not in use. The only refusal path.

    Says the one consequence it can honestly claim: the chain is what answers, and
    no model is substituted for the one configured. Whether the chain HAS a
    backend that can answer is not claimed here -- `fallback=` on the startup line
    reports that, and claiming it here would be the misleading success this
    replaces.
    """
    decide(RouteStatus(wired=False, reason=reason, detail=detail))
    log.error(
        "opencode: the brain route is NOT wired [%s]: %s. No model is substituted for the one "
        "configured; the fallback chain is what answers, and the startup line's fallback= field "
        "says whether it has a backend that can", reason, detail,
    )
    return None


async def _reap_forever(store: OcSessionStore, interval_s: float = REAP_INTERVAL_S) -> None:
    """Sweep the stale sessions every `interval_s` until cancelled.

    The sweep runs on the loop rather than inside a request: it is a cleanup task,
    and the only thing that should ever interrupt it is shutdown cancelling it.
    Cancellation is therefore the whole shutdown mechanism, which is why this has no
    `stop` flag -- a flag would be a second path to the same exit, and one of the
    two would eventually be forgotten.
    """
    while True:
        await asyncio.sleep(interval_s)
        await store.reap()


async def _reattach(readers: SessionReaders, memory: Memory) -> None:
    """Start a reader for every session already bound, so a restart loses none.

    A database that cannot be read is not fatal: a reader per session is the
    permission path, and each session starts its own on its next turn. No turn is
    affected either way, which is why this logs and carries on.
    """
    try:
        bindings = await memory.all_oc_sessions()
    except (OSError, sqlite3.Error) as exc:
        log.warning(
            "opencode events: the bound sessions could not be read (%s); each session will "
            "start its own reader on its next turn", exc,
        )
        return
    for binding in bindings:
        readers.ensure(binding.application_id, binding.session_id)


async def _validated(
    backend: OpencodeSessionBackend, spec: BackendSpec, *, reachable: bool
) -> RouteStatus:
    """The C1 gate, as a decision. Never raises; never substitutes a model.

    Three refusal reasons, one per operator mistake, each with its own fix:

    * `MODEL_UNKNOWN` -- the server answered and does not offer the id: a typo, or
      the shipped `config/backends.json` naming a model measured elsewhere. The
      message says so, because the file to edit is named in it.
    * `MODEL_UNCONFIGURED` -- a slot is empty, so opencode would answer from ITS
      own default. A different line, a different mistake.
    * `CATALOGUE_UNREADABLE` -- `GET /config/providers` could not be read, so it is
      UNKNOWN whether the models exist. Deliberately NOT `MODEL_UNKNOWN`: that
      would send the operator after a config typo that is not the cause, which is
      why `core/opencode/models.py` has a class for it.

    `UNVERIFIED` is not a refusal: the route is wired, and the per-turn
    `client.health()` in `core/brain.py` gates the actual routing.
    """
    if not reachable:
        detail = (
            f"{spec.name!r} did not answer at startup, so {spec.fast_model!r} is UNVALIDATED; "
            "every turn checks liveness before it routes, and the models are checked when it does"
        )
        log.info("opencode: %s", detail)
        return RouteStatus(wired=True, reason=UNVERIFIED, detail=detail)
    try:
        return RouteStatus(wired=True, models=await backend.validate_models())
    except (OpencodeModelUnknown, OpencodeModelUnconfigured) as exc:
        _refused(
            MODEL_UNKNOWN if isinstance(exc, OpencodeModelUnknown) else MODEL_UNCONFIGURED,
            f"{exc} Read GET /config/providers and put a model it lists into config/backends.json",
        )
    except (OpencodeModelCheckFailed, httpx.HTTPError, OpencodeError) as exc:
        # The third mode, and the one the old single sentence got wrong: the
        # catalogue was never read, so nothing is known about the models.
        _refused(CATALOGUE_UNREADABLE, f"{spec.name!r}: {exc}")
    return RouteStatus(wired=False, reason=CATALOGUE_UNREADABLE)
