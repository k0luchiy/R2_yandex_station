"""R2D2's brain: one turn in, one spoken answer out -- or an acknowledgement and
a result in Telegram (plan todo 15).

Alice gives a webhook **4.5 s** and **1024 characters**. The opencode server gives
one persistent session per user, a tool-less voice agent that answers inside
`r2d2_fast_deadline`, and a full agent that does real work. This module decides
which of the two a turn gets, and the decision is forced by measurements from
`docs/11-opencode-contract.md` rather than by taste:

* **C8 -- a cold session costs 15.5-18.6 s.** So a session whose message count is
  zero skips the synchronous voice turn entirely, submits the work to the agent
  and acknowledges in under a second. A warm session (p50 1.667 s, p95 2.247 s)
  answers directly.
* **C1 -- a 200 is not a reply.** `info.error` carries a 403 `FreeTierError` or a
  402 inside an HTTP 200, so the opencode turn is a failure, never an answer, and
  the turn falls back to the provider chain. This is the headline hazard of the
  rewrite: a refusal text that reaches a speaker is worse than no answer, because
  the user cannot tell it from one.

Three more decisions, each load-bearing:

* **A deadline is not an abort.** A voice turn that outruns the budget keeps
  running server-side; the user is acknowledged and an `opencode_reply` job
  collects the answer for Telegram. Aborting would destroy work already paid for,
  and the collector needs a marker -- hence the last message the session held when
  the deadline fired is recorded in the job.
* **The fallback chain excludes the session backend.** `config/backends.json`
  lists `opencode` first, but the opencode route has just tried it, and a second
  attempt on the same host would spend budget the turn no longer has. It is also
  the only kind `build_chain` cannot build without the wiring, so leaving it in
  made the whole chain raise `BackendConfigError` and every fallback turn answer
  with `ERROR_TEXT`.
* **The sentinel never reaches a speaker.** `parse_voice_reply` is the structural
  guard, `sanitize_for_speech` the total one, and `_speakable` the third: it runs
  on the RAW text of every turn, in `process`, BEFORE `render.clean` strips the
  brackets the sentinel is written with and truncates at 1024 characters. A guard
  after the cleaner would look for a string the cleaner had already dismantled and
  would miss a sentinel past the cut.

`authorized()` is unchanged and is still the only thing between a network-exposed
webhook and a stranger's laptop.

allow: SIZE_OK -- the whole orchestrator, in the one file plan todo 15 pins it to
and `app/main.py` imports by name. Splitting it would mean a second module whose
only entry point is `_handle`, and `_chain_turn` below is preserved code that
cannot shrink without changing behaviour its tests assert.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Final

import httpx

from app.config import Config
from core import policies, render, routing
from core.async_worker import Worker
from core.backends.base import Choice
from core.backends.config_loader import BackendChain, BackendSpec, load_backend_specs
from core.backends.openai_compatible import BackendError
from core.backends.opencode_session import OpencodeSessionBackend, current_application_id
from core.backends.registry import build_chain
from core.memory import Memory
from core.opencode.client import OpencodeClient, OpencodeDeadlineExceeded, OpencodeError
from core.opencode.session_store import OcSessionStore
from core.permissions import PendingPermission, PermissionBroker, PermissionVerdict
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

#: The backend kind that answers from a persistent opencode session. It is the
#: opencode ROUTE, never a member of the fallback chain -- see the module docstring.
SESSION_KIND: Final = "opencode_session"
#: The job type `core/async_worker.py` does not dispatch yet; todo 16 adds the
#: branch. Until then the worker logs the failure, which is the sanctioned
#: intermediate state rather than a silent loss.
JOB_OPENCODE_REPLY: Final = "opencode_reply"
#: The collector's ceiling, stated in the job rather than left to the reader. It
#: is a ceiling and not a wait: `collect_reply` ends on the idle condition first.
COLLECT_TIMEOUT_S: Final = 600.0
#: What the deadline path may spend finding its marker message. It has already
#: spent the whole voice budget, so this is small and absolute -- a server that
#: will not answer here costs the answer, never the acknowledgement.
MARKER_TIMEOUT_S: Final = 0.5
#: What a brokered permission answer is spoken as. The title opencode supplies is
#: deliberately absent: it is server-controlled, and it can be kilobytes long.
PERMISSION_APPROVED: Final = "Принято, выполняю."
PERMISSION_REFUSED: Final = "Отменяю."
#: The agent turn carries the request in the user's own words, so the agent has
#: it even when the voice model offered no hint.
TASK_PREFIX: Final = "Пользователь попросил голосом: "


@dataclass(frozen=True, slots=True)
class HybridWiring:
    """The opencode route's collaborators, as todo 18's composition root has them.

    Grouped into one value because they are one thing: the route either exists
    completely or is not used at all, and a half-wired route that silently
    answered from the chain instead would be a bug nobody could see. `spec` is
    carried because the brain needs `fast_model` and `summarize_model` from the
    one place they are configured -- changing a model must stay a one-line edit in
    `config/backends.json`. `broker` is optional so a deployment without todo 14
    still routes; without it a pending ask is simply not answered here.
    """

    spec: BackendSpec
    client: OpencodeClient
    store: OcSessionStore
    backend: OpencodeSessionBackend
    broker: PermissionBroker | None = None


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

        answered = await self._answer_permission(app_id, command)
        if answered is not None:
            return answered, False

        wiring = self.opencode
        if wiring is not None and (await wiring.client.health()).reachable:
            try:
                return await self._opencode_turn(wiring, app_id, command)
            except (BackendError, httpx.HTTPError) as exc:
                # C1: a 200 whose body is a refusal arrives here, and it is a
                # failure -- the chain below answers, and the refusal is never spoken.
                self.logger.warning(
                    "opencode %r could not answer %s; the turn falls back to the chain: %r",
                    wiring.spec.name, app_id, exc,
                )
        markup = request.get("markup") or {}
        return await self._chain_turn(app_id, command, bool(markup.get("dangerous_context")))

    async def _answer_permission(self, app_id: str, command: str) -> str | None:
        """What to speak for a brokered opencode ask, or None to carry on.

        `unrelated` means there was no ask of ours, or the text is not an answer at
        all, and it changes nothing -- the question is then answered normally and
        the ask stays pending, which is the only way it can still be answered.
        """
        wiring = self.opencode
        broker = wiring.broker if wiring is not None else None
        if broker is None:
            return None
        verdict: PermissionVerdict = await broker.resolve_from_text(app_id, command)
        match verdict:
            case "approved":
                return PERMISSION_APPROVED
            case "rejected":
                return PERMISSION_REFUSED
            case _:
                return None

    async def _opencode_turn(self, wiring: HybridWiring, app_id: str, command: str) -> tuple[str, bool]:
        """One turn in the user's own opencode session, or the ack for work started.

        `complete()` resolves the session itself through the `ContextVar`, so the
        store is consulted here for the two things it alone knows: which session
        the agent turn belongs to, and whether this session has ever been used.
        """
        session_id = await wiring.store.resolve(app_id)
        if not await self._prepare(wiring, app_id, session_id):
            # C8: the first message in a fresh session costs 15.5-18.6s, so it is
            # submitted to the agent and acknowledged rather than waited on.
            await wiring.backend.submit_task(session_id, f"{TASK_PREFIX}{command}")
            return self.cfg.r2d2_task_ack, False
        current_application_id.set(app_id)
        try:
            choice = await wiring.backend.complete(
                [{"role": "user", "content": command}],
                timeout=self.cfg.r2d2_fast_deadline,
                model=wiring.spec.fast_model,
            )
        except OpencodeDeadlineExceeded:
            # NOT aborted: the turn is still running server-side and the collector
            # below fetches it. The user has already been given the ack.
            await self._collect_later(wiring, app_id, session_id)
            return self.cfg.r2d2_task_ack, False
        decision = routing.parse_voice_reply(
            choice.content, sentinel=self.cfg.r2d2_needs_agent_sentinel
        )
        if decision.kind == "speak":
            return decision.spoken, False
        hint = f"\n{decision.task_hint}" if decision.task_hint else ""
        await wiring.backend.submit_task(session_id, f"{TASK_PREFIX}{command}{hint}")
        return self.cfg.r2d2_task_ack, False

    async def _prepare(self, wiring: HybridWiring, app_id: str, session_id: str) -> bool:
        """Count the turn, summarise a long session once, and report whether it is warm.

        The count is read BEFORE it is bumped, because zero is the C8 signal and
        bumping first would make every session look warm.
        """
        count = await wiring.store.message_count(app_id)
        if count > self.cfg.r2d2_session_soft_limit:
            provider, model = routing.split_model(wiring.spec.summarize_model)
            await wiring.client.summarize(session_id, provider=provider, model=model)
            self.logger.info(
                "opencode %r: summarised the session of %s after %d messages", session_id, app_id, count
            )
        await self.memory.touch_oc_session(app_id, message_delta=1)
        return count > 0

    async def _collect_later(self, wiring: HybridWiring, app_id: str, session_id: str) -> None:
        """Hand the still-running turn to the worker, which ships the answer to Telegram.

        The marker is the last message the session held when the deadline fired, so
        the collector can only return text produced after this turn and never
        replays the session. Finding it costs one GET on a path that has already
        spent the whole voice budget, and it is bounded: a server that will not
        answer here loses the answer, never the acknowledgement.
        """
        try:
            records = await asyncio.wait_for(
                wiring.client.list_messages(session_id), MARKER_TIMEOUT_S
            )
        except (TimeoutError, httpx.HTTPError, OpencodeError) as exc:
            self.logger.warning(
                "opencode %r: the turn in session %s of %s keeps running but cannot be "
                "collected: %s", wiring.spec.name, session_id, app_id, exc,
            )
            return
        await self.worker.enqueue(
            {
                "type": JOB_OPENCODE_REPLY,
                "application_id": app_id,
                "session_id": session_id,
                "since_message_id": records[-1].id if records else "",
                "timeout_s": COLLECT_TIMEOUT_S,
            }
        )

    async def _chain_turn(self, app_id: str, command: str, dangerous: bool) -> tuple[str, bool]:
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
