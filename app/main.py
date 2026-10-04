"""The composition root: build the ASGI object, start the collaborators, stop them
(plan todo 18).

Everything R2D2 is made of is created here, in the order the objects depend on
each other, and every task started here is stopped here in the reverse order --
an un-stopped task is a "Task was destroyed but it is pending" warning at every
reload, and a warning nobody reads is how a leak survives.

The order, and why it is this order:

1. `Memory` -- everything else needs the database, and it is the only thing that
   must exist before the registry can be read meaningfully.
2. the startup gate (`probe_opencode_server`) -- it answers "is the brain there?",
   and the wiring below is built differently depending on the answer.
3. the opencode route (`app.opencode_route.wire_opencode`): client, session store,
   session backend, broker, event readers, and the C1 model gate.
4. the `Worker`, **with** the session backend when there is one -- todo 16's
   `opencode_reply` job collects the answer of a turn that outran the voice
   budget, and a worker without the collector answers "Неизвестная задача.".
5. the `Brain`, with the route as `HybridWiring` or with nothing, which is the
   supported "answer from the chain only" deployment.
6. start: the worker, the broker's 30 s sweep, the 60 s reaper, and the session
   readers that are already attached.

**This module's namespace is the project's one injection seam, and it is read per
request.** `load_backend_specs`, `OpencodeClient` and `send_message` are named
here and nowhere else; the five routes live in `app/http.py` and receive the three
of them as parameters, which `app/diagnostics.py` has always done for the same
two names. They are read inside the handler rather than captured when the process
was imported, because a deployment swaps them after startup -- a hung opencode
server, a test double, a Telegram channel that must not be the owner's -- and a
route holding a name from import time would keep talking to whatever the import
said. A helper that imported them for itself would be a second, invisible copy:
a test that replaced this one would watch the real server being called instead,
which is the worst kind of test failure -- a green one that measured nothing.

**What this module does NOT own.** A request's meaning is `app/http.py`: five
functions, one per endpoint, that read `app.state` and return a body. Who a
request belongs to is `app/identity.py`: the declared `chat_id=application_id`
binding and the startup posture, both re-exported below so the names that were
importable from here stay importable from here. Both are split out because both
have readers other than this one, and a fact three readers need is a fact someone
has to own -- the owner should not also be the module that builds the ASGI object.

**The server is never started, never killed and never waited for.** A systemd user
unit (`scripts/r2d2-opencode.service`) owns `opencode serve`; this process is its
client. No process-control API is named anywhere in `app/` -- not even in a
docstring -- and `tests/test_serve_unit.py` asserts their absence by reading this
file's text.

**A missing opencode server does not stop R2D2 from starting.** The fallback chain
carries every turn, `/health` reports the degradation in a field, and the startup
line says `opencode=unwired` rather than a bare "started" -- a misleading success
is the failure mode here, not a crash.
"""

import logging
import re
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request

from app import diagnostics, http
from app.config import Config
from app.diagnostics import TG_APPLICATION_ID_VAR
# `authorisation_posture` and `tg_application_id` are DEFINED in
# `app/identity.py` and re-exported here, because this is where both were
# importable from and two test modules still reach for them by that name. The
# lifespan below calls the first one; keeping the second next to it costs one
# import and saves a reader from two ways to spell one function.
from app.identity import authorisation_posture, tg_application_id
from app.opencode_route import wire_opencode
from app.route_status import route_status
from core.async_worker import Worker
from core.backends.config_loader import BackendConfigError, load_backend_specs
from core.brain import Brain
from core.memory import Memory
from core.opencode.client import OpencodeClient, OpencodeHealth
from core.tools.telegram_tool import send_message

__all__ = ["authorisation_posture", "build_app", "probe_opencode_server", "tg_application_id"]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("r2d2")

#: The bot token lives in the URL PATH of every Telegram call, and `httpx` logs the
#: full URL at INFO -- which this module just enabled globally. Observed in a live
#: run: `POST https://api.telegram.org/bot<token>/sendMessage "HTTP/1.1 200 OK"`.
#: That is the same exposure as the credentials route, arriving through a channel
#: nobody audits: the systemd journal keeps it long after the request is forgotten.
#:
#: Only the token is masked. Method, endpoint and status all survive, because those
#: are what the line is FOR -- a 2xx the client cannot read is a fact an operator
#: needs, and blanking the whole URL would throw that away to hide a substring.
_BOT_TOKEN_IN_URL = re.compile(r"/bot\d+:[A-Za-z0-9_-]{10,}")


class _RedactBotToken(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        rendered = record.getMessage()
        if _BOT_TOKEN_IN_URL.search(rendered):
            record.msg = _BOT_TOKEN_IN_URL.sub("/bot<token>", rendered)
            record.args = ()
        return True


for _handler in logging.getLogger().handlers:
    _handler.addFilter(_RedactBotToken())

#: What the owner runs when the gate below reports the server unreachable.
LAUNCHER = "scripts/opencode_serve.sh"


async def probe_opencode_server(cfg: Config) -> OpencodeHealth | None:
    """One liveness probe of the opencode server, logged. Never raises.

    `None` means "no opencode backend to probe" -- either the registry could not
    be read or it declares no such backend -- which is a different situation from
    "the server is down", and R2D2 must boot either way: the fallback backend
    chain carries the request until `wire_opencode` has its say.
    """
    try:
        _chain, specs = load_backend_specs(cfg)
    except BackendConfigError as exc:
        logger.error("opencode: cannot read %s: %s", cfg.backends_path, exc)
        return None
    spec = specs.get("opencode")
    if spec is None:
        logger.info("opencode: no 'opencode' backend in %s", cfg.backends_path)
        return None

    client = OpencodeClient(spec, cfg.resolved_workspace())
    try:
        health = await client.health()
    finally:
        # The sibling probe in app/diagnostics.py closes its client and says why:
        # an unclosed client leaks one connection pool per probe. This one built
        # one at every process start and never closed it.
        await client.aclose()
    if health.reachable:
        logger.info("opencode: server reachable at %s, version %s", spec.base_url, health.version)
    else:
        logger.error(
            "opencode: server unreachable at %s and reported no version; start it with %s "
            "-- until then R2D2 answers from the fallback backends",
            spec.base_url,
            LAUNCHER,
        )
    return health


def build_app() -> FastAPI:
    cfg = Config.load()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        memory = await Memory(cfg.resolved_db_path()).connect()
        # Unwind is registered BEFORE the corresponding `start()`, because a
        # callback pushed after the thing it stops cannot run in the case that
        # matters: a startup that raises partway through. Every stop below is
        # safe on a never-started object -- `Worker.stop` cancels and gathers two
        # empty lists, `route.stop` returns at once behind its `_stopped` guard --
        # so the LIFO unwind closes route, worker and memory on a failed boot in
        # the same order as a clean one. The old code started the worker and the
        # route, then called `fallback_backends`, and only reached its `finally`
        # if all of that succeeded: a bad backends.json raised with the worker's
        # loop task and the sqlite connection still open, and the traceback named
        # the config error while saying nothing about the two objects it leaked.
        async with AsyncExitStack() as stack:
            stack.push_async_callback(memory.close)
            health = await probe_opencode_server(cfg)
            route = await wire_opencode(cfg, memory, health, OpencodeClient)
            worker = Worker(cfg, memory, logger, route.wiring.backend if route else None)
            # Push order is memory, worker, route: LIFO stops route, then worker,
            # then memory. Each push must precede its `start()` -- a callback
            # registered after the line that can raise protects nothing.
            if route is not None:
                stack.push_async_callback(route.stop)
            stack.push_async_callback(worker.stop)
            brain = Brain(cfg, memory, worker, logger, opencode=route.wiring if route else None)
            app.state.cfg = cfg
            app.state.memory = memory
            app.state.worker = worker
            app.state.brain = brain
            app.state.route = route
            await worker.start()
            if route is not None:
                await route.start()
            fallback = await diagnostics.fallback_backends(cfg, load_backend_specs)
            # `opencode=wired`/`unwired` is the field that keeps this line from reading
            # as a guarantee: a startup that says only "started" is what an operator
            # with a dead opencode server (or a refused model) would take for a brain.
            # `reason` is the same decision named, so "unwired" is never the whole
            # answer -- it is one token of `app/route_status.py`'s vocabulary, and it
            # says WHICH of the refusals this is. The last field is `fallback` and NOT
            # `chain` on purpose: these are the backends `build_chain` could actually
            # build, while `/health`'s `chain` is the declared order minus the session
            # backend. Two different numbers under one name is how an operator
            # concludes the health probe is lying.
            auth = authorisation_posture(cfg)
            if auth is not None:
                logger.error("%s", auth)
            status = route_status()
            telegram = diagnostics.telegram_binding(cfg)
            if not telegram["declared"]:
                # Discoverability, not a behaviour change: `/tg/webhook` already refuses
                # an undeclared chat, and it must keep refusing. What was missing is
                # that the deployment LOOKS complete while its headline feature is inert
                # -- long results and permission questions never arrive, and the only
                # symptom is a Telegram chat that answers nothing.
                logger.warning(
                    "telegram: %s is empty, so no chat has an identity. Long results and "
                    "permission questions will not arrive, /tg/webhook drops every message with a "
                    "warning, and no identity is invented for it -- one chat, one application_id, "
                    "or a 'да' in Telegram cannot answer a question asked by voice. Declare %s='%s' "
                    "and restart (pairs are separated by commas or whitespace).",
                    TG_APPLICATION_ID_VAR, TG_APPLICATION_ID_VAR,
                    f"{diagnostics.TG_BINDING_FORMAT} for each chat",
                )
            alice_posture = (
                "open"
                if cfg.r2d2_allow_unauthenticated
                else ("declared" if auth is None else "closed")
            )
            logger.info(
                "R2D2 started, db=%s, opencode=%s, models=%s, fallback=%s, alice=%s, "
                "alice_user=%s, reason=%s, telegram=%s",
                cfg.resolved_db_path(),
                "wired" if route is not None else "unwired",
                route.models_label if route is not None else "none",
                fallback,
                alice_posture,
                cfg.alice_user_id or "unset",
                status.reason,
                "declared" if telegram["declared"] else "unbound",
            )
            yield

    app = FastAPI(title="R2D2", lifespan=lifespan)

    # The five routes, in the order they have always been registered in: FastAPI
    # matches in registration order, and `/health` answering before `/` is a fact
    # about this composition root rather than about the handlers. Each one reads
    # the collaborators it needs from THIS module's namespace at call time and
    # hands them to `app/http.py`; the bodies are not here.
    @app.get("/health")
    async def health(request: Request) -> Any:
        return await http.health(request, load=load_backend_specs, factory=OpencodeClient)

    @app.get("/diagnostics/providers")
    async def diagnostics_providers(request: Request) -> Any:
        return await http.diagnostics_providers(
            request, load=load_backend_specs, factory=OpencodeClient
        )

    @app.get("/")
    async def root() -> Any:
        return await http.root()

    @app.post("/webhook")
    async def webhook(request: Request) -> Any:
        return await http.webhook(request)

    @app.post("/tg/webhook")
    async def tg_webhook(request: Request) -> Any:
        return await http.tg_webhook(request, send=send_message)

    return app


app = build_app()
