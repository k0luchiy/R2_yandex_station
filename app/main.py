import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Final

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.config import Config
from core.async_worker import Worker
from core.backends.config_loader import BackendConfigError, BackendSpec, load_backend_specs
from core.brain import SESSION_KIND, Brain
from core.memory import Memory
from core.opencode.client import (
    OpencodeClient,
    OpencodeError,
    OpencodeHealth,
    OpencodeProtocolError,
)
from core.tools.telegram_tool import send_message

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("r2d2")

#: What the owner runs when the gate below reports the server unreachable.
LAUNCHER = "scripts/opencode_serve.sh"
#: The bound on the `/health` liveness probe, and it is the ROUTE's bound rather
#: than the client's: a server that stops reading the socket does not end the
#: client's own timeout any sooner, and a probe that hangs is the one thing an
#: operator cannot diagnose with.
HEALTH_PROBE_TIMEOUT_S: Final = 2.0
#: The only body the diagnostics route answers 503 with, and the only error a
#: caller has to parse.
UNREACHABLE: Final = "opencode unreachable"


async def probe_opencode_server(cfg: Config) -> OpencodeHealth | None:
    """One liveness probe of the opencode server, logged. Never raises.

    `None` means "no opencode backend to probe" -- either the registry could not
    be read or it declares no such backend -- which is a different situation
    from "the server is down", and R2D2 must boot either way: the fallback
    backend chain carries the request until todo 18 wires the client in.
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


async def probe_opencode(spec: BackendSpec | None, cfg: Config) -> OpencodeHealth:
    """One bounded liveness probe of a configured opencode server. Never raises.

    A registry with no `opencode` backend reads exactly like a server that is
    down -- R2D2 has no opencode route and the chain carries every turn -- so it
    answers with the same `reachable=False` and no address.

    It does not log: `probe_opencode_server` already reported the cause at
    startup with the command that fixes it, and a route a monitor polls every few
    seconds would repeat that line forever. The answer belongs in the body.
    """
    if spec is None:
        return OpencodeHealth(reachable=False)
    client = OpencodeClient(spec, cfg.r2d2_workspace)
    try:
        return await asyncio.wait_for(client.health(), HEALTH_PROBE_TIMEOUT_S)
    except TimeoutError:
        return OpencodeHealth(reachable=False)
    finally:
        # The client opens its own connection pool on first use, so a client left
        # unclosed here leaks one pool per probe -- i.e. per health check.
        await client.aclose()


async def health_view(cfg: Config) -> tuple[dict[str, object], list[str]]:
    """`(the opencode key of the health body, the fallback chain)`. Never raises.

    The chain is the *effective* one: `config/backends.json` lists the session
    backend first because it is the primary route, and `core/brain.py` drops it
    from the walk (a second attempt on the host that just failed spends budget the
    turn no longer has), so reporting it here would advertise a backend that can
    never answer a turn.

    A registry that cannot be read is the loud case, not a quiet one: the body
    then carries `chain: []`, which says out loud that R2D2 has no brain at all
    rather than a brain with a missing part.
    """
    try:
        chain, specs = load_backend_specs(cfg)
    except BackendConfigError:
        return {"reachable": False, "version": None, "base_url": ""}, []
    spec = specs.get("opencode")
    liveness = await probe_opencode(spec, cfg)
    return (
        {
            "reachable": liveness.reachable,
            "version": liveness.version,
            "base_url": spec.base_url if spec is not None else "",
        },
        [name for name in chain.order if specs[name].kind != SESSION_KIND],
    )


def unreachable() -> JSONResponse:
    return JSONResponse({"error": UNREACHABLE}, status_code=503)


def build_app() -> FastAPI:
    cfg = Config.load()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        memory = await Memory(cfg.resolved_db_path()).connect()
        worker = Worker(cfg, memory, logger)
        await worker.start()
        brain = Brain(cfg, memory, worker, logger)
        app.state.cfg = cfg
        app.state.memory = memory
        app.state.worker = worker
        app.state.brain = brain
        await probe_opencode_server(cfg)
        logger.info("R2D2 started, db=%s, provider=%s", cfg.resolved_db_path(), cfg.llm_provider)
        yield
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
        liveness, chain = await health_view(cfg)
        return {
            "status": "ok",
            "opencode": liveness,
            "sessions": len(await memory.all_oc_sessions()),
            "chain": chain,
        }

    @app.get("/diagnostics/providers")
    async def diagnostics_providers(request: Request):
        """The live `GET /config/providers` and `GET /agent` payloads, unedited.

        Which models the server can reach and which agents it exposes is the pair
        of facts that decides whether a turn is answered or refused -- C1 measured
        a 403 `FreeTierError` inside an HTTP 200 -- and neither is visible in a log
        line. The route is unauthenticated, so it returns what the server said and
        nothing else: an upstream body that will not parse is never echoed back.
        """
        cfg: Config = request.app.state.cfg
        try:
            _chain, specs = load_backend_specs(cfg)
        except BackendConfigError:
            return unreachable()
        spec = specs.get("opencode")
        if spec is None:
            return unreachable()
        client = OpencodeClient(spec, cfg.r2d2_workspace)
        try:
            providers = await client.providers()
            agents = await client.agents()
        except OpencodeProtocolError:
            # A 2xx the client cannot read: the server spoke, and it spoke
            # nonsense. 502, and never the body -- see the docstring.
            return JSONResponse({"error": "opencode answered with a malformed body"}, status_code=502)
        except (httpx.HTTPError, OpencodeError):
            return unreachable()
        finally:
            await client.aclose()
        return {"providers": providers, "agents": agents}

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
            fake = {
                "meta": {"interfaces": {}},
                "request": {"type": "SimpleUtterance", "command": text},
                "session": {
                    "new": True,
                    "application": {"application_id": f"tg:{chat_id}"},
                    "user": {"user_id": f"tg:{chat_id}"},
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
