from core.policies import confirmation_verdict, risk_level
from core.tools.base import ToolContext, ToolResult, run_shell_process

SCHEMA = {
    "type": "function",
    "function": {
        "name": "run_shell",
        "description": (
            "Выполнить команду в терминале на ноутбуке. Для любых действий с системой, "
            "которые нельзя сделать другими инструментами. Опасные команды (удаление, "
            "перезагрузка, sudo) выполняются только после подтверждения пользователем."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Команда для выполнения"},
                "spoken_reply": {
                    "type": "string",
                    "description": "Короткая фраза, которую услышит пользователь сразу",
                },
            },
            "required": ["command", "spoken_reply"],
        },
    },
}


async def handler(ctx: ToolContext, args: dict) -> ToolResult:
    command = (args.get("command") or "").strip()
    if not command:
        return ToolResult("Не понял, что выполнить.", ok=False)
    if not ctx.cfg.shell_enabled:
        return ToolResult("Запуск команд сейчас отключён.", ok=False)

    level = risk_level(command)
    if level == "dangerous" and not ctx.approved and ctx.cfg.shell_require_confirm:
        question = args.get("spoken_reply") or f"Подтверди: выполнить {command}?"
        return ToolResult(
            text=question,
            needs_confirm=True,
            pending={"tool": "run_shell", "arguments": {"command": command}},
        )

    output, code = await run_shell_process(command, timeout=ctx.cfg.shell_timeout)
    if code != 0:
        return ToolResult(
            "Команда завершилась с ошибкой.",
            ok=False,
            error=output.strip()[:1000] or f"код {code}",
        )

    short = output.strip()[:300]
    if len(output.strip()) > 300:
        return ToolResult(
            text="Готово, подробности в телеграме.",
            tg_send=f"Команда: {command}\n\n{output.strip()[:3500]}",
        )
    spoken = args.get("spoken_reply")
    if spoken:
        return ToolResult(text=spoken)
    return ToolResult(text=short or "Команда выполнена.")


async def confirm_risky_action_handler(ctx: ToolContext, args: dict) -> ToolResult:
    command = (args.get("command") or args.get("action") or "").strip()
    question = args.get("spoken_reply") or f"Подтверди: {command or 'это действие'}?"
    return ToolResult(
        text=question,
        needs_confirm=True,
        pending={"tool": "run_shell", "arguments": {"command": command}} if command else {"tool": "none"},
    )


CONFIRM_SCHEMA = {
    "type": "function",
    "function": {
        "name": "confirm_risky_action",
        "description": (
            "Запросить у пользователя подтверждение перед рискованным действием. "
            "Используй для опасных команд. После подтверждения пользователем действие выполнится."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Команда или действие, которое нужно подтвердить"},
                "spoken_reply": {"type": "string", "description": "Вопрос для озвучки, например «Подтверди: удалить папку?»"},
            },
            "required": ["command", "spoken_reply"],
        },
    },
}


def confirmation_verdict_for(text: str | None) -> str | None:
    return confirmation_verdict(text)
