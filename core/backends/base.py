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
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol, TypeAlias, runtime_checkable

__all__ = ["Backend", "ChatMessage", "Choice", "ToolCall", "ToolSchema"]

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
