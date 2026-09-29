"""What this process can still answer with: the liveness probes and the two
unauthenticated bodies (plan todo 18).

`app/main.py` is the composition root -- it builds the collaborators, starts them
and stops them -- so the two questions it is asked on every request ("is the
opencode server up?", "what answers when it is not?") live here, and the routes
stay thin enough to read as a table of endpoints.

**The two seams are injected, and that is deliberate.** `load_backend_specs` and
`OpencodeClient` are resolved by the CALLER and handed in, so the composition
root's own module namespace stays the single place either name lives. A helper
that imported them here would be a second, invisible copy of the registry loader
and of the client factory: a test that replaced the composition root's copy would
watch the real server being called instead, which is the worst kind of test
failure -- a green one that measured nothing.

**What these bodies may contain is the load-bearing part.** They are
unauthenticated, so they may carry a model id, an agent name, an address, a
count and a fallback chain. Never a credential, and never the workspace path,
which names the operator's home and the layout of their disk.

**Neither probe can hang.** `HEALTH_PROBE_TIMEOUT_S` is the ROUTE's bound rather
than the client's: a server that stops reading the socket does not end the
client's own timeout any sooner, and a probe that hangs is the one thing an
operator cannot diagnose with. A provider catalog that does not parse is a clean
502 rather than the upstream body, because an unparsed body from a server R2D2
does not control has no business being echoed out of a public route.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Final, TypeAlias

import httpx
from fastapi.responses import JSONResponse

from app.config import Config
from app.route_status import route_status
from core.backends.config_loader import BackendChain, BackendConfigError, BackendSpec
from core.backends.registry import build_chain
from core.brain import SESSION_KIND
from core.opencode.client import (
    OpencodeClient,
    OpencodeError,
    OpencodeHealth,
    OpencodeProtocolError,
)

__all__ = [
    "ClientFactory", "HEALTH_PROBE_TIMEOUT_S", "RegistryLoader", "TG_APPLICATION_ID_VAR",
    "TG_BINDING_FORMAT", "UNREACHABLE", "fallback_backends", "health_view", "probe_opencode",
    "providers_view", "route_view", "telegram_binding", "unreachable",
]

log = logging.getLogger("r2d2")

#: The bound on the `/health` liveness probe, and it is the ROUTE's bound rather
#: than the client's: see the module docstring.
HEALTH_PROBE_TIMEOUT_S: Final = 2.0
#: The only body `/diagnostics/providers` answers 503 with, and the only error a
#: caller has to parse.
UNREACHABLE: Final = "opencode unreachable"
#: The variable that declares which human a Telegram chat belongs to. Carried in
#: the diagnostics body so an operator learns the NAME of the missing knob from
#: the route they were already reading, instead of from a stranger's message that
#: was silently dropped.
TG_APPLICATION_ID_VAR: Final = "R2D2_TG_APPLICATION_ID"
#: The exact shape of one declared pair, quoted in the same body. An id invented
#: from a chat id is what this project refused to do, so the format is part of the
#: answer: `chat_id=application_id`, pairs separated by commas or whitespace.
TG_BINDING_FORMAT: Final = "chat_id=application_id"

#: `(spec, directory) -> client`: what the composition root builds, handed in so
#: the name `OpencodeClient` is resolved where it is imported.
ClientFactory: TypeAlias = Callable[[BackendSpec, str], OpencodeClient]
#: `(cfg) -> (chain, specs)`, for the same reason.
RegistryLoader: TypeAlias = Callable[[Config], tuple[BackendChain, dict[str, BackendSpec]]]


async def probe_opencode(
    factory: ClientFactory, spec: BackendSpec | None, cfg: Config
) -> OpencodeHealth:
    """One bounded liveness probe of a configured opencode server. Never raises.

    A registry with no `opencode` backend reads exactly like a server that is
    down -- R2D2 has no opencode route and the chain carries every turn -- so it
    answers with the same `reachable=False` and no address.

    It does not log: the startup gate in `app/main.py` already reported the cause
    with the command that fixes it, and a route a monitor polls every few seconds
    would repeat that line forever. The answer belongs in the body.
    """
    if spec is None:
        return OpencodeHealth(reachable=False)
    client = factory(spec, cfg.resolved_workspace())
    try:
        return await asyncio.wait_for(client.health(), HEALTH_PROBE_TIMEOUT_S)
    except TimeoutError:
        return OpencodeHealth(reachable=False)
    finally:
        # The client opens its own connection pool on first use, so a client left
        # unclosed here leaks one pool per probe -- i.e. per health check.
        await client.aclose()


async def health_view(
    cfg: Config, load: RegistryLoader, factory: ClientFactory
) -> tuple[dict[str, object], list[str]]:
    """`(the opencode key of the health body, the fallback chain)`. Never raises.

    The chain is the *effective* one: `config/backends.json` lists the session
    backend first because it is the primary route, and `core/brain.py` drops it
    from the walk (a second attempt on the host that just failed spends budget the
    turn no longer has), so reporting it here would advertise a backend that can
    never answer a turn.

    A registry that cannot be read is the loud case, not a quiet one: the body then
    carries `chain: []`, which says out loud that R2D2 has no brain at all rather
    than a brain with a missing part.
    """
    try:
        chain, specs = load(cfg)
    except BackendConfigError:
        return {"reachable": False, "version": None, "base_url": ""}, []
    spec = specs.get("opencode")
    liveness = await probe_opencode(factory, spec, cfg)
    return (
        {
            "reachable": liveness.reachable,
            "version": liveness.version,
            "base_url": spec.base_url if spec is not None else "",
        },
        [name for name in chain.order if specs[name].kind != SESSION_KIND],
    )


async def providers_view(
    cfg: Config, load: RegistryLoader, factory: ClientFactory
) -> dict[str, object] | JSONResponse:
    """The live `GET /config/providers` and `GET /agent` payloads, unedited,
    plus what this process made of them.

    Which models the server can reach and which agents it exposes is the pair of
    facts that decides whether a turn is answered or refused -- C1 measured a 403
    `FreeTierError` inside an HTTP 200 -- and neither is visible in a log line.
    An unparsed upstream body is never echoed back, so a 2xx the client cannot
    read is a 502 that says only that.

    `route` and `telegram` are the two facts about THIS process rather than the
    server, and they are the reason the route is reachable at all when it has
    refused to wire: a server that answers, and an assistant that is nevertheless
    not using it, looks exactly like a working deployment from the outside. They
    are read from the recorded startup decision, not recomputed, so the body
    cannot disagree with what the process is doing. The 503 and 502 bodies stay
    one-field each: there is nothing to describe when the server is silent, and
    that silence is the answer.
    """
    try:
        _chain, specs = load(cfg)
    except BackendConfigError:
        return unreachable()
    spec = specs.get("opencode")
    if spec is None:
        return unreachable()
    client = factory(spec, cfg.resolved_workspace())
    try:
        providers = await client.providers()
        agents = await client.agents()
    except OpencodeProtocolError:
        return JSONResponse({"error": "opencode answered with a malformed body"}, status_code=502)
    except (httpx.HTTPError, OpencodeError):
        return unreachable()
    finally:
        await client.aclose()
    return {
        "providers": providers,
        "agents": agents,
        "route": await route_view(cfg, load),
        "telegram": telegram_binding(cfg),
    }


async def route_view(cfg: Config, load: RegistryLoader) -> dict[str, object]:
    """The opencode wiring decision, and the backends that answer in its place.

    `reason` is `app/route_status.py`'s closed vocabulary, so `"model-unknown"`
    ("this server does not offer the model you configured"), `"models-unverified"`
    ("it did not answer at startup") and an empty list under `fallback` ("and
    nothing else can answer either") are three facts an operator can act on
    separately -- which is the whole point, because the assistant is silent in all
    three and they have nothing to do with each other.

    The decision's `detail` is deliberately NOT here. It names the offending model
    ids for a refusal, but `NO_WORKSPACE` and `REGISTRY_UNREADABLE` quote the very
    paths this module's docstring forbids in a public body, and one reason is
    enough to lose the other two. It is in the startup ERROR, which is where an
    operator reads prose.
    """
    status = route_status()
    return {
        "wired": status.wired,
        "models": list(status.models),
        "reason": status.reason,
        "fallback": await fallback_backends(cfg, load, loud=False),
    }


def telegram_binding(cfg: Config) -> dict[str, object]:
    """Whether a Telegram chat has a DECLARED identity, and the exact declaration.

    `declared` is deliberately not `bound`: resolving a chat to an `application_id`
    is `app/main.py:tg_application_id`, and this body has no chat id to resolve.
    What it reports is the deployment-level fact an operator is looking for when
    the broker asks nothing and long results never arrive -- the variable is
    empty, and while it is, `/tg/webhook` drops every message with a warning and
    no `permission.asked` can be turned into a question.

    No chat id, no application id and no token: this route is unauthenticated, and
    the module docstring's rule is a count and a name, never an identity.
    """
    return {
        "declared": bool(cfg.r2d2_tg_application_id.strip()),
        "variable": TG_APPLICATION_ID_VAR,
        "format": TG_BINDING_FORMAT,
    }


async def fallback_backends(cfg: Config, load: RegistryLoader, *, loud: bool = True) -> list[str]:
    """The names a turn can actually be answered by, or `[]` with the reason logged.

    Built through `build_chain`, so a backend with no credential or no model is
    counted out here exactly as it is on a turn -- and an empty result is an ERROR
    rather than a log line nobody reads, because "R2D2 has no brain at all" is the
    one degradation an operator must act on immediately. The session kind is
    excluded for the same reason `core/brain.py` excludes it: it is the route, not
    a fallback, and it is the one kind `build_chain` cannot build without the
    wiring this module does not have.

    `loud=False` is for the unauthenticated diagnostics route, which a monitor may
    poll every few seconds: an empty result is reported as `"fallback": []` in the
    body, and repeating an ERROR on every poll would turn a read into a way to
    fill the operator's log.
    """
    try:
        chain, specs = load(cfg)
    except BackendConfigError as exc:
        _no_brain(loud, exc)
        return []
    order = tuple(name for name in chain.order if specs[name].kind != SESSION_KIND)
    try:
        backends = build_chain(specs, BackendChain(order=order, unused=chain.unused))
    except BackendConfigError as exc:
        _no_brain(loud, exc)
        return []
    try:
        return [backend.name for backend in backends]
    finally:
        # Nothing was built before the walk and nothing was requested yet, so these
        # close no sockets; closing them anyway means a future backend that opens
        # its pool at construction cannot leak one per startup.
        for backend in backends:
            await backend.aclose()


def _no_brain(loud: bool, exc: BackendConfigError) -> None:
    """The one ERROR this module owns, at the level the caller asked for."""
    log.log(
        logging.ERROR if loud else logging.INFO,
        "backend chain: %s; every turn will answer with the graceful text", exc,
    )


def unreachable() -> JSONResponse:
    """The one 503 body of `/diagnostics/providers`."""
    return JSONResponse({"error": UNREACHABLE}, status_code=503)
