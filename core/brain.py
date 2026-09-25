import asyncio
import logging

import httpx

from app.config import Config
from core import policies, render
from core.backends.base import Choice
from core.backends.config_loader import load_backend_specs
from core.backends.openai_compatible import BackendError
from core.backends.registry import build_chain
from core.memory import Memory
from core.tools.base import ToolContext
from core.tools.registry import build_registry
from core.tools.telegram_tool import send_message

SYSTEM_PROMPT = (
    "Ты — R2D2, голосовой ассистент в Яндекс Станции. Твои ответы Алиса зачитывает ВСЛУХ.\n"
    "Правила:\n"
    "1. Отвечай ОЧЕНЬ КРАТКО: одна-три коротких предложения, как живой человек.\n"
    "2. Никогда не используй markdown, эмодзи, звёздочки, решётки, ссылки и спецсимволы.\n"
    "3. Не выводи вслух длинные списки, большие объёмы чисел и ссылки.\n"
    "4. Если контент длинный (сводка, отчёт, статья) — НЕ зачитывай его. Используй инструмент "
    "tg_send для отправки в телеграм и подтверди одной фразой, например «Отправил в телеграм».\n"
    "5. На обычные вопросы отвечай САМ, без инструментов. Инструменты вызывай ТОЛЬКО когда "
    "пользователь явно просит действие: открыть приложение, проверить статус/заряд, отправить "
    "в телеграм, сводка статей с arxiv. Не угадывай запрос для поиска — используй слова "
    "пользователя дословно.\n"
    "6. Если нужно действие — вызови инструмент. В аргументах ОБЯЗАТЕЛЬНО укажи поле "
    "spoken_reply: короткую фразу, которую пользователь услышит сразу.\n"
    "7. Опасные команды (удаление, перезагрузка, sudo, изменение системы) — сначала вызови "
    "confirm_risky_action и спроси: «Подтверди: действие?». Выполняй только после явного «да».\n"
    "8. Не выдумывай факты. Не знаешь — так и скажи.\n"
    "9. Отвечай по-русски."
)

HELP_TEXT = (
    "Я Р2Д2. Умею отвечать на вопросы, управлять ноутбуком и отправлять сообщения в телеграм. "
    "Например: какой заряд у ноутбука, открой браузер, отправь в телеграм текст, сводка статей "
    "с arxiv по теме. Скажи помощь, чтобы повторить."
)

GREETING = "Привет, я Р2Д2. Спрашивай что угодно или скажи что сделать: открыть приложение, проверить статус, отправить в телеграм."

ERROR_TEXT = "Что-то пошло не так. Попробуй ещё раз."

EXIT_WORDS = {"стоп", "выход", "хватит", "закончить", "до свидания", "пока", "завершить"}

HELP_WORDS = {"помощь", "help", "что ты умеешь", "что умеешь", "команды", "справка", "что ты можешь"}

GREET_WORDS = {"привет", "здравствуй", "здравствуйте", "салют", "добрый день", "добрый вечер", "доброе утро", "hello", "hi", "хелоу"}


class Brain:
    def __init__(
        self,
        cfg: Config,
        memory: Memory,
        worker,
        logger: logging.Logger | None = None,
    ):
        self.cfg = cfg
        self.memory = memory
        self.worker = worker
        self.logger = logger or logging.getLogger("r2d2.brain")
        self.registry = build_registry()

    def authorized(self, body: dict) -> bool:
        session = body.get("session", {})
        skill_id = session.get("skill_id")
        user_id = (session.get("user") or {}).get("user_id")
        if self.cfg.alice_skill_id and skill_id != self.cfg.alice_skill_id:
            return False
        if self.cfg.alice_user_id and user_id != self.cfg.alice_user_id:
            return False
        return True

    async def process_alice(self, body: dict) -> dict:
        if not self.authorized(body):
            return render.alice_response("Доступ запрещён.", end_session=True)
        return await self.process(body)

    async def process_tg(self, body: dict) -> dict:
        return await self.process(body)

    async def process(self, body: dict) -> dict:
        try:
            text, end = await self._handle(body)
        except Exception:
            self.logger.exception("handle failed")
            text, end = ERROR_TEXT, False
        return render.alice_response(text, end_session=end)

    async def _handle(self, body: dict) -> tuple[str, bool]:
        session = body.get("session", {})
        request = body.get("request", {})
        command = (request.get("command") or "").strip()
        original = request.get("original_utterance") or ""
        new = bool(session.get("new"))
        app_id = (
            (session.get("application") or {}).get("application_id")
            or (session.get("user") or {}).get("user_id")
            or "unknown"
        )

        low = command.lower()
        if low == "ping" or "ping" in original.lower():
            return "Понг.", False
        if low in EXIT_WORDS:
            return "До встречи.", True

        pending = await self.memory.get_pending(app_id)
        if pending:
            verdict = policies.confirmation_verdict(command)
            if verdict == "yes":
                await self.memory.clear_pending(app_id)
                if pending.get("tool") == "run_shell":
                    ctx = ToolContext(self.cfg, self.memory, app_id, self.logger, approved=True)
                    result = await self.registry.execute(ctx, "run_shell", pending.get("arguments", {}))
                    return result.text, False
                return "Готово.", False
            if verdict == "no":
                await self.memory.clear_pending(app_id)
                return "Отменяю.", False

        if low in HELP_WORDS:
            return HELP_TEXT, False
        if new and (not low or low in GREET_WORDS):
            return GREETING, False

        await self.memory.append_message(app_id, "user", command, self.cfg.max_history)
        history = await self.memory.load_history(app_id)
        messages = [{"role": "system", "content": SYSTEM_PROMPT}, *history]
        markup = request.get("markup") or {}
        if markup.get("dangerous_context"):
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "Запрос помечен как потенциально опасный. Не выполняй никаких действий "
                        "без явного подтверждения пользователем."
                    ),
                }
            )

        choice = await self._call_llm(messages)
        self.logger.info(
            "llm %s: msgs=%d tools=%s content_len=%d",
            choice.provider,
            len(messages),
            [t.name for t in choice.tool_calls],
            len(choice.content or ""),
        )
        if choice.tool_calls:
            tool_call = choice.tool_calls[0]
            ctx = ToolContext(self.cfg, self.memory, app_id, self.logger)
            result = await self.registry.execute(ctx, tool_call.name, tool_call.arguments)
            await self.memory.append_message(app_id, "assistant", f"[{tool_call.name}]", self.cfg.max_history)
            if result.tg_send:
                asyncio.create_task(send_message(self.cfg, result.tg_send))
            if result.is_async and result.job:
                await self.worker.enqueue(result.job)
            if result.needs_confirm and result.pending:
                await self.memory.set_pending(app_id, result.pending)
            return result.text or "Готово.", False

        text = choice.content or ""
        await self.memory.append_message(app_id, "assistant", text, self.cfg.max_history)
        return text, False

    async def _call_llm(self, messages: list[dict]) -> Choice:
        """One LLM call with function calling, walking the configured chain.

        The chain in ``config/backends.json`` replaces the old
        ``llm_provider``/``fallback_provider`` pair: one backend per entry, in
        order, and a failure moves to the NEXT backend rather than retrying the
        same one -- the deadline is per call, so a second attempt on the same
        host would spend budget this turn no longer has. ``BackendError``
        (refused, overloaded, malformed body) and ``httpx.HTTPError``
        (``httpx.TimeoutException`` included) are the two families that mean
        "try the next one"; anything else is a bug and must surface.
        """
        chain, specs = load_backend_specs(self.cfg)
        backends = build_chain(specs, chain)
        tools = self.registry.schemas()
        last_error: Exception | None = None
        try:
            for backend in backends:
                try:
                    return await backend.complete(
                        messages,
                        tools,
                        max_tokens=self.cfg.llm_max_tokens,
                        timeout=self.cfg.llm_timeout,
                    )
                except (BackendError, httpx.HTTPError) as exc:
                    last_error = exc
                    self.logger.warning("LLM backend %s failed: %r", backend.name, exc)
        finally:
            for backend in backends:
                await backend.aclose()
        raise RuntimeError(f"all LLM providers failed: {last_error!r}")
