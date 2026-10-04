from collections.abc import Awaitable, Callable

from core.tools.base import ToolContext, ToolResult
from core.tools import arxiv_tool, laptop_tool, shell_tool, tg_tool

#: What every registered handler is: given the turn's context and the model's
#: arguments, produce a `ToolResult`. Spelled with `typing.Callable` because
#: `callable` is the builtin FUNCTION, and `dict[str, callable]` is not a type at
#: all -- it type-checks as anything and tells a reader nothing about the
#: signature it is supposed to be enforcing.
Handler = Callable[[ToolContext, dict], Awaitable[ToolResult]]


class ToolRegistry:
    def __init__(self, offered: frozenset[str]):
        self._schemas: list[dict] = []
        self._handlers: dict[str, Handler] = {}
        self._offered: frozenset[str] = offered

    def register(self, schema: dict, handler: Handler) -> None:
        name = schema["function"]["name"]
        self._schemas.append(schema)
        self._handlers[name] = handler

    def schemas(self) -> list[dict]:
        return list(self._schemas)

    def names(self) -> list[str]:
        return list(self._handlers.keys())

    def offers(self, name: str) -> bool:
        return name in self._offered

    def offered_schemas(self) -> list[dict]:
        """Only the schemas whose names are in `_offered`, in registration order.

        The set is decided once, at construction, by `build_registry`. It is a
        property of the registry rather than of any one caller because the question
        it answers -- "may a bare LLM reach this tool with no agent and no
        permission question in front of it?" -- is a property of the TOOL, not of
        whoever happens to be holding the registry this turn.
        """
        return [s for s in self._schemas if s["function"]["name"] in self._offered]

    async def execute(self, ctx: ToolContext, name: str, arguments: dict | None) -> ToolResult:
        handler = self._handlers.get(name)
        if handler is None:
            return ToolResult("Неизвестный инструмент.", ok=False)
        try:
            return await handler(ctx, arguments or {})
        except Exception as exc:
            ctx.logger.exception("tool %s failed", name)
            return ToolResult("Не смог выполнить действие.", ok=False, error=str(exc))


#: What a bare LLM on the fallback chain may reach with no agent and no permission
#: question in front of it. One tool qualifies: it reads a fact, changes nothing.
#:
#: The registry has exactly ONE consumer -- `Brain._call_llm`. The CLI shim does not
#: come through here (it calls handlers directly, behind the opencode permission
#: matrix), so nothing omitted below is a capability the agent loses. Without this
#: partition the fallback chain ran `open_app`'s `xdg-open`, scraped arxiv and ran
#: shell commands in process, chosen by a model with no agent behind it and no risk
#: gate on `open_app`. Being a fallback is not a reason to be trusted with more.
FALLBACK_SAFE_TOOLS: frozenset[str] = frozenset({"system_status"})


def build_registry() -> ToolRegistry:
    reg = ToolRegistry(FALLBACK_SAFE_TOOLS)
    reg.register(tg_tool.SCHEMA, tg_tool.handler)
    reg.register(laptop_tool.SCHEMA, laptop_tool.handler)
    reg.register(laptop_tool.APP_SCHEMA, laptop_tool.open_app)
    reg.register(shell_tool.SCHEMA, shell_tool.handler)
    reg.register(shell_tool.CONFIRM_SCHEMA, shell_tool.confirm_risky_action_handler)
    reg.register(arxiv_tool.SCHEMA, arxiv_tool.handler)
    return reg
