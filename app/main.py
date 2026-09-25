import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.config import Config
from core.async_worker import Worker
from core.brain import Brain
from core.memory import Memory
from core.tools.telegram_tool import send_message

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("r2d2")


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
        logger.info("R2D2 started, db=%s, provider=%s", cfg.resolved_db_path(), cfg.llm_provider)
        yield
        await worker.stop()
        await memory.close()

    app = FastAPI(title="R2D2", lifespan=lifespan)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/")
    async def root():
        return {"service": "R2D2", "health": "/health", "webhook": "/webhook", "tg": "/tg/webhook"}

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
