"""The whole stack, in and out, against a server (plan todo 18).

`tests/test_brain_hybrid.py` drives the real `Brain` over the real opencode
objects with a `MockTransport` at the wire, and `tests/test_metrics_and_health.py`
drives the real app over its real lifespan with a mocked server. Neither has
exercised the thing this file exists for: **the composition root**. Before todo 18
`app/main.py` built a `Memory`, a `Worker` and a `Brain`, probed the server and
threw the answer away -- there was no opencode session, no broker, no event
reader and no reaper inside a running application, and a turn that took the
opencode route emitted no record at all.

What is real here: the real `FastAPI` app over its real lifespan, the real
`OpencodeClient`, `OcSessionStore`, `OpencodeSessionBackend`, `PermissionBroker`,
`Worker`, `Brain`, `EventSource` and `core.render`, over a real HTTP surface
(`tests/fake_opencode.py`) that streams, hangs and refuses. The only doubles are
the wire's far side and the two things R2D2 cannot be given a real one of: the
opencode server, and Telegram (patched at the four `send_message` import sites,
because the real one would answer for api.telegram.org).

**The metrics gap this todo closes is asserted here, not assumed.** An
opencode-routed turn is the primary route and the one whose latency proves we fit
Alice's 4.5 s budget, so `turn route=opencode` with a non-zero `llm_ms` and
`path=deadline` on the branch that outran the budget are the first two things the
file pins after the JSON contract.

**Decisions a reviewer will ask about, stated here:**

* **The first turn of a new session is acknowledged, not answered** (C8: a cold
  cache costs 15.5-18.6 s). Most voice-path assertions therefore warm the session
  with one turn first, and the C8 behaviour has its own test rather than being an
  accident of a fixture.
* **"The server is down" is a refused connection, not a 503.** The in-process
  transport raises `httpx.ConnectError`, because that is what every caller in the
  stack is written against; the real refusal, with the process actually gone, is
  proved out of process in `/tmp/r2d2-qa/todo18-fail.txt`.
* **The permission round trip needs a real socket.** `httpx.ASGITransport`
  collects a response body before the client sees any of it, so an SSE stream
  cannot be observed through it; the two SSE tests serve the fake over loopback
  and skip themselves when the run refuses outbound sockets (`-p no_net`).
* **No test sleeps to "let something finish".** The collector ends on its own
  idle condition, and every wait is a subscription: to a Telegram message, to an
  event-stream attachment, or to the reaper's own log line.

allow: SIZE_OK -- pure LOC is over the 250 ceiling and the file carries a
`SIZE_OK` marker for it. Every test module in this repo is 436-751 pure LOC
(`test_opencode_client.py` 751, `test_sse.py` 699) and a test module grows with
the number of behaviours it pins, not with the number of concepts it owns. The
250 pure-LOC ceiling targets source modules; splitting this would scatter one
contract -- what Alice may be told, and what the record says about it -- across
files that each need the whole app-over-its-own-lifespan harness to say anything.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import socket
import threading
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import httpx
import pytest
import respx
import uvicorn
from fastapi import FastAPI

import core.brain
import core.permissions
import core.async_worker
from app import main as main_module
from app.config import Config
from app.main import build_app
from core.backends.config_loader import load_backend_specs
from core.brain import ERROR_TEXT, GREETING
from core.memory import Memory
from core.opencode.client import OpencodeClient
from core.render import MAX_TEXT
from tests.fake_opencode import ANSWER, SENTINEL, FakeOpencode, FakeTurn

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

#: Where the in-process fake is reachable. Nothing listens here: the `EventSource`
#: the composition root builds reaches for this address, finds nothing and
#: reconnects, which is the harmless "no server" case rather than a test failure.
BASE_URL: Final = "http://127.0.0.1:4599"
ZEN_URL: Final = "https://zen.test/v1"
OPENROUTER_URL: Final = "https://openrouter.test/api/v1"
TELEGRAM_PREFIX: Final = "https://api.telegram.org/"

#: Alice's platform contract, verbatim: this is the shape a webhook must answer.
ALICE_KEYS: Final = {"response", "version"}
RESPONSE_KEYS: Final = {"text", "tts", "end_session"}
ALICE_VERSION: Final = "1.0"

#: The shipped values, read off the dataclass so a rename fails here instead of
#: making four assertions quietly wrong.
ACK: Final = Config().r2d2_task_ack
DEADLINE_S: Final = 0.5
#: The collector's own idle condition, shortened from 2 s so no test waits on it.
POLL_S: Final = 0.05
#: `docs/07-latency-strategy.md` budgets about 2.5 s of Alice's 4.5 s for R2D2.
BUDGET_MS: Final = 2500

VOICE_AGENT: Final = "r2d2-voice"
TASK_AGENT: Final = "r2d2-agent"
MODEL: Final = "opencode/space-bunny-free"
MODEL_ID: Final = "space-bunny-free"

APP_ID: Final = "alice-app-1"
TG_APP_ID: Final = "tg:4242"
SKILL_ID: Final = "skill-abc"
USER_ID: Final = "user-xyz"
CHAT_ID: Final = "4242"
QUESTION: Final = "что такое квантовые точки"
#: A word the fake treats as "this needs the agent", not as content.
DIGEST: Final = "собери digest статей про RAG"
FALLBACK_ANSWER: Final = "Ответ запасного провайдера."
AGENT_REPLY: Final = "Сводка готова: три статьи про RAG."
LONG_REPLY: Final = "А" * 5000
MARKDOWN_REPLY: Final = "Вот [документация](https://example.com/docs) по теме."

#: Every credential this deployment can hold, each a value nobody has, so "it is
#: not in the log" is a claim about a string that appears nowhere else.
OC_USERNAME: Final = "opencode-sentinel-user"
OC_PASSWORD: Final = "oc-password-sentinel-4f2c9a7b"
ZEN_KEY: Final = "sk-or-zen-sentinel-7c1e5b3d"
OPENROUTER_KEY: Final = "sk-or-openrouter-sentinel-1a2b3c4d"
TELEGRAM_TOKEN: Final = "123456789:AAbbCCddEEffGGhhIIjjKKllMMnnOOpp"
SECRETS: Final = (OC_PASSWORD, OC_USERNAME, ZEN_KEY, OPENROUTER_KEY, TELEGRAM_TOKEN)
#: The workspace names the operator's home and their disk layout; no public body
#: may repeat it either.
WORKSPACE_NAME: Final = "r2d2-workspace-MUST-NOT-LEAK"

#: The two routes the plan pins a `field=value` record for.
TURN_PREFIX: Final = "turn route="
#: The startup line the adversarial class of "a misleading success" is about.
STARTED_PREFIX: Final = "R2D2 started"
#: A generous ceiling on a call that should be bounded by `HEALTH_PROBE_TIMEOUT_S`.
HEALTH_CEILING_S: Final = 4.0
#: `permission.asked` and the answer R2D2 posts back -- never `always` (C5).
PERMISSION_QUESTION: Final = "Нужно подтверждение"
APPROVED_TEXT: Final = "Принято, выполняю."


# ---------------------------------------------------------------------------
# The registry the deployment under test reads
# ---------------------------------------------------------------------------


def registry_json(base_url: str) -> str:
    """`config/backends.json` as the plan ships it, pointed at `base_url`.

    The session backend is FIRST in the chain because it is the primary route,
    and `core/brain.py` drops it from the fallback walk -- so a chain turn that
    re-tried it would be a second attempt at the host that just failed.
    """
    return json.dumps(
        {
            "chain": ["opencode", "zen", "openrouter"],
            "backends": [
                {
                    "name": "opencode",
                    "kind": "opencode_session",
                    "base_url": base_url,
                    "username": "${R2D2_OC_USERNAME}",
                    "password": "${R2D2_OC_PASSWORD}",
                    "voice_agent": VOICE_AGENT,
                    "task_agent": TASK_AGENT,
                    "fast_model": MODEL,
                    "task_model": MODEL,
                    "summarize_model": MODEL,
                    "timeout": 3.2,
                },
                {
                    "name": "zen",
                    "kind": "openai_compatible",
                    "base_url": ZEN_URL,
                    "api_key": "${R2D2_ZEN_KEY}",
                    "model": MODEL_ID,
                    "auth_style": "bearer",
                },
                {
                    "name": "openrouter",
                    "kind": "openai_compatible",
                    "base_url": OPENROUTER_URL,
                    "api_key": "${OPENROUTER_API_KEY}",
                    "model": "some:free",
                    "auth_style": "bearer",
                },
            ],
        },
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class Switchable(httpx.AsyncBaseTransport):
    """The fake in process, and a refused connection while it is switched off.

    "The server is not running" is a transport failure, not a status code: the
    real client sees `ConnectError` and every caller above it is written for that.
    Answering 503 instead would exercise the status-error path and leave the
    connection-refused one untested -- and the plan's degradation is the second.
    """

    def __init__(self, fake: FakeOpencode) -> None:
        self._fake = fake
        self._asgi = httpx.ASGITransport(app=fake.app())

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self._fake.error_status != 200:
            raise httpx.ConnectError(f"opencode is not running at {request.url.netloc.decode()}")
        return await self._asgi.handle_async_request(request)


@dataclass
class Telegram:
    """The one channel that can push, replaced at its four import sites.

    The real `send_message` is covered by `tests/test_permission_broker.py`; what
    this file needs is the message the user would read AT THE MOMENT it arrives,
    so the wait is a subscription to the delivery rather than a sleep.
    """

    messages: list[str] = field(default_factory=list)
    _seen: int = 0
    _arrival: asyncio.Event = field(default_factory=asyncio.Event)

    async def __call__(self, _cfg: Config, text: str, chat_id: int | None = None) -> bool:
        self.messages.append(text)
        self._arrival.set()
        return True

    async def wait_for(self, fragment: str, timeout_s: float = 10.0) -> str:
        """The first unconsumed message containing `fragment`; a timeout is a failure.

        Polling a counter would be the alternative, and it is a flake generator:
        the wait is long enough to be safe on a loaded machine and slow on an idle
        one. This subscribes instead, so the common case costs nothing.
        """
        async def watch() -> str:
            while True:
                for message in self.messages[self._seen :]:
                    self._seen = self.messages.index(message) + 1
                    if fragment in message:
                        return message
                await self._arrival.wait()
                self._arrival.clear()

        try:
            return await asyncio.wait_for(watch(), timeout_s)
        except TimeoutError as exc:
            raise AssertionError(
                f"no Telegram message containing {fragment!r} arrived; got {self.messages}"
            ) from exc


def speaking(message: str) -> Any:
    """One fallback-LLM answer: text, no tools."""

    def side_effect(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": message}}],
                "model": json.loads(request.content)["model"],
            },
        )

    return side_effect


# ---------------------------------------------------------------------------
# The running app
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Stack:
    """A running app, the client bound to it, and the server behind it."""

    http: httpx.AsyncClient
    app: FastAPI
    fake: FakeOpencode
    telegram: Telegram

    @property
    def cfg(self) -> Config:
        return self.app.state.cfg  # type: ignore[no-any-return]

    async def ask(self, command: str, *, app_id: str = APP_ID, new: bool = False) -> dict[str, Any]:
        """One Alice `SimpleUtterance` in, the raw webhook body out."""
        response = await self.http.post("/webhook", json=alice_body(command, app_id=app_id, new=new))
        return json_of(response)

    async def say_telegram(self, text: str) -> dict[str, Any]:
        """One Telegram message in, the raw webhook body out."""
        response = await self.http.post(
            "/tg/webhook", json={"message": {"chat": {"id": int(CHAT_ID)}, "text": text}}
        )
        return json_of(response)

    async def health(self) -> dict[str, Any]:
        response = await self.http.get("/health")
        assert response.status_code == 200, response.text
        return json_of(response)

    async def warm(self, app_id: str = APP_ID) -> str:
        """Give `app_id` a warm session, as any returning user has one.

        The FIRST turn of a session is C8's cold-cache case: it is acknowledged
        and submitted, never waited on. Every assertion about a spoken answer
        therefore needs a second turn, and this is that second turn -- written
        once so no test has to remember it.
        """
        await self.ask(QUESTION, app_id=app_id)
        return await self.ask(QUESTION, app_id=app_id)

    def turns(self) -> list[FakeTurn]:
        return self.fake.turns

    def routes(self) -> list[str]:
        return self.fake.routes

    def asked(self, method: str, path: str) -> int:
        return self.fake.route_count(method, path)


def alice_body(command: str, *, app_id: str = APP_ID, new: bool = False) -> dict[str, Any]:
    """One Alice webhook body, shaped as the platform sends it."""
    return {
        "meta": {"interfaces": [{"type": "Voice"}]},
        "request": {"type": "SimpleUtterance", "command": command},
        "session": {
            "new": new,
            "skill_id": SKILL_ID,
            "application": {"application_id": app_id},
            "user": {"user_id": USER_ID},
        },
        "version": "1.0",
    }


def json_of(response: httpx.Response) -> dict[str, Any]:
    """The body, checked to be a JSON OBJECT before any other assertion reads it."""
    content_type = response.headers.get("content-type", "")
    assert content_type.startswith("application/json"), f"{content_type!r}: {response.text[:200]!r}"
    document = response.json()
    assert isinstance(document, dict), document
    return document


def turn_records(caplog: pytest.LogCaptureFixture) -> list[dict[str, str]]:
    """The `field=value` pairs of every turn record in a capture."""
    return [
        dict(re.findall(r"(\w+)=(\S+)", record.getMessage()))
        for record in caplog.records
        if record.getMessage().startswith(TURN_PREFIX)
    ]


def startup_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Every record of this test, INCLUDING the setup phase.

    pytest gives each phase its own capture buffer, and `caplog.records` only ever
    holds the current one -- so a fixture that starts the app logs into "setup" and
    a test reading `caplog.records` sees nothing. That is exactly the shape of a
    test that would quietly assert nothing.
    """
    return [
        record.getMessage()
        for record in (*caplog.get_records("setup"), *caplog.get_records("call"))
    ]


def install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    base_url: str = BASE_URL,
    **overrides: Any,
) -> Path:
    """A temp registry, workspace and credential set, and a `Config.load` that reads them.

    The sentinels are the assertion material: "no credential in a log record" is
    only a claim about a string that appears nowhere else. The registry is
    written per test because the loopback tests need the opencode backend pointed
    at their own ephemeral port, and `overrides` because a test that needs a
    different clock must not inherit this one.
    """
    for name, value in (
        ("R2D2_OC_USERNAME", OC_USERNAME),
        ("R2D2_OC_PASSWORD", OC_PASSWORD),
        ("R2D2_ZEN_KEY", ZEN_KEY),
        ("OPENROUTER_API_KEY", OPENROUTER_KEY),
    ):
        monkeypatch.setenv(name, value)
    workspace = tmp_path / WORKSPACE_NAME
    workspace.mkdir(exist_ok=True)
    backends = tmp_path / "backends.json"
    backends.write_text(registry_json(base_url), encoding="utf-8")
    settings: dict[str, Any] = {
        "alice_skill_id": SKILL_ID,
        "alice_user_id": USER_ID,
        "telegram_bot_token": TELEGRAM_TOKEN,
        "telegram_chat_id": CHAT_ID,
        "backends_path": str(backends),
        "db_path": str(tmp_path / "sessions.db"),
        "r2d2_workspace": str(workspace),
        "r2d2_fast_deadline": DEADLINE_S,
        "r2d2_event_poll_interval": POLL_S,
    }
    settings.update(overrides)
    monkeypatch.setattr(Config, "load", classmethod(lambda cls: Config(**settings)))
    return workspace


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _info_records(caplog: pytest.LogCaptureFixture) -> None:
    """Let the STARTUP records reach `caplog`, not just the ones a test asks for.

    The lifespan logs before the test body runs, and pytest's default capture level
    is WARNING -- so without this, "the startup line says the route is wired" would
    be an assertion about a record nobody captured.
    """
    caplog.set_level(logging.INFO)


@pytest.fixture
def fake() -> FakeOpencode:
    return FakeOpencode()


@pytest.fixture
def serve_in_process(monkeypatch: pytest.MonkeyPatch, fake: FakeOpencode) -> FakeOpencode:
    """Point the composition root's client at the in-process fake; return that fake.

    The one seam todo 6 built -- `OpencodeClient(spec, directory, client=...)` --
    resolved through `app.main`'s own namespace, which is also what a test that
    replaces that namespace is FOR: `app/diagnostics.py` reads the client through
    the same global, so a probe and a turn talk to the same server.
    """

    def factory(spec: Any, directory: str, *, client: Any = None) -> OpencodeClient:
        return OpencodeClient(
            spec, directory, client=httpx.AsyncClient(transport=Switchable(fake))
        )

    monkeypatch.setattr(main_module, "OpencodeClient", factory)
    return fake


@pytest.fixture
def telegram(monkeypatch: pytest.MonkeyPatch) -> Telegram:
    channel = Telegram()
    for module in (core.permissions, core.brain, core.async_worker, main_module):
        monkeypatch.setattr(module, "send_message", channel)
    return channel


@pytest.fixture(autouse=True)
def fallbacks() -> Iterator[Any]:
    """`respx` in front of the two chain backends, so the fallback route is real.

    Autouse, because a turn that leaves the opencode route -- a cold first turn
    before the wiring exists, a dead server, a refused model -- lands on the chain,
    and an unmocked chain call would leave the test waiting on a DNS timeout
    instead of failing on an assertion. The opencode routes are NOT here: they are
    the fake server, and respx would intercept them. An unmocked call raises
    rather than escaping to the network.
    """
    with respx.mock(assert_all_called=False) as router:
        router.post(f"{ZEN_URL}/chat/completions").mock(side_effect=speaking(FALLBACK_ANSWER))
        router.post(f"{OPENROUTER_URL}/chat/completions").mock(side_effect=speaking(FALLBACK_ANSWER))
        # Loopback is NOT a double: the SSE tests point the real client at the real
        # server on 127.0.0.1, and a mock in front of it would be the very thing
        # those tests exist to avoid.
        router.route(host="127.0.0.1").pass_through()
        yield router


@pytest.fixture
async def stack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, serve_in_process: FakeOpencode, telegram: Telegram
) -> AsyncIterator[Stack]:
    """The real app over its real lifespan, with the server one process away.

    The client's transport is the only seam, and it is the seam todo 6 built for
    (`OpencodeClient(spec, directory, client=...)`). Everything above it is the
    code that ships: the lifespan wires the route, the broker and the worker, and
    `/health` reads what the startup found.
    """
    fake = serve_in_process
    install(tmp_path, monkeypatch)
    app = build_app()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://r2d2.test"
        ) as http:
            yield Stack(http=http, app=app, fake=fake, telegram=telegram)


@contextlib.contextmanager
def serving(app: FastAPI) -> Iterator[str]:
    """Run `app` on an ephemeral loopback port in this process; yield its base URL."""
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error", lifespan="off")
    )
    thread = threading.Thread(target=server.run, daemon=True, name="fake-opencode")
    thread.start()
    deadline = time.monotonic() + 10.0
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    if not server.started:
        server.should_exit = True
        raise RuntimeError("the fake opencode server did not start on loopback")
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10.0)


@pytest.fixture
def loopback(fake: FakeOpencode) -> Iterator[str]:
    """The fake served over TCP, or a skip when this run refuses outbound sockets.

    The two SSE tests need a real socket -- `httpx.ASGITransport` collects a
    response body before the client sees any of it, so a stream cannot be observed
    through it -- and a hermeticity run (`-p no_net`) forbids exactly that. The
    probe is the same thing the test is about: can this process open a connection
    at all?
    """
    with serving(fake.app()) as base_url:
        port = int(base_url.rsplit(":", 1)[1])
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=2.0):
                pass
        except OSError as exc:
            pytest.skip(f"this run refuses outbound sockets, so the server is unreachable: {exc}")
        yield base_url


@pytest.fixture
async def served(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, loopback: str, fake: FakeOpencode, telegram: Telegram
) -> AsyncIterator[Stack]:
    """The real app over its real lifespan, talking to the standalone fake over TCP.

    No client seam at all: the registry points at the served port, so the real
    `OpencodeClient` and the real `EventSource` open their own pools and speak
    HTTP. This is the only place the SSE reader is observed doing its job.
    """
    install(tmp_path, monkeypatch, loopback)
    app = build_app()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://r2d2.test"
        ) as http:
            yield Stack(http=http, app=app, fake=fake, telegram=telegram)


# ---------------------------------------------------------------------------
# 1. What Alice is allowed to receive
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "warm"),
    (
        pytest.param("привет", False, id="greeting"),
        pytest.param("помощь", False, id="help"),
        pytest.param(QUESTION, True, id="question"),
        pytest.param(DIGEST, True, id="escalation"),
        pytest.param(QUESTION, False, id="cold-first-turn"),
    ),
)
async def test_every_answer_is_the_alice_json_object(
    stack: Stack, command: str, warm: bool
) -> None:
    # Given / When: a turn of each shape, warmed first where the route needs it
    if warm:
        await stack.warm()
    body = await stack.ask(command)
    # Then the body is the documented envelope and nothing else
    assert set(body) == ALICE_KEYS
    assert body["version"] == ALICE_VERSION
    assert set(body["response"]) == RESPONSE_KEYS
    # ... the text and the spoken text are the same string, because Alice reads
    # one of them and there is no reason for them to differ
    assert body["response"]["text"] == body["response"]["tts"]
    # ... it is text, non-empty, and inside Alice's 1024-character limit
    assert isinstance(body["response"]["text"], str)
    assert 0 < len(body["response"]["text"]) <= MAX_TEXT


async def test_a_five_thousand_character_reply_is_truncated_to_the_alice_limit(stack: Stack) -> None:
    # Given a model that will not stop talking
    await stack.warm()
    stack.fake.say(LONG_REPLY)
    # When
    body = await stack.ask(QUESTION)
    # Then Alice is answered inside her limit, and the answer is still the model's
    assert len(body["response"]["text"]) <= MAX_TEXT
    assert body["response"]["text"].startswith("А")


async def test_a_markdown_link_never_reaches_alice_as_markdown(stack: Stack) -> None:
    # Given a reply carrying a link, which a voice cannot read and Alice rejects
    await stack.warm()
    stack.fake.say(MARKDOWN_REPLY)
    # When
    text = (await stack.ask(QUESTION))["response"]["text"]
    # Then no link syntax and no URL survive into the spoken answer
    assert "](" not in text
    assert "http" not in text
    assert "example.com" not in text


async def test_the_escalation_sentinel_never_reaches_alice(stack: Stack) -> None:
    # Given a model that asks for the agent instead of answering
    await stack.warm()
    assert SENTINEL in stack.fake.escalation
    # When
    body = await stack.ask(DIGEST)
    # Then the user is acknowledged, and the machine protocol is nowhere in the body
    assert SENTINEL not in json.dumps(body, ensure_ascii=False)
    assert body["response"]["text"] == ACK


# ---------------------------------------------------------------------------
# 2. The two routes, and what they cost
# ---------------------------------------------------------------------------


async def test_a_greeting_costs_no_opencode_traffic_at_all(stack: Stack) -> None:
    # Given / When: a greeting on a brand-new session
    body = await stack.ask("привет", new=True)
    # Then the brain answers it, because a greeting is not a question
    assert body["response"]["text"] == GREETING
    # ... and no session was created, no turn was posted and nothing was collected
    assert stack.fake.sessions == {}
    assert stack.fake.turns == []


async def test_the_first_turn_of_a_new_session_is_acknowledged_and_submitted(stack: Stack) -> None:
    # Given a user R2D2 has never seen, so the session is cold
    # When
    body = await stack.ask(QUESTION)
    # Then the user is acknowledged inside the budget rather than made to wait for
    # a cold cache (C8: 15.5-18.6 s against Alice's 4.5 s)
    assert body["response"]["text"] == ACK
    # ... and the work went to the AGENT, submitted and not awaited
    assert len(stack.fake.turns) == 1
    assert stack.fake.turns[0].agent == TASK_AGENT
    assert stack.fake.turns[0].submitted is True
    assert stack.fake.turns[0].model == MODEL_ID


async def test_a_cold_turn_delivers_what_the_agent_answered_to_telegram(stack: Stack) -> None:
    # Given a user R2D2 has never seen: the C8 branch, which used to submit the work
    # and return the ack with nobody left to read the answer back out of the session
    stack.fake.task_reply = AGENT_REPLY
    # When
    body = await stack.ask(QUESTION, app_id="cold-app")
    # Then the user is released with the ack ...
    assert body["response"]["text"] == ACK
    # ... and the agent's own text reaches Telegram, which is the only channel that
    # can push. On the live run the agent fetched sixty arxiv papers into this reply
    # and the user was told nothing at all, because no collector was armed.
    assert await stack.telegram.wait_for(AGENT_REPLY) == AGENT_REPLY
    # The anchor of a cold turn's collector is the empty string -- a session created
    # microseconds earlier has nothing to anchor on, and the collector reads "" as
    # "everything the session holds is newer". A turn that lands inside that window is
    # delivered with it, which is why the next test's warm-up collector reports this
    # turn's answer together with the one that followed it.


async def test_an_escalated_turn_delivers_what_the_agent_answered_to_telegram(
    stack: Stack,
) -> None:
    # Given a warm session whose OWN cold turn has been collected and delivered
    # already -- waiting for it is what keeps the second delivery unambiguous
    await stack.warm()
    assert "Сводка готова." in await stack.telegram.wait_for("Сводка готова.")
    stack.fake.task_reply = AGENT_REPLY
    # When the voice agent hands the turn to the full agent
    body = await stack.ask(DIGEST)
    # Then the user is released with the ack ...
    assert body["response"]["text"] == ACK
    # ... and the escalated turn's own answer is delivered, which is the promise the
    # `r2d2-agent` prompt makes and the branch used to break
    assert await stack.telegram.wait_for(AGENT_REPLY) == AGENT_REPLY


async def test_a_warm_session_answers_from_the_voice_agent(stack: Stack) -> None:
    # Given a session that has been used once
    await stack.warm()
    stack.fake.turns.clear()
    # When
    body = await stack.ask(QUESTION)
    # Then the model speaks, and its own words are what Alice says
    assert body["response"]["text"] == ANSWER
    # ... through the voice agent on the configured model, and synchronously
    assert [turn.agent for turn in stack.fake.turns] == [VOICE_AGENT]
    assert stack.fake.turns[0].submitted is False
    assert stack.fake.turns[0].model == MODEL_ID
    # ... into the session the store resolved, with ?directory= on the wire (C6)
    assert stack.fake.turns[0].directory == stack.cfg.r2d2_workspace


async def test_an_escalated_turn_is_acknowledged_and_handed_to_the_agent(stack: Stack) -> None:
    # Given a warm session and a model that asks for the agent instead of answering
    await stack.warm()
    stack.fake.turns.clear()
    # When the user asks for something slow
    body = await stack.ask(DIGEST)
    # Then the speaker is released immediately with the ack
    assert body["response"]["text"] == ACK
    # ... and the agent turn was submitted with the user's own request in it, so the
    # agent has the question even when the voice model offered no hint
    submitted = [turn for turn in stack.fake.turns if turn.submitted]
    assert len(submitted) == 1
    assert submitted[0].agent == TASK_AGENT
    assert submitted[0].model == MODEL_ID
    assert DIGEST in submitted[0].text


async def test_a_turn_that_outran_the_budget_is_collected_into_telegram(stack: Stack) -> None:
    # Given a warm session and a server that accepts the turn and works on it for
    # longer than the voice budget -- the C8 shape, and the branch that outran it
    await stack.warm()
    stack.fake.hang = True
    stack.fake.task_reply = AGENT_REPLY
    stack.cfg.r2d2_fast_deadline = 0.05
    # When
    body = await stack.ask(QUESTION)
    # Then the user is released with the ack ...
    assert body["response"]["text"] == ACK
    # ... and the answer the server finishes afterwards is collected by the worker
    # and pushed to Telegram, which is the only channel that can push. Both answers
    # are in ONE delivery: the turn that outran the budget and the agent turn it was
    # handed to share a collector, because a turn has exactly one.
    delivered = await stack.telegram.wait_for(ANSWER)
    assert ANSWER in delivered
    assert AGENT_REPLY in delivered


async def test_a_turn_that_outran_the_budget_still_gives_the_agent_the_request(
    stack: Stack,
) -> None:
    # Given a warm session and a voice turn that outruns the budget, with nothing in
    # the app yet to carry the request on: the branch acknowledged and collected, and
    # the request existed nowhere at all
    await stack.warm()
    stack.fake.turns.clear()
    stack.fake.hang = True
    stack.fake.task_reply = AGENT_REPLY
    stack.cfg.r2d2_fast_deadline = 0.05
    asked = "открой браузер на ноутбуке"
    # When
    body = await stack.ask(asked)
    # Then the user is acknowledged ...
    assert body["response"]["text"] == ACK
    # ... the turn that outran the budget was not thrown away ...
    assert stack.fake.aborted == []
    # ... and the AGENT was given the user's own words. Whether the late voice turn
    # will answer or ask for the agent is unknowable at the deadline, and guessing
    # that it will is what dropped every laptop-control request on the live run.
    submitted = [turn for turn in stack.fake.turns if turn.submitted]
    assert len(submitted) == 1, stack.fake.turns
    assert submitted[0].agent == TASK_AGENT
    assert asked in submitted[0].text
    # ... so the work it does reaches the user rather than a session and nowhere else
    assert await stack.telegram.wait_for(AGENT_REPLY) == AGENT_REPLY


async def test_two_questions_for_one_application_share_a_single_session(stack: Stack) -> None:
    # Given a user with a warm session
    await stack.warm()
    # When two more questions arrive for the same application
    await stack.ask(QUESTION)
    await stack.ask(QUESTION)
    # Then the session is reused: the server was asked to create exactly one, in
    # the whole conversation, and the binding never moved
    assert stack.asked("POST", "/session") == 1
    assert len(stack.fake.sessions) == 1
    # ... and every turn went into that one session, with the directory the client
    # is bound to (C6)
    assert {turn.session_id for turn in stack.fake.turns} == set(stack.fake.sessions)
    assert {turn.directory for turn in stack.fake.turns} == {stack.cfg.r2d2_workspace}


# ---------------------------------------------------------------------------
# 3. The opencode route is measured
# ---------------------------------------------------------------------------


async def test_an_opencode_turn_is_recorded_with_its_route_and_a_real_duration(
    stack: Stack, caplog: pytest.LogCaptureFixture
) -> None:
    # Given a warm session, so the turn is answered by the opencode route
    await stack.warm()
    caplog.clear()
    caplog.set_level(logging.INFO)
    # When
    await stack.ask(QUESTION)
    # Then the turn is on the record -- the primary route, whose latency is the
    # whole reason the record exists, used to emit nothing at all
    records = turn_records(caplog)
    assert len(records) == 1, [r.getMessage() for r in caplog.records]
    fields = records[0]
    assert fields["route"] == "opencode"
    assert fields["path"] == "voice"
    assert fields["model"] == MODEL
    assert fields["agent"] == VOICE_AGENT
    assert fields["escalated"] == "False"
    # ... with a model call that really took time, and a turn inside the budget
    assert int(fields["llm_ms"]) > 0
    assert int(fields["total_ms"]) >= int(fields["llm_ms"]) < BUDGET_MS


async def test_a_turn_that_outruns_the_voice_budget_is_recorded_as_a_deadline(
    stack: Stack, caplog: pytest.LogCaptureFixture
) -> None:
    # Given a warm session and a server that accepts the turn and never answers
    await stack.warm()
    stack.fake.hang = True
    stack.cfg.r2d2_fast_deadline = 0.05
    caplog.clear()
    caplog.set_level(logging.INFO)
    # When
    body = await stack.ask(QUESTION)
    # Then the user is acknowledged rather than left in silence ...
    assert body["response"]["text"] == ACK
    # ... and the record says the turn was neither answered nor escalated: it ran
    # out of budget and was handed to the collector
    fields = turn_records(caplog)[-1]
    assert fields["route"] == "opencode"
    assert fields["path"] == "deadline"
    assert fields["agent"] == VOICE_AGENT
    assert int(fields["llm_ms"]) >= 50
    # ... and nothing aborted the turn server-side: it is still running, and the
    # collector is what fetches it
    assert stack.fake.aborted == []


# ---------------------------------------------------------------------------
# 4. The server going away
# ---------------------------------------------------------------------------


async def test_a_dead_opencode_server_is_answered_from_the_chain_and_the_process_stays_up(
    stack: Stack
) -> None:
    # Given a user with a warm session, and then the server disappears
    await stack.warm()
    stack.fake.error_status = 503
    # When the same user asks again
    body = await stack.ask(QUESTION)
    # Then the fallback chain answers -- valid Alice JSON, from a provider that works
    assert set(body) == ALICE_KEYS
    assert body["response"]["text"] == FALLBACK_ANSWER
    # ... and /health reports the degradation in a field rather than a status code
    health = await stack.health()
    assert health["status"] == "ok"
    assert health["opencode"]["reachable"] is False
    assert health["chain"] == ["zen", "openrouter"]
    # ... and the process is still serving: a third question is answered too
    assert (await stack.ask(QUESTION))["response"]["text"] == FALLBACK_ANSWER


async def test_every_fallback_backend_failing_still_answers_with_the_graceful_text(
    stack: Stack, fallbacks: Any
) -> None:
    # Given an opencode server that is down AND a chain with nowhere to go, so the
    # turn raises rather than being answered
    stack.fake.error_status = 503
    for route in fallbacks.routes:
        route.mock(return_value=httpx.Response(500, json={"error": {"message": "refused"}}))
    # When
    response = await stack.http.post("/webhook", json=alice_body(QUESTION))
    body = json_of(response)
    # Then the user is told the graceful text, and no traceback reaches the platform
    assert response.status_code == 200
    assert body["response"]["text"] == ERROR_TEXT
    assert "Traceback" not in response.text


async def test_health_does_not_hang_on_a_server_that_accepts_and_never_answers(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a server that takes the connection and never answers, which a mock
    # cannot express and which the route's own bound exists for
    stack.fake.health_hang_s = 30.0
    # When
    started = time.monotonic()
    health = await stack.health()
    elapsed = time.monotonic() - started
    # Then /health answered anyway, and reported the degradation
    assert elapsed < HEALTH_CEILING_S, f"/health took {elapsed:.1f}s against a hung opencode"
    assert health["opencode"]["reachable"] is False
    assert health["status"] == "ok"


# ---------------------------------------------------------------------------
# 5. The permission round trip -- a real socket, a real stream
# ---------------------------------------------------------------------------


async def test_a_permission_ask_reaches_telegram_and_a_yes_posts_once(
    served: Stack
) -> None:
    # Given a warm session with a live event stream, and an ask the server raises
    await served.warm()
    session_id = next(iter(served.fake.sessions))
    await served.fake.wait_for_stream()
    served.fake.ask_permission(session_id, title="echo привет")
    # When the broker puts the question where a human will see it
    question = await served.telegram.wait_for(PERMISSION_QUESTION)
    # Then the question names the command, and no durable grant is ever mentioned
    # as something R2D2 sent
    assert "echo привет" in question
    assert served.fake.answers() == []
    # When the user says yes, in the same session that owns the ask
    body = await served.ask("да")
    # Then the answer lands as `once` and the user is told it was accepted
    assert body["response"]["text"] == APPROVED_TEXT
    assert served.fake.answers() == ["once"]
    assert served.fake.permission_answers[0][1] == "perm_1"


async def test_the_telegram_round_trip_answers_the_ask_it_raised_itself(served: Stack) -> None:
    # Given a question that arrived over Telegram, so the ask belongs to the
    # Telegram application key rather than to an Alice one
    await served.say_telegram(f"{QUESTION} {DIGEST}")
    session_id = next(iter(served.fake.sessions))
    await served.fake.wait_for_stream()
    served.fake.ask_permission(session_id, title="echo привет")
    await served.telegram.wait_for(PERMISSION_QUESTION)
    # When the same user answers "да" in Telegram
    await served.say_telegram("да")
    # Then the one-time approval reached the server
    assert served.fake.answers() == ["once"]


# ---------------------------------------------------------------------------
# 6. Lifecycle: start, stop, start again
# ---------------------------------------------------------------------------


async def test_a_start_stop_cycle_leaves_no_task_and_nothing_destroyed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, serve_in_process: FakeOpencode,
    telegram: Telegram, caplog: pytest.LogCaptureFixture,
) -> None:
    # Given the app started once already, so the tasks below are a SECOND set
    install(tmp_path, monkeypatch)

    async def cycle() -> None:
        app = build_app()
        async with app.router.lifespan_context(app):
            await asyncio.sleep(0)

    await cycle()
    mine = asyncio.current_task()
    before = {task for task in asyncio.all_tasks() if task is not mine}
    # When it is started and stopped again
    await cycle()
    await asyncio.sleep(0.05)
    after = {task for task in asyncio.all_tasks() if task is not mine and not task.done()}
    # Then nothing it started is still running, and nothing was destroyed on the way
    assert not after - before
    assert "Task was destroyed but it is pending" not in caplog.text
    assert "Task exception was never retrieved" not in caplog.text


async def test_a_second_start_does_not_double_start_the_broker_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, serve_in_process: FakeOpencode,
    telegram: Telegram, caplog: pytest.LogCaptureFixture,
) -> None:
    # Given an app that has already been started and stopped once
    install(tmp_path, monkeypatch)
    app = build_app()
    async with app.router.lifespan_context(app):
        pass
    caplog.clear()
    caplog.set_level(logging.INFO)
    # When it is started a second time
    app = build_app()
    async with app.router.lifespan_context(app):
        armed = [r for r in caplog.records if "sweep armed" in r.getMessage()]
    # Then the sweep is armed exactly once per start, with no stale state carried
    # over from the first one -- `PermissionBroker.start()` says so in a WARNING
    # and that line must not be here
    assert len(armed) == 1
    assert "already armed" not in caplog.text


async def test_the_startup_sweep_reaps_a_session_the_server_still_calls_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, serve_in_process: FakeOpencode,
    telegram: Telegram,
) -> None:
    # Given a user whose session a crashed run left BUSY, older than the staleness
    # window -- a reaper that does not run leaves that turn running for ever
    fake = serve_in_process
    install(tmp_path, monkeypatch, r2d2_stale_session_seconds=0.0)
    fake.sessions["ses_stuck"] = {
        "id": "ses_stuck",
        "title": "r2d2:alice:alice-app-1",
        "directory": "",
    }
    fake.busy_sessions.add("ses_stuck")
    memory = await Memory(str(tmp_path / "sessions.db")).connect()
    await memory.bind_oc_session(APP_ID, "ses_stuck", "r2d2:alice:alice-app-1")
    await memory.touch_oc_session(APP_ID, message_delta=0)
    await memory.close()
    # When the app starts
    app = build_app()
    async with app.router.lifespan_context(app):
        pass
    # Then the startup sweep asked the server to stop it -- a staleness window of
    # zero makes "unused for longer than the window" unambiguous in a test
    assert fake.aborted == ["ses_stuck"]


# ---------------------------------------------------------------------------
# 7. The startup log is not allowed to lie
# ---------------------------------------------------------------------------


async def test_a_healthy_startup_says_the_route_is_wired(
    stack: Stack, caplog: pytest.LogCaptureFixture
) -> None:
    # Given / When: the stack fixture has started the app against a live server
    messages = startup_lines(caplog)
    # Then the startup line names the route as wired, not merely as "started" --
    # "started" alone is exactly the misleading success this pins down
    started = [line for line in messages if line.startswith(STARTED_PREFIX)]
    assert started, messages
    assert "opencode=wired" in started[0]
    # ... and the models were actually checked, rather than waved through
    assert "models=opencode/space-bunny-free" in started[0]
    # ... and a healthy startup logged nothing at ERROR
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


async def test_a_registry_without_an_opencode_backend_says_the_route_is_unwired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, telegram: Telegram,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Given a registry that declares no opencode backend at all -- a supported
    # deployment, because the fallback chain is a brain too
    install(tmp_path, monkeypatch)
    registry = tmp_path / "backends.json"
    document = json.loads(registry.read_text(encoding="utf-8"))
    document["chain"] = ["zen", "openrouter"]
    document["backends"] = [b for b in document["backends"] if b["name"] != "opencode"]
    registry.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(
        main_module, "OpencodeClient", lambda *a, **k: pytest.fail("no opencode client expected")
    )
    # When
    app = build_app()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://r2d2.test"
        ) as http:
            body = json_of(await http.post("/webhook", json=alice_body(QUESTION)))
    # Then the answer came from the chain, and the startup line said out loud that
    # the opencode route is not in use -- an operator must never read "started"
    # as "the opencode brain is live"
    started = [r for r in caplog.records if r.getMessage().startswith(STARTED_PREFIX)]
    assert started and "opencode=unwired" in started[0].getMessage()
    assert [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert body["response"]["text"] == FALLBACK_ANSWER


async def test_a_model_this_server_does_not_list_refuses_the_route_and_names_the_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, serve_in_process: FakeOpencode,
    telegram: Telegram, caplog: pytest.LogCaptureFixture,
) -> None:
    # Given a registry naming a model the server does not offer: opencode would
    # substitute a DIFFERENT one and answer 200, so the user would be answered by
    # a brain nobody chose (C1)
    install(tmp_path, monkeypatch)
    registry = tmp_path / "backends.json"
    document = json.loads(registry.read_text(encoding="utf-8"))
    document["backends"][0]["fast_model"] = "opencode/model-that-does-not-exist"
    registry.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    # When
    app = build_app()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://r2d2.test"
        ) as http:
            body = json_of(await http.post("/webhook", json=alice_body(QUESTION)))
    # Then R2D2 still starts and still answers, from the chain ...
    assert body["response"]["text"] == FALLBACK_ANSWER
    # ... the offending model is named in an ERROR, not in a warning nobody reads ...
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors, [r.getMessage() for r in caplog.records]
    assert any("model-that-does-not-exist" in r.getMessage() for r in errors)
    # ... and the startup line does not claim a live opencode route
    started = [r for r in caplog.records if r.getMessage().startswith(STARTED_PREFIX)]
    assert started and "opencode=unwired" in started[0].getMessage()


# ---------------------------------------------------------------------------
# 8. Malformed input, and secret hygiene
# ---------------------------------------------------------------------------


async def test_a_webhook_that_is_not_json_is_a_400(stack: Stack) -> None:
    # Given a body Alice would never send
    response = await stack.http.post(
        "/webhook", content=b"{not json", headers={"content-type": "application/json"}
    )
    # Then it is refused as bad input, in JSON, with no traceback
    assert response.status_code == 400
    assert json_of(response) == {"error": "bad json"}
    assert "Traceback" not in response.text


async def test_a_webhook_without_a_session_is_refused_rather_than_answered(stack: Stack) -> None:
    # Given a body with no `session` at all, so there is no application to answer
    # and no skill to check -- the whitelist is the only thing between a public
    # webhook and somebody's laptop
    response = await stack.http.post("/webhook", json={"request": {"command": QUESTION}})
    # Then it is refused, in JSON
    assert response.status_code == 403
    assert json_of(response) == {"error": "forbidden"}


async def test_no_credential_reaches_a_log_record_a_body_or_the_server(
    stack: Stack, caplog: pytest.LogCaptureFixture
) -> None:
    # Given a deployment whose every credential is a sentinel value, expanded by
    # the real loader from the real environment, so the specs really hold them
    specs = load_backend_specs(Config.load())[1]
    assert OC_PASSWORD in specs["opencode"].password
    # When a whole turn runs, with the log at its most verbose
    caplog.set_level(logging.DEBUG)
    await stack.warm()
    responses = [await stack.http.get("/health"), await stack.http.get("/diagnostics/providers")]
    # Then no log record, no public body and no recorded wire payload carries one
    for secret in SECRETS:
        assert secret not in caplog.text, f"{secret[:12]}... reached a log record"
        for response in responses:
            assert secret not in response.text, f"{secret[:12]}... reached {response.request.url.path}"
    for turn in stack.fake.turns:
        assert all(secret not in turn.text for secret in SECRETS)
    # ... and neither body names the workspace the agent's file tools are rooted at
    for response in responses:
        assert WORKSPACE_NAME not in response.text


# ---------------------------------------------------------------------------
# 9. The app is still the app
# ---------------------------------------------------------------------------


async def test_the_root_route_names_the_routes_that_exist(stack: Stack) -> None:
    # Given / When the owner is looking for what this process serves
    body = json_of(await stack.http.get("/"))
    # Then the two diagnostics routes are discoverable without reading the source
    assert body["webhook"] == "/webhook"
    assert body["tg"] == "/tg/webhook"
    assert body["health"] == "/health"
    assert body["diagnostics"] == "/diagnostics/providers"


async def test_the_app_package_never_manages_the_server_process() -> None:
    # Given every module of the ASGI application, parsed rather than grepped
    import ast

    sources = {
        path.name: path.read_text(encoding="utf-8")
        for path in Path(main_module.__file__).parent.glob("*.py")
    }
    # When each one is parsed
    forbidden: set[str] = set()
    for source in sources.values():
        called = {
            node.func.id if isinstance(node.func, ast.Name) else node.func.attr
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call)
        }
        forbidden |= {name for name in called if name in {"system", "popen", "exec", "execv", "fork", "spawn"}}
    # Then no module names a process-control API at all: a systemd user unit owns
    # the opencode process (scripts/r2d2-opencode.service), and R2D2 starting or
    # killing one would put a live server's lifetime inside a webhook's lifetime
    assert not forbidden, f"{sorted(forbidden)} in {sorted(sources)}"
    assert not [name for name, text in sources.items() if "subprocess" in text]
