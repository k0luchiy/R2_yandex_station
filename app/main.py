"""The composition root: build the stack, start it, stop it (plan todo 18).

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
from contextlib import asynccontextmanager
from typing import Final

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app import diagnostics
from app.config import Config
from app.opencode_route import wire_opencode
from core.async_worker import Worker
from core.backends.config_loader import BackendConfigError, load_backend_specs
from core.brain import Brain
from core.memory import Memory
from core.opencode.client import OpencodeClient, OpencodeHealth
from core.tools.telegram_tool import send_message

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("r2d2")

#: What the owner runs when the gate below reports the server unreachable.
LAUNCHER = "scripts/opencode_serve.sh"
#: The variable that declares which human a Telegram chat belongs to. Named in the
#: warning a chat with no binding gets, because the whole point of not inventing
#: an identity is that the operator is told exactly which knob is missing.
TG_APPLICATION_ID_VAR: Final = "R2D2_TG_APPLICATION_ID"
#: What separates the `chat_id=application_id` pairs of that declaration.
TG_BINDING_SEPARATORS: Final = re.compile(r"[,\s]+")


def tg_application_id(cfg: Config, chat_id: int) -> str | None:
    """The `application_id` this Telegram chat was bound to, or `None` for no binding.

    **The binding is declared, never derived.** One human is one `application_id`,
    and that id is what owns their single opencode session and their single pending
    permission question -- so a Telegram turn and an Alice turn of the same person
    have to arrive under the same one, or the answer to «да» is delivered to a
    session the question was never asked in. `/tg/webhook` used to build
    `f"tg:{chat_id}"` for itself, which is the live defect in `qa/live-run.md` §4c.

    `cfg.r2d2_tg_application_id` holds `chat_id=application_id` pairs separated by
    commas or whitespace; a single-user deployment has exactly one, and a second
    chat id is bound by declaring a second pair rather than by being invented. An
    id may not contain a separator -- it is an opaque Alice `application_id`, and a
    space in one would silently truncate the binding.

    `None` is the honest answer for a chat nobody declared, and the caller must
    refuse the turn on it rather than fall back to anything. A token this build
    cannot read is a WARNING naming the variable; it binds nothing, so one typo
    cannot be mistaken for a declaration.
    """
    for token in TG_BINDING_SEPARATORS.split(cfg.r2d2_tg_application_id.strip()):
        if not token:
            continue
        declared_chat, separator, app_id = token.partition("=")
        if not separator or not declared_chat.isdigit() or not app_id.strip():
            logger.warning(
                "telegram: %s carries a token this build cannot read (%r); it expects "
                "chat_id=application_id, and this token binds nothing",
                TG_APPLICATION_ID_VAR, token,
            )
            continue
        if int(declared_chat) == chat_id:
            return app_id.strip()
    return None


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

    health = await OpencodeClient(spec, cfg.r2d2_workspace).health()
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


def authorisation_posture(cfg: Config) -> str | None:
    """What to tell the operator at startup about who this webhook serves, or `None`.

    `Brain.authorized` fails closed, so an undeclared id is a closed webhook and not
    an open one -- which is the safe state, and also a state that looks like a broken
    skill to anyone who has not read the source. Saying it once, at startup, with the
    variable names in the message, is what turns "my skill stopped answering" into
    "I have not told it who I am".

    Returns `None` when the deployment is configured, and a message when it is not.
    The message is an operator instruction, not a diagnostic, so it is phrased as the
    command to run and never mentions an id value.
    """
    if cfg.r2d2_allow_unauthenticated:
        return (
            "R2D2_ALLOW_UNAUTHENTICATED is set: /webhook answers ANY caller, and the "
            "opencode agent and the r2d2_do shim are reachable behind it. Set "
            "ALICE_SKILL_ID and ALICE_USER_ID and unset this before exposing the port."
        )
    missing = [
        name for name, value in
        (("ALICE_SKILL_ID", cfg.alice_skill_id), ("ALICE_USER_ID", cfg.alice_user_id))
        if not value
    ]
    if not missing:
        return None
    return (
        f"{' and '.join(missing)} not set: /webhook refuses every request, because an "
        "undeclared id is nobody in particular. Register the private skill at "
        "dialogs.yandex.ru, put its skill_id and your user_id in .env, and restart. "
        "To drive the voice path before registering, set R2D2_ALLOW_UNAUTHENTICATED=1 "
        "and keep the port on loopback."
    )


def build_app() -> FastAPI:
    cfg = Config.load()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        memory = await Memory(cfg.resolved_db_path()).connect()
        health = await probe_opencode_server(cfg)
        route = await wire_opencode(cfg, memory, health, OpencodeClient)
        worker = Worker(cfg, memory, logger, route.wiring.backend if route else None)
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
        # The last field is `fallback` and NOT `chain` on purpose: these are the
        # backends `build_chain` could actually build, while `/health`'s `chain` is
        # the declared order minus the session backend. Two different numbers under
        # one name is how an operator concludes the health probe is lying.
        auth = authorisation_posture(cfg)
        if auth is not None:
            logger.error("%s", auth)
        logger.info(
            "R2D2 started, db=%s, opencode=%s, models=%s, fallback=%s, alice=%s",
            cfg.resolved_db_path(),
            "wired" if route is not None else "unwired",
            route.models_label if route is not None else "none",
            fallback,
            auth or "closed",
        )
        try:
            yield
        finally:
            if route is not None:
                await route.stop()
            await worker.stop()
            await memory.close()

    app = FastAPI(title="R2D2", lifespan=lifespan)

    @app.get("/health")
    async def health(request: Request):
        """Liveness of THIS process, plus what it can still answer with.

        The status code is 200 whenever the process is serving, degraded or not.
        An unreachable opencode server is a degraded *dependency*, not a fault
        here: R2D2 keeps answering from the fallback chain, and a monitor that
        flaps on a dependency -- or that restarts R2D2, which cannot fix one --
        is worse than a probe that reports the degradation in a field.
        """
        cfg: Config = request.app.state.cfg
        memory: Memory = request.app.state.memory
        liveness, chain = await diagnostics.health_view(cfg, load_backend_specs, OpencodeClient)
        return {
            "status": "ok",
            "opencode": liveness,
            "sessions": len(await memory.all_oc_sessions()),
            "chain": chain,
        }

    @app.get("/diagnostics/providers")
    async def diagnostics_providers(request: Request):
        """The live provider and agent catalogs; `app/diagnostics.py` has the why."""
        cfg: Config = request.app.state.cfg
        return await diagnostics.providers_view(cfg, load_backend_specs, OpencodeClient)

    @app.get("/")
    async def root():
        return {
            "service": "R2D2",
            "health": "/health",
            "webhook": "/webhook",
            "tg": "/tg/webhook",
            "diagnostics": "/diagnostics/providers",
        }

    @app.post("/webhook")
    async def webhook(request: Request):
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "bad json"}, status_code=400)
        brain: Brain = request.app.state.brain
        if not brain.authorized(body):
            return JSONResponse({"error": "forbidden"}, status_code=403)
        return await brain.process_alice(body)

    @app.post("/tg/webhook")
    async def tg_webhook(request: Request):
        try:
            body = await request.json()
        except Exception:
            return {"ok": True}
        message = body.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        text = (message.get("text") or "").strip()
        brain: Brain = request.app.state.brain
        cfg: Config = request.app.state.cfg
        if (
            chat_id
            and text
            and cfg.telegram_chat_id_int
            and chat_id == cfg.telegram_chat_id_int
        ):
            app_id = tg_application_id(cfg, int(chat_id))
            if app_id is None:
                # No declared identity means no session to share, no pending row to
                # answer and nobody to answer to. Minting `tg:<chat_id>` here is the
                # defect this replaced: a second opencode session for one person,
                # and a «да» that landed in a session the question was never asked
                # in. Dropping the message keeps the pending ask refusable by the
                # sweep, which is the direction every ambiguous path must fail in.
                logger.warning(
                    "telegram: chat %s is not bound to an application_id, so this message is "
                    "not answered; set %s='%s=<the application_id your Alice turns use>'. No "
                    "identity is invented here: a second one would be a second session for one "
                    "person, and could not answer the permission question that person was asked",
                    chat_id, TG_APPLICATION_ID_VAR, chat_id,
                )
                return {"ok": True}
            fake = {
                "meta": {"interfaces": {}},
                "request": {"type": "SimpleUtterance", "command": text},
                "session": {
                    "new": True,
                    # Both identity fields carry the bound id, not the chat id: the brain
                    # falls back to `user_id` when `application_id` is absent, so a chat
                    # id in either one could become an identity again.
                    "application": {"application_id": app_id},
                    "user": {"user_id": app_id},
                },
                "version": "1.0",
            }
            resp = await brain.process_tg(fake)
            reply = resp["response"]["text"]
            if reply:
                await send_message(cfg, reply, chat_id=chat_id)
        return {"ok": True}

    return app


app = build_app()
