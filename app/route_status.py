"""What this process decided about the opencode route, as a value both of its
readers can name -- the C1 gate's outcomes, and nothing else.

The decision is made once, at startup, in `app/opencode_route.py`; this module is
where it is written down, and it is a module of its own because there are three
readers and two of them already depend on each other: `app/opencode_route.py`
supplies `ClientFactory` to `app/diagnostics.py`, so a `route_status` living in
either and imported by the other would be a cycle. The decision is not "the route"
and it is not "the diagnostics" -- it is the fact both of them report, and a fact
with two readers and no owner is how two answers start disagreeing.

**Why a value and not a log line.** The startup line, `GET /diagnostics/providers`
and any monitor have to say the SAME thing. Recomputing the answer per request
would let the diagnostics report a route as wired while the running process
actually refused it, which is the misleading success `app/opencode_route.py`'s C1
refusal exists to remove. So `route_status()` is a reading of a recorded decision,
never a second probe, and it can contradict nothing.

**What it deliberately does not do.** It never names a model as a substitute, never
downgrades a refusal, and never carries a credential: `detail` holds model ids and
file paths, which are configuration. The closed vocabulary below is the contract a
consumer switches on, so a new outcome is a new constant rather than a new string
typed at the call site.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

__all__ = [
    "CATALOGUE_UNREADABLE", "MODEL_UNCONFIGURED", "MODEL_UNKNOWN", "NO_BACKEND",
    "NO_WORKSPACE", "NOT_CHECKED", "REGISTRY_UNREADABLE", "RouteStatus", "UNVERIFIED",
    "WIRED", "decide", "route_status",
]

#: `wire_opencode` ran and every configured model is verified against the server.
#: The only state with an empty `reason`, and the only one that means "working".
WIRED: Final = "wired"
#: `wire_opencode` has not run yet, so nobody knows anything. Distinct from every
#: refusal on purpose: a reader must never report "not started" as "refused".
NOT_CHECKED: Final = "not-checked"
#: Wired, but nothing verified the models -- the server did not answer at startup.
#: NOT a refusal: the route is in use, and the per-turn `client.health()` in
#: `core/brain.py` gates the actual routing. "Wired" and "working" are not one word.
UNVERIFIED: Final = "models-unverified"
#: `config/backends.json` could not be read or parsed, so there is no backend list.
REGISTRY_UNREADABLE: Final = "registry-unreadable"
#: The registry declares no `opencode` backend. A supported chain-only deployment.
NO_BACKEND: Final = "no-opencode-backend"
#: `r2d2_workspace` is not a directory, so `?directory=` would scope the agent's file
#: tools to a root that does not exist and every session request would fail (C6).
NO_WORKSPACE: Final = "workspace-missing"
#: A model slot holds no id at all, so opencode would answer from ITS default model
#: -- the one substitution C1 is about, arrived at from the other direction.
MODEL_UNCONFIGURED: Final = "model-unconfigured"
#: The server answered its catalogue and does not offer the configured id. The
#: snapshot case: a model this build, or this account, does not carry.
MODEL_UNKNOWN: Final = "model-unknown"
#: `GET /config/providers` itself could not be read or parsed, so it is UNKNOWN
#: whether the models exist. Never collapsed into `MODEL_UNKNOWN`: that would send
#: the operator after a config typo that is not the cause.
CATALOGUE_UNREADABLE: Final = "catalogue-unreadable"


@dataclass(frozen=True, slots=True)
class RouteStatus:
    """The decision, its model ids, and the reason for it. Immutable, and safe
    to put in an unauthenticated body: model ids, names and reasons only.

    `reason` is `WIRED` only for a fully verified route. Every other outcome names
    itself with one of the constants above, and `detail` carries the offending ids
    or the field involved -- never a credential, which is why this value is built
    from exception messages about model ids and not from a spec dump.
    """

    wired: bool
    models: tuple[str, ...] = ()
    reason: str = WIRED
    detail: str = ""


_STATUS: RouteStatus | None = None


def route_status() -> RouteStatus:
    """The decision this process made, or `NOT_CHECKED` before `wire_opencode` ran."""
    return _STATUS if _STATUS is not None else RouteStatus(wired=False, reason=NOT_CHECKED)


def decide(status: RouteStatus) -> None:
    """Record the decision. `app/opencode_route.py` is the only caller."""
    global _STATUS
    _STATUS = status
