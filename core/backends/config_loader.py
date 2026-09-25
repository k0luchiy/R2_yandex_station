"""Typed loader for ``config/backends.json`` (plan todo 2).

Placeholder policy
------------------
``${VAR}`` expands from ``os.environ`` in a single pass -- a value produced by
one expansion is never rescanned.  Three outcomes:

* the variable is set -> its value is substituted, empty values included;
* the variable is missing and the field is a credential
  (``api_key``/``username``/``password``) -> the field becomes ``""`` and a
  WARNING names the variable.  An uncredentialed fallback must not invalidate
  the whole registry; dropping it with a reason is ``build_chain``'s job
  (todo 5).  Pass ``strict_credentials=True`` to fail fast instead;
* the variable is missing in any other field -> ``BackendConfigError`` naming
  both the variable and the backend.

Whatever the branch, a ``${`` still present after expansion is an error: a
spec that parsed "successfully" while holding an unexpanded reference is a
misleading success, not a parse.

No error message ever embeds a field's value, only its name, because field
values in this file are credentials.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, fields as dataclass_fields
from pathlib import Path
from types import MappingProxyType
from typing import Final

from app.config import Config

log = logging.getLogger(__name__)

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
VALID_KINDS: Final[tuple[str, ...]] = ("opencode_session", "openai_compatible")
CREDENTIAL_FIELDS: Final[frozenset[str]] = frozenset({"api_key", "username", "password"})
DEFAULT_TIMEOUT: Final = 3.0

_PLACEHOLDER: Final = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class BackendConfigError(Exception):
    """``config/backends.json`` is unreadable, malformed or self-inconsistent."""


@dataclass(frozen=True, slots=True)
class BackendChain:
    """Fallback order plus the declared-but-unused backends."""

    order: tuple[str, ...]
    unused: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BackendSpec:
    name: str
    kind: str
    base_url: str
    api_key: str = ""
    username: str = ""
    password: str = ""
    model: str = ""
    fast_model: str = ""
    task_agent: str = ""
    voice_agent: str = ""
    task_model: str = ""
    summarize_model: str = ""
    timeout: float = DEFAULT_TIMEOUT
    auth_style: str = ""
    auth_mode: str = ""
    extra: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "extra", MappingProxyType(dict(self.extra)))


@dataclass(frozen=True, slots=True)
class _Field:
    backend: str
    name: str


def _type_name(value: object) -> str:
    return "null" if value is None else type(value).__name__


def _as_text(raw: object, field: _Field) -> str:
    if not isinstance(raw, str):
        raise BackendConfigError(
            f"backend {field.backend!r} field {field.name!r} must be a string, got {_type_name(raw)}"
        )
    return raw


def _expand(text: str, field: _Field, *, strict_credentials: bool) -> str:
    def substitute(match: re.Match[str]) -> str:
        var = match.group(1)
        if var in os.environ:
            return os.environ[var]
        if field.name in CREDENTIAL_FIELDS and not strict_credentials:
            log.warning(
                "backend %r: ${%s} is unset, %r stays empty", field.backend, var, field.name
            )
            return ""
        raise BackendConfigError(
            f"backend {field.backend!r} field {field.name!r} references ${{{var}}}, "
            "which is not set in the environment"
        )

    expanded = _PLACEHOLDER.sub(substitute, text)
    if "${" in expanded:
        # Report the occurrence count only: the value may be a credential.
        raise BackendConfigError(
            f"backend {field.backend!r} field {field.name!r} still holds an unexpanded reference "
            f"after expansion ({expanded.count('${')} occurrence(s))"
        )
    return expanded


def _extra_of(entry: Mapping[str, object], name: str, *, strict_credentials: bool) -> dict[str, str]:
    known = {f.name for f in dataclass_fields(BackendSpec)}
    return {
        key: _expand(_as_text(raw, _Field(name, key)), _Field(name, key), strict_credentials=strict_credentials)
        for key, raw in entry.items()
        if key not in known
    }


def _spec_from_entry(entry: object, *, strict_credentials: bool) -> BackendSpec:
    if not isinstance(entry, Mapping):
        raise BackendConfigError(f"backend entry must be a JSON object, got {_type_name(entry)}")
    if "name" not in entry:
        raise BackendConfigError("backend entry is missing required field 'name'")
    name = _as_text(entry["name"], _Field("<unnamed>", "name"))
    for required in ("kind", "base_url"):
        if required not in entry:
            raise BackendConfigError(f"backend {name!r} is missing required field {required!r}")
    kind = _as_text(entry["kind"], _Field(name, "kind"))
    if kind not in VALID_KINDS:
        raise BackendConfigError(
            f"backend {name!r} has kind {kind!r}; valid kinds are {', '.join(VALID_KINDS)}"
        )

    values: dict[str, object] = {}
    for f in dataclass_fields(BackendSpec):
        if f.name in ("name", "kind", "extra") or f.name not in entry:
            continue
        raw = entry[f.name]
        if f.name == "timeout":
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                raise BackendConfigError(
                    f"backend {name!r} field 'timeout' must be a number, got {_type_name(raw)}"
                )
            values[f.name] = float(raw)
        else:
            field = _Field(name, f.name)
            values[f.name] = _expand(
                _as_text(raw, field), field, strict_credentials=strict_credentials
            )
    return BackendSpec(
        name=name,
        kind=kind,
        extra=_extra_of(entry, name, strict_credentials=strict_credentials),
        **values,
    )


def _read(path: Path) -> Mapping[str, object]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise BackendConfigError(f"cannot read backends config at {path}: {exc.strerror}") from exc
    except json.JSONDecodeError as exc:
        raise BackendConfigError(
            f"{path} is not valid JSON: {exc.msg} (line {exc.lineno} column {exc.colno})"
        ) from exc
    if not isinstance(document, Mapping):
        raise BackendConfigError(
            f"{path}: top level must be a JSON object, got {_type_name(document)}"
        )
    return document


def _chain_of(document: Mapping[str, object], path: Path) -> tuple[str, ...]:
    if "chain" not in document:
        raise BackendConfigError(f"{path}: missing required key 'chain'")
    chain = document["chain"]
    if not isinstance(chain, list) or not all(isinstance(n, str) for n in chain):
        raise BackendConfigError(f"{path}: 'chain' must be a list of backend names")
    return tuple(chain)


def _specs_of(
    document: Mapping[str, object], path: Path, *, strict_credentials: bool
) -> dict[str, BackendSpec]:
    if "backends" not in document:
        raise BackendConfigError(f"{path}: missing required key 'backends'")
    entries = document["backends"]
    if not isinstance(entries, list):
        raise BackendConfigError(f"{path}: 'backends' must be a list of objects")
    specs: dict[str, BackendSpec] = {}
    for entry in entries:
        spec = _spec_from_entry(entry, strict_credentials=strict_credentials)
        if spec.name in specs:
            raise BackendConfigError(f"{path}: duplicate backend name {spec.name!r}")
        specs[spec.name] = spec
    return specs


def load_backend_specs(
    cfg: Config, *, strict_credentials: bool = False
) -> tuple[BackendChain, dict[str, BackendSpec]]:
    """Parse, expand and validate the backend registry described by ``cfg``.

    Raises ``BackendConfigError`` -- never a raw ``KeyError``/``TypeError`` --
    for any unreadable, malformed or self-inconsistent document.
    """
    path = Path(cfg.backends_path)
    if not path.is_absolute():
        path = REPO_ROOT / path
    document = _read(path)
    order = _chain_of(document, path)
    specs = _specs_of(document, path, strict_credentials=strict_credentials)
    for name in order:
        if name not in specs:
            raise BackendConfigError(f"{path}: chain references unknown backend {name!r}")
    unused = tuple(name for name in specs if name not in set(order))
    if unused:
        log.warning("backends declared but absent from chain: %s", ", ".join(unused))
    return BackendChain(order=order, unused=unused), specs
