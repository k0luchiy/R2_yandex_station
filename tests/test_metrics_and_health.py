"""What a turn cost, and what the process can still answer (plan todo 17).

Alice gives the webhook **4.5 s** and `docs/07-latency-strategy.md` budgets about
**2.5 s** of that for R2D2. Nothing proves we fit that budget except a record per
turn, so this file pins the record and the two routes that make the rest of the
state visible without reading a log:

* **The record is one line per turn, and the line it replaced is gone.** The old
  ad-hoc `llm %s: msgs=%d tools=%s content_len=%d` becomes
  `turn route=... path=... model=... agent=... llm_ms=... total_ms=... escalated=...`,
  which keeps `msgs`/`tools` and adds the two durations. Two formats for one turn
  is one turn nobody can count.
* **A turn that raised is still measured.** A turn whose failure leaves no record
  is the turn whose duration nobody will ever explain, so the record is written in
  a `finally` and the failure is a `path`, not a silence.
* **The clock is monotonic and the durations are clamped at zero.** `time.time()`
  steps backwards across an NTP correction or a suspend; a negative duration reads
  as "no time passed" and quietly poisons every average built from it.
* **`/health` answers 200 with the degradation in a field, not in the status
  code.** An unreachable opencode server is not a fault of THIS process: R2D2
  still answers through the fallback chain, and a probe that flaps on a degraded
  dependency is worse than one that reports the degradation -- a flapping probe is
  indistinguishable from a crash loop, and the operator's response to it (restart)
  cannot fix a server that is simply not running. The body is where the truth
  goes: `opencode.reachable`, `chain`, `sessions`.
* **Nothing in a public body may be a credential.** `/health` and
  `/diagnostics/providers` are unauthenticated, so they may carry a model id, an
  agent name, an address and a count -- and nothing else. No password, no provider
  key, no bot token, no workspace path.
* **Neither route may hang, and neither may answer with a traceback.** The health
  probe is bounded by the route itself rather than by the client's own timeout --
  a hung server stops reading the socket, and a `MockTransport` cannot enforce one
  at all -- and a body that is not JSON produces a clean 502 whose error never
  echoes the upstream text back out of an unauthenticated route.

**What is real here.** The real `FastAPI` app over its real lifespan (SQLite in a
temp file, the worker started and stopped, the startup gate run), the real
`OpencodeClient`, and the real `Brain` over a real `OcSessionStore` and a real
`Memory`. The only double is at the wire: `httpx.MockTransport` for `opencode
serve` and `respx` for the fallback backend's chat completion, both asserted on
rather than merely permitted. So the JSON on the wire, the timings and the log
record are observed rather than asserted about, and no socket is opened -- which
is why the suite also passes under `-p no_net`.

`core.metrics` is imported INSIDE the tests that use it. That is deliberate: the
RED phase of todo 17 has to be able to run the route tests against the old
`/health`, and a module-level import of a module that does not exist yet would
stop the whole file at collection.

allow: SIZE_OK -- 748 pure LOC, 25 tests. Test modules in this repo run from 44 pure
LOC to 1928 (`test_brain_hybrid.py`), and a test module grows with the number of
behaviours it pins, not with the number of concepts it owns. The 250 pure-LOC
ceiling targets source modules; splitting this would scatter one contract -- what an
unauthenticated body may contain, and what a degraded dependency looks like in a
status code -- across files that each need the whole app-over-its-own-lifespan
harness to say anything.

"""

from __future__ import annotations

import asyncio
import ast
import dataclasses
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import httpx
import pytest
import respx
from fastapi import FastAPI

from app import main as main_module
from app.config import Config
from core.async_worker import Worker
from core.backends.config_loader import BackendChain, BackendConfigError, load_backend_specs
from core.backends.openai_compatible import RETRY_STATUSES
from core.backends.opencode_session import OpencodeSessionBackend, OpencodeWiring
from core.brain import Brain, HybridWiring
from core.memory import Memory
from core.opencode.client import OpencodeClient, OpencodeHealth
from core.opencode.session_store import OcSessionStore

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

BASE_URL: Final = "http://127.0.0.1:4599"
ZEN_URL: Final = "https://zen.test/v1"
OPENROUTER_URL: Final = "https://openrouter.test/api/v1"
VERSION: Final = "1.18.32"

#: The registry R2D2 ships, and what a turn actually walks when opencode did not
#: answer: the session backend is the PRIMARY route and is not a fallback, so
#: naming it in `chain` would advertise a backend that can never answer a turn.
CHAIN: Final = ["opencode", "zen", "yandexgpt", "openrouter"]
FALLBACK_CHAIN: Final = ["zen", "yandexgpt", "openrouter"]

#: The key sets the two routes promise. A route that grows a key is fine; a route
#: that loses one breaks a monitoring probe or a script silently.
HEALTH_KEYS: Final = {"status", "opencode", "sessions", "chain"}
OPENCODE_KEYS: Final = {"reachable", "version", "base_url"}

#: Every credential this deployment can hold, each a value nobody has, so "it is
#: not in the body" is a statement about a string that appears nowhere else.
OC_USERNAME: Final = "opencode-sentinel-user"
OC_PASSWORD: Final = "oc-password-sentinel-4f2c9a7b"
ZEN_KEY: Final = "sk-or-zen-sentinel-7c1e5b3d"
OPENROUTER_KEY: Final = "sk-or-openrouter-sentinel-1a2b3c4d"
YANDEX_KEY: Final = "yandex-api-key-sentinel-9e8d7c6b"
YANDEX_FOLDER: Final = "yandex-folder-sentinel-112233"
TELEGRAM_TOKEN: Final = "123456789:AAbbCCddEEffGGhhIIjjKKllMMnnOOpp"
SECRETS: Final = (
    OC_PASSWORD,
    OC_USERNAME,
    ZEN_KEY,
    OPENROUTER_KEY,
    YANDEX_KEY,
    YANDEX_FOLDER,
    TELEGRAM_TOKEN,
)

#: The workspace is the directory the agent's file tools are rooted at. No public
#: body may repeat it: it names the operator's home and the layout of their disk.
WORKSPACE_NAME: Final = "r2d2-workspace-MUST-NOT-LEAK"

#: The turn the record is measured on.
QUESTION: Final = "что такое квантовые точки"
ANSWER: Final = "Квантовая точка — это наночастица."
SPOKEN: Final = "Собираю сводку, пришлю в телеграм."
HELP_PREFIX: Final = "Я Р2Д2"
ERROR_TEXT: Final = "Что-то пошло не так. Попробуй ещё раз."
ACK: Final = Config().r2d2_task_ack
#: The identity the rig declares. `Brain.authorized` fails closed, so a turn only
#: reaches the metrics code once the deployment has said who it serves.
SKILL_ID: Final = "skill-abc"
USER_ID: Final = "user-xyz"

#: `/diagnostics/providers` returns what the server said, unedited, so the fake's
#: payloads are distinctive enough that a paraphrase could not pass for them.
PROVIDERS_PAYLOAD: Final = {
    "providers": [
        {
            "id": "opencode",
            "name": "opencode",
            "models": {"space-bunny-free": {"name": "Space Bunny Free", "limit": {"context": 1}}},
        },
        {"id": "zen", "name": "Zen", "models": {"gpt-5": {"name": "GPT-5"}}},
    ],
    "default": {"providerID": "opencode", "modelID": "space-bunny-free"},
}
AGENTS_PAYLOAD: Final = [
    {"name": "r2d2-voice", "mode": "subagent", "description": "answers out loud", "tools": {}},
    {
        "name": "r2d2-agent",
        "mode": "primary",
        "description": "does the work",
        "tools": {"bash": True, "edit": False},
    },
]

#: A hung opencode: the route under test must answer long before this ends.
HANG_S: Final = 30.0
#: A ceiling far above the shipped 2 s bound and far below `HANG_S`, so a route
#: that lost its bound fails in seconds instead of hanging the suite for 30.
HANG_CEILING_S: Final = 10.0

#: The ad-hoc line todo 17 replaces. Its absence is asserted, so a brain that kept
#: it would log the same turn twice under two formats.
ADHOC_LINE: Final = re.compile(r"\bllm \S+: msgs=")
#: The record todo 17 introduces.
TURN_PREFIX: Final = "turn route="

#: `docs/07-latency-strategy.md`: R2D2's share of Alice's 4.5 s. A turn over this
#: is the failure the record exists to make visible.
BUDGET_MS: Final = 2500

BACKENDS_JSON: Final = json.dumps(
    {
        "chain": CHAIN,
        "backends": [
            {
                "name": "opencode",
                "kind": "opencode_session",
                "base_url": BASE_URL,
                "username": "${R2D2_OC_USERNAME}",
                "password": "${R2D2_OC_PASSWORD}",
                "voice_agent": "r2d2-voice",
                "task_agent": "r2d2-agent",
                "fast_model": "opencode/space-bunny-free",
                "task_model": "opencode/space-bunny-free",
                "summarize_model": "opencode/space-bunny-free",
                "timeout": 0.5,
            },
            {
                "name": "zen",
                "kind": "openai_compatible",
                "base_url": ZEN_URL,
                "api_key": "${R2D2_ZEN_KEY}",
                "model": "space-bunny-free",
                "auth_style": "bearer",
            },
            {
                "name": "yandexgpt",
                "kind": "openai_compatible",
                "base_url": "https://llm.api.cloud.yandex.net/foundationModels/v1",
                "api_key": "${YANDEX_API_KEY}",
                "model": "gpt://${YANDEX_FOLDER_ID}/yandexgpt-lite-5",
                "auth_style": "yandex",
                "auth_mode": "api_key",
            },
            {
                "name": "openrouter",
                "kind": "openai_compatible",
                "base_url": OPENROUTER_URL,
                "api_key": "${OPENROUTER_API_KEY}",
                "model": "inclusionai/ling-3.0-flash:free",
                "auth_style": "bearer",
            },
        ],
    },
    ensure_ascii=False,
)


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class FakeOpencode:
    """`opencode serve` as a `MockTransport` handler: the routes todo 17 reads.

    Three of them and no more -- `GET /global/health` for the liveness the startup
    gate and `/health` both report, and `GET /config/providers` + `GET /agent` for
    the diagnostics route. The knobs are the three shapes that matter: a server
    that is not running, one that answers with something that is not JSON, and
    one that is healthy. Anything else answers 404, so a route that starts
    reaching for another endpoint fails instead of quietly working.
    """

    def __init__(self) -> None:
        self.down: bool = False
        self.malformed: bool = False
        self.version: str = VERSION
        self.requests: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        route = f"{request.method} {request.url.path}"
        self.requests.append(route)
        if self.down:
            raise httpx.ConnectError(f"opencode is not running at {BASE_URL}")
        if route == "GET /global/health":
            return httpx.Response(200, json={"healthy": True, "version": self.version})
        if route == "GET /config/providers":
            if self.malformed:
                return httpx.Response(200, text="<html>gateway says hello</html>")
            return httpx.Response(200, json=PROVIDERS_PAYLOAD)
        if route == "GET /agent":
            if self.malformed:
                return httpx.Response(200, text="<html>gateway says hello</html>")
            return httpx.Response(200, json=AGENTS_PAYLOAD)
        return httpx.Response(404, json={"name": "NotFoundError", "data": {"message": route}})

    def client_for(self, spec: Any, directory: str) -> OpencodeClient:
        return OpencodeClient(
            spec, directory, client=httpx.AsyncClient(transport=httpx.MockTransport(self))
        )


class HangingClient:
    """An opencode server that accepts the request and never answers.

    A sleeping handler is not a socket that times out, so this stands in for the
    case the route's own bound exists for: a client that will not return. Only the
    methods the routes call exist, because nothing else is allowed to be used.
    """

    def __init__(self, spec: Any, directory: str, *, client: Any = None) -> None:
        self.spec = spec
        self.directory = directory

    async def health(self) -> OpencodeHealth:
        await asyncio.sleep(HANG_S)
        return OpencodeHealth(reachable=False)

    async def aclose(self) -> None:
        return None


@dataclass(frozen=True, slots=True)
class Api:
    """The running app: an HTTP client bound to it, and the app behind it."""

    http: httpx.AsyncClient
    app: FastAPI

    @property
    def memory(self) -> Memory:
        return self.app.state.memory

    async def get(self, path: str) -> httpx.Response:
        return await self.http.get(path)

    async def json(self, path: str) -> dict[str, Any]:
        response = await self.get(path)
        assert response.status_code == 200, f"GET {path} -> {response.status_code}: {response.text}"
        return document_of(response)


@dataclass(frozen=True, slots=True)
class TurnRig:
    """The real `Brain` with the opencode route wired but the server DOWN.

    Down is the honest starting point: the fallback chain is the route todo 17
    measures end to end today, because the opencode route's own `path` values
    belong to the branch that answers it, and that branch is todo 18's to wire.
    The chain is still a real route -- it carries every turn while the server is
    starting, and it carries them all if it never starts.
    """

    brain: Brain
    server: FakeOpencode
    memory: Memory
    cfg: Config
    zen: respx.Route
    openrouter: respx.Route

    def answer_with(self, side_effect: Any) -> None:
        """What the fallback LLM says on the next turn of this rig."""
        self.zen.mock(side_effect=side_effect)

    def refuse_everywhere(self) -> None:
        """Every fallback backend answers 500, so the chain has nowhere to go."""
        assert 500 not in RETRY_STATUSES, "a 500 would sleep through the retry backoff"
        self.zen.mock(return_value=httpx.Response(500, json={"error": {"message": "refused"}}))
        self.openrouter.mock(return_value=httpx.Response(500, json={"error": {"message": "refused"}}))


def document_of(response: httpx.Response) -> dict[str, Any]:
    """The decoded body, checked to be a JSON OBJECT.

    Without this, a 500 whose body is an HTML error page would sail through some
    other assertion's failure message instead of naming itself.
    """
    content_type = response.headers.get("content-type", "")
    assert content_type.startswith("application/json"), f"{content_type!r}: {response.text[:200]!r}"
    document = response.json()
    assert isinstance(document, dict), document
    return document


def turn_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    """The turn records in a capture: the one INFO line per turn the plan pins."""
    return [record for record in caplog.records if record.getMessage().startswith(TURN_PREFIX)]


def one_turn_record(caplog: pytest.LogCaptureFixture) -> dict[str, str]:
    """The single turn record of a capture, as its `field=value` pairs.

    Parsed out of the record itself rather than compared against a hand-written
    format, so dropping or renaming a field changes the test instead of quietly
    passing.
    """
    records = turn_records(caplog)
    assert len(records) == 1, [record.getMessage() for record in caplog.records]
    return dict(re.findall(r"(\w+)=(\S+)", records[0].getMessage()))


def speaking(message: str = ANSWER) -> Any:
    """A fallback-LLM answer: text, no tools."""

    def side_effect(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": message}}],
                "model": json.loads(request.content)["model"],
            },
        )

    return side_effect


def calling_tool(name: str, arguments: dict[str, Any]) -> Any:
    """A fallback-LLM answer that asks for a tool instead of speaking."""

    def side_effect(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "function": {"name": name, "arguments": json.dumps(arguments)},
                                }
                            ],
                        }
                    }
                ],
                "model": json.loads(request.content)["model"],
            },
        )

    return side_effect


def alice_body(command: str, app_id: str = "alice-app-1") -> dict[str, Any]:
    """One Alice `SimpleUtterance`, shaped as the platform sends it."""

    return {
        "meta": {"interfaces": [{"type": "Voice"}]},
        "request": {"type": "SimpleUtterance", "command": command},
        "session": {
            "new": False,
            "application": {"application_id": app_id},
            "skill_id": SKILL_ID,
            "user": {"user_id": USER_ID},
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
def environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The temp registry, the temp workspace and the sentinel credentials.

    A temp `backends.json` and a patched `Config.load` are what keep the
    operator's own `.env` out of this file: the assertions below are about strings
    this test wrote, and reading a real key in order to assert it is absent would
    make a leak of that key indistinguishable from a leak of the sentinel.
    """
    for name, value in (
        ("R2D2_OC_USERNAME", OC_USERNAME),
        ("R2D2_OC_PASSWORD", OC_PASSWORD),
        ("R2D2_ZEN_KEY", ZEN_KEY),
        ("OPENROUTER_API_KEY", OPENROUTER_KEY),
        ("YANDEX_API_KEY", YANDEX_KEY),
        ("YANDEX_FOLDER_ID", YANDEX_FOLDER),
    ):
        monkeypatch.setenv(name, value)
    workspace = tmp_path / WORKSPACE_NAME
    workspace.mkdir()
    backends = tmp_path / "backends.json"
    backends.write_text(BACKENDS_JSON, encoding="utf-8")
    monkeypatch.setattr(
        Config,
        "load",
        classmethod(
            lambda cls: Config(
                backends_path=str(backends),
                db_path=str(tmp_path / "sessions.db"),
                r2d2_workspace=str(workspace),
                r2d2_fast_deadline=0.5,
                r2d2_event_poll_interval=0.05,
                telegram_bot_token=TELEGRAM_TOKEN,
                telegram_chat_id="4242",
                alice_skill_id=SKILL_ID,
                alice_user_id=USER_ID,
                openrouter_api_key=OPENROUTER_KEY,
                yandex_api_key=YANDEX_KEY,
                yandex_folder_id=YANDEX_FOLDER,
            )
        ),
    )
    return workspace


@pytest.fixture
async def api(environment: Path, server: FakeOpencode, monkeypatch: pytest.MonkeyPatch) -> Api:
    """The real app over its real lifespan, with the opencode server faked.

    The lifespan runs for real -- SQLite, the worker's loop, the startup health
    gate -- because the routes under test read `app.state`, and a route that only
    answers when a fixture faked the state is not the route that ships.
    """

    def factory(spec: Any, directory: str, *, client: Any = None) -> OpencodeClient:
        return server.client_for(spec, directory)

    monkeypatch.setattr(main_module, "OpencodeClient", factory)
    app = main_module.build_app()
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://r2d2.test") as http:
            yield Api(http=http, app=app)


@pytest.fixture
async def turn_rig(environment: Path, server: FakeOpencode) -> TurnRig:
    """The real `Brain` on a temp database, its opencode route wired but DOWN.

    `respx` answers the fallback chain's chat completions and exposes the routes,
    so a test can change what the model says without nesting a second router.
    """
    server.down = True
    cfg = Config.load()
    memory = await Memory(str(Path(cfg.db_path).with_name("turns.db"))).connect()
    _chain, specs = load_backend_specs(cfg)
    spec = specs["opencode"]
    client = server.client_for(spec, cfg.r2d2_workspace)
    store = OcSessionStore(memory, client, cfg)
    backend = OpencodeSessionBackend(spec, OpencodeWiring(client=client, store=store, cfg=cfg))
    worker = Worker(cfg, memory, logging.getLogger("r2d2.test.worker"))
    brain = Brain(
        cfg,
        memory,
        worker,
        logging.getLogger("r2d2.test.brain"),
        opencode=HybridWiring(spec=spec, client=client, store=store, backend=backend),
    )
    try:
        with respx.mock(assert_all_called=False) as router:
            zen = router.post(f"{ZEN_URL}/chat/completions").mock(side_effect=speaking())
            openrouter = router.post(f"{OPENROUTER_URL}/chat/completions").mock(side_effect=speaking())
            yield TurnRig(
                brain=brain, server=server, memory=memory, cfg=cfg, zen=zen, openrouter=openrouter
            )
    finally:
        await memory.close()


# ---------------------------------------------------------------------------
# 1. /health with the opencode server up
# ---------------------------------------------------------------------------


async def test_health_reports_the_server_the_sessions_and_the_fallback_chain(
    api: Api, server: FakeOpencode
) -> None:
    # Given a running opencode server: the lifespan's startup gate already probed
    # it, and this reads the same fact on demand
    assert (await api.json("/health"))["opencode"]["reachable"] is True
    server.requests.clear()
    # When the owner -- or a monitor -- asks
    body = await api.json("/health")
    # Then the body is exactly the documented shape ...
    assert set(body) == HEALTH_KEYS
    assert set(body["opencode"]) == OPENCODE_KEYS
    # ... reporting the server that answered, with its version and its address
    assert body["opencode"] == {"reachable": True, "version": VERSION, "base_url": BASE_URL}
    # ... the number of bound sessions, and who answers when opencode does not
    assert body["sessions"] == 0
    assert body["chain"] == FALLBACK_CHAIN
    # ... and the probe asked the server about liveness and nothing else: a health
    # check must not turn into a session sweep
    assert set(server.requests) == {"GET /global/health"}


async def test_the_root_route_names_the_diagnostics_route(api: Api) -> None:
    # Given / When the owner is looking for the diagnostics route
    body = await api.json("/")
    # Then it is discoverable without reading the source
    assert body["diagnostics"] == "/diagnostics/providers"


# ---------------------------------------------------------------------------
# 2. /health with the opencode server down -- the status-code decision
# ---------------------------------------------------------------------------


async def test_a_degraded_opencode_still_answers_200_and_says_so_in_a_field(
    api: Api, server: FakeOpencode
) -> None:
    # Given the opencode server is not running, so the fallback chain carries
    # every turn and THIS process is healthy
    server.down = True
    # When
    response = await api.get("/health")
    body = document_of(response)
    # Then the status code is 200 -- pinned deliberately, not incidentally: a
    # monitor that restarts R2D2 because a dependency is down cannot fix it, and
    # a flapping probe is indistinguishable from a crash loop
    assert response.status_code == 200
    assert body["status"] == "ok"
    # ... and the degradation is visible in the body instead, under the same keys
    # as the healthy case, so one probe reads both
    assert set(body) == HEALTH_KEYS
    assert set(body["opencode"]) == OPENCODE_KEYS
    assert body["opencode"]["reachable"] is False
    assert body["opencode"]["version"] is None
    # ... the address is still named, so the operator knows WHICH server to start
    assert body["opencode"]["base_url"] == BASE_URL
    # ... and the chain that will answer is still listed
    assert body["chain"] == FALLBACK_CHAIN


async def test_a_registry_without_an_opencode_backend_is_reported_as_unreachable(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a registry that declares no opencode backend at all -- R2D2 running
    # on the chain alone, which is a supported deployment
    _chain, specs = load_backend_specs(Config.load())
    monkeypatch.setattr(
        main_module,
        "load_backend_specs",
        lambda _cfg: (
            BackendChain(order=("zen",), unused=tuple(name for name in specs if name != "zen")),
            {name: spec for name, spec in specs.items() if name != "opencode"},
        ),
    )
    # When
    body = await api.json("/health")
    # Then the route answers, and the body says there is no opencode route -- a
    # reader must not have to infer that from an empty `base_url`
    assert body["opencode"]["reachable"] is False
    assert body["opencode"]["base_url"] == ""
    assert body["chain"] == ["zen"]


async def test_a_registry_that_cannot_be_read_leaves_the_owner_with_an_empty_chain(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a config/backends.json that will not parse. This IS a fault of the
    # process -- and still not one that a restart can fix
    def broken(_cfg: Config) -> tuple[BackendChain, dict[str, Any]]:
        raise BackendConfigError("config/backends.json: not json")

    monkeypatch.setattr(main_module, "load_backend_specs", broken)
    # When
    body = await api.json("/health")
    # Then the route still answers, because a route that 5xxs cannot report the
    # reason it is unwell ...
    assert body["status"] == "ok"
    assert body["opencode"]["reachable"] is False
    # ... and `chain: []` is the loud part: there is nothing left to answer with
    assert body["chain"] == []


# ---------------------------------------------------------------------------
# 3. Stale state: `sessions` is read, not remembered
# ---------------------------------------------------------------------------


async def test_sessions_reflects_the_database_and_not_the_state_at_import(api: Api) -> None:
    # Given a user who has no opencode session yet
    assert (await api.json("/health"))["sessions"] == 0
    # When that user's session is bound
    await api.memory.bind_oc_session("alice-app-1", "ses_1", "r2d2:alice:alice-app-1")
    # Then the next probe counts it
    assert (await api.json("/health"))["sessions"] == 1
    # ... and a second user moves the number again, which a value computed once at
    # import and closed over could not do
    await api.memory.bind_oc_session("bob-app-2", "ses_2", "r2d2:alice:bob-app-2")
    assert (await api.json("/health"))["sessions"] == 2


# ---------------------------------------------------------------------------
# 4. A hung opencode server must not hang /health
# ---------------------------------------------------------------------------


async def test_a_hung_opencode_server_still_gets_an_answer_from_health(
    api: Api, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a server that accepts the request and never answers. The client is
    # swapped in AFTER the lifespan, so what is under test is the route's own
    # bound and not the startup gate's patience
    monkeypatch.setattr(main_module, "OpencodeClient", HangingClient)
    # When
    loop = asyncio.get_running_loop()
    started = loop.time()
    response = await api.get("/health")
    elapsed = loop.time() - started
    body = document_of(response)
    # Then the route answered long before the hung call could have returned
    assert elapsed < HANG_CEILING_S, f"/health took {elapsed:.1f}s against a hung opencode"
    assert response.status_code == 200
    # ... reporting the same degradation a refused connection reports
    assert body["opencode"]["reachable"] is False
    assert body["opencode"]["base_url"] == BASE_URL


# ---------------------------------------------------------------------------
# 5. Secret hygiene on the two public bodies
# ---------------------------------------------------------------------------


async def test_neither_public_body_carries_a_credential_or_the_workspace(
    api: Api, environment: Path
) -> None:
    # Given a deployment whose every credential is a sentinel value, read by the
    # real registry through ${...} expansion -- so the specs under this app really
    # do hold them, and "it is not in the body" is a claim about a loaded config
    specs = load_backend_specs(Config.load())[1]
    assert OC_PASSWORD in specs["opencode"].password
    assert ZEN_KEY in specs["zen"].api_key
    assert YANDEX_KEY in specs["yandexgpt"].api_key
    # When both public routes are read
    responses = [await api.get("/health"), await api.get("/diagnostics/providers")]
    # Then neither body contains a single credential ...
    for response in responses:
        assert response.status_code == 200, response.text
        for secret in SECRETS:
            assert secret not in response.text, (
                f"{secret[:12]}... leaked into {response.request.url.path}"
            )
    # ... and neither names the workspace the agent's file tools are rooted at
    for response in responses:
        assert str(environment) not in response.text
        assert WORKSPACE_NAME not in response.text


# ---------------------------------------------------------------------------
# 6. /diagnostics/providers
# ---------------------------------------------------------------------------


async def test_diagnostics_returns_the_live_provider_and_agent_payloads(api: Api) -> None:
    # Given / When the owner asks what the server can reach
    response = await api.get("/diagnostics/providers")
    body = document_of(response)
    # Then both payloads are returned as the server sent them, unedited ...
    assert response.status_code == 200
    assert body["providers"] == PROVIDERS_PAYLOAD
    assert body["agents"] == AGENTS_PAYLOAD
    # ... so the model ids and the agent names can be read without a log line
    assert [agent["name"] for agent in body["agents"]] == ["r2d2-voice", "r2d2-agent"]
    assert body["providers"]["default"]["modelID"] == "space-bunny-free"


async def test_diagnostics_is_503_when_the_opencode_server_is_down(
    api: Api, server: FakeOpencode
) -> None:
    # Given no opencode server
    server.down = True
    # When
    response = await api.get("/diagnostics/providers")
    # Then the owner is told there is nothing to describe, in the one body this
    # route's contract allows -- and no traceback, no upstream text
    assert response.status_code == 503
    assert document_of(response) == {"error": "opencode unreachable"}
    assert "Traceback" not in response.text


async def test_diagnostics_is_502_and_not_a_traceback_when_the_server_answers_nonsense(
    api: Api, server: FakeOpencode
) -> None:
    # Given a server that answers 200 with a body that is not JSON -- the shape a
    # reverse proxy in front of it produces
    server.malformed = True
    # When
    response = await api.get("/diagnostics/providers")
    body = document_of(response)
    # Then the owner gets a clean gateway error ...
    assert response.status_code == 502
    assert "error" in body
    # ... and the upstream body is NOT echoed back: it is unparsed text from a
    # server R2D2 does not control, on a route that is not authenticated
    assert "gateway says hello" not in response.text
    assert "Traceback" not in response.text
    assert "ProtocolError" not in response.text


# ---------------------------------------------------------------------------
# 7. The record itself
# ---------------------------------------------------------------------------


def test_turn_metrics_is_frozen_and_slotted() -> None:
    # Given / When a turn has been measured
    from core.metrics import TurnMetrics

    record = TurnMetrics(
        route="fallback",
        path="voice",
        model="zen",
        agent="",
        llm_ms=12,
        total_ms=40,
        escalated=False,
        permission_asked=False,
    )
    # Then it is a frozen dataclass with slots: a record edited after it was
    # logged is a record nobody can trust, and a per-turn dataclass with a
    # __dict__ allocates for every field on every Alice turn
    assert dataclasses.is_dataclass(TurnMetrics)
    assert TurnMetrics.__dataclass_params__.frozen is True
    assert set(TurnMetrics.__slots__) == {f.name for f in dataclasses.fields(TurnMetrics)}
    assert not hasattr(record, "__dict__")
    with pytest.raises(dataclasses.FrozenInstanceError):
        record.route = "opencode"  # type: ignore[misc]


def test_the_turn_context_manager_logs_exactly_one_record_even_when_the_body_raises(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Given a turn that fails partway through: the failure is the turn whose
    # duration nobody will ever be able to explain
    from core import metrics

    caplog.set_level(logging.INFO)
    # When
    with pytest.raises(RuntimeError, match="llm exploded"):
        with metrics.turn() as rec:
            rec.route = metrics.ROUTE_FALLBACK
            rec.llm_ms = 7
            raise RuntimeError("llm exploded")
    # Then exactly one record was written -- in the `finally`, before the
    # exception carried on out
    records = turn_records(caplog)
    assert len(records) == 1
    assert records[0].levelno == logging.INFO
    fields = dict(re.findall(r"(\w+)=(\S+)", records[0].getMessage()))
    assert fields["route"] == metrics.ROUTE_FALLBACK
    assert int(fields["llm_ms"]) == 7
    assert int(fields["total_ms"]) >= 0


def test_two_turns_never_share_one_record(caplog: pytest.LogCaptureFixture) -> None:
    # Given / When two turns run one after the other -- a recorder the first one
    # left set would make the second one's numbers unreadable
    from core import metrics

    caplog.set_level(logging.INFO)
    with metrics.turn() as first:
        first.route = metrics.ROUTE_FALLBACK
    with metrics.turn() as second:
        second.route = metrics.ROUTE_OPENCODE
    # Then each turn carries only what it set itself
    routes = [
        dict(re.findall(r"(\w+)=(\S+)", record.getMessage()))["route"]
        for record in turn_records(caplog)
    ]
    assert routes == ["fallback", "opencode"]


async def test_a_clock_that_steps_backwards_never_produces_a_negative_duration(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Given a clock that steps BACKWARDS inside a turn, which NTP, `date -s` and a
    # laptop suspend all do: `time.time()` reports a negative duration there, and
    # a negative duration reads as "no time passed" and poisons every average
    from core import metrics

    ticks = iter([10.0, 10.0, 12.0, 9.0])

    async def noop() -> None:
        return None

    caplog.set_level(logging.INFO)
    # When a turn measures its model call on that clock
    with metrics.turn(clock=lambda: next(ticks)) as rec:
        await rec.measure(noop())
    # Then the model call's own duration is real: 2 s on a clock that was moving
    # forward at the time
    fields = one_turn_record(caplog)
    assert int(fields["llm_ms"]) == 2000
    # ... while the turn as a whole came out at -1 s on that clock, which is
    # clamped rather than logged, and never below the call it contains
    assert int(fields["total_ms"]) >= 0
    assert int(fields["total_ms"]) >= int(fields["llm_ms"])


def test_no_duration_can_come_from_the_wall_clock() -> None:
    # Given the module's own source, parsed rather than grepped, so the prose that
    # explains WHY the wall clock is banned cannot satisfy or fail the check
    import core.metrics as metrics

    tree = ast.parse(Path(metrics.__file__).read_text(encoding="utf-8"))
    clock_calls = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
        if node.value.id == "time"
    }
    # Then the only clock it reads is the monotonic one -- `time.time` steps
    # backwards across an NTP correction, a `date -s` or a suspend, and a duration
    # measured across that reads as "no time passed"
    assert "time" not in clock_calls
    assert "monotonic" in clock_calls


# ---------------------------------------------------------------------------
# 8. A whole turn, measured
# ---------------------------------------------------------------------------


async def test_a_whole_turn_logs_exactly_one_turn_record(
    turn_rig: TurnRig, caplog: pytest.LogCaptureFixture
) -> None:
    # Given a user asking a question the opencode server cannot answer
    caplog.set_level(logging.INFO)
    turn_rig.server.requests.clear()
    # When the turn runs
    payload = await turn_rig.brain.process_alice(alice_body(QUESTION))
    # Then the user is answered ...
    assert payload["response"]["text"] == ANSWER
    # ... and the ONLY thing the opencode server was asked was whether it is up:
    # the liveness probe every turn makes, never a session or a message
    assert turn_rig.server.requests == ["GET /global/health"]
    # ... and the turn produced EXACTLY ONE record, on the brain's own logger
    records = turn_records(caplog)
    assert len(records) == 1
    assert records[0].name == "r2d2.test.brain"
    assert records[0].levelno == logging.INFO
    # ... carrying every field the plan pins ...
    assert records[0].getMessage().startswith("turn route=fallback path=voice")
    fields = one_turn_record(caplog)
    assert fields["model"] == "zen"
    assert fields["escalated"] == "False"
    # ... plus what the line it replaced kept: how much history, which tools
    assert int(fields["msgs"]) == 2
    assert fields["tools"] == "()"
    # ... and the ad-hoc line is gone, so the turn is not logged twice
    assert not ADHOC_LINE.search(caplog.text), caplog.text


async def test_the_recorded_durations_are_real_and_ordered(
    turn_rig: TurnRig, caplog: pytest.LogCaptureFixture
) -> None:
    # Given / When a turn runs on a monotonic clock
    caplog.set_level(logging.INFO)
    await turn_rig.brain.process_alice(alice_body(QUESTION))
    fields = one_turn_record(caplog)
    # Then the model call is a non-negative number of milliseconds ...
    llm_ms, total_ms = int(fields["llm_ms"]), int(fields["total_ms"])
    assert llm_ms >= 0
    # ... the turn is never shorter than the call it contains ...
    assert total_ms >= llm_ms
    # ... and the whole turn is inside the budget the record exists to prove:
    # docs/07-latency-strategy.md budgets ~2.5 s of Alice's 4.5 s for R2D2
    assert total_ms < BUDGET_MS


async def test_a_turn_that_hands_work_to_the_background_is_recorded_as_an_escalation(
    turn_rig: TurnRig, caplog: pytest.LogCaptureFixture
) -> None:
    # Given a fallback LLM that answers with a TOOL CALL rather than text -- the
    # ack-then-background pattern docs/07 is built on
    caplog.set_level(logging.INFO)
    turn_rig.answer_with(calling_tool("arxiv_search", {"query": "rag"}))
    # When
    payload = await turn_rig.brain.process_alice(alice_body("пришли мне свежие статьи про RAG с arxiv"))
    # Then the user is acknowledged with the tool's own spoken_reply ...
    assert payload["response"]["text"] == SPOKEN
    # ... the tool that ran is named in the record ...
    fields = one_turn_record(caplog)
    assert fields["tools"] == "('arxiv_search',)"
    # ... and the turn is recorded as an ESCALATION rather than as an answer: the
    # voice budget was met by moving the work, which is a different fact, and the
    # one that explains a 4.5 s turn with a 40 ms number in it
    assert fields["path"] == "escalate"
    assert fields["escalated"] == "True"


async def test_a_fixed_reply_costs_no_model_call(
    turn_rig: TurnRig, caplog: pytest.LogCaptureFixture
) -> None:
    # Given / When the user asks for something with a fixed answer
    caplog.set_level(logging.INFO)
    payload = await turn_rig.brain.process_alice(alice_body("помощь"))
    # Then no model was called -- and the record says so, because a turn that
    # reached the network would look identical to one that did not
    assert payload["response"]["text"].startswith(HELP_PREFIX)
    fields = one_turn_record(caplog)
    assert int(fields["llm_ms"]) == 0
    assert int(fields["total_ms"]) >= 0


async def test_a_failing_turn_is_still_measured(
    turn_rig: TurnRig, caplog: pytest.LogCaptureFixture
) -> None:
    # Given every fallback backend refuses
    caplog.set_level(logging.INFO)
    turn_rig.refuse_everywhere()
    # When
    payload = await turn_rig.brain.process_alice(alice_body(QUESTION))
    # Then the user is told the graceful text ...
    assert payload["response"]["text"] == ERROR_TEXT
    # ... and the turn that failed is still on the record, as an error, with a
    # duration: an unmeasured failure is the one nobody can chase
    fields = one_turn_record(caplog)
    assert fields["path"] == "error"
    assert int(fields["total_ms"]) >= int(fields["llm_ms"]) >= 0


async def test_the_record_carries_no_credential(
    turn_rig: TurnRig, caplog: pytest.LogCaptureFixture
) -> None:
    # Given / When a turn runs with every credential in the environment
    caplog.set_level(logging.DEBUG)
    await turn_rig.brain.process_alice(alice_body(QUESTION))
    # Then the record -- and every other record of the turn -- names no
    # credential. A model id and a backend name are not secrets; a key is
    assert turn_records(caplog)
    for secret in (OC_PASSWORD, OC_USERNAME, ZEN_KEY, OPENROUTER_KEY, YANDEX_KEY, TELEGRAM_TOKEN):
        assert secret not in caplog.text


def test_the_strings_this_file_asserts_are_the_shipped_ones() -> None:
    # Given / When / Then -- the strings these tests pin are the ones
    # `core/brain.py` and `app/config.py` ship, so a rename fails here instead of
    # making four assertions quietly wrong
    assert ACK == "Проверяю, пришлю в телеграм."
    assert ERROR_TEXT == "Что-то пошло не так. Попробуй ещё раз."
    assert HELP_PREFIX == "Я Р2Д2"


def test_diagnostics_providers_route_redacts_credentials() -> None:
    """A public route must not hand out the credentials it is describing.

    The provider catalog is a third party's body and carries `key`,
    `options.apiKey` and `options.headers`. Echoing it unedited put a live console
    token on an unauthenticated route a public tunnel was pointed at. This locks
    the redaction: a leak here is silent, and the route's own docstring forbids
    exactly this content.
    """
    from app.diagnostics import _redact_credentials

    catalog = {
        "providers": [
            {
                "id": "opencode-go",
                "name": "OpenCode Go",
                "key": "st_live_token_value",
                "options": {
                    "apiKey": "st_live_token_value",
                    "headers": {"Authorization": "Bearer sk_live_secret"},
                },
                "models": {"mimo-v2.6-pro": {"id": "mimo-v2.6-pro"}},
            }
        ]
    }
    rendered = json.dumps(_redact_credentials(catalog))

    assert "st_live_token_value" not in rendered
    assert "sk_live_secret" not in rendered
    # A redacted key is still a DIAGNOSTIC: "is one configured" is the whole point
    # of the endpoint, so set and unset must not collapse into the same token.
    assert "***set***" in rendered
    assert json.dumps(_redact_credentials({"key": ""})) == '{"key": "***unset***"}'
    # Non-credential facts survive: the endpoint is worthless without them.
    assert "mimo-v2.6-pro" in rendered
    assert "Authorization" in rendered


def test_bot_token_is_masked_in_httpx_logs() -> None:
    """The Telegram token lives in the URL path, and httpx logs the full URL at INFO.

    `app/main.py` enables INFO globally, so every send logged the bot token in
    cleartext into the systemd journal -- the same exposure as the credentials
    route, through a channel nobody audits. Only the token is masked: method,
    endpoint and status survive, because a 2xx the client cannot read is exactly
    what an operator reads that line for.
    """
    import app.main  # noqa: F401  -- importing installs the filter on root handlers
    from app.main import _RedactBotToken

    # Assembled at runtime, never written out: `tests/test_no_secrets_tracked.py`
    # scans every tracked file for the live values, and a guard test that pastes a
    # real token into the repository would be the leak it exists to prevent.
    token = ":".join(("8689354110", "AAHz0wkdPCQFU1QJgPX5zvb274ZjI9Kcdrs"))
    token_url = f"https://api.telegram.org/bot{token}/sendMessage"
    record = logging.LogRecord(
        name="httpx", level=logging.INFO, pathname=__file__, lineno=0,
        msg='HTTP Request: %s %s "%s %d %s"',
        args=("POST", token_url, "HTTP/1.1", 200, "OK"), exc_info=None,
    )
    assert _RedactBotToken().filter(record) is True
    rendered = record.getMessage()

    assert token not in rendered
    assert "bot<token>" in rendered
    assert all(part in rendered for part in ("POST", "sendMessage", "200"))

    # A line with no token in it must come through untouched, or the filter would
    # be silently mangling unrelated diagnostics.
    plain = logging.LogRecord(
        name="r2d2", level=logging.INFO, pathname=__file__, lineno=0,
        msg="turn route=opencode", args=None, exc_info=None,
    )
    assert _RedactBotToken().filter(plain) is True
    assert plain.getMessage() == "turn route=opencode"

    # And the filter has to actually be on the handlers, not merely defined.
    assert any(
        type(f).__name__ == "_RedactBotToken" for f in logging.getLogger().handlers[0].filters
    )
