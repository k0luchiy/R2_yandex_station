"""Config-driven backend factory, replacing `core/providers/` (plan todo 5).

`core/providers/factory.py` was eleven lines of `if key in ("yandex",
"yandexgpt", "yandex-gpt")`: a backend set frozen into the source. Here the set
is whatever `config/backends.json` declares, and the only thing the code knows
is `kind -> constructor`. Adding a provider becomes a JSON edit.

Two responsibilities, deliberately separate:

`build_backends(specs)` is raw kind dispatch. It builds what it is handed and
refuses anything it has no constructor for -- an unknown `kind` raises
`BackendConfigError`, because a registry that silently returns `{}` for a kind it
does not recognise turns a typo in the config into "the brain has no LLM" hours
later.

The `opencode_session` kind is the one that cannot be constructed from a spec
alone: its adapter needs the `OpencodeClient`, the `OcSessionStore`, the `Config`
and (under `EVENT_MODE == "sse"`) the `EventSource`, none of which this module
has or can invent. The composition root (plan todo 18) assembles them once into an
`OpencodeWiring` and passes it as `opencode=`. A spec of that kind arriving
WITHOUT a wiring is a real misconfiguration -- a composition root that never built
the client -- and it keeps the plan's original error string, which used to mean
"unwritten" and from todo 9 means "not supplied".

`build_chain(specs, chain)` is the curated product: the backends from
`chain.order`, minus the ones that cannot possibly work. A backend is dropped
when a field it cannot run without is empty -- its credential, its base URL, or
its model -- and the drop is a WARNING naming the backend and the field. That
filter runs BEFORE dispatch, so the shipped `opencode` entry (no
`R2D2_OC_PASSWORD` on this machine) is skipped with a reason instead of taking
the whole chain down. An empty result is a `BackendConfigError`, never `[]`.

No log line or error message here embeds a field VALUE -- in this config file
every value is a credential. Names, kinds and field names only.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from core.backends.base import Backend
from core.backends.config_loader import (
    VALID_KINDS,
    BackendChain,
    BackendConfigError,
    BackendSpec,
)
from core.backends.openai_compatible import OpenAICompatibleBackend
from core.backends.opencode_session import OpencodeSessionBackend, OpencodeWiring

__all__ = ["build_backends", "build_chain"]

log = logging.getLogger(__name__)

#: Fields a backend cannot be called without. `model` is per-kind because
#: `opencode_session` answers with `fast_model` and takes its models from the
#: opencode session rather than from a request body. `base_url` empty means the
#: request URL degenerates to `/chat/completions`; `model` empty means
#: `complete()` raises before a request exists. Both are certain failures, so
#: both are dropped rather than offered as a fallback.
REQUIRED_FIELDS: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {
        "opencode_session": ("base_url", "fast_model"),
        "openai_compatible": ("base_url", "model"),
    }
)
#: Credentials per kind. `opencode_session` authenticates with basic auth, whose
#: password is the mandatory half; the username has a server-side default.
#: `openai_compatible` sends a bearer or Api-Key header built from `api_key`.
REQUIRED_CREDENTIALS: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {
        "opencode_session": ("password",),
        "openai_compatible": ("api_key",),
    }
)


def _blocked_field(spec: BackendSpec) -> str | None:
    """The first field that makes this backend unusable, or None if it is usable.

    A `kind` absent from both tables is not blocked: an unwired kind is
    `build_backends`' error to raise, not this function's to hide.
    """
    for name in REQUIRED_FIELDS.get(spec.kind, ()) + REQUIRED_CREDENTIALS.get(spec.kind, ()):
        if not getattr(spec, name):
            return name
    return None


def build_backends(
    specs: Mapping[str, BackendSpec], *, opencode: OpencodeWiring | None = None
) -> dict[str, Backend]:
    """Every named spec, built through its `kind`'s constructor.

    `opencode` is the wiring an `opencode_session` spec is built from; without it
    that kind cannot be constructed at all, which is a `BackendConfigError` rather
    than a silently missing backend.
    """
    built: dict[str, Backend] = {}
    for spec in specs.values():
        match spec.kind:
            case "openai_compatible":
                built[spec.name] = OpenAICompatibleBackend(spec)
            case "opencode_session":
                if opencode is None:
                    raise BackendConfigError(
                        f"backend {spec.name!r}: opencode_session backend not yet available to this "
                        "call because no opencode client, session store or event source was supplied; "
                        "pass opencode=OpencodeWiring(...) to build_backends/build_chain (plan todo 18)"
                    )
                built[spec.name] = OpencodeSessionBackend(spec, opencode)
            case _:
                raise BackendConfigError(
                    f"backend {spec.name!r}: kind {spec.kind!r} has no registered constructor; "
                    f"valid kinds are {', '.join(VALID_KINDS)}"
                )
    return built


def build_chain(
    specs: Mapping[str, BackendSpec],
    chain: BackendChain,
    *,
    opencode: OpencodeWiring | None = None,
) -> list[Backend]:
    """The usable backends, in `chain.order`.

    `BackendChain` carries the order and the declared-but-unused names together,
    so the second argument is the plan's `order` plus the bookkeeping the caller
    needs in order to explain what did not make the cut.
    """
    usable: dict[str, BackendSpec] = {}
    for name in chain.order:
        spec = specs.get(name)
        if spec is None:
            raise BackendConfigError(f"chain references unknown backend {name!r}")
        blocked = _blocked_field(spec)
        if blocked is not None:
            log.warning(
                "backend %r (kind %s): dropped from the chain, %r is empty",
                spec.name,
                spec.kind,
                blocked,
            )
            continue
        usable[name] = spec
    built = build_backends(usable, opencode=opencode)
    ordered = [built[name] for name in chain.order if name in built]
    if not ordered:
        raise BackendConfigError(
            "backend chain is empty: every backend in "
            f"{', '.join(chain.order) or '<none declared>'} is unusable or uncredentialed"
        )
    return ordered
