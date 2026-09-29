"""Every route this process answers, as one plain async function per endpoint, and
the three names it deliberately does not own.

`app/main.py` is the composition root: it builds the ASGI object, starts the
collaborators and stops them. What a request MEANS is a different question, and
this module is the whole of the answer -- five functions, each of which reads
`app.state` and returns a body. A route split across two files is a route whose
half-answer nobody can find, so nothing here starts a task, opens a connection or
caches a value: every one of these is safe to call twice and wrong to call twice
at once, which is exactly the contract a webhook needs.

**The three seams are parameters, and that is the same rule
`app/diagnostics.py` follows.** `load_backend_specs`, `OpencodeClient` and
`send_message` are resolved by the composition root and handed in, so its module
namespace stays the single place any of the three names lives. Two reasons, one
architectural and one behavioural:

* A helper that imported them here would be a second, invisible copy of the
  registry loader, the client factory and the Telegram sender: a test that
  replaced the composition root's copy would watch the real server being called
  instead, which is the worst kind of test failure -- a green one that measured
  nothing.
* They are read **per request, not once at import.** A deployment swaps them
  after startup -- a hung opencode server, a test double, a Telegram channel that
  must not be the owner's -- and a route that had captured the names when the
  process was imported would keep talking to whatever the import said. That is
  why `app/main.py` passes them from inside the handler rather than closing over
  them in a table of partials.

**Order is the composition root's, not this module's.** FastAPI matches in
registration order, so `/health` before `/` before `/webhook` is a fact about
`app/main.py` and belongs there, next to the lifespan that makes `app.state` mean
anything at all.

**What these bodies may contain is the load-bearing part** for the two
unauthenticated ones. A count, a model id, a chain, a boolean: never a
credential, and never the workspace path, which names the operator's home.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any, TypeAlias

from fastapi import Request
from fastapi.responses import JSONResponse

from app import diagnostics
from app.config import Config
from app.diagnostics import TG_APPLICATION_ID_VAR, ClientFactory, RegistryLoader
from app.identity import tg_application_id
from core.brain import Brain
from core.memory import Memory

__all__ = [
    "MessageSender", "diagnostics_providers", "health", "root", "tg_webhook", "webhook",
]

logger = logging.getLogger("r2d2")

#: `(cfg, text, chat_id=...) -> awaitable`, for the same reason as the two above:
#: the composition root owns the name, so a test can point the route at a channel
#: that is not the owner's chat.
MessageSender: TypeAlias = Callable[..., Awaitable[bool]]


async def health(request: Request, *, load: RegistryLoader, factory: ClientFactory) -> Any:
    """Liveness of THIS process, plus what it can still answer with.

    The status code is 200 whenever the process is serving, degraded or not.
    An unreachable opencode server is a degraded *dependency*, not a fault
    here: R2D2 keeps answering from the fallback chain, and a monitor that
    flaps on a dependency -- or that restarts R2D2, which cannot fix one --
    is worse than a probe that reports the degradation in a field.
    """
    cfg: Config = request.app.state.cfg
    memory: Memory = request.app.state.memory
    liveness, chain = await diagnostics.health_view(cfg, load, factory)
    return {
        "status": "ok",
        "opencode": liveness,
        "sessions": len(await memory.all_oc_sessions()),
        "chain": chain,
    }


async def diagnostics_providers(
    request: Request, *, load: RegistryLoader, factory: ClientFactory
) -> Any:
    """The live provider and agent catalogs; `app/diagnostics.py` has the why."""
    cfg: Config = request.app.state.cfg
    return await diagnostics.providers_view(cfg, load, factory)


async def root() -> dict[str, str]:
    return {
        "service": "R2D2",
        "health": "/health",
        "webhook": "/webhook",
        "tg": "/tg/webhook",
        "diagnostics": "/diagnostics/providers",
    }


async def webhook(request: Request) -> Any:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    brain: Brain = request.app.state.brain
    if not brain.authorized(body):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return await brain.process_alice(body)


async def tg_webhook(request: Request, *, send: MessageSender) -> Any:
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
            await send(cfg, reply, chat_id=chat_id)
    return {"ok": True}
