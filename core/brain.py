"""R2D2's brain: one turn in, one spoken answer out -- or an acknowledgement and
a result in Telegram (plan todo 15).

Alice gives a webhook **4.5 s** and **1024 characters**. This module is the half of
the orchestrator that stands on its own: the auth gate, the fixed intents, the choice
between the two routes, and the chain a turn falls back to. The opencode session turn,
the deadline it may outrun and the Telegram collector are the other two modules.

* **C1 -- a 200 is not a reply.** `info.error` carries a 403 `FreeTierError` or a
  402 inside an HTTP 200, so the opencode turn is a failure, never an answer, and
  the turn falls back to the chain below. A refusal that reaches a speaker is
  worse than no answer, because the user cannot tell it from one.
* **The fallback chain excludes the session backend.** `config/backends.json` lists
  `opencode` first, but the opencode route has just tried it, and a second attempt on
  the same host would spend budget the turn no longer has. It is also the only kind
  `build_chain` cannot build without the wiring, so leaving it in made every
  fallback turn answer with `ERROR_TEXT`.
* **The sentinel never reaches a human.** `parse_voice_reply` is the structural
  guard, `sanitize_for_speech` the total one, and `_speakable` below the third: it
  runs on the RAW text of every turn, BEFORE `render.clean` strips the brackets
  the sentinel is written with and truncates at 1024 characters. Telegram has its
  own guard, `routing.for_human`, because a collected answer is read out of the
  session rather than out of a model reply.

`authorized()` is the only thing between a network-exposed webhook and a stranger's
laptop, and it **fails closed**: a deployment that has not declared which Alice skill
and which user it serves refuses every request, because an unconfigured value is not a
match, it is an absence of a decision. The one exception is
`Config.r2d2_allow_unauthenticated`, which exists so a developer can drive the voice
path before the skill is registered, and which is named in a startup ERROR and in the
health output rather than being something you find out from a stranger's request.

The earlier form read "if a value is set, compare it", which made an empty value skip
the check entirely -- and `.env.example` described the opposite of what the code did,
so a deployment that believed the comment would have exposed an unauthenticated webhook
controlling the machine, with the opencode agent and the `r2d2_do` shim behind it.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from app.config import Config
from core import metrics, policies, render, routing
from core.async_worker import Worker
from core.backends.base import Choice
from core.backends.config_loader import BackendChain, load_backend_specs
from core.backends.openai_compatible import BackendError
from core.backends.registry import build_chain
from core.memory import Memory
from core.permissions import PendingPermission
#: The compatibility surface of the opencode half of the orchestrator. These
#: names were DEFINED here before the split and are re-exported from the modules
#: that own them now, because `app/main.py`, `app/diagnostics.py` and the tests
#: import them from `core.brain`. `SessionRoute` is the one new name here: the
#: route itself, which the orchestrator drives.
from core.session_collector import COLLECT_TIMEOUT_S, JOB_OPENCODE_REPLY, MARKER_TIMEOUT_S, TASK_PREFIX
from core.session_route import HybridWiring, PERMISSION_APPROVED, PERMISSION_REFUSED, SESSION_KIND, SessionRoute
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
        worker: Worker,
        logger: logging.Logger | None = None,
        *,
        opencode: HybridWiring | None = None,
    ):
        self.cfg = cfg
        self.memory = memory
        self.worker = worker
        self.logger = logger or logging.getLogger("r2d2.brain")
        self.registry = build_registry()
        self.opencode = opencode
        self._notifications: set[asyncio.Task[None]] = set()
        self.session = SessionRoute(cfg, memory, worker, self.logger)

    def authorized(self, body: dict) -> bool:
        """Whether this body came from the one skill and the one user we serve.

        Fails closed on an undeclared id: a deployment with no `ALICE_SKILL_ID` or
        no `ALICE_USER_ID` is a deployment that has not decided who it is for, and it
        refuses rather than serving everyone. `r2d2_allow_unauthenticated` is the one
        way past that, and it exists for driving the voice path before the skill is
        registered -- not for production, which is why the startup check names it.
        """
        if self.cfg.r2d2_allow_unauthenticated:
            return True
        session = body.get("session", {})
        skill_id = session.get("skill_id")
        user_id = (session.get("user") or {}).get("user_id")
        if not self.cfg.alice_skill_id or skill_id != self.cfg.alice_skill_id:
            return False
        if not self.cfg.alice_user_id or user_id != self.cfg.alice_user_id:
            return False
        return True

    async def process_alice(self, body: dict) -> dict:
        if not self.authorized(body):
            return render.alice_response("Доступ запрещён.", end_session=True)
        return await self.process(body)

    async def process_tg(self, body: dict) -> dict:
        return await self.process(body)

    async def process(self, body: dict) -> dict:
        with metrics.turn(self.logger) as rec:
            try:
                text, end = await self._handle(body)
            except Exception:
                self.logger.exception("handle failed")
                rec.path = metrics.PATH_ERROR
                text, end = ERROR_TEXT, False
        return render.alice_response(self._speakable(text), end_session=end)

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
        if pending is not None and PendingPermission.from_record(pending) is None:
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

        answered = await self.session.answer_permission(self.opencode, app_id, command)
        if answered is not None:
            return answered, False

        wiring = self.opencode
        if wiring is not None and (await wiring.client.health()).reachable:
            try:
                return await self.session.turn(wiring, app_id, command)
            except (BackendError, httpx.HTTPError) as exc:
                # C1: a 200 whose body is a refusal arrives here, and it is a
                # failure -- the chain below answers, and the refusal is never spoken.
                self.logger.warning(
                    "opencode %r could not answer %s; the turn falls back to the chain: %r",
                    wiring.spec.name, app_id, exc,
                )
        markup = request.get("markup") or {}
        return await self._chain_turn(app_id, command, bool(markup.get("dangerous_context")))

    async def _chain_turn(self, app_id: str, command: str, dangerous: bool) -> tuple[str, bool]:
        rec = metrics.current()
        await self.memory.append_message(app_id, "user", command, self.cfg.max_history)
        history = await self.memory.load_history(app_id)
        messages = [{"role": "system", "content": SYSTEM_PROMPT}, *history]
        if dangerous:
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "Запрос помечен как потенциально опасный. Не выполняй никаких действий "
                        "без явного подтверждения пользователем."
                    ),
                }
            )

        choice = await rec.measure(self._call_llm(messages))
        rec.answered(
            route=metrics.ROUTE_FALLBACK,
            model=choice.provider,
            msgs=len(messages),
            tools=[t.name for t in choice.tool_calls],
        )
        if choice.tool_calls:
            tool_call = choice.tool_calls[0]
            ctx = ToolContext(self.cfg, self.memory, app_id, self.logger)
            result = await self.registry.execute(ctx, tool_call.name, tool_call.arguments)
            await self.memory.append_message(app_id, "assistant", f"[{tool_call.name}]", self.cfg.max_history)
            if result.tg_send:
                self._notify(result.tg_send)
            if result.is_async and result.job:
                rec.escalated = True
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

        The session kind is removed from the order: the opencode route has just
        tried it, and it is the only kind ``build_chain`` cannot construct without
        the wiring.
        """
        chain, specs = load_backend_specs(self.cfg)
        order = tuple(name for name in chain.order if specs[name].kind != SESSION_KIND)
        backends = build_chain(specs, BackendChain(order=order, unused=chain.unused))
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

    def _notify(self, text: str) -> None:
        """Send a Telegram notification without blocking the turn, and keep it alive.

        `asyncio.create_task` with no reference to the result is a documented way to
        lose work: the event loop only holds a weak reference, so a task nobody keeps
        can be garbage-collected before it ever runs. Every other `create_task` in
        this project stores its task -- `SessionReaders._tasks`, `Worker._tasks`,
        `PermissionSweep._task` -- and this one did not, which meant the confirmation
        of a synchronous tool action could simply never arrive: the action ran, the
        user was told nothing, and nothing in the logs said why. The set holds the
        tasks until they finish and `discard` drops exactly the finished one.
        """
        task = asyncio.create_task(send_message(self.cfg, text))
        self._notifications.add(task)
        task.add_done_callback(self._notifications.discard)

    def _speakable(self, text: str) -> str:
        """The third guard: no reply carrying the escalation sentinel reaches Alice.

        Total, and it runs on the RAW text of every turn before ``render.clean``,
        which strips the brackets the sentinel is written with and truncates at
        1024 characters. A guard placed after the cleaner would be searching for a
        string the cleaner had already dismantled, and would miss a sentinel past
        the cut -- the one place a truncation-blind guard leaks.
        """
        spoken = routing.sanitize_for_speech(text, sentinel=self.cfg.r2d2_needs_agent_sentinel)
        if text and not spoken:
            self.logger.warning(
                "a reply carried the escalation sentinel and was replaced with the ack before speaking"
            )
            return self.cfg.r2d2_task_ack
        return spoken
