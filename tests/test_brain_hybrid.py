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
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import httpx
import pytest
import respx

import core.brain
from app.config import Config
from core.async_worker import Worker
from core.backends.config_loader import BackendSpec, load_backend_specs
from core.backends.opencode_session import (
    OpencodeSessionBackend,
    OpencodeWiring,
    current_application_id,
)
from core.brain import ERROR_TEXT, GREETING, HELP_TEXT, Brain
from core.memory import Memory
from core.opencode.client import OpencodeClient
from core.opencode.session_store import OcSessionStore, title_for
from core.permissions import PermissionBroker
from core.render import MAX_TEXT

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
SHIPPED_DEADLINE_S: Final = 3.2
SHIPPED_POLL_S: Final = 2.0

APP: Final = "alice-app-1"
OTHER_APP: Final = "bob-app-2"
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
#: brain writes. Todo 16 reads `session_id`, `since_message_id` and `timeout_s`.
JOB_TYPE: Final = "opencode_reply"
JOB_FIELDS: Final = frozenset(
    {"type", "application_id", "session_id", "since_message_id", "timeout_s"}
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
        #: A bounded wait before the turn answers, so a test can put the deadline
        #: on either side of the answer and prove which one decided the outcome.
        self.delay_s: float = 0.0
        #: The message id of each hung voice turn, in order -- what the brain must
        #: record as `since_message_id`, because at the moment the deadline fires
        #: that user turn is the last thing the session holds.
        self.hung_ids: list[str] = []
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
            self._record(session_id, request, submitted=True)
            return httpx.Response(204)
        if method == "GET" and path.endswith("/message"):
            return self._poll(session_id)
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
            return httpx.Response(200, json={})
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
        self.seed(session_id, said(turn.text, f"msg_u{len(self.turns)}", role="user"))
        return httpx.Response(
            200, json={"info": {"id": f"msg_a{len(self.turns)}", "role": "assistant"}, "parts": parts}
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
            text = await self._backend.collect_reply(
                str(job["session_id"]),
                str(job["since_message_id"]),
                float(job.get("timeout_s", COLLECT_TIMEOUT_S)),
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

    async def warm(self, app_id: str = APP, count: int = 6) -> str:
        """Give `app_id` a bound session and a non-zero message count.

        A brand-new session is the C8 case and has its own test; every other test
        is about a warm one, so this is the fixture's job rather than each test's.
        """
        session_id = await self.store.resolve(app_id)
        await self.memory.touch_oc_session(app_id, message_delta=count)
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
    """One Alice `SimpleUtterance` webhook body, shaped as the platform sends it."""
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


@pytest.fixture
async def rig(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, net: Net, server: FakeOpencode
):
    """The whole stack, assembled exactly as todo 18 will assemble it in main.

    The registry file is the shipped `config/backends.json` shape with the
    opencode session backend FIRST in the chain, so the fallback tests prove the
    brain does not re-try the session backend after the opencode route failed.
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
    broker = PermissionBroker(memory, client, cfg)
    worker = RecordingWorker(cfg, memory)
    wiring = core.brain.HybridWiring(
        spec=oc_spec, client=client, store=store, backend=backend, broker=broker
    )
    brain = Brain(cfg, memory, worker, logging.getLogger("r2d2.test.brain"), opencode=wiring)
    try:
        yield Rig(brain, server, net, memory, cfg, oc_spec, store, backend)
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


# ---------------------------------------------------------------------------
# 3. The permission gate: opencode's ask is answered, and only by the user
# ---------------------------------------------------------------------------


async def store_ask(rig: Rig, app_id: str = APP, title: str = "rm -rf /tmp/x") -> dict:
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
    assert await rig.memory.get_pending(APP) is None
    # ... and no LLM turn happened at all
    assert rig.server.turns == []


async def test_no_refuses_a_pending_opencode_ask(rig: Rig) -> None:
    # Given / When
    await store_ask(rig)
    text = await rig.say("нет")
    # Then
    assert [answer[2] for answer in rig.server.permission_answers] == [{"response": "reject"}]
    assert await rig.memory.get_pending(APP) is None
    assert rig.server.turns == []
    assert len(text) <= 60


async def test_text_that_is_not_an_answer_leaves_the_ask_pending(rig: Rig) -> None:
    # Given an opencode ask, and a user who asks something else entirely
    record = await store_ask(rig)
    await rig.warm()
    rig.server.reply = ANSWER
    # When
    text = await rig.say(QUESTION)
    # Then the question is answered, the ask is untouched, and nothing was posted
    # to the permissions route -- "unrelated" must change nothing
    assert text == ANSWER
    assert rig.server.permission_answers == []
    assert await rig.memory.get_pending(APP) == record


async def test_the_shell_confirmation_gate_still_runs_its_own_pending_action(rig: Rig) -> None:
    # Given the OTHER feature's pending row: a risky shell command
    await rig.memory.set_pending(
        APP, {"tool": "run_shell", "arguments": {"command": "echo r2d2-hybrid-gate-ok"}}
    )
    # When the user confirms
    text = await rig.say("да")
    # Then the command ran and its output is spoken ...
    assert "r2d2-hybrid-gate-ok" in text
    # ... and it did not go through the opencode route at all
    assert rig.server.requests == []
    assert rig.server.permission_answers == []


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
    assert list(rig.server.sessions.values())[0]["title"] == title_for(APP)
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
    # Then exactly one job was enqueued, of the type todo 16 dispatches ...
    jobs = rig.brain.worker.jobs
    assert len(jobs) == 1
    job = jobs[0]
    assert job["type"] == JOB_TYPE
    assert set(job) == JOB_FIELDS
    # ... carrying everything the collector needs and nothing it does not
    assert job["session_id"] == session_id
    assert job["application_id"] == APP
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
    job = rig.brain.worker.jobs[0]
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
    history = await rig.memory.load_history(APP)
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
    await rig.ask(QUESTION, app_id=OTHER_APP)
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
    cfg = Config(backends_path=str(backends_path), telegram_chat_id="42")
    try:
        brain = Brain(cfg, memory, RecordingWorker(cfg, memory), logging.getLogger("r2d2.test"))
        # When a question arrives with no opencode route wired at all
        payload = await brain.process_alice(alice_body(QUESTION))
        # Then the fallback chain answers and no opencode call is even attempted
        assert payload["response"]["text"] == FALLBACK_ANSWER
    finally:
        await memory.close()
