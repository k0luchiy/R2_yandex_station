from core.tools.base import ToolContext, ToolResult
from core.tools import arxiv_tool, laptop_tool, shell_tool, tg_tool


class ToolRegistry:
    def __init__(self):
        self._schemas: list[dict] = []
        self._handlers: dict[str, callable] = {}

    def register(self, schema: dict, handler: callable) -> None:
        name = schema["function"]["name"]
        self._schemas.append(schema)
        self._handlers[name] = handler

    def schemas(self) -> list[dict]:
        return list(self._schemas)

    def names(self) -> list[str]:
        return list(self._handlers.keys())

    async def execute(self, ctx: ToolContext, name: str, arguments: dict | None) -> ToolResult:
        handler = self._handlers.get(name)
        if handler is None:
            return ToolResult("Неизвестный инструмент.", ok=False)
        try:
            return await handler(ctx, arguments or {})
        except Exception as exc:
            ctx.logger.exception("tool %s failed", name)
            return ToolResult("Не смог выполнить действие.", ok=False, error=str(exc))


def build_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(tg_tool.SCHEMA, tg_tool.handler)
    reg.register(laptop_tool.SCHEMA, laptop_tool.handler)
    reg.register(laptop_tool.APP_SCHEMA, laptop_tool.open_app)
    reg.register(shell_tool.SCHEMA, shell_tool.handler)
    reg.register(shell_tool.CONFIRM_SCHEMA, shell_tool.confirm_risky_action_handler)
    reg.register(arxiv_tool.SCHEMA, arxiv_tool.handler)
    return reg
