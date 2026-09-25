"""Backend protocol plus the LLM response value types shared by every backend.

`ToolCall` and `Choice` are relocated VERBATIM from `core/providers/base.py` — same
field names, same order, same defaults, no behaviour change. `tests/test_backend_base.py`
pins that parity against an embedded copy of the original source.

`Backend` replaces the old `Provider` ABC. Backends are now constructed from config
specs instead of being subclassed from a fixed set, so what the registry needs is a
structural contract: a `@runtime_checkable` Protocol that any conforming class
satisfies without inheriting. Runtime checks verify attribute PRESENCE only — never
signatures — so `isinstance(obj, Backend)` is a wiring check, not a type check.

The OpenAI-shaped response parser (`parse_choice`) deliberately did NOT move here: it
belongs to the `openai_compatible` backend, which reuses it verbatim.

The four error classes DO live here, moved verbatim out of
`core/backends/openai_compatible.py` (plan todo 6) with their messages and attributes
unchanged; that module re-exports them, so every existing import keeps working. The
reason for the move is ownership, not cosmetics: `opencode`'s client subclasses
`BackendError` too, and a brain that branches on one error root must not have to reach
into a sibling backend's module to find it. The class HIERARCHY is the contract --
`BackendError` is "this backend could not produce a reply, fall back",
`BackendStatusError` is a non-2xx that survived any retry, `BackendProtocolError` is a
2xx body that is not usable, and `BackendErrorEnvelope` is the 2xx-whose-payload-is-the-
failure case (spike correction C1).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol, TypeAlias, runtime_checkable

__all__ = [
    "Backend",
    "BackendError",
    "BackendErrorEnvelope",
    "BackendProtocolError",
    "BackendStatusError",
    "ChatMessage",
    "Choice",
    "ToolCall",
    "ToolSchema",
]

ChatMessage: TypeAlias = Mapping[str, object]
ToolSchema: TypeAlias = Mapping[str, object]


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict = field(default_factory=dict)


@dataclass
class Choice:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    provider: str = ""
    model: str = ""
    raw: dict | None = None


class BackendError(Exception):
    """This backend could not produce a reply, so the caller should fall back.

    `httpx.TimeoutException` is deliberately NOT wrapped: the brain
    distinguishes it to stop retrying.
    """


class BackendStatusError(BackendError):
    """The provider answered with a non-2xx status that survived any retry."""

    def __init__(self, backend: str, status_code: int, detail: str = "") -> None:
        super().__init__(f"backend {backend!r}: HTTP {status_code} ({detail})" if detail else f"backend {backend!r}: HTTP {status_code}")
        self.backend = backend
        self.status_code = status_code
        self.detail = detail


class BackendProtocolError(BackendError):
    """A 2xx body that is not a usable chat-completion response."""


class BackendErrorEnvelope(BackendProtocolError):
    """A 2xx body whose payload is an error envelope rather than a reply (C1)."""


@runtime_checkable
class Backend(Protocol):
    name: str

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSchema] | None = None,
        *,
        max_tokens: int = 400,
        temperature: float = 0.7,
        timeout: float = 3.0,
        model: str | None = None,
    ) -> Choice: ...

    async def aclose(self) -> None: ...
