"""Behavioural tests for the hybrid brain: fast voice turn or opencode session,
and never a machine token in Alice's mouth (plan todo 15).

Alice's webhook has **4.5 s** and **1024 characters**. The measurements that shape
every assertion here are in `docs/11-opencode-contract.md`:

* **C8 -- a cold session costs 15.5-18.6 s.** The first message in a brand-new
  session must therefore never sit on the synchronous voice path, or the first
  question of every user blows the budget by ten seconds. Section 5.
* **C1 -- a 200 is not a reply.** opencode answers HTTP 200 with
  `info.error = {statusCode: 403, FreeTierError}` for every free Zen model except
  `space-bunny-free`, and 402 for the paid ones. A brain that trusts the status
  code speaks the refusal to the user. Section 4.
* **A deadline is not an abort.** A turn that outruns `r2d2_fast_deadline` keeps
  running server-side; the user gets the ack and the real answer in Telegram.
  Nothing may issue `POST /session/:id/abort`. Section 6.
* **The sentinel is machine protocol.** `[[NEEDS_AGENT]]` routes between the two
  agents and must never be read aloud -- three independent guards stand between
  it and Alice's `text`, and this file proves all three on paths that reach
  them. Sections 2 and 7.

**What is real here.** The real `Brain`, the real `OpencodeClient`, the real
`OcSessionStore`, the real `OpencodeSessionBackend`, the real `PermissionBroker`,
the real `Memory` over a temp SQLite file, and the real `core.render`. The
doubles sit at the wire: `httpx.MockTransport` for `opencode serve` and `respx`
for the two non-opencode HTTP calls R2D2 makes (Telegram and the fallback
backend). So the agent name, the model split, `?directory=`, the request paths,
the posted body and the message the user would actually read are all observed
rather than asserted about. Nothing touches a network (proven by re-running the
suite under `-p no_net`).

**Timing.** No test sleeps to "let something finish". The deadline is a
per-test `Config` field, set to 50 ms, so the hang tests are bounded by the same
clock the production code uses; the collector ends on the idle condition
(`r2d2_event_poll_interval`, 50 ms here) rather than on its 600 s ceiling; and
the two tests that need a background job SUBSCRIBE to the Telegram delivery
instead of polling a counter.

**One deliberate contract decision**, stated because a reviewer will ask:
a user message that literally contains `[[NEEDS_AGENT]]` is **not** an
escalation signal. Only the MODEL's reply is parsed for the sentinel, because
the sentinel is the model's own protocol -- a user who says the words out loud
is asking what they mean, and escalating their question to the agent would drop
the turn on the floor. The text still reaches the session verbatim
(`test_a_user_message_carrying_the_sentinel_is_not_an_escalation_signal`).

allow: SIZE_OK -- 2084 pure LOC, a test module grows with the behaviours it pins.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final

import httpx
import pytest
import respx

import core.brain
import core.session_settle as settle_module
from app.config import Config
from core.async_worker import Worker
from core.backends.config_loader import BackendSpec, load_backend_specs
from core.backends.opencode_session import (
    OpencodeSessionBackend,
    OpencodeWiring,
    current_application_id,
)
from core.brain import ERROR_TEXT, GREETING, HELP_TEXT, Brain, tool_pending_key
from core.memory import Memory
from core.opencode.client import OpencodeClient, OpencodeDeadlineExceeded
from core.opencode.session_store import OcSessionStore, title_for
from core.opencode.sse_frames import TURN_COMPLETE, OpencodeEvent
from core.opencode.turn_watch import TurnWatch
from core.permissions import PermissionBroker
from core.render import MAX_TEXT
from core.session_collector import BUSY_REFUSED, PARKED
from tests.fake_opencode import (
    completed_tool_message,
    failed_tool_message,
    refused_tool_message,
)

# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------

BASE_URL: Final = "http://127.0.0.1:4599"
FALLBACK_URL: Final = "https://zen.test/v1"
SECOND_FALLBACK_URL: Final = "https://openrouter.test/api/v1"
TELEGRAM_PREFIX: Final = "https://api.telegram.org/"

#: The shipped values the plan fixed, read off the dataclass so a change to
#: `app/config.py` is a test failure rather than a silent drift.
ACK: Final = Config().r2d2_task_ack
SENTINEL: Final = Config().r2d2_needs_agent_sentinel
SHIPPED_DEADLINE_S: Final = 3.3
SHIPPED_POLL_S: Final = 2.0

APP: Final = "alice-app-1"
OTHER_USER: Final = "bob-2"
SKILL_ID: Final = "skill-abc"
USER_ID: Final = "user-xyz"

VOICE_AGENT: Final = "r2d2-voice"
TASK_AGENT: Final = "r2d2-agent"
MODEL: Final = "opencode/space-bunny-free"
WIRED_MODEL: Final = {"providerID": "opencode", "modelID": "space-bunny-free"}

ANSWER: Final = "Квантовая точка — это наночастица."
FALLBACK_ANSWER: Final = "Ответ запасного провайдера."
SECOND_FALLBACK_ANSWER: Final = "Ответ второго запасного провайдера."
AGENT_REPLY: Final = "Сводка готова: три статьи про RAG."
QUESTION: Final = "что такое квантовые точки"

#: Credentials. Each appears in exactly one place (the temp `backends.json` and
#: the environment the loader expands it from), so "it is not in the reply, a
#: log record or an exception" is a real assertion.
PASSWORD: Final = "R2D2_OC_PASSWORD_VALUE-9f3a"
ZEN_KEY: Final = "sk-or-R2D2_ZEN_KEY_VALUE-7c1e"
OPENROUTER_KEY: Final = "sk-or-openrouter-test-4b2d"
TELEGRAM_TOKEN: Final = "TESTTOKEN"

#: The job type todo 16 adds to `Worker._dispatch`, and the exact field set the
#: brain writes. Todo 16 reads `session_id`, `since_message_id`, `turn_text`
#: and `timeout_s`.
JOB_TYPE: Final = "opencode_reply"
JOB_FIELDS: Final = frozenset(
    {"type", "application_id", "session_id", "since_message_id", "turn_text", "timeout_s"}
)
#: The collector ceiling the brain states in the job. It is never reached in a
#: test (the idle condition ends the poll first), so the value is pinned rather
#: than waited out.
COLLECT_TIMEOUT_S: Final = 600.0
NO_REPLY: Final = "Агент не ответил."

#: The per-test clocks. The production code reads the same two config fields, so
#: a test never waits on wall clock longer than the deadline it configured.
DEADLINE_S: Final = 0.05
POLL_S: Final = 0.05
#: A generous ceiling on a hung turn, 20x the configured deadline: it fails only
#: if the turn is not bounded at all, and cannot flake on a loaded machine.
HANG_CEILING_S: Final = 2.0

#: A 200 whose `info.error` is the refusal the spike measured on this machine (C1).
FREE_TIER_ERROR: Final = {
    "name": "APIError",
    "data": {
        "message": "OpenCode's free tier can only be used from within OpenCode",
        "statusCode": 403,
        "isRetryable": False,
    },
}

#: The registry the brain's fallback walks: the shipped `config/backends.json`
#: shape, with the opencode session backend FIRST -- so the test proves the brain
#: does not re-try it as a fallback after the opencode route just failed.
BACKENDS_JSON: Final = json.dumps(
    {
        "chain": ["opencode", "zen", "openrouter"],
        "backends": [
            {
                "name": "opencode",
                "kind": "opencode_session",
                "base_url": BASE_URL,
                "username": "${R2D2_OC_USERNAME}",
                "password": "${R2D2_OC_PASSWORD}",
                "voice_agent": VOICE_AGENT,
                "task_agent": TASK_AGENT,
                "fast_model": MODEL,
                "task_model": MODEL,
                "summarize_model": MODEL,
                "timeout": 0.5,
            },
            {
                "name": "zen",
                "kind": "openai_compatible",
                "base_url": FALLBACK_URL,
                "api_key": "${R2D2_ZEN_KEY}",
                "model": "space-bunny-free",
                "auth_style": "bearer",
            },
            {
                "name": "openrouter",
                "kind": "openai_compatible",
                "base_url": SECOND_FALLBACK_URL,
                "api_key": "${OPENROUTER_API_KEY}",
                "model": "some:free",
                "auth_style": "bearer",
            },
        ],
    },
    ensure_ascii=False,
)


@dataclass(frozen=True, slots=True)
class Turn:
    """One message R2D2 put into a session: the voice turn or the agent turn."""

    session_id: str
    agent: str
    model: str
    text: str
    submitted: bool
    directory: str

    @property
    def model_id(self) -> str:
        return self.model.get("modelID", "") if isinstance(self.model, dict) else ""


def said(text: str, message_id: str, role: str = "assistant") -> dict[str, object]:
    """One `GET /session/:id/message` entry: `{info, parts}` (C7)."""
    return {"info": {"id": message_id, "role": role}, "parts": [{"type": "text", "text": text}]}


class _EndlessStream(httpx.AsyncByteStream):
    """A body that never ends, so a deadline is the only way out."""

    async def __aiter__(self) -> object:
        await asyncio.sleep(3600)
        yield b""  # pragma: no cover - the wait above never returns


class FakeOpencode:
    """`opencode serve` as a `MockTransport` handler -- every route the brain uses.

    The knobs are the behaviours the real server produced in the spike: an
    `info.error` inside a 200 (C1), a turn that never answers, a server that is
    not running at all. Requests are recorded BEFORE the handler runs, so "the
    abort path was never hit" means the transport never saw an abort, and
    "no opencode traffic at all" means `requests` is empty.
    """

    def __init__(self, workspace: str = "") -> None:
        self.workspace = workspace
        self.sessions: dict[str, dict[str, str]] = {}
        self.turns: list[Turn] = []
        self.requests: list[httpx.Request] = []
        self.summarizes: list[tuple[str, dict[str, object]]] = []
        self.permission_answers: list[tuple[str, str, dict[str, object]]] = []
        self.aborted: list[str] = []
        self.transcript: dict[str, list[dict[str, object]]] = {}
        self.reply: str = ANSWER
        self.turn_error: dict[str, object] | None = None
        self.down: bool = False
        self.hang_turn: bool = False
        self.empty_turn: bool = False
        #: Called synchronously the moment a hung voice turn is ACCEPTED, so a test can
        #: release something from OUTSIDE the request -- the broker storing an ask while
        #: this turn is still waiting is the only way to reproduce the shape where an
        #: ask and the deadline belong to the same turn.
        self.on_hung_turn: Callable[[], None] | None = None
        #: A bounded wait before the turn answers, so a test can put the deadline
        #: on either side of the answer and prove which one decided the outcome.
        self.delay_s: float = 0.0
        #: The message id of each hung voice turn, in order -- what the brain must
        #: record as `since_message_id`, because at the moment the deadline fires
        #: that user turn is the last thing the session holds.
        self.hung_ids: list[str] = []
        #: `(session_id, message_id)` of every `DELETE .../message/...` the brain
        #: asked for, and the status the server answered with. The routing signal
        #: must leave the session on the very turn that observed it.
        self.deleted: list[tuple[str, str]] = []
        #: How many transcript reads answer with nothing at all, and the reason they give.
        #: D15's precondition: the sweep's half-second read does not get an answer while
        #: the server is perfectly healthy, and the turn is acknowledged all the same.
        self.stall_polls: int = 0
        self.stall_reason: object = httpx.ReadTimeout("")
        self.delete_status: int = 200
        #: Sessions `GET /session/status` reports as `busy` -- the only status value
        #: the server reports, and the one that means a turn submitted right now
        #: will not be served (NEW-2, `qa/live-run-v12.md`). Empty is the common
        #: case: an idle session, and every session this double creates.
        self.busy: set[str] = set()
        #: When True, `GET /session/status` answers 500 instead of a mapping: the
        #: unreadable-answer case, which must NOT read as a refusal.
        self.status_broken: bool = False
        self._hung: dict[str, tuple[str, str]] = {}
        self._answered: set[str] = set()
        self._delivered: set[str] = set()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.down:
            raise httpx.ConnectError(f"opencode is not running at {BASE_URL}")
        method, path = request.method, request.url.path
        parts = path.split("/")
        session_id = parts[2] if len(parts) > 2 and parts[1] == "session" else ""
        if method == "GET" and path == "/global/health":
            return httpx.Response(200, json={"healthy": True, "version": "1.18.32"})
        if method == "GET" and path == "/session":
            return httpx.Response(200, json=list(self.sessions.values()))
        if method == "POST" and path == "/session":
            return self._create(request)
        if method == "POST" and path.endswith("/message"):
            return self._turn(session_id, request)
        if method == "POST" and path.endswith("/prompt_async"):
            turn = self._record(session_id, request, submitted=True)
            # The real server STORES the submitted task as a user message, and the
            # transcript read that follows is how a collector anchored late tells what the
            # session held before the hand-off from what it held after it.
            self.seed(session_id, said(turn.text, f"msg_task{len(self.turns)}", role="user"))
            return httpx.Response(204)
        if method == "GET" and path.endswith("/message"):
            if self.stall_polls:
                self.stall_polls -= 1
                raise self.stall_reason
            return self._poll(session_id)
        if method == "DELETE" and "/message/" in path:
            return self._delete(session_id, parts[4])
        if method == "POST" and path.endswith("/summarize"):
            self.summarizes.append((session_id, json.loads(request.content)))
            return httpx.Response(200, json=True)
        if method == "POST" and "/permissions/" in path:
            self.permission_answers.append(
                (session_id, parts[4], json.loads(request.content))
            )
            return httpx.Response(200, json=True)
        if method == "POST" and path.endswith("/abort"):
            self.aborted.append(session_id)
            return httpx.Response(200, json=True)
        if method == "GET" and path == "/session/status":
            if self.status_broken:
                return httpx.Response(500, json={"name": "UnknownError"})
            return httpx.Response(200, json={sid: {"type": "busy"} for sid in self.busy})
        return httpx.Response(404, json={"name": "NotFoundError", "data": {"message": path}})

    # -- what the tests read off the wire ----------------------------------

    def routes(self) -> list[str]:
        return [f"{request.method} {request.url.path}" for request in self.requests]

    def route_count(self, method: str, suffix: str) -> int:
        return len([r for r in self.requests if r.method == method and r.url.path.endswith(suffix)])

    def bodies(self, method: str, suffix: str) -> list[dict[str, object]]:
        return [
            json.loads(request.content)
            for request in self.requests
            if request.method == method and request.url.path.endswith(suffix)
        ]

    def seed(self, session_id: str, *messages: dict[str, object]) -> None:
        self.transcript.setdefault(session_id, []).extend(messages)

    def client(self, spec: BackendSpec) -> OpencodeClient:
        return OpencodeClient(
            spec,
            self.workspace,
            client=httpx.AsyncClient(transport=httpx.MockTransport(self)),
        )

    # -- routes -------------------------------------------------------------

    def _create(self, request: httpx.Request) -> httpx.Response:
        session_id = f"ses_{len(self.sessions) + 1}"
        self.sessions[session_id] = {
            "id": session_id,
            "title": json.loads(request.content)["title"],
            "directory": request.url.params.get("directory", ""),
        }
        return httpx.Response(200, json=self.sessions[session_id])

    def _record(self, session_id: str, request: httpx.Request, *, submitted: bool) -> Turn:
        body: dict[str, object] = json.loads(request.content)
        model = body["model"]
        turn = Turn(
            session_id=session_id,
            agent=str(body.get("agent", "")),
            model=model if isinstance(model, dict) else {},
            text=str(body["parts"][0]["text"]),
            submitted=submitted,
            directory=request.url.params.get("directory", ""),
        )
        self.turns.append(turn)
        return turn

    def _turn(self, session_id: str, request: httpx.Request) -> object:
        turn = self._record(session_id, request, submitted=False)
        if self.hang_turn:
            # The turn is ACCEPTED and never answered: exactly what the client sees
            # when a cold cache outruns the voice budget. Only the USER's message
            # exists at the moment the deadline fires; the assistant's lands later,
            # which is the whole reason the marker has to be the user's message.
            message_id = f"msg_hung_{len(self.hung_ids) + 1}"
            self.hung_ids.append(message_id)
            self._hung[session_id] = (turn.text, message_id)
            if self.on_hung_turn is not None:
                # The seam that makes "an ask arrives WHILE this turn waits" a
                # reproducible test rather than a race: the handler is synchronous,
                # so it cannot await, and all it has to do is release whatever the
                # test parked outside the request.
                self.on_hung_turn()
            return httpx.Response(200, stream=_EndlessStream())
        if self.turn_error is not None:
            return httpx.Response(
                200, json={"info": {"id": "msg_1", "error": self.turn_error}, "parts": []}
            )
        if self.delay_s:
            return self._answer_later(self._answer(session_id, turn), self.delay_s)
        return self._answer(session_id, turn)

    def _answer(self, session_id: str, turn: Turn) -> httpx.Response:
        parts = [] if self.empty_turn else [{"type": "text", "text": self.reply}]
        message_id = f"msg_a{len(self.turns)}"
        self.seed(session_id, said(turn.text, f"msg_u{len(self.turns)}", role="user"))
        # The real server STORES the assistant message, and the id it reports in
        # `info.id` is the id that message has. A fake that returned the text
        # without keeping it would hide the whole escalation-signal defect: the
        # routing token would never enter the transcript the agent reads back.
        if not self.empty_turn:
            self.seed(session_id, said(self.reply, message_id))
        return httpx.Response(
            200, json={"info": {"id": message_id, "role": "assistant"}, "parts": parts}
        )

    @staticmethod
    async def _answer_later(response: httpx.Response, delay_s: float) -> httpx.Response:
        """A turn that takes time. `MockTransport` awaits what is not a Response.

        The wait must be ASYNC: a blocking one would starve the event loop and the
        deadline under test could never fire, which is the opposite of what the
        test is measuring.
        """
        await asyncio.sleep(delay_s)
        return response

    def _poll(self, session_id: str) -> httpx.Response:
        if session_id in self._hung:
            if session_id not in self._answered:
                self._answered.add(session_id)
                text, message_id = self._hung[session_id]
                self.seed(session_id, said(text, message_id, role="user"))
            elif session_id not in self._delivered:
                self._delivered.add(session_id)
                self.seed(session_id, said(AGENT_REPLY, "msg_agent"))
        return httpx.Response(200, json=self.transcript.get(session_id, []))

    def _delete(self, session_id: str, message_id: str) -> httpx.Response:
        held = self.transcript.get(session_id, [])
        kept = [entry for entry in held if entry["info"].get("id") != message_id]
        removed = len(kept) != len(held)
        if removed:
            self.transcript[session_id] = kept
        self.deleted.append((session_id, message_id))
        if self.delete_status != 200:
            return httpx.Response(
                self.delete_status, json={"name": "UnknownError", "data": {"message": "no"}}
            )
        return httpx.Response(200, json=removed)


def replying(content: str):
    """A respx side effect: one OpenAI-shaped completion carrying `content`."""
    def side_effect(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": content}}],
                "model": json.loads(request.content)["model"],
            },
        )

    return side_effect


class Net:
    """`respx` in front of the two non-opencode calls R2D2 makes.

    Telegram, because the whole point of the async route is the message the user
    would actually read; the two fallback backends, because the chain's failure
    behaviour is a property of the walk and not of any one provider. An unmocked
    call raises instead of escaping to the network, and `assert_all_called` is
    off because several tests must send nothing at all -- which they assert.
    """

    def __init__(self) -> None:
        self.router = respx.mock(assert_all_called=False)
        self.telegram_route = self.router.post(url__startswith=TELEGRAM_PREFIX).mock(
            side_effect=self._telegram
        )
        self.zen = self.router.post(f"{FALLBACK_URL}/chat/completions").mock(
            side_effect=self._chat
        )
        self.openrouter = self.router.post(
            f"{SECOND_FALLBACK_URL}/chat/completions"
        ).mock(side_effect=self._chat)
        self.telegram: list[str] = []
        self.zen_bodies: list[dict[str, object]] = []
        self.openrouter_bodies: list[dict[str, object]] = []
        self.zen_status = 200
        self.openrouter_status = 200
        self.delivered = asyncio.Event()

    def _telegram(self, request: httpx.Request) -> httpx.Response:
        self.telegram.append(str(json.loads(request.content)["text"]))
        self.delivered.set()
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(self.telegram)}})

    def _chat(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        target = self.zen if request.url.host == "zen.test" else self.openrouter
        bodies = self.zen_bodies if target is self.zen else self.openrouter_bodies
        status = self.zen_status if target is self.zen else self.openrouter_status
        bodies.append(body)
        if status != 200:
            return httpx.Response(status, json={"error": {"message": "refused"}})
        text = FALLBACK_ANSWER if target is self.zen else SECOND_FALLBACK_ANSWER
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": text}}],
                "model": body["model"],
            },
        )

    def fallback_calls(self) -> int:
        return len(self.zen_bodies) + len(self.openrouter_bodies)


class CollectorWorker(Worker):
    """The real `Worker` with todo 16's `opencode_reply` branch.

    `core/async_worker.py` does not dispatch this job type yet -- todo 16 adds
    the branch below, and until then the job answers "Неизвестная задача.". Having
    it here makes the contract executable BEFORE it is implemented, so the test
    that proves "the collected text reaches Telegram" is a real end-to-end
    observation of the job the brain enqueues, not a restatement of this docstring.
    """

    def __init__(self, cfg: Config, memory: Memory, backend: OpencodeSessionBackend) -> None:
        super().__init__(cfg, memory, logging.getLogger("r2d2.test.collector"))
        self._backend = backend

    async def _dispatch(self, job: dict) -> str:
        if job.get("type") == JOB_TYPE:
            turn = job.get("turn_text")
            text = await self._backend.collect_reply(
                str(job["session_id"]),
                str(job["since_message_id"]),
                float(job.get("timeout_s", COLLECT_TIMEOUT_S)),
                turn_text=turn if isinstance(turn, str) else "",
            )
            return text or NO_REPLY
        return await super()._dispatch(job)


class RecordingWorker(Worker):
    """The real `Worker` with its loop unstarted, so `enqueue` is observable."""

    def __init__(self, cfg: Config, memory: Memory) -> None:
        super().__init__(cfg, memory, logging.getLogger("r2d2.test.recorder"))
        self.jobs: list[dict] = []

    async def enqueue(self, job: dict) -> str:
        self.jobs.append(job)
        return await super().enqueue(job)


class Rig:
    """The real `Brain` over the real opencode stack, plus the wires to inspect."""

    def __init__(
        self,
        brain: Brain,
        server: FakeOpencode,
        net: Net,
        memory: Memory,
        cfg: Config,
        spec: BackendSpec,
        store: OcSessionStore,
        backend: OpencodeSessionBackend,
    ) -> None:
        self.brain = brain
        self.server = server
        self.net = net
        self.memory = memory
        self.cfg = cfg
        self.spec = spec
        self.store = store
        self.backend = backend

    async def warm(self, user_id: str = USER_ID, count: int = 6) -> str:
        """Give `user_id` a bound session and a non-zero message count.

        A brand-new session is the C8 case and has its own test; every other test
        is about a warm one, so this is the fixture's job rather than each test's.

        The identity is the USER, not the application: R2D2 keys memory on
        `session.user.user_id`, so warming an `application_id` would warm a key no
        turn ever looks up.
        """
        session_id = await self.store.resolve(user_id)
        await self.memory.touch_oc_session(user_id, message_delta=count)
        return session_id

    async def ask(
        self,
        command: str = QUESTION,
        *,
        app_id: str = APP,
        new: bool = False,
        original: str = "",
        markup: dict | None = None,
        skill_id: str = SKILL_ID,
        user_id: str = USER_ID,
    ) -> dict:
        body = alice_body(
            command, app_id=app_id, new=new, original=original, markup=markup,
            skill_id=skill_id, user_id=user_id,
        )
        return await self.brain.process_alice(body)

    async def say(self, command: str = QUESTION, **kwargs: object) -> str:
        return str((await self.ask(command, **kwargs))["response"]["text"])


def alice_body(
    command: str = "",
    *,
    app_id: str = APP,
    new: bool = False,
    original: str = "",
    markup: dict | None = None,
    skill_id: str = SKILL_ID,
    user_id: str = USER_ID,
) -> dict:
    """One Alice `SimpleUtterance` webhook body, shaped as the platform sends it.

    `application_id` and `user_id` really are different values in production --
    the platform scopes the first to one app and the second to the person -- and
    `test_the_memory_key_is_the_user_so_two_apps_share_one_session` is what pins
    which of the two R2D2 stores under.
    """
    request: dict = {"type": "SimpleUtterance", "command": command}
    if original:
        request["original_utterance"] = original
    if markup is not None:
        request["markup"] = markup
    return {
        "meta": {"interfaces": [{"type": "Voice"}]},
        "request": request,
        "session": {
            "new": new,
            "skill_id": skill_id,
            "application": {"application_id": app_id},
            "user": {"user_id": user_id},
        },
        "version": "1.0",
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def server() -> FakeOpencode:
    return FakeOpencode()


@pytest.fixture
def net():
    double = Net()
    with double.router:
        yield double


@pytest.fixture(autouse=True)
def no_leaked_application() -> object:
    """No application is current except where a test sets one.

    `current_application_id` is process-wide task-local state; a test that leaked a
    value would make the next test's session assertions pass for the wrong reason.
    """
    current_application_id.set(None)
    yield
    current_application_id.set(None)


async def build_stack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, net: Net, server: FakeOpencode, *, broker: bool
):
    """Assemble the whole stack once, with or without a broker, and return it.

    The registry file is the shipped `config/backends.json` shape with the
    opencode session backend FIRST in the chain, so the fallback tests prove the
    brain does not re-try the session backend after the opencode route failed.

    `broker=False` is a real supported deployment (`EVENT_MODE` unknown, or a
    deployment that wired no broker), not a broken fixture -- and it is the one
    configuration in which the F1 park gate must NOT fire, because a build that
    brokered nothing created no park to refuse. That is why the switch is a
    parameter here rather than a second hand-written fixture: the two would
    otherwise drift, and the drift would be invisible in exactly the test that
    exists to catch it.
    """
    monkeypatch.setenv("R2D2_OC_USERNAME", "opencode")
    monkeypatch.setenv("R2D2_OC_PASSWORD", PASSWORD)
    monkeypatch.setenv("R2D2_ZEN_KEY", ZEN_KEY)
    monkeypatch.setenv("OPENROUTER_API_KEY", OPENROUTER_KEY)
    backends_path = tmp_path / "backends.json"
    backends_path.write_text(BACKENDS_JSON, encoding="utf-8")
    workspace = tmp_path / "r2d2-workspace"
    workspace.mkdir()
    server.workspace = str(workspace)

    cfg = Config(
        alice_skill_id=SKILL_ID,
        alice_user_id=USER_ID,
        telegram_bot_token=TELEGRAM_TOKEN,
        telegram_chat_id="42",
        backends_path=str(backends_path),
        r2d2_workspace=str(workspace),
        r2d2_fast_deadline=DEADLINE_S,
        r2d2_event_poll_interval=POLL_S,
        r2d2_session_soft_limit=40,
        max_history=20,
    )
    memory = await Memory(str(tmp_path / "sessions.db")).connect()
    _chain, specs = load_backend_specs(cfg)
    oc_spec = specs["opencode"]
    client = server.client(oc_spec)
    store = OcSessionStore(memory, client, cfg)
    backend = OpencodeSessionBackend(oc_spec, OpencodeWiring(client=client, store=store, cfg=cfg))
    wiring = core.brain.HybridWiring(
        spec=oc_spec,
        client=client,
        store=store,
        backend=backend,
        broker=PermissionBroker(memory, client, cfg) if broker else None,
    )
    # This rig wires NO `TurnWatch`, which is the deployment the settled wait degrades in
    # (`EVENT_MODE = "poll"`, or no opencode route): with no reader there is no way to learn
    # that a still-running voice turn ended, so it waits a fixed grace instead. The shipped
    # grace is 5s -- longer than any test may pay -- and this is the only place it is set.
    monkeypatch.setattr(settle_module, "SETTLE_GRACE_S", POLL_S)
    worker = RecordingWorker(cfg, memory)
    brain = Brain(cfg, memory, worker, logging.getLogger("r2d2.test.brain"), opencode=wiring)
    return Rig(brain, server, net, memory, cfg, oc_spec, store, backend), memory


@pytest.fixture
async def rig(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, net: Net, server: FakeOpencode
):
    """The whole stack, assembled exactly as todo 18 will assemble it in main."""
    built, memory = await build_stack(tmp_path, monkeypatch, net, server, broker=True)
    try:
        yield built
    finally:
        await memory.close()


@pytest.fixture
async def core_brain_no_broker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, net: Net, server: FakeOpencode
):
    """The same stack with `broker=None`: the supported deployment that never parks."""
    built, memory = await build_stack(tmp_path, monkeypatch, net, server, broker=False)
    try:
        yield built
    finally:
        await memory.close()


# ---------------------------------------------------------------------------
# 0. The fixed intents still answer from the brain, with no opencode traffic
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    (
        pytest.param({"command": "ping"}, "Понг.", id="ping"),
        pytest.param({"command": "проверка связи", "original": "ping"}, "Понг.", id="ping-utterance"),
        pytest.param({"command": "стоп"}, "До встречи.", id="exit"),
        pytest.param({"command": "помощь"}, HELP_TEXT, id="help"),
        pytest.param({"command": "привет", "new": True}, GREETING, id="greeting"),
        pytest.param({"command": "", "new": True}, GREETING, id="empty-greeting"),
    ),
)
async def test_a_fixed_intent_answers_from_the_brain_with_no_opencode_traffic(
    rig: Rig, kwargs: dict, expected: str
) -> None:
    # Given a warm session and a reachable opencode server -- so "no traffic" can
    # only mean the turn never left the brain. The wire is cleared because
    # warming the session is this fixture's work, not the turn's.
    await rig.warm()
    rig.server.requests.clear()
    # When
    payload = await rig.ask(**kwargs)
    # Then the fixed string is spoken ...
    assert payload["response"]["text"] == expected
    assert payload["version"] == "1.0"
    # ... and neither the opencode server nor the fallback chain nor Telegram
    # was touched, so the fast intents cost no network at all
    assert rig.server.requests == []
    assert rig.net.fallback_calls() == 0
    assert rig.net.telegram == []


async def test_the_exit_intent_ends_the_session(rig: Rig) -> None:
    # Given / When
    payload = await rig.ask("стоп")
    # Then
    assert payload["response"]["end_session"] is True


# ---------------------------------------------------------------------------
# 1. The fast voice path
# ---------------------------------------------------------------------------


async def test_a_plain_question_returns_the_assistant_text_in_one_turn(rig: Rig) -> None:
    # Given a user whose session is warm
    session_id = await rig.warm()
    # When
    said_text = await rig.say()
    # Then the answer is the model's own text, spoken ...
    assert said_text == ANSWER
    # ... by exactly one turn, in the user's session, on the voice agent
    assert len(rig.server.turns) == 1
    turn = rig.server.turns[0]
    assert (turn.session_id, turn.agent, turn.text, turn.submitted) == (
        session_id,
        VOICE_AGENT,
        QUESTION,
        False,
    )


async def test_the_voice_turn_reaches_the_wire_with_the_voice_agent_and_fast_model(
    rig: Rig,
) -> None:
    # Given
    await rig.warm()
    # When
    await rig.say()
    # Then the body is the one the contract pins: the EXACT agent name (C2 keeps
    # 11 foreign agents visible) and the model split on the FIRST "/"
    body = rig.server.bodies("POST", "/message")[0]
    assert body["agent"] == VOICE_AGENT
    assert body["model"] == WIRED_MODEL
    assert body["parts"] == [{"type": "text", "text": QUESTION}]
    # ... addressed to the dedicated workspace (C6 is a query parameter)
    assert rig.server.turns[0].directory == rig.cfg.r2d2_workspace


async def test_the_voice_turn_is_bounded_by_the_deadline_and_not_by_anything_else(
    rig: Rig,
) -> None:
    # Given a server that answers, but only after 200ms -- a real wait, bracketed
    # by the two deadlines below, so neither outcome depends on timing luck
    rig.server.delay_s = 0.2
    await rig.warm()
    # When the deadline is shorter than the answer
    rig.cfg.r2d2_fast_deadline = DEADLINE_S
    started = time.monotonic()
    first = await rig.say()
    first_elapsed = time.monotonic() - started
    # Then the user is acknowledged rather than left waiting ...
    assert first == ACK
    assert first_elapsed < HANG_CEILING_S
    # ... and the same question with a deadline longer than the answer is spoken
    rig.cfg.r2d2_fast_deadline = 5.0
    assert await rig.say() == ANSWER


# ---------------------------------------------------------------------------
# 2. Escalation: the sentinel routes, and never reaches Alice
# ---------------------------------------------------------------------------


async def test_a_sentinel_reply_returns_the_ack_and_submits_a_second_turn(rig: Rig) -> None:
    # Given a voice agent that answered with the escalation sentinel
    rig.server.reply = f"Это займёт времени. {SENTINEL} собрать последние статьи про RAG"
    session_id = await rig.warm()
    # When
    text = await rig.say()
    # Then exactly the ack is spoken
    assert text == ACK
    # ... and the session received TWO messages: this voice turn and the agent turn
    assert len(rig.server.turns) == 2
    assert all(turn.session_id == session_id for turn in rig.server.turns)


async def test_the_task_turn_used_the_agent_agent_and_the_only_usable_model(rig: Rig) -> None:
    # Given / When
    rig.server.reply = f"Понял, займусь. {SENTINEL} сделать сводку"
    await rig.warm()
    await rig.say()
    # Then the second turn is the AGENT agent, submitted not awaited, on the only
    # model opencode serve accepts (C1: every other free Zen model is a 403 and
    # the paid ones a 402, so there is no "strong" model to escalate to)
    task = rig.server.turns[1]
    assert (task.agent, task.submitted, task.model) == (TASK_AGENT, True, WIRED_MODEL)
    body = rig.server.bodies("POST", "/prompt_async")[0]
    assert body["agent"] == TASK_AGENT
    assert body["model"] == WIRED_MODEL
    # ... and the agent is told what the user actually asked, plus the hint the
    # voice model wrote BEFORE the sentinel -- and nothing of the discarded tail
    assert task.text == f"Пользователь попросил голосом: {QUESTION}\nПонял, займусь."
    assert "сделать сводку" not in task.text


async def test_a_reply_with_the_sentinel_on_both_sides_of_it_never_reaches_alice(
    rig: Rig,
) -> None:
    # Given the payload is not at the start and not at the end: the tail after the
    # sentinel is exactly what a guard that only strips a prefix would miss
    rig.server.reply = f"Понял, начинаю сборку. {SENTINEL} собери и пришли в телеграм."
    await rig.warm()
    # When
    payload = await rig.ask()
    text = str(payload["response"]["text"])
    # Then neither half of the reply is spoken, only the ack
    assert text == ACK
    assert SENTINEL not in text
    assert "Понял" not in text
    assert len(text) <= MAX_TEXT


async def test_a_sentinel_as_the_very_last_characters_still_escalates(rig: Rig) -> None:
    # Given a reply that is nothing but the sentinel
    rig.server.reply = SENTINEL
    await rig.warm()
    # When
    text = await rig.say()
    # Then
    assert text == ACK
    assert SENTINEL not in text
    # ... and the agent turn carries the user's request, because the hint was empty
    assert rig.server.turns[1].text == f"Пользователь попросил голосом: {QUESTION}"


async def test_a_user_message_carrying_the_sentinel_is_not_an_escalation_signal(
    rig: Rig,
) -> None:
    # Given the USER says the words -- the sentinel is the model's protocol, not
    # the user's, so the inbound text must never be read as a routing signal
    asked = f"что значит {SENTINEL}"
    rig.server.reply = "Это служебная метка маршрутизации."
    await rig.warm()
    # When
    text = await rig.say(asked)
    # Then the question is answered normally, not acked ...
    assert text == "Это служебная метка маршрутизации."
    # ... and the text reached the session verbatim, unmangled by routing
    assert rig.server.turns[0].text == asked
    assert len(rig.server.turns) == 1


async def test_an_escalated_turn_enqueues_the_collector_that_will_ship_the_answer(
    rig: Rig,
) -> None:
    # Given a warm session and a voice agent that handed the turn to the full agent
    rig.server.reply = f"Это займёт времени. {SENTINEL} собрать последние статьи про RAG"
    session_id = await rig.warm()
    # When
    text = await rig.say()
    # Then the ack is spoken, and the agent was given the request ...
    assert text == ACK
    assert [turn.agent for turn in rig.server.turns if turn.submitted] == [TASK_AGENT]
    # ... and EXACTLY ONE collector is armed to bring its answer back. The escalation
    # is the branch the whole two-path design exists for, and with no collector the
    # promise `r2d2-agent`'s prompt makes ("твой финальный текст доставляется в
    # телеграм") is kept by nothing at all.
    jobs = [job for job in rig.brain.worker.jobs if job["type"] == JOB_TYPE]
    assert len(jobs) == 1
    job = jobs[0]
    assert set(job) == JOB_FIELDS
    assert (job["application_id"], job["session_id"]) == (USER_ID, session_id)
    # ... anchored on the last message the session held BEFORE this turn's work was
    # submitted, so the collector returns the agent's answer and never replays the
    # conversation the user already had
    # the newest message that is not R2D2's own submitted task, which the sweep read
    # before the submit and which the real server also stores
    held = [
        entry
        for entry in rig.server.transcript[session_id]
        if not str(entry["parts"][0]["text"]).startswith(TASK_TEXT_PREFIX)
    ]
    assert held, "the session was empty, so there is no marker to check"
    assert job["since_message_id"] == held[-1]["info"]["id"]
    assert job["timeout_s"] == COLLECT_TIMEOUT_S


async def test_a_transcript_read_that_failed_still_leaves_a_collector_armed(
    rig: Rig, net: Net
) -> None:
    """**D15.** The ack is spoken, so a collector must be armed whatever the read did.

    Measured: the sweep's `GET /session/:id/message` is bounded by half a second on a path
    that has already spent the whole voice budget, and when it timed out the hand-off simply
    returned. The user was told "Проверяю, пришлю в телеграм.", the voice agent went on to
    write "Париж.", and no `opencode_reply` row existed for that turn at all -- the answer was
    written to the session and delivered to nobody (`qa/live-run-v3.md` §D15, 12:36:49).

    The collector is therefore armed on an anchor resolved off Alice's clock, and the anchor
    is the part that has to be right: `since_message_id=""` is what `collect_reply` reads as
    "everything in the session is newer", which is the whole conversation replayed into
    Telegram. The task message is the one thing known to be written after the hand-off, so
    the late read is cut there.
    """
    # Given: a warm session whose transcript does not answer the sweep inside its half second
    session_id = await rig.warm()
    rig.server.seed(session_id, said("предыдущий вопрос", "msg_old", role="user"))
    rig.server.reply = f"Это займёт времени. {SENTINEL} собрать последние статьи про RAG"
    rig.server.stall_polls = 1
    # When
    assert await rig.say() == ACK
    # Then a collector IS armed -- the turn is not the D15 turn any more ...
    job = await _armed_job(rig)
    assert job["session_id"] == session_id
    # ... anchored BEFORE the task R2D2 submitted, so the answer is collected and the
    # conversation the user already had is not
    assert job["since_message_id"] != "", "an empty anchor replays the whole session"
    assert job["since_message_id"] == "msg_u1"
    # ... and the answer it fetches is the agent's own, with nothing of the session around it
    rig.server.seed(session_id, said(AGENT_REPLY, "msg_agent"))
    worker = CollectorWorker(rig.cfg, rig.memory, rig.backend)
    await worker.start()
    try:
        net.telegram.clear()
        await worker.enqueue(job)
        await asyncio.wait_for(_delivered(net, AGENT_REPLY), timeout=5.0)
    finally:
        await worker.stop()
    assert net.telegram == [AGENT_REPLY]


async def test_a_transcript_that_never_answers_states_the_loss_in_the_users_chat(
    rig: Rig, net: Net, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other end of D15's trade-off: bounded, but never silent.

    A bounded loss of an answer is acceptable -- the plan's own rule. A SILENT one is not,
    because the user has already been acknowledged and is waiting for a message that will
    not arrive. The deadline is shortened to what a test can pay; the branch is the one that
    matters.
    """
    # Given: a server that answers NOTHING on the transcript, ever
    import core.session_sweeps as sweeps_module

    monkeypatch.setattr(sweeps_module, "ANCHOR_TIMEOUT_S", 0.1)
    session_id = await rig.warm()
    rig.server.reply = f"Это займёт времени. {SENTINEL} собрать последние статьи про RAG"
    rig.server.stall_polls = 1000
    # When
    assert await rig.say() == ACK
    await asyncio.wait_for(_delivered(net, sweeps_module.ANCHOR_LOST), timeout=5.0)
    # Then: no collector was armed, and the user was told
    assert [job for job in rig.brain.worker.jobs if job["type"] == JOB_TYPE] == []
    assert net.telegram == [sweeps_module.ANCHOR_LOST]
    assert session_id


async def test_a_deadline_turns_marker_is_swept_before_the_next_turn_reads_history(
    rig: Rig
) -> None:
    """**D6-residue.** The signal a deadline turn leaves is deleted before anything reads it.

    The deadline branch hands the work to the agent while the voice turn is still running, so
    the sweep that runs before the agent's turn cannot see the `[[NEEDS_AGENT]]` that turn is
    about to write. It arrives afterwards, the agent reads it as its own history and writes
    one itself, and the collector ships that echo: measured live, the user received a stripped
    copy of their own question instead of the requested essay (`qa/live-run-v3.md` §3). A later
    voice-path turn swept nothing at all and the token was still in the session afterwards.

    Deleting it on that turn is impossible -- the message does not exist yet -- so it is
    deleted at the entry of the NEXT turn for that session, which is the last moment before
    any agent reads the history again. And only a session the deadline branch touched is
    swept, so the common turn pays nothing.
    """
    # Given: a turn that overran the budget, so its marker is written too late to be swept
    session_id = await rig.warm()
    rig.server.reply = f"Это займёт времени. {SENTINEL} собрать последние статьи про RAG"
    rig.server.hang_turn = True
    rig.cfg.r2d2_fast_deadline = 0.05
    assert await rig.say() == ACK
    # ... and the voice turn then lands, marker and all
    rig.server.hang_turn = False
    rig.server.seed(session_id, said(rig.server.reply, "msg_late_signal"))
    # When: the NEXT turn for that user runs
    rig.cfg.r2d2_fast_deadline = 5.0
    rig.server.reply = ANSWER
    assert await rig.say() == ANSWER
    # Then the late signal is gone from the session, and only it
    assert ("msg_late_signal") in [message_id for _session, message_id in rig.server.deleted]
    assert SENTINEL not in "\n".join(stored_texts(rig, session_id))


async def test_a_turn_that_never_overran_the_budget_pays_no_residue_read(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cost side of the residue sweep: a session with no debt is not read at all.

    A read on the fast path would be one more request inside Alice's 4.5 s for every
    question ever asked, so the debt is a set lookup and nothing else.
    """
    # Given: a warm session that has never overrun the budget
    session_id = await rig.warm()
    before = rig.server.route_count("GET", "/message")
    # When
    assert await rig.say() == ANSWER
    # Then
    assert rig.server.route_count("GET", "/message") == before
    assert rig.server.transcript[session_id]


async def _armed_job(rig: Rig, timeout_s: float = 5.0) -> dict:
    """The one `opencode_reply` job the worker holds, once the hand-off has fully run.

    The hand-off is off Alice's clock on the deadline branch -- it waits for the voice turn
    that outran the budget to stop writing -- so the test waits for the hand-off rather than
    for a duration; a job that never appears fails here instead of in an assertion three
    lines later. BOTH halves are required, and the order between them is what makes this
    safe rather than merely eventual: `_collect_later` writes the `jobs` row before
    `_hand_over` submits, so observing the submit proves the row exists and the fixture's
    teardown cannot close the database under a write still in flight.
    """
    async def appeared() -> dict:
        while True:
            jobs = [job for job in rig.brain.worker.jobs if job["type"] == JOB_TYPE]
            if jobs and [turn for turn in rig.server.turns if turn.submitted]:
                return jobs[0]
            await asyncio.sleep(0.01)

    return await asyncio.wait_for(appeared(), timeout=timeout_s)


async def _delivered(net: Net, text: str, timeout_s: float = 5.0) -> None:
    """Wait for Telegram to carry `text`, and fail on anything else."""

    async def arrived() -> None:
        while True:
            if text in net.telegram:
                return
            await asyncio.sleep(0.01)

    await asyncio.wait_for(arrived(), timeout=timeout_s)


async def test_two_turns_that_escalate_arm_one_collector_each_and_never_twice(
    rig: Rig,
) -> None:
    # Given a cold session, and then a warm one -- the two branches that submit
    # work to the agent without a deadline
    session_id = await rig.store.resolve(USER_ID)
    assert await rig.say() == ACK
    rig.server.reply = f"Понял. {SENTINEL} собрать сводку"
    assert await rig.say() == ACK
    # When / Then: two turns, two collectors, one each
    jobs = [job for job in rig.brain.worker.jobs if job["type"] == JOB_TYPE]
    assert len(jobs) == 2
    assert all(job["application_id"] == USER_ID for job in jobs)
    assert all(job["session_id"] == session_id for job in jobs)
    # ... a second collector for one turn would deliver the same answer twice, which
    # on a phone reads as two answers and is the failure a duplicate Telegram
    # message looks like to the user
    assert all(job["timeout_s"] == COLLECT_TIMEOUT_S for job in jobs)


# ---------------------------------------------------------------------------
# 2b. The signal must not stay in the session either
# ---------------------------------------------------------------------------


#: The prefix `SessionCollector` puts on the task it submits, quoted here rather than
#: imported so a rename in the product cannot silently change what these tests filter.
TASK_TEXT_PREFIX: Final = "Пользователь попросил голосом: "


def stored_texts(rig: Rig, session_id: str) -> list[str]:
    """Every message text the server still holds for this session, EXCEPT R2D2's own tasks.

    The task is stored as a user message because the real server stores it that way -- and
    because a collector anchored late has to tell what the session held before the hand-off
    from what it held after it, which it can only do if the task is in the transcript. These
    assertions are about the USER's conversation surviving a sweep, and the task is R2D2's
    own text rather than something the user said, so it is filtered here instead of in six
    assertions.
    """
    return [
        str(entry["parts"][0]["text"])
        for entry in rig.server.transcript[session_id]
        if not str(entry["parts"][0]["text"]).startswith(TASK_TEXT_PREFIX)
    ]


async def test_an_escalation_removes_the_routing_signal_from_the_session(rig: Rig) -> None:
    # Given a warm session and a voice agent that escalated
    rig.server.reply = f"Это займёт времени. {SENTINEL} собрать последние статьи про RAG"
    session_id = await rig.warm()
    # When
    assert await rig.say() == ACK
    # Then the signal was deleted from the session, by id, on this very turn.
    # Left in place it is the newest thing `r2d2-agent` reads when the task is
    # submitted microseconds later, and a model shown a token the voice agent was
    # told to emit will emit it too -- which is how one escalation disabled the
    # agent path for good on the live run.
    assert rig.server.deleted == [(session_id, "msg_a1")]
    assert stored_texts(rig, session_id) == [QUESTION]
    assert all(SENTINEL not in text for text in stored_texts(rig, session_id))


async def test_the_sweep_keeps_the_users_own_conversation(rig: Rig) -> None:
    # Given a session with real history in it -- the one-session-per-human model
    # exists so this content survives, and a fix for a poisoned session must not
    # be a fix that empties it
    rig.server.reply = f"Понял. {SENTINEL} собрать сводку"
    session_id = await rig.warm()
    rig.server.seed(
        session_id,
        said("что такое RAG", "msg_old_u", role="user"),
        said("Retrieval augmented generation.", "msg_old_a"),
    )
    # When
    assert await rig.say() == ACK
    # Then every message that was not the protocol is still there, byte for byte
    assert stored_texts(rig, session_id) == [
        "что такое RAG",
        "Retrieval augmented generation.",
        QUESTION,
    ]
    assert [mid for _sid, mid in rig.server.deleted] == ["msg_a1"]


async def test_the_collector_anchors_at_a_message_the_server_still_lists(rig: Rig) -> None:
    # Given an escalation, where the newest message IS the signal being deleted
    rig.server.reply = f"Понял. {SENTINEL} собрать сводку"
    session_id = await rig.warm()
    # When
    assert await rig.say() == ACK
    # Then the anchor is a message that survives the sweep. Anchoring at the
    # message about to be deleted would leave the collector holding an id the
    # server no longer lists, and `core/turn_lease.py` reads an anchor the window
    # cannot position as nothing -- a stated loss, never the whole session.
    job = rig.brain.worker.jobs[0]
    live = {str(entry["info"]["id"]) for entry in rig.server.transcript[session_id]}
    assert job["since_message_id"] == "msg_u1"
    assert job["since_message_id"] in live
    assert job["since_message_id"] not in {mid for _sid, mid in rig.server.deleted}


async def test_the_signal_goes_even_when_summarise_answered_true_and_did_nothing(
    rig: Rig,
) -> None:
    # Given a long session, so `_prepare` calls the server's summariser -- and the
    # server answers `true` and compresses nothing, which is what the live run
    # measured (39 messages, all originals, sentinel still present). A fix that
    # leaned on summarisation would therefore be a fix that does not work.
    rig.server.reply = f"Понял. {SENTINEL} собрать сводку"
    session_id = await rig.warm(count=rig.cfg.r2d2_session_soft_limit + 10)
    # When
    assert await rig.say() == ACK
    # Then the summariser was asked, and the signal was deleted anyway
    assert [sid for sid, _body in rig.server.summarizes] == [session_id]
    assert rig.server.deleted == [(session_id, "msg_a1")]
    assert stored_texts(rig, session_id) == [QUESTION]


async def test_a_session_poisoned_by_an_earlier_turn_is_repaired_on_the_next_escalation(
    rig: Rig,
) -> None:
    # Given a session that already holds a signal -- written by a turn that
    # outran the deadline and landed after R2D2 had stopped looking, which is the
    # one way a signal can still arrive without anybody escalating
    rig.server.reply = f"Понял. {SENTINEL} новая задача"
    session_id = await rig.warm()
    rig.server.seed(
        session_id,
        said("старый вопрос", "msg_old_u", role="user"),
        said(f"{SENTINEL} сигнал прошлого хода", "msg_stale"),
    )
    # When
    assert await rig.say() == ACK
    # Then the sweep is not a one-message patch: it takes out every stored signal,
    # so a session that is already poisoned recovers on its next escalation
    assert [mid for _sid, mid in rig.server.deleted] == ["msg_stale", "msg_a1"]
    assert stored_texts(rig, session_id) == ["старый вопрос", QUESTION]


async def test_a_user_message_carrying_the_token_is_never_deleted(rig: Rig) -> None:
    # Given the owner said the words out loud, so the token is in THEIR message
    asked = f"что значит {SENTINEL}"
    rig.server.reply = f"Понял. {SENTINEL} собрать сводку"
    session_id = await rig.warm()
    # When
    assert await rig.say(asked) == ACK
    # Then only the model's own message was deleted. The role decides, not the
    # string: a user's content is never this module's to remove.
    assert [mid for _sid, mid in rig.server.deleted] == ["msg_a1"]
    assert asked in stored_texts(rig, session_id)


async def test_a_refused_delete_leaves_the_turn_successful_and_says_why(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    # Given a server that will not remove the message
    rig.server.reply = f"Понял. {SENTINEL} собрать сводку"
    rig.server.delete_status = 500
    await rig.warm()
    # When
    with caplog.at_level(logging.WARNING, logger="r2d2.brain"):
        text = await rig.say()
    # Then the user is still acknowledged -- the failure is not theirs to hear --
    # and the reason is in the log, because a session that keeps a signal is a
    # session whose agent may start copying it
    assert text == ACK
    assert rig.server.turns[1].agent == TASK_AGENT
    assert any("routing signal" in record.getMessage() for record in caplog.records)


async def test_a_stored_permission_refusal_is_deleted_on_the_escalating_turn(
    rig: Rig,
) -> None:
    # Given a session that already holds a permission refusal opencode stored for
    # the VOICE agent -- a `tool` part with `state.status == "error"`, the voice
    # matrix spelled out in its `state.error`, and no text part at all. Left in
    # place the other agent reads that matrix as its own and stops calling tools
    # its own matrix allows (D9, measured in qa/live-run-v3.md).
    rig.server.reply = f"Понял. {SENTINEL} скачай страницу"
    session_id = await rig.warm()
    rig.server.seed(
        session_id,
        said("Проверь, работает ли интернет.", "msg_u0", role="user"),
        refused_tool_message("msg_refused"),
        said("Мне запрещено выполня эту команду в терминале.", "msg_prose"),
    )
    # When the user asks for something that escalates
    assert await rig.say() == ACK
    # Then the refusal is deleted BY ID on this very turn, in the same sweep and
    # from the same snapshot that chose the anchor -- and the anchor is a message
    # that survives, so the collector cannot be handed a marker the server has
    # just dropped
    assert [mid for _sid, mid in rig.server.deleted] == ["msg_refused", "msg_a1"]
    job = rig.brain.worker.jobs[0]
    live = {str(entry["info"]["id"]) for entry in rig.server.transcript[session_id]}
    assert job["since_message_id"] == "msg_u1"
    assert job["since_message_id"] in live
    assert job["since_message_id"] not in {mid for _sid, mid in rig.server.deleted}
    # And what the user keeps: the request that was refused and the plain-prose
    # answer the refused agent gave about it. Only the rule dump is gone.
    assert stored_texts(rig, session_id) == [
        "Проверь, работает ли интернет.",
        "Мне запрещено выполня эту команду в терминале.",
        QUESTION,
    ]


async def test_a_successful_tool_turn_in_the_session_is_never_deleted(rig: Rig) -> None:
    # Given a session holding a tool call that COMPLETED -- also stored with no
    # text part, which is what made "no text" unusable as a refusal marker and
    # would take the record of everything that worked if it were used
    rig.server.reply = f"Понял. {SENTINEL} скачай страницу"
    session_id = await rig.warm()
    rig.server.seed(
        session_id,
        said("Скачай страницу.", "msg_u0", role="user"),
        completed_tool_message("msg_worked"),
    )
    # When
    assert await rig.say() == ACK
    # Then only the escalation signal goes; the completed tool call stays, and it
    # is not mistaken for a refusal on the strength of its missing text part
    assert [mid for _sid, mid in rig.server.deleted] == ["msg_a1"]
    assert "msg_worked" in {
        str(entry["info"]["id"]) for entry in rig.server.transcript[session_id]
    }


async def test_a_tool_that_failed_for_its_own_reason_is_never_deleted(rig: Rig) -> None:
    # Given a session holding a `webfetch` that could not resolve: also
    # `state.status == "error"`, and NOT opencode's enforcement state. It is the
    # user's own result, so the status alone cannot be the discriminator.
    rig.server.reply = f"Понял. {SENTINEL} скачай страницу"
    session_id = await rig.warm()
    rig.server.seed(
        session_id,
        said("Скачай страницу.", "msg_u0", role="user"),
        failed_tool_message("msg_netfail"),
    )
    # When
    assert await rig.say() == ACK
    # Then it survives, and the anchor is the user's own message for this turn
    assert [mid for _sid, mid in rig.server.deleted] == ["msg_a1"]
    assert "msg_netfail" in {
        str(entry["info"]["id"]) for entry in rig.server.transcript[session_id]
    }
    assert rig.brain.worker.jobs[0]["since_message_id"] == "msg_u1"


async def test_the_collected_answer_is_the_agents_and_not_a_replay(
    rig: Rig, net: Net
) -> None:
    # Given the real worker with todo 16's branch, and an escalating turn in a
    # session that already holds an earlier answer -- the collector's window has to
    # survive a deletion on the way to being anchored, so it is observed end to end
    # and not only in the job row.
    rig.server.reply = f"Понял. {SENTINEL} собрать сводку"
    session_id = await rig.warm()
    rig.server.seed(
        session_id, said("прошлый вопрос", "msg_old_u", role="user"),
        said("Прошлый ответ.", "msg_old_a"),
    )
    assert await rig.say() == ACK
    job = rig.brain.worker.jobs[0]
    # When the agent's work lands in the session and the job is dispatched for real
    rig.server.seed(session_id, said(AGENT_REPLY, "msg_agent"))
    worker = CollectorWorker(rig.cfg, rig.memory, rig.backend)
    await worker.start()
    try:
        net.delivered.clear()
        await worker.enqueue(job)
        await asyncio.wait_for(net.delivered.wait(), timeout=5.0)
    finally:
        await worker.stop()
    # Then Telegram gets the agent's answer alone. An anchor that pointed at a
    # deleted message is unpositionable, and the collector reads that as
    # "everything is newer" -- the earlier answer and the signal in one message.
    assert net.telegram == [AGENT_REPLY]


# ---------------------------------------------------------------------------
# 3. The permission gate: opencode's ask is answered, and only by the user
# ---------------------------------------------------------------------------


async def store_ask(rig: Rig, app_id: str = USER_ID, title: str = "rm -rf /tmp/x") -> dict:
    """An unanswered `permission.asked`, stored the way the broker stores it."""
    record = {
        "kind": "opencode_permission",
        "session_id": await rig.store.resolve(app_id),
        "permission_id": "per_1",
        "title": title,
        "always": ["rm *"],
        "requested_at": time.time(),
    }
    await rig.memory.set_pending(app_id, record)
    return record


async def test_yes_answers_a_pending_opencode_ask_with_a_one_time_approval(rig: Rig) -> None:
    # Given an opencode ask waiting for this user
    await store_ask(rig)
    # When the user says yes
    text = await rig.say("да")
    # Then the server was told "once" -- never "always" (C5: one "always" grants
    # the whole advertised mask for the rest of the session)
    assert len(rig.server.permission_answers) == 1
    session_id, permission_id, answer = rig.server.permission_answers[0]
    assert answer == {"response": "once"}
    assert permission_id == "per_1"
    assert session_id
    # ... a short phrase is spoken, without the server-controlled title
    assert text != ACK and len(text) <= 60
    assert "rm -rf" not in text
    # ... the record is gone, so the same "да" cannot answer a second time
    assert await rig.memory.get_pending(USER_ID) is None
    # ... and no LLM turn happened at all
    assert rig.server.turns == []


async def test_no_refuses_a_pending_opencode_ask(rig: Rig) -> None:
    # Given / When
    await store_ask(rig)
    text = await rig.say("нет")
    # Then
    assert [answer[2] for answer in rig.server.permission_answers] == [{"response": "reject"}]
    assert await rig.memory.get_pending(USER_ID) is None
    assert rig.server.turns == []
    assert len(text) <= 60


async def test_text_that_is_not_an_answer_leaves_the_ask_pending(rig: Rig) -> None:
    """`unrelated` must change NOTHING -- and with F1 that includes the turn itself.

    The subject here is the reader, not the gate: a message that is not a confirmation
    word must not resolve a pending ask, and this is the one test that proves the
    reader says `unrelated` rather than guessing. F1 changed what the TURN does in
    this state -- a session parked on our own ask now refuses the new question with a
    stated loss instead of submitting it into a session the server calls `busy` (see
    section 3a) -- and the old `text == ANSWER` assertion is the visible consequence.
    The ask is still untouched, which is the property this test is named for.
    """
    # Given an opencode ask, and a user who asks something else entirely
    record = await store_ask(rig)
    await rig.warm()
    rig.server.reply = ANSWER
    # When
    text = await rig.say(QUESTION)
    # Then nothing was posted to the permissions route -- "unrelated" must change
    # nothing -- and the ask is still exactly as it was
    assert rig.server.permission_answers == []
    assert await rig.memory.get_pending(USER_ID) == record
    # And the turn is the F1 stated loss, not a submit into a parked session
    assert text == PARKED
    assert rig.server.turns == []


async def test_the_shell_confirmation_gate_still_runs_its_own_pending_action(rig: Rig) -> None:
    # Given the OTHER feature's pending row: a risky shell command
    await rig.memory.set_pending(
        tool_pending_key(USER_ID), {"tool": "run_shell", "arguments": {"command": "echo r2d2-hybrid-gate-ok"}}
    )
    # When the user confirms
    text = await rig.say("да")
    # Then the command ran and its output is spoken ...
    assert "r2d2-hybrid-gate-ok" in text
    # ... and it did not go through the opencode route at all
    assert rig.server.requests == []
    assert rig.server.permission_answers == []


async def test_a_shell_confirmation_is_not_recorded_as_a_spoken_voice_answer(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    """`voice` means the user HEARD the text, and this branch cannot know that.

    `core/brain.py`'s confirmation gate is reached from two channels: Alice, which
    speaks the shell's output, and `/tg/webhook`, which sends it as a message
    nobody speaks. `voice` is true on the first and false on the second, and a
    value that is right on one channel and wrong on the other is the shape the
    ledger cannot be read for -- the same defect NEW-4 fixed for opencode asks.
    `confirm` claims only what both channels share.
    """
    await rig.memory.set_pending(
        tool_pending_key(USER_ID), {"tool": "run_shell", "arguments": {"command": "echo r2d2-hybrid-gate-ok"}}
    )
    caplog.set_level(logging.INFO)
    text = await rig.say("да")
    assert "r2d2-hybrid-gate-ok" in text
    fields = turn_record(caplog)
    assert fields["path"] == "confirm"
    # ... and no model is named, because none was asked anything: R2D2 ran its own
    # pending action inline, and nothing was posted to opencode's server
    assert fields["model"] == ""
    assert int(fields["llm_ms"]) == 0
    assert rig.server.permission_answers == []


async def test_a_refused_shell_confirmation_is_recorded_as_a_confirmation_too(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    """`нет` takes the same branch as `да`, so it must take the same record.

    Without this, a `confirm` reachable only from the approval arm would say
    confirmations were rare exactly when the user refused one.
    """
    await rig.memory.set_pending(
        tool_pending_key(USER_ID), {"tool": "run_shell", "arguments": {"command": "echo r2d2-hybrid-gate-ok"}}
    )
    caplog.set_level(logging.INFO)
    await rig.say("нет")
    fields = turn_record(caplog)
    assert fields["path"] == "confirm"
    assert fields["model"] == ""


async def test_an_unrelated_reply_is_not_recorded_as_a_confirmation(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    """The opposite false claim, and the one this change nearly shipped.

    `policies.confirmation_verdict` returns `"yes"`, `"no"`, or `None` -- and
    `None` is a real answer meaning "decides nothing", which leaves the pending
    ask exactly where it was. Guarding the new assignment with anything other
    than `is not None` marks every ordinary message that happens to arrive while
    a confirmation is pending as a decision the user never made.

    `помощь` is the witness and not an arbitrary choice. A `None` verdict falls
    *through* the gate, and every path after it overwrites `path` on its way to a
    model -- so an ordinary question would record correctly even with the guard
    broken, and a test built on one cannot fail. The help and greeting branches
    return without setting `path`, so the stale value survives to the record and
    the difference is observable. The first version of this test used a normal
    question and passed against a deliberately broken guard; that is why the
    witness is named here.
    """
    await rig.memory.set_pending(
        tool_pending_key(USER_ID), {"tool": "run_shell", "arguments": {"command": "echo r2d2-hybrid-gate-ok"}}
    )
    caplog.set_level(logging.INFO)
    await rig.say("помощь")
    fields = turn_record(caplog)
    assert fields["path"] == "voice"
    # ... and the pending action is untouched, because nothing was decided
    pending = await rig.memory.get_pending(tool_pending_key(USER_ID))
    assert pending is not None
    assert pending["tool"] == "run_shell"


# ---------------------------------------------------------------------------
# 3a. F1: a second question while the session is parked on our own ask
# ---------------------------------------------------------------------------


async def test_a_question_asked_while_the_session_is_parked_is_refused_not_queued(
    rig: Rig,
) -> None:
    """The turn F1 lost, pinned at the place the decision is made.

    Measured on opencode 1.18.33 (`qa/live-run-v9.md` F1): with one ask
    outstanding the server reports the session `busy`, a question asked then is
    acknowledged inside Alice's budget, and the turn is accepted with 204 and then
    **never served** — the transcript keeps the user message and no `assistant`
    message ever answers it. The live run spent 3.2 s of voice budget, armed a
    collector, and burned its whole 600 s ceiling on that turn before the job was
    superseded. Nothing about that is recoverable from the client side, because
    `GET /session/status` says only `busy` and the queue has no drain route.

    So the invariant is that no task is submitted into a parked session at all, and
    it is asserted on the wire: zero turns, not "the answer arrived late".
    """
    # Given: a WARM session (so the voice path would run and the C8 shortcut is not
    # what is being tested) with an unanswered ask of ours, which is what parks it
    await rig.warm()
    record = await store_ask(rig)
    rig.server.reply = ANSWER
    # When: the user asks something else entirely
    text = await rig.say(QUESTION)
    # Then: NOTHING reached the message route -- no voice turn, no submitted task
    assert rig.server.turns == []
    # And the ask is untouched: the gate refuses THIS turn, it does not answer
    # the question the user is already being asked
    assert rig.server.permission_answers == []
    assert await rig.memory.get_pending(USER_ID) == record
    # And the user is told the truth, in seconds, naming the cause and the remedy
    assert text == PARKED
    assert "не выполнил" in text.lower()
    assert "«да»" in text or "«нет»" in text


# ---------------------------------------------------------------------------
# 3a-bis. NEW-2: a second question while the server calls the session busy
# ---------------------------------------------------------------------------
#
# The sibling F1 refuses a session parked on an ask of OURS, read from our own
# database. This one refuses a session the SERVER reports `busy`, read from
# `GET /session/status` -- the only status value the server reports, and "busy"
# does not mean "queued" (`qa/live-run-v9.md` F1). Measured fifteen times out of
# fifteen on 1.18.33 (`qa/live-run-v12.md`): question A outstanding, question B
# asked, «Проверяю, пришлю в телеграм.» and then nothing at all, because when
# B's submission makes the session busy A's task is never served -- so no answer
# is on its way from anywhere, and the `TurnSuperseded` that ended the turn was
# a correct lease decision resting on a false premise.


async def test_a_question_asked_while_the_server_calls_the_session_busy_is_refused_not_queued(
    rig: Rig,
) -> None:
    """The turn NEW-2 lost, pinned at the place the decision is made.

    Same shape as the F1 test above, for a cause nothing in this process can
    see: the server says `busy` and names no reason, so the refusal cannot name
    one either -- but it still states the loss in seconds instead of promising
    an answer nothing will send. `permission_asked` is False here, and that is
    the load-bearing half: `parked` names a pending ask and this field agrees
    with it, while a `busy` record claiming one would describe an event that
    never happened.
    """
    # Given: a WARM session (so the voice path would run and the C8 shortcut is
    # not what is being tested) that the server reports busy, with NO ask of
    # ours outstanding -- nothing is parked, the server alone says no
    session_id = await rig.warm()
    rig.server.busy.add(session_id)
    rig.server.reply = ANSWER
    # When: the user asks something else entirely
    text = await rig.say(QUESTION)
    # Then: NOTHING reached the message route -- no voice turn, no submitted task
    assert rig.server.turns == []
    # And the user is told the truth, in seconds: a stated loss, not the ack
    assert text == BUSY_REFUSED
    assert "не выполнил" in text.lower()


async def test_a_busy_refusal_is_recorded_as_busy_without_claiming_an_ask(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    """`path=busy` exists so the ledger never reads this turn as `parked`.

    A record reading `parked` here would name a pending ask that the very next
    field (`permission_asked=False`) contradicts -- the one shape of wrong
    `core/metrics.py` exists to prevent.
    """
    # Given: a warm session the server reports busy, and no ask of ours
    session_id = await rig.warm()
    rig.server.busy.add(session_id)
    rig.server.reply = ANSWER
    caplog.set_level(logging.INFO)
    # When
    text = await rig.say(QUESTION)
    # Then: the turn is the NEW-2 refusal ...
    assert text == BUSY_REFUSED
    # ... and the record says busy without claiming an ask, and no model
    fields = turn_record(caplog)
    assert fields["path"] == "busy"
    assert fields["permission_asked"] == "False"
    assert fields["model"] == ""
    assert fields["escalated"] == "False"


async def test_a_first_cold_turn_is_not_caught_by_the_busy_gate(rig: Rig) -> None:
    """The refusal must not widen to a turn that is not a second question.

    A session created seconds ago has nothing running in it, so the server has
    nothing to call busy: the C8 branch still submits the work and acknowledges.
    A `busy` set naming some OTHER session proves the check is per-session
    rather than a global mute -- the cold turn proceeds while that one is busy.
    """
    # Given: a session that is busy, belonging to nobody in this test ...
    rig.server.busy.add("ses_someone_elses")
    rig.server.reply = ANSWER
    # ... and a user whose session does not exist yet (C8: count zero)
    # When: their first turn
    text = await rig.say(QUESTION)
    # Then: the work was submitted to the agent and acknowledged, not refused
    assert text == ACK
    assert [turn for turn in rig.server.turns if turn.submitted] != []


async def test_an_unreadable_session_status_is_not_a_refusal(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    """Not being able to ask is not the same as being told no.

    Refusing on an unreadable answer would state a loss the evidence does not
    support and would turn one slow route into a refusal of every turn,
    including the first cold one. So the turn proceeds as if the session were
    idle -- and the silence is logged, because a slow status route is still
    worth knowing about.
    """
    # Given: a warm session whose status the server will not answer readably
    await rig.warm()
    rig.server.status_broken = True
    rig.server.reply = ANSWER
    caplog.set_level(logging.INFO)
    # When: the user asks
    text = await rig.say(QUESTION)
    # Then: the voice turn ran and its answer was spoken -- no refusal anywhere
    assert text == ANSWER
    assert len(rig.server.turns) == 1
    assert "could not read /session/status" in caplog.text


async def test_a_hand_over_into_a_session_that_turned_busy_states_the_loss_in_telegram(
    rig: Rig,
) -> None:
    """The `_hand_over` gate: F1's shape for a park discovered after the entry check.

    The entry gate already refused sessions that were busy on arrival; this one
    fires when the session turned busy between that check and the submit -- the
    same second gate the parked case has. The voice channel keeps the ack it
    already promised (returning anything else here would rewrite history), and
    the stated loss goes to the user's own chat through `_say`.
    """
    # Given: a warm session that turns busy after the route's own entry check
    session_id = await rig.warm()
    rig.server.busy.add(session_id)
    rig.net.telegram.clear()
    # When: the hand-off runs against that session
    assert rig.brain.opencode is not None
    text, escalated = await rig.brain.session.collector.hand_to_agent(
        rig.brain.opencode, USER_ID, session_id, QUESTION
    )
    # Then: nothing was submitted into the busy session ...
    assert [turn for turn in rig.server.turns if turn.submitted] == []
    # ... the voice channel keeps the ack ...
    assert (text, escalated) == (ACK, False)
    # ... and the stated loss reached the user's own chat
    assert rig.net.telegram == [BUSY_REFUSED]


# ---------------------------------------------------------------------------
# 3b. The record of a turn a permission ask touched (NEW-3, NEW-4)
# ---------------------------------------------------------------------------
#
# `core/metrics.py` shipped `permission_asked` in `TURN_FORMAT` and NOTHING ever
# set it, and every `да` typed in Telegram was recorded as `path=voice` -- the
# vocabulary for "the user heard this spoken aloud", for an answer that is
# delivered as a message nobody speaks. Both were found by a live run and both are
# the same defect: a record that does not describe what happened.
#
# The field CANNOT be set when the turn opens. The frame arrives on the opencode
# event stream in another task at a moment no caller can predict, so the tests
# below pin it at the three places a turn can honestly know, and pin `False` at the
# places that provably cannot see one -- a flag that reads True because nobody
# thought about it is the same false claim with the opposite sign.


def turn_record(caplog: pytest.LogCaptureFixture) -> dict[str, str]:
    """The `field=value` pairs of the LAST turn record the capture holds.

    The value class is `\\S*` and not `\\S+`: `model=` is EMPTY on the two paths where
    no model was asked anything (`parked` and `permission`), and a parser that cannot
    see an empty value drops the key entirely -- which would hide precisely the
    claims these tests are about.
    """
    records = [
        dict(re.findall(r"(\w+)=(\S*)", record.getMessage()))
        for record in caplog.records
        if record.getMessage().startswith("turn route=")
    ]
    assert records, [r.getMessage() for r in caplog.records]
    return records[-1]


async def test_a_turn_refused_because_the_session_is_parked_records_the_ask(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    """`permission_asked=False` on a turn that WAS refused for an ask is the defect.

    This is the one branch where the fact is already in hand -- `parked()` is what
    refused the turn -- so a flag that reads False here cannot be excused as a race:
    the record contradicts the branch two lines above it. A `False` here means the
    ledger cannot be read for "how often does a park refuse a turn", which is the
    only number the field exists for.
    """
    # Given: a warm session with an ask of ours still unanswered
    await rig.warm()
    await store_ask(rig)
    caplog.set_level(logging.INFO)
    # When: the user asks something else while the session is parked
    text = await rig.say(QUESTION)
    # Then: the turn is the F1 refusal ...
    assert text == PARKED
    # ... and the record says the ask touched it, rather than nothing having happened
    fields = turn_record(caplog)
    assert fields["path"] == "parked"
    assert fields["permission_asked"] == "True"
    # ... with no model claimed, because none was asked anything
    assert fields["model"] == ""
    assert int(fields["llm_ms"]) == 0


async def test_a_permission_answer_is_not_recorded_as_a_spoken_voice_answer(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    """A `да` delivered as a Telegram message is not something the user heard.

    Measured: every such turn logged `turn route= path=voice model= agent=
    llm_ms=0 total_ms=35`. `voice` claims aloud a text nobody spoke, and the whole
    ledger is read through that field -- so `permission` is its own value, and the
    record must also stop claiming a route and a model that were never involved.
    """
    # Given: an opencode ask waiting for this user, and the user answering it
    await store_ask(rig)
    caplog.set_level(logging.INFO)
    text = await rig.say("да")
    # Then: the turn is the ANSWER, which `voice` denies and `escalate` also denies
    assert text and text != ACK
    fields = turn_record(caplog)
    assert fields["path"] == "permission"
    assert fields["permission_asked"] == "True"
    # ... the route is the opencode session route that answered it ...
    assert fields["route"] == "opencode"
    # ... and nothing was sent to a model, so no model is named and the turn is
    # not an escalation: nothing moved to the agent, this turn is what released it
    assert fields["model"] == ""
    assert fields["escalated"] == "False"
    assert int(fields["llm_ms"]) == 0
    assert int(fields["msgs"]) == 0
    assert fields["tools"] == "()"


async def test_a_refusal_is_recorded_the_same_way_an_approval_is(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    """`нет` takes the same branch as `да`, so it must take the same record.

    Without this, a `path=permission` reachable only from the approval arm is a
    second "the field the ledger cannot be read for": a ledger that says permission
    answers were rare exactly when the user refused one.
    """
    await store_ask(rig)
    caplog.set_level(logging.INFO)
    await rig.say("нет")
    fields = turn_record(caplog)
    assert fields["path"] == "permission"
    assert fields["permission_asked"] == "True"


async def test_a_voice_turn_that_outran_the_budget_on_a_permission_ask_says_so(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    """The one place an ask can appear while a turn is in flight, and it is read there.

    The shape the live run measured: the voice agent reaches for a tool, opencode
    raises `permission.asked` and blocks that turn, the question goes to the user,
    and R2D2 gives up waiting at the deadline -- so the ask and the deadline belong
    to the SAME turn, and a record that cannot say so is a turn that reads as an
    ordinary slow one. `parked()` returned False at the entry of this turn (the ask
    did not exist yet), which is exactly why the branch re-reads it after the last
    await instead of trusting the check it already made.
    """
    # Given: a warm session and a voice turn that is ACCEPTED and never answers
    session_id = await rig.warm()
    rig.server.hang_turn = True
    # A deadline generous enough that one SQLite write inside it is not a coin flip:
    # the shipped value is 3.3 s and this is the same field, read by the same code.
    rig.cfg.r2d2_fast_deadline = 0.5
    broker = rig.brain.opencode.broker
    assert broker is not None
    hung = asyncio.Event()
    rig.server.on_hung_turn = hung.set

    async def the_server_raises_an_ask() -> None:
        await hung.wait()
        await broker.on_permission_requested(USER_ID, session_id, "per_deadline", "ls -la", [])

    raised = asyncio.create_task(the_server_raises_an_ask())
    caplog.set_level(logging.INFO)
    try:
        text = await rig.say(QUESTION)
    finally:
        await raised
    # Then: the user is acknowledged -- a deadline is not an abort ...
    assert text == ACK
    assert rig.server.aborted == []
    # ... the question really did reach the user's chat ...
    assert any("ls -la" in message for message in rig.net.telegram)
    assert await rig.memory.get_pending(USER_ID) is not None
    # ... and the record says the turn was stopped on an ask, which is what happened
    fields = turn_record(caplog)
    assert fields["path"] == "deadline"
    assert fields["permission_asked"] == "True"
    assert int(fields["llm_ms"]) > 0


async def test_a_deadline_turn_with_no_ask_records_no_ask(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    """The other half of the flag: it must not be True because a check exists.

    A flag that is set whenever the code bothers to look is as false as one that is
    never set -- it just fails in the direction nobody notices. A slow voice turn
    with nothing parked is the common case, and it must read `False`.
    """
    await rig.warm()
    rig.server.hang_turn = True
    caplog.set_level(logging.INFO)
    assert await rig.say(QUESTION) == ACK
    fields = turn_record(caplog)
    assert fields["path"] == "deadline"
    assert fields["permission_asked"] == "False"


async def test_a_turn_spoken_in_place_records_no_ask(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    """A turn that ended with text cannot have been parked: a blocked turn answers none.

    This is the derivation the module docstring claims for the fast path, asserted
    where the claim is made. It is the shape that proves `permission_asked` is a
    measurement and not a mood: with the flag True-by-default this would pass, and
    with it True-by-paranoia so would the test above.
    """
    await rig.warm()
    rig.server.reply = ANSWER
    caplog.set_level(logging.INFO)
    assert await rig.say(QUESTION) == ANSWER
    fields = turn_record(caplog)
    assert fields["path"] == "voice"
    assert fields["permission_asked"] == "False"


async def test_a_permission_answer_leaves_no_trace_on_a_later_spoken_turn(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    """The flag is per TURN, not a latch the recorder forgot to clear.

    `permission_asked` lives on one `Turn`, but nothing stops a future branch from
    setting it on the wrong one, and a latch would show up as a whole session of
    voice turns claiming to have been parked -- which would be worse than the
    constant False it replaced, because it would look like a working metric.
    """
    await store_ask(rig)
    await rig.say("да")
    await rig.warm()
    rig.server.reply = ANSWER
    caplog.set_level(logging.INFO)
    assert await rig.say(QUESTION) == ANSWER
    assert turn_record(caplog)["permission_asked"] == "False"


async def test_the_park_refusal_does_not_wedge_the_ask_it_refused_over(rig: Rig) -> None:
    """A gate that also blocks the answer would trade a lost turn for a lost session.

    The refusal is scoped to the turn that arrived while the session was parked. The
    ask itself stays answerable, which is the only thing that makes the remedy in
    the refusal text real: the user is told to answer «да»/«нет» and ask again, and
    that instruction has to work.
    """
    # Given: the parked session from the refusal above, still holding its ask
    await rig.warm()
    await store_ask(rig)
    await rig.say(QUESTION)
    # When: the user answers the ORIGINAL question
    text = await rig.say("да")
    # Then: the ask was approved, and the pending row is gone
    assert [answer[2] for answer in rig.server.permission_answers] == [{"response": "once"}]
    assert await rig.memory.get_pending(tool_pending_key(USER_ID)) is None
    assert len(text) <= 60


async def test_the_park_gate_closes_once_the_ask_is_answered(rig: Rig) -> None:
    """The gate reads the ROW, so an answered ask stops refusing turns.

    Without this, a session that had ever parked would refuse every turn for the
    rest of the process's life — the same wedge, one answer later.
    """
    # Given: a parked session whose ask is then answered
    await rig.warm()
    record = await store_ask(rig)
    await rig.say("да")
    assert await rig.memory.get_pending(tool_pending_key(USER_ID)) is None
    # When: the user asks a normal question, answered in place
    rig.server.reply = ANSWER
    text = await rig.say(QUESTION)
    # Then: the turn is served again, in place, with no escalation and no re-ask
    assert text == ANSWER
    assert [turn.agent for turn in rig.server.turns] == [VOICE_AGENT]
    assert not [turn for turn in rig.server.turns if turn.submitted]
    assert [answer[2] for answer in rig.server.permission_answers] == [{"response": "once"}]
    assert record["permission_id"] == "per_1"


async def test_an_ask_in_another_users_session_does_not_park_this_one(rig: Rig) -> None:
    """`parked` is per (application_id, session_id): the row is keyed by the user.

    The two halves have to match, and matching only the user would make a stranger's
    ask park this user's session; matching only the session would make the check
    unreadable, because the row is stored per user. This is the direction that is
    wrong in silence, so it is pinned: `OTHER_USER` has its own session, and its ask
    must not reach across.
    """
    # Given: a warm session for this user and an ask parked on a DIFFERENT user's session
    await rig.warm()
    other_session = await rig.store.resolve(OTHER_USER)
    assert other_session
    await rig.memory.set_pending(
        OTHER_USER,
        {
            "kind": "opencode_permission",
            "session_id": other_session,
            "permission_id": "per_other",
            "title": "rm -rf /tmp/other",
            "always": [],
            "requested_at": time.time(),
        },
    )
    rig.server.reply = ANSWER
    # When: THIS user asks a question
    text = await rig.say(QUESTION)
    # Then: it is served normally, because the parked session is not this one
    assert text == ANSWER
    assert [turn.agent for turn in rig.server.turns] == [VOICE_AGENT]


async def test_a_deployment_with_no_broker_still_escalates(core_brain_no_broker) -> None:
    """A gate that refuses when it cannot tell is an outage with a security rationale.

    `EVENT_MODE` unknown, or a deployment that wired no broker, is a supported
    configuration. The park is a fact R2D2 itself created, so a build that brokered
    nothing created none — and the honest reading there is `False`, not a refusal
    nobody asked for.
    """
    rig = core_brain_no_broker
    # Given: a warm session and NO broker at all
    await rig.warm()
    rig.server.reply = SENTINEL
    # When: the user asks for real work
    await rig.say(QUESTION)
    # Then: the turn reached the agent, exactly as it did before the gate existed
    assert [turn.agent for turn in rig.server.turns if turn.submitted] == [TASK_AGENT]


# ---------------------------------------------------------------------------
# 4. Availability and the fallback chain
# ---------------------------------------------------------------------------


async def test_a_question_still_gets_an_answer_when_the_opencode_server_is_down(
    rig: Rig,
) -> None:
    # Given no opencode server at all
    rig.server.down = True
    # When
    text = await rig.say()
    # Then the fallback chain answered
    assert text == FALLBACK_ANSWER
    assert rig.net.zen_bodies
    # ... and the opencode session's transcript was left alone
    assert rig.server.turns == []


async def test_a_200_carrying_an_inner_error_is_a_failure_and_never_an_answer(rig: Rig) -> None:
    # Given the refusal the spike measured: HTTP 200 with an inner 403 FreeTierError
    rig.server.turn_error = FREE_TIER_ERROR
    await rig.warm()
    # When
    payload = await rig.ask()
    text = str(payload["response"]["text"])
    # Then the fallback chain answered, and the refusal is not what the user hears
    assert text == FALLBACK_ANSWER
    assert "free tier" not in text.lower()
    assert "403" not in text
    assert "APIError" not in text
    # ... and the session backend was tried exactly ONCE: the fallback chain must
    # not spend the budget again on the host that just refused
    assert rig.server.route_count("POST", "/message") == 1


async def test_the_fallback_chain_moves_on_to_the_next_backend(rig: Rig) -> None:
    # Given the first fallback refuses
    rig.server.down = True
    rig.net.zen_status = 500
    # When
    text = await rig.say()
    # Then the second one answers
    assert text == SECOND_FALLBACK_ANSWER
    assert rig.net.zen_bodies and rig.net.openrouter_bodies
    # ... and the opencode session backend is never a member of the fallback chain
    assert rig.server.route_count("POST", "/message") == 0


async def test_a_dangerous_context_markup_adds_the_system_note_to_the_fallback_turn(
    rig: Rig,
) -> None:
    # Given Alice marked the request as potentially dangerous
    rig.server.down = True
    # When
    await rig.ask(QUESTION, markup={"dangerous_context": True})
    # Then the fallback LLM was told, in the messages, not in a log line
    messages = rig.net.zen_bodies[0]["messages"]
    assert isinstance(messages, list)
    assert any(
        "потенциально опасный" in str(entry.get("content", "")) for entry in messages
    )
    assert messages[0]["role"] == "system"


async def test_every_backend_failing_still_returns_valid_alice_json(rig: Rig) -> None:
    # Given nothing answers at all
    rig.server.down = True
    rig.net.zen_status = 500
    rig.net.openrouter_status = 500
    # When
    payload = await rig.ask()
    # Then the webhook still answers with the graceful text, not a 500 and not a
    # stack trace: the platform retries a non-2xx and the user hears nothing
    assert payload["response"]["text"] == ERROR_TEXT
    assert payload["response"]["end_session"] is False
    assert payload["version"] == "1.0"
    assert len(payload["response"]["text"]) <= MAX_TEXT
    assert "Traceback" not in payload["response"]["text"]


# ---------------------------------------------------------------------------
# 5. C8: the cold first turn never sits on the voice path
# ---------------------------------------------------------------------------


async def test_a_brand_new_session_skips_the_synchronous_voice_turn(rig: Rig) -> None:
    # Given a user who has never asked anything: the session is created now and
    # its first message costs 15.5-18.6s (C8), which no 4.5s budget survives
    # When
    text = await rig.say()
    # Then the ack is returned immediately ...
    assert text == ACK
    # ... the session was created ...
    assert rig.server.route_count("POST", "/session") == 1
    assert list(rig.server.sessions.values())[0]["title"] == title_for(USER_ID)
    # ... and NOT ONE blocking voice turn was issued
    assert rig.server.route_count("POST", "/message") == 0
    # ... the request went to the agent instead, submitted and not awaited
    assert rig.server.route_count("POST", "/prompt_async") == 1
    assert rig.server.turns[0].agent == TASK_AGENT


async def test_the_second_turn_of_a_new_user_answers_synchronously(rig: Rig) -> None:
    # Given the first turn took the C8 path and warmed the session
    assert await rig.say() == ACK
    rig.server.reply = ANSWER
    # When the same user asks again
    text = await rig.say()
    # Then it is answered inside the budget, with no second session
    assert text == ANSWER
    assert rig.server.route_count("POST", "/session") == 1
    assert rig.server.route_count("POST", "/message") == 1


async def test_a_cold_first_turn_enqueues_the_collector_that_will_ship_the_answer(
    rig: Rig,
) -> None:
    # Given a user R2D2 has never asked anything, so the turn takes the C8 branch
    session_id = await rig.store.resolve(USER_ID)
    # When
    text = await rig.say("собери последние статьи про RAG")
    # Then the ack is spoken, and the work is with the agent ...
    assert text == ACK
    assert [turn.agent for turn in rig.server.turns if turn.submitted] == [TASK_AGENT]
    # ... and EXACTLY ONE collector is armed for it. Without one the agent researches
    # for as long as it likes and the user receives nothing: the live run did exactly
    # that, and nothing else in the system would ever read the answer back out.
    jobs = [job for job in rig.brain.worker.jobs if job["type"] == JOB_TYPE]
    assert len(jobs) == 1
    job = jobs[0]
    assert set(job) == JOB_FIELDS
    assert (job["application_id"], job["session_id"]) == (USER_ID, session_id)
    # ... anchored on an empty session: the marker is `""`, which the collector reads
    # as "everything the session ever holds is newer than this", the truth for a
    # session that was created microseconds ago
    assert job["since_message_id"] == ""
    assert job["timeout_s"] == COLLECT_TIMEOUT_S


# ---------------------------------------------------------------------------
# 6. A deadline is not an abort
# ---------------------------------------------------------------------------


async def test_a_deadline_exceeded_voice_turn_is_acknowledged_and_never_aborted(
    rig: Rig,
) -> None:
    # Given a voice turn that outruns the deadline -- the turn is still running
    # server-side, and C8 says the cold cache can take 18s
    rig.server.hang_turn = True
    await rig.warm()
    # When
    text = await rig.say()
    # Then the user is acknowledged rather than left waiting
    assert text == ACK
    # ... and the running turn was NOT aborted: aborting would destroy work the
    # user already paid for and the collector is there to fetch it
    assert rig.server.aborted == []
    assert rig.server.route_count("POST", "/abort") == 0


async def test_a_deadline_turn_is_handed_to_the_worker_as_an_opencode_reply_job(
    rig: Rig,
) -> None:
    # Given / When
    rig.server.hang_turn = True
    session_id = await rig.warm()
    rig.server.seed(session_id, said("предыдущий вопрос", "msg_old", role="user"))
    await rig.say()
    # The job exists on the OTHER side of the settled wait now -- off Alice's clock, which
    # is the whole of the fix -- so the test waits for the job rather than for a duration.
    job = await _armed_job(rig)
    # Then exactly one job was enqueued, of the type todo 16 dispatches ...
    assert rig.brain.worker.jobs == [job]
    assert job["type"] == JOB_TYPE
    assert set(job) == JOB_FIELDS
    # ... carrying everything the collector needs and nothing it does not
    assert job["session_id"] == session_id
    assert job["application_id"] == USER_ID
    # ... anchored to the last message BEFORE this turn, so the collector can
    # only return text produced after it and never replays the whole session
    assert job["since_message_id"] == rig.server.hung_ids[0]
    # ... and NOT the agent message that lands afterwards, or the collector
    # would skip the very reply it exists to fetch
    assert job["since_message_id"] != "msg_agent"
    assert job["timeout_s"] == COLLECT_TIMEOUT_S


async def test_the_collected_turn_ships_the_agents_own_text_to_telegram(
    rig: Rig, net: Net
) -> None:
    # Given the real worker with todo 16's branch, and a deadline-exceeded turn
    rig.server.hang_turn = True
    session_id = await rig.warm()
    rig.server.seed(session_id, said("предыдущий вопрос", "msg_old", role="user"))
    await rig.say()
    job = await _armed_job(rig)
    worker = CollectorWorker(rig.cfg, rig.memory, rig.backend)
    await worker.start()
    try:
        # When the job todo 16 will dispatch is enqueued for real
        net.delivered.clear()
        await worker.enqueue(job)
        # Then the agent's own text is what reaches the user's Telegram
        await asyncio.wait_for(net.delivered.wait(), timeout=5.0)
    finally:
        await worker.stop()
    assert net.telegram == [AGENT_REPLY]
    assert job["session_id"] == session_id


async def test_a_re_verification_that_timed_out_does_not_lose_the_answer(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """**D12, second half.** The turn the deadline classification was for.

    `OcSessionStore.resolve` re-checks the binding against `GET /session` on every turn,
    because a binding the server has never heard of is a wedge. Right after an `opencode
    serve` restart that read timed out against a server that was healthy -- the health probe
    in the same turn answered 200, and the list of every session the server holds is a big
    answer -- and the turn fell through to the fallback chain, which was empty of working
    members that day, so the user heard the graceful error text with a working brain behind
    it.

    The transport now types that read as `OpencodeDeadlineExceeded`
    (`tests/test_opencode_client.py`), and this is where the type earns its keep: a deadline
    on the re-verification is a statement about the server's SPEED, not about the binding, so
    the turn continues on the bound session. A session the server really had dropped answers
    the voice turn 404, which the brain already falls back from -- the direction of this
    tolerance is the safe one.
    """
    # Given: a warm, bound session whose FIRST re-verification cannot complete in time
    session_id = await rig.warm()
    asked_once = False

    async def slow_first_time(app_id: str) -> str:
        nonlocal asked_once
        if not asked_once:
            asked_once = True
            raise OpencodeDeadlineExceeded("opencode: GET /session did not answer within 3.2s")
        return await real_resolve(app_id)

    real_resolve = rig.store.resolve
    monkeypatch.setattr(rig.store, "resolve", slow_first_time)
    # When
    with caplog.at_level(logging.WARNING, logger="r2d2"):
        spoken = await rig.say()
    # Then: the user is answered, from the very session the database claims ...
    assert spoken == ANSWER, "a slow re-verification cost the user the answer"
    assert [turn.session_id for turn in rig.server.turns] == [session_id]
    # ... and the reason it fell back to the claim is on record, with the reason it timed out
    assert "did not re-verify it in time" in caplog.text
    assert "did not answer within" in caplog.text


async def test_a_re_verification_that_stays_slow_leaves_the_answer_a_deadline_not_an_error(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same read failing again inside the voice turn: a deadline, not a lost turn.

    `complete()` resolves the session itself, so a server that is slow twice hands the
    deadline branch a turn that cannot be answered in place. That branch is the right one:
    the user is acknowledged, the work is submitted to the agent, and the collector ships the
    answer to Telegram. What D12 removed is the *other* outcome, the graceful error text
    from an empty chain against a brain that was working the whole time.
    """
    # Given: a warm, bound session whose re-verification never completes in time
    await rig.warm()

    async def always_slow(_app_id: str) -> str:
        raise OpencodeDeadlineExceeded("opencode: GET /session did not answer within 3.2s")

    monkeypatch.setattr(rig.store, "resolve", always_slow)
    # When
    spoken = await rig.say()
    # Then: acknowledged, not the graceful error, and a collector is armed for the answer
    assert spoken == ACK
    assert spoken != ERROR_TEXT
    job = await _armed_job(rig)
    assert [job["type"] for job in [job]] == [JOB_TYPE]
    assert job["application_id"] == USER_ID


async def test_a_re_verification_deadline_with_no_binding_still_falls_back(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other side of the same tolerance: with nothing to fall back ON, the chain answers.

    A brand-new user has no binding, so "trust the claim" would mean inventing a session.
    The turn then does what it always did on a transport failure -- the chain answers --
    which is the supported degradation rather than a new one.
    """
    # Given: this person on an app R2D2 has never bound, and a re-verification
    # that cannot complete. The identity is unchanged -- only the app is new, which
    # is what makes this the same human reaching the gateway somewhere else.
    async def always_slow(_app_id: str) -> str:
        raise OpencodeDeadlineExceeded("opencode: GET /session did not answer within 3.2s")

    monkeypatch.setattr(rig.store, "resolve", always_slow)
    # When
    spoken = await rig.say(app_id="never-seen")
    # Then
    assert spoken == FALLBACK_ANSWER


async def test_a_turn_that_outran_the_deadline_still_gives_the_agent_the_request(
    rig: Rig,
) -> None:
    # Given a voice turn that outruns the deadline. It is still running server-side
    # and will answer one way or the other -- an answer, or the escalation marker --
    # and at the moment the deadline fires R2D2 cannot know which.
    rig.server.hang_turn = True
    session_id = await rig.warm()
    asked = "открой браузер на ноутбуке"
    # When
    text = await rig.say(asked)
    # Then the user is acknowledged rather than left waiting ...
    assert text == ACK
    # ... the voice turn was NOT aborted: the work it started is not thrown away ...
    assert rig.server.aborted == []
    # ... and the AGENT was given the user's own words anyway. Guessing that the
    # late turn will answer is what dropped the request on the floor: on the live
    # run six laptop-control attempts all overran the voice deadline and the agent saw none
    # of them, so the work existed nowhere at all. The submit is off Alice's clock
    # now -- it waits for that voice turn to stop writing -- so the test waits for
    # the collector that proves it happened.
    await _armed_job(rig)
    submitted = [turn for turn in rig.server.turns if turn.submitted]
    assert len(submitted) == 1, rig.server.turns
    assert submitted[0].agent == TASK_AGENT
    assert submitted[0].session_id == session_id
    assert submitted[0].text == f"Пользователь попросил голосом: {asked}"
    # ... with the SAME single collector the branch already armed, so the agent's
    # answer is still delivered exactly once -- and its window now begins where that
    # voice turn stopped writing, so the voice turn's own late answer is context for
    # the agent rather than a second half of the same delivery
    jobs = [job for job in rig.brain.worker.jobs if job["type"] == JOB_TYPE]
    assert len(jobs) == 1
    assert jobs[0]["session_id"] == session_id


# ---------------------------------------------------------------------------
# 6b. No agent turn may begin while a voice turn for that session is still in flight
# ---------------------------------------------------------------------------


def _watched(rig: Rig) -> TurnWatch:
    """The rig's wiring with a real event reader attached, and fed by hand.

    The fixture builds the wiring with no `TurnWatch`, which is the shape the settled
    wait degrades into. "Wait for the voice turn to end" is only measurable from the
    stream, so these tests wire one and note real frames into it -- which is exactly
    what `app/opencode_route.py`'s per-session reader does with what the server sends.
    """
    turns = TurnWatch()
    rig.brain.opencode = replace(rig.brain.opencode, turns=turns)
    return turns


def _idle(turns: TurnWatch, session_id: str) -> None:
    """One `session.idle` for `session_id`, verbatim off the wire (C5, U4)."""
    turns.note(OpencodeEvent(type=TURN_COMPLETE, properties={"sessionID": session_id}))


def _order(rig: Rig) -> dict[str, int]:
    """Where each opencode write landed, so "before" and "after" can be asserted."""
    seen: dict[str, int] = {}
    for index, request in enumerate(rig.server.requests):
        method, path = request.method, request.url.path
        if method == "POST" and path.endswith("/prompt_async"):
            seen.setdefault("submit", index)
        elif method == "DELETE" and "/message/" in path:
            seen.setdefault("delete", index)
    return seen


def _live_ids(rig: Rig, session_id: str) -> set[str]:
    """The ids the server still lists: what a collector's anchor has to be one of."""
    return {str(entry["info"]["id"]) for entry in rig.server.transcript[session_id]}


def _deleted_ids(rig: Rig) -> set[str]:
    """Every id R2D2 asked the server to delete, in any session."""
    return {message_id for _session, message_id in rig.server.deleted}


async def test_no_agent_turn_starts_while_the_voice_turn_that_outran_the_budget_is_running(
    rig: Rig,
) -> None:
    """**The race.** Two turns in one session at once is what killed the agent for a user.

    The deadline branch used to submit the agent's task inside the request, so `r2d2-agent`
    began while the still-running voice turn was writing -- and whatever that turn stored
    landed behind the sweep, which the agent then read as its own history. Measured on the
    live run (`qa/live-run-v6.md` §D9): the residue was a stored tool refusal whose
    `state.error` enumerates the refused agent's whole permission matrix, and the agent
    spent five consecutive turns refusing `echo`, `ls`, `r2d2_do shell` and `read` -- four
    commands its own matrix permits. Deleting that record did not undo it, because the prose
    the refusing agent wrote in its place is conversation and is never swept.
    """
    # Given a session with a reader attached, and a voice turn that outruns the budget
    turns = _watched(rig)
    session_id = await rig.warm()
    _idle(turns, session_id)  # the reader is attached before the wait begins
    rig.server.hang_turn = True
    rig.cfg.r2d2_fast_deadline = DEADLINE_S
    # When the user is released
    assert await rig.say() == ACK
    # Then: no task, and no collector, while that voice turn is still running
    assert [turn for turn in rig.server.turns if turn.submitted] == [], rig.server.turns
    assert rig.brain.worker.jobs == []
    # ... and the still-running turn lands: a tool refusal carrying the matrix of the
    # agent that was refused, which is exactly what the sweep used to miss
    rig.server.hang_turn = False
    rig.server.seed(session_id, refused_tool_message("msg_voice_refusal"))
    # ... and the server reports that the turn is over
    _idle(turns, session_id)
    job = await _armed_job(rig)
    # Then the request reached the agent, the residue is gone, and it went BEFORE the
    # submit -- an agent asked to work in a session that still holds the refusal reads
    # the refused agent's matrix as its own, and that is the whole defect
    order = _order(rig)
    assert "msg_voice_refusal" in [message_id for _session, message_id in rig.server.deleted]
    assert order["delete"] < order["submit"], order
    assert job["since_message_id"] not in _deleted_ids(rig)
    assert job["since_message_id"] in _live_ids(rig, session_id)


async def test_the_marker_a_deadline_turn_leaves_is_swept_before_that_turns_collector_anchors(
    rig: Rig, net: Net
) -> None:
    """**D6-residue, in its new shape.** The echo can no longer be shipped as an answer.

    On the deadline branch the voice turn wrote `[[NEEDS_AGENT]]` after R2D2 had stopped
    looking, the agent read it as its own history and wrote one itself, and that echo was
    the first message after the anchor -- so the collector shipped the user a stripped copy
    of their own question instead of the result (`qa/live-run-v3.md` §3). The hand-off now
    reads its snapshot after that turn has ended, so the marker is in it, goes out, and the
    anchor is a message the server still lists.
    """
    # Given a voice turn that outran the budget and then wrote the marker anyway
    turns = _watched(rig)
    session_id = await rig.warm()
    _idle(turns, session_id)
    rig.server.hang_turn = True
    rig.cfg.r2d2_fast_deadline = DEADLINE_S
    assert await rig.say() == ACK
    rig.server.hang_turn = False
    rig.server.seed(session_id, said(f"Понял. {SENTINEL} собрать сводку", "msg_late_signal"))
    _idle(turns, session_id)
    # When the collector is armed
    job = await _armed_job(rig)
    # Then the marker is gone, and the anchor is not it and is not anything deleted
    assert "msg_late_signal" in [message_id for _session, message_id in rig.server.deleted]
    assert job["since_message_id"] != "msg_late_signal"
    assert job["since_message_id"] in _live_ids(rig, session_id)
    # ... and what the user reads in Telegram is the agent's answer, never the echo
    worker = CollectorWorker(rig.cfg, rig.memory, rig.backend)
    await worker.start()
    try:
        net.delivered.clear()
        await worker.enqueue(job)
        assert await asyncio.wait_for(net.delivered.wait(), timeout=5.0)
    finally:
        await worker.stop()
    assert net.telegram == [AGENT_REPLY]


async def test_a_deadline_turn_that_outlives_its_wait_is_still_handed_over_and_swept_next_turn(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bound is a bound and not a promise, so the backstop is what covers the rest.

    A voice turn that is still running after `SETTLE_TIMEOUT_S` gets the old ordering: the
    request is submitted and the collector is armed, but the hand-off's snapshot cannot be
    known to hold everything that turn will write. What distinguishes that turn is the
    **debt** -- it is left standing, so the entry of the next turn still sweeps, which is
    what turns a missing ordering guarantee into a delay rather than a loss. The wait is
    shortened to what a test can pay; the branch is the one that matters.
    """
    # Given a wait that cannot be satisfied
    monkeypatch.setattr(settle_module, "SETTLE_TIMEOUT_S", 0.1)
    turns = _watched(rig)
    session_id = await rig.warm()
    _idle(turns, session_id)
    rig.server.hang_turn = True
    rig.cfg.r2d2_fast_deadline = DEADLINE_S
    assert await rig.say() == ACK
    # ... the voice turn still writing a residue
    rig.server.hang_turn = False
    rig.server.seed(session_id, refused_tool_message("msg_after_the_bound"))
    # When the bound elapses
    job = await _armed_job(rig)
    # Then the work still reached the agent, exactly once
    assert [turn.agent for turn in rig.server.turns if turn.submitted] == [TASK_AGENT]
    assert job["session_id"] == session_id
    # ... and the next turn still pays the residue read, because nothing observed the end
    rig.cfg.r2d2_fast_deadline = 5.0
    rig.server.reply = ANSWER
    reads = rig.server.route_count("GET", "/message")
    assert await rig.say() == ANSWER
    assert rig.server.route_count("GET", "/message") == reads + 1
    assert "msg_after_the_bound" in _deleted_ids(rig)
    assert session_id


async def test_a_deadline_turn_whose_end_was_observed_leaves_no_residue_debt_behind(
    rig: Rig,
) -> None:
    """The converse, and the reason the wait is worth anything: the next turn pays nothing.

    Once the hand-off's own read has run after the voice turn was SEEN to end, the residue
    that turn owed is gone and the debt is written off, so `sweep_residue` costs the next
    turn one set lookup and no request -- which is the whole trade the deadline branch makes:
    a bounded wait off Alice's clock, paid for by the turn after it, never by the speaker.
    """
    # Given a deadline turn whose end the reader reports
    turns = _watched(rig)
    session_id = await rig.warm()
    _idle(turns, session_id)
    rig.server.hang_turn = True
    rig.cfg.r2d2_fast_deadline = DEADLINE_S
    assert await rig.say() == ACK
    rig.server.hang_turn = False
    rig.server.seed(session_id, refused_tool_message("msg_swept_by_the_handover"))
    _idle(turns, session_id)
    await _armed_job(rig)
    assert "msg_swept_by_the_handover" in _deleted_ids(rig)
    # When the NEXT turn for that user runs, on the fast path
    rig.cfg.r2d2_fast_deadline = 5.0
    rig.server.reply = ANSWER
    reads = rig.server.route_count("GET", "/message")
    # Then it is answered in place and reads nothing: there is no debt left
    assert await rig.say() == ANSWER
    assert rig.server.route_count("GET", "/message") == reads
    assert session_id


async def test_a_deadline_branch_without_an_event_reader_still_hands_the_request_over(
    rig: Rig,
) -> None:
    """The degradation is honest, not silent -- and the request is still not lost.

    `EVENT_MODE = "poll"`, or a deployment with no opencode route, attaches no reader, so
    there is no way to learn that the still-running voice turn ended. The wait falls back to
    a fixed grace, says in a WARNING that the ordering guarantee is not there, and hands
    over anyway: D5 is not conditional on how the server was watched.
    """
    # Given the fixture's wiring, which has no TurnWatch at all
    assert rig.brain.opencode.turns is None
    rig.server.hang_turn = True
    session_id = await rig.warm()
    rig.cfg.r2d2_fast_deadline = DEADLINE_S
    # When
    assert await rig.say() == ACK
    job = await _armed_job(rig)
    # Then the agent has the request, exactly once, in the right session
    submitted = [turn for turn in rig.server.turns if turn.submitted]
    assert [turn.session_id for turn in submitted] == [session_id]
    assert job["session_id"] == session_id
    assert [entry["type"] for entry in rig.brain.worker.jobs] == [JOB_TYPE]


# ---------------------------------------------------------------------------
# 7. The third guard, and the order it runs in
# ---------------------------------------------------------------------------


async def test_the_guard_runs_before_the_truncation_so_a_sentinel_past_the_cut_is_caught(
    rig: Rig,
) -> None:
    # Given the FALLBACK path, where parse_voice_reply never runs -- so the third
    # guard is the only one -- answering with a sentinel PAST the 1024-character
    # cut, which is exactly where a guard placed after render.clean cannot see it
    rig.server.down = True
    rig.net.zen.mock(side_effect=replying(f"{'аб' * 900} {SENTINEL} собери и пришли в телеграм"))
    # When
    text = await rig.say()
    # Then the ack is returned: the guard saw the WHOLE reply, so truncation
    # never happened to it. A 1024-character wall of filler would be the
    # signature of a guard that ran after render.clean.
    assert text == ACK
    assert len(text) < MAX_TEXT
    assert SENTINEL not in text


async def test_a_long_answer_without_a_sentinel_is_truncated_to_alices_limit(rig: Rig) -> None:
    # Given a plain, honest, very long answer on the same unguarded path
    rig.server.down = True
    rig.net.zen.mock(side_effect=replying("б" * 3000))
    # When
    payload = await rig.ask()
    text = str(payload["response"]["text"])
    # Then Alice's own limit is what bounds it, and the answer is not thrown away
    assert len(text) == MAX_TEXT
    assert text.endswith("…")
    assert text != ACK


# ---------------------------------------------------------------------------
# 8. Stale state: a long session is summarised once, then carries on
# ---------------------------------------------------------------------------


async def test_a_session_past_the_soft_limit_is_summarised_once_and_continues(
    rig: Rig,
) -> None:
    # Given a session that has grown past the soft limit
    rig.cfg.r2d2_session_soft_limit = 2
    session_id = await rig.warm(count=7)
    # When
    text = await rig.say()
    # Then it was summarised exactly once -- not once per attempt, not in a loop
    assert len(rig.server.summarizes) == 1
    assert rig.server.summarizes[0][0] == session_id
    assert rig.server.summarizes[0][1] == WIRED_MODEL
    # ... and the turn carried on to a real answer
    assert text == ANSWER
    assert len(rig.server.turns) == 1


async def test_a_session_under_the_soft_limit_is_not_summarised(rig: Rig) -> None:
    # Given a session comfortably under the limit
    rig.cfg.r2d2_session_soft_limit = 40
    await rig.warm(count=7)
    # When
    await rig.say()
    # Then
    assert rig.server.summarizes == []


# ---------------------------------------------------------------------------
# 9. The only auth on a network-exposed webhook
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    (("skill_id", "someone-elses-skill"), ("user_id", "someone-elses-user")),
)
async def test_a_mismatched_identity_is_refused_before_anything_else(
    rig: Rig, field: str, value: str
) -> None:
    # Given / When
    payload = await rig.ask(QUESTION, **{field: value})
    # Then the request is refused ...
    assert payload["response"]["text"] == "Доступ запрещён."
    assert payload["response"]["end_session"] is True
    # ... with no LLM, no opencode and no Telegram traffic whatsoever
    assert rig.server.requests == []
    assert rig.net.fallback_calls() == 0
    assert rig.net.telegram == []


async def test_the_authorised_identity_is_answered(rig: Rig) -> None:
    # Given / When
    await rig.warm()
    text = await rig.say()
    # Then -- the whitelist is a whitelist, not a blacklist
    assert text == ANSWER
    assert rig.brain.authorized(alice_body(QUESTION)) is True


@pytest.mark.parametrize("missing", ("alice_skill_id", "alice_user_id"))
async def test_an_undeclared_identity_refuses_rather_than_admitting_everyone(
    rig: Rig, missing: str
) -> None:
    """An unset id is an absence of a decision, not a wildcard.

    The code used to read "if a value is set, compare it", so a blank variable
    skipped the check and the webhook served whoever asked. Behind /webhook sit the
    opencode agent and the `r2d2_do` shim, so that is the difference between a private
    skill and a remote control for the machine.
    """
    # Given a deployment that has not declared one half of who it serves
    setattr(rig.cfg, missing, "")
    # When
    verdict = rig.brain.authorized(alice_body(QUESTION))
    # Then it refuses, and it refuses the body that would otherwise be served
    assert verdict is False


async def test_the_refusal_reaches_the_caller_and_touches_nothing_else(rig: Rig) -> None:
    # Given the same undeclared deployment
    rig.cfg.alice_skill_id = ""
    # When a real question arrives over the wire
    payload = await rig.ask(QUESTION)
    # Then the caller is told, and nothing downstream ran
    assert payload["response"]["text"] == "Доступ запрещён."
    assert payload["response"]["end_session"] is True
    assert rig.server.requests == []
    assert rig.net.fallback_calls() == 0
    assert rig.net.telegram == []


async def test_the_development_escape_hatch_opens_the_door_it_names(rig: Rig) -> None:
    # Given the one variable whose whole job is to bypass the check
    rig.cfg.alice_skill_id = ""
    rig.cfg.alice_user_id = ""
    rig.cfg.r2d2_allow_unauthenticated = True
    # When
    verdict = rig.brain.authorized(alice_body(QUESTION))
    # Then it admits, because that is what it is for
    assert verdict is True
    # And a declared deployment is unaffected by it either way
    rig.cfg.r2d2_allow_unauthenticated = False
    rig.cfg.alice_skill_id = SKILL_ID
    rig.cfg.alice_user_id = USER_ID
    assert rig.brain.authorized(alice_body(QUESTION)) is True


def test_the_startup_refusal_names_the_variables_an_operator_has_to_set() -> None:
    """A closed webhook that says nothing looks like a broken skill.

    The message is the difference between "my skill stopped answering" and "I have
    not told it who I am", so it must carry both variable names and the opt-in, and
    it must not carry an id value.
    """
    from app.main import authorisation_posture

    missing_skill = authorisation_posture(Config(alice_user_id=USER_ID))
    assert missing_skill is not None
    assert "ALICE_SKILL_ID" in missing_skill
    assert "ALICE_USER_ID" not in missing_skill
    assert "R2D2_ALLOW_UNAUTHENTICATED" in missing_skill

    missing_user = authorisation_posture(Config(alice_skill_id=SKILL_ID))
    assert missing_user is not None
    assert "ALICE_USER_ID" in missing_user
    assert "ALICE_SKILL_ID" not in missing_user

    # A configured deployment is told nothing, because there is nothing to tell.
    assert authorisation_posture(
        Config(alice_skill_id=SKILL_ID, alice_user_id=USER_ID)
    ) is None


def test_the_development_escape_hatch_is_announced_as_the_risk_it_is() -> None:
    from app.main import authorisation_posture

    message = authorisation_posture(Config(r2d2_allow_unauthenticated=True))
    assert message is not None
    assert "ALICE_SKILL_ID" in message
    assert "r2d2_do" in message


def test_the_default_listener_is_loopback_not_the_world() -> None:
    """A default of 0.0.0.0 plus a blank id is the combination this fix removes."""
    assert Config().server_host == "127.0.0.1"


# ---------------------------------------------------------------------------
# 10. Concurrency
# ---------------------------------------------------------------------------


async def test_two_concurrent_turns_for_one_application_share_one_session(rig: Rig) -> None:
    # Given one user asking two questions in the same instant
    await rig.warm()
    rig.server.requests.clear()
    # When
    first, second = await asyncio.gather(rig.say("первый вопрос"), rig.say("второй вопрос"))
    # Then both were answered from the ONE session -- the store's per-application
    # lock is what stops two `POST /session` from racing
    assert (first, second) == (ANSWER, ANSWER)
    assert rig.server.route_count("POST", "/session") == 0
    assert len({turn.session_id for turn in rig.server.turns}) == 1
    assert len(rig.server.turns) == 2


async def test_two_concurrent_cold_turns_for_one_application_create_one_session(
    rig: Rig,
) -> None:
    # Given a user who has never asked anything -- the worst case for the race,
    # because both turns want to CREATE a session
    # When
    await asyncio.gather(rig.say("первый вопрос"), rig.say("второй вопрос"))
    # Then exactly one session exists for them
    assert rig.server.route_count("POST", "/session") == 1
    assert len(rig.server.sessions) == 1
    assert len(rig.server.turns) == 2


async def test_two_concurrent_fallback_turns_leave_well_formed_history(rig: Rig) -> None:
    # Given the fallback path, which is the only one that writes sessions.history
    rig.server.down = True
    # When
    await asyncio.gather(rig.say("первый вопрос"), rig.say("второй вопрос"))
    # Then the stored history is a list of WHOLE messages: no entry is a mixture
    # of two turns, none is half-written, and the row still parses. (A lost
    # update is a separate, pre-existing matter in core/memory.py -- what this
    # pins is that concurrent writes cannot CORRUPT the row.)
    history = await rig.memory.load_history(USER_ID)
    assert isinstance(history, list)
    assert history
    for entry in history:
        assert set(entry) == {"role", "content"}
        assert entry["role"] in {"user", "assistant"}
        assert isinstance(entry["content"], str) and entry["content"]
    assert {entry["role"] for entry in history} == {"user", "assistant"}
    assert len(history) <= rig.cfg.max_history


# ---------------------------------------------------------------------------
# 11. Secret hygiene
# ---------------------------------------------------------------------------


async def test_no_credential_reaches_a_reply_or_a_log_record(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    # Given a turn answered by the opencode route, then one by the fallback, with
    # every log record of the whole turn captured at DEBUG
    caplog.set_level(logging.DEBUG)
    await rig.warm()
    await rig.say()
    rig.server.down = True
    await rig.ask(QUESTION, user_id=OTHER_USER)
    # Then neither the opencode password nor either provider key is in a record
    for secret in (PASSWORD, ZEN_KEY, OPENROUTER_KEY, TELEGRAM_TOKEN):
        assert secret not in caplog.text
    # ... nor in what the user is told
    assert PASSWORD not in ANSWER and ZEN_KEY not in ANSWER


async def test_no_credential_reaches_an_exception_when_everything_fails(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    # Given a turn in which every single backend fails
    caplog.set_level(logging.DEBUG)
    rig.server.down = True
    rig.net.zen_status = 500
    rig.net.openrouter_status = 500
    # When
    payload = await rig.ask()
    # Then the failure is reported as the graceful text, and the traceback that
    # logged it carries no credential either
    assert payload["response"]["text"] == ERROR_TEXT
    assert "all LLM providers failed" in caplog.text
    for secret in (PASSWORD, ZEN_KEY, OPENROUTER_KEY, TELEGRAM_TOKEN):
        assert secret not in caplog.text


# ---------------------------------------------------------------------------
# 12. The wiring todo 18 will build
# ---------------------------------------------------------------------------


async def test_the_shipped_budgets_are_the_ones_the_plan_fixed() -> None:
    # Given / When / Then -- the two clocks the measurements were taken against
    assert Config().r2d2_fast_deadline == SHIPPED_DEADLINE_S
    assert Config().r2d2_event_poll_interval == SHIPPED_POLL_S
    assert Config().r2d2_needs_agent_sentinel == SENTINEL
    assert Config().r2d2_task_ack == ACK


async def test_a_brain_built_without_the_wiring_answers_from_the_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, net: Net
) -> None:
    # Given the app/main.py construction of todo 15's predecessors -- four
    # positional arguments and no opencode wiring, which must keep working
    monkeypatch.setenv("R2D2_ZEN_KEY", ZEN_KEY)
    backends_path = tmp_path / "backends.json"
    backends_path.write_text(BACKENDS_JSON, encoding="utf-8")
    memory = await Memory(str(tmp_path / "sessions.db")).connect()
    cfg = Config(backends_path=str(backends_path), telegram_chat_id="42",
                 alice_skill_id=SKILL_ID, alice_user_id=USER_ID)
    try:
        brain = Brain(cfg, memory, RecordingWorker(cfg, memory), logging.getLogger("r2d2.test"))
        # When a question arrives with no opencode route wired at all
        payload = await brain.process_alice(alice_body(QUESTION))
        # Then the fallback chain answers and no opencode call is even attempted
        assert payload["response"]["text"] == FALLBACK_ANSWER
    finally:
        await memory.close()


# ---------------------------------------------------------------------------
# the identity a turn is stored and answered under
# ---------------------------------------------------------------------------


async def test_the_memory_key_is_the_user_so_two_apps_share_one_session(
    rig: Rig,
) -> None:
    # Given: one person who has already spoken to R2D2, from one app
    session_id = await rig.warm()
    # When: the same person speaks from a DIFFERENT app -- a phone and a Station
    # are two application ids for one human, and the platform says so
    rig.server.requests.clear()
    spoken = await rig.say(app_id="a-second-app")
    # Then: it is the same session, so the warm answer comes back rather than the
    # C8 acknowledgement a never-bound identity would earn
    assert spoken == ANSWER
    assert rig.server.turns[-1].session_id == session_id


async def test_two_people_never_share_a_session_or_a_pending_question(
    rig: Rig,
) -> None:
    # Given: a pending opencode ask belonging to the first person
    session_id = await rig.warm()
    await store_ask(rig)
    assert await rig.memory.get_pending(USER_ID) is not None
    # When: a different person says «да»
    await rig.ask("да", user_id=OTHER_USER)
    # Then: it did not answer the first person's question
    assert await rig.memory.get_pending(USER_ID) is not None
    assert await rig.memory.get_pending(OTHER_USER) is None


async def test_a_body_that_identifies_nobody_is_answered_and_not_stored(
    tmp_path: Path, net: Net
) -> None:
    # Given: a gateway opened to anyone, and a turn carrying neither
    # session.user.user_id nor session.application.application_id
    backends_path = tmp_path / "backends.json"
    backends_path.write_text(BACKENDS_JSON, encoding="utf-8")
    memory = await Memory(str(tmp_path / "sessions.db")).connect()
    cfg = Config(backends_path=str(backends_path), telegram_chat_id="42",
                 alice_skill_id=SKILL_ID, r2d2_allow_unauthenticated=True)
    try:
        brain = Brain(cfg, memory, RecordingWorker(cfg, memory), logging.getLogger("r2d2.test"))
        body = alice_body(QUESTION, user_id="")
        del body["session"]["application"]
        # When
        payload = await brain.process_alice(body)
        # Then: it is told so, and nothing was written under a key nobody owns --
        # a shared placeholder would let one stranger answer another's ask
        assert payload["response"]["end_session"] is False
        assert await memory.load_history("") == []
        assert await memory.get_pending("") is None
    finally:
        await memory.close()


async def test_a_shell_confirmation_and_a_brokered_ask_stop_overwriting_each_other(
    rig: Rig,
) -> None:
    """`pending_actions` holds one row per key, and two features used to share it.

    A risky tool's confirmation and the opencode permission broker both wrote
    `pending_actions[app_id]`, so whichever went last destroyed the other's row.
    The broker's row is the one that cannot be rebuilt: the sweep skips a row with
    no `kind`, so a shell confirmation written over an outstanding ask left
    opencode blocked on a question nobody could ever refuse, and a broker save
    written over a confirmation answered the wrong question with the user's «да».
    """
    # Given a brokered ask AND a shell confirmation, for the same person
    await rig.warm()
    await store_ask(rig)
    await rig.memory.set_pending(
        tool_pending_key(USER_ID),
        {"tool": "run_shell", "arguments": {"command": "rm -rf /tmp/x"}},
    )
    # Then both rows exist, each readable by its own owner
    assert (await rig.memory.get_pending(USER_ID))["kind"] == "opencode_permission"
    assert (await rig.memory.get_pending(tool_pending_key(USER_ID)))["tool"] == "run_shell"
    # ... and the sweep still finds the ask, which is the half that used to vanish
    broker_ids = list(await rig.memory.all_pending_ids())
    assert USER_ID in broker_ids
