from core.tools.base import ToolContext, ToolResult
from core.tools.telegram_tool import send_message

SCHEMA = {
    "type": "function",
    "function": {
        "name": "tg_send",
        "description": (
            "Отправить сообщение в личный телеграм пользователя. Используй для длинного "
            "контента, сводок, отчётов, напоминаний и всего, что неудобно зачитывать вслух."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Текст сообщения"},
                "spoken_reply": {
                    "type": "string",
                    "description": "Короткая фраза для озвучки, например «Отправил в телеграм»",
                },
            },
            "required": ["text", "spoken_reply"],
        },
    },
}


async def handler(ctx: ToolContext, args: dict) -> ToolResult:
    text = (args.get("text") or "").strip()
    if not text:
        return ToolResult("Нечего отправлять.", ok=False)
    ok = await send_message(ctx.cfg, text)
    if not ok:
        return ToolResult("Не смог отправить в телеграм.", ok=False)
    return ToolResult(text=args.get("spoken_reply") or "Отправил в телеграм.")
