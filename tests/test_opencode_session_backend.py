"""Behavioural tests for `core.backends.opencode_session` (plan todo 9).

The adapter makes one persistent opencode session per R2D2 user answerable through
the ordinary `Backend` protocol, so `core/brain.py` can call the same interface on
the fast voice path and on the agent path. What these tests lock is the set of
ways that adapter could answer with something other than the truth:

* **C1 -- an unknown `modelID` is not an error.** opencode substitutes a
  different model, answers HTTP **200** and puts the failure (or nothing at all)
  in the body. So `validate_models()` refuses a configured model the server does
  not list, and `complete()` raises on a 200 whose `info.error` is a 403
  `FreeTierError` instead of returning a `Choice`. Sections 1 and 2.
* **The `ContextVar` is the whole concurrency story.** Two Alice users are two
  tasks, and each must reach its OWN session; a process-wide variable would
  cross-deliver one user's question into another user's context. Section 1.
* **A deadline is not an abort.** The turn keeps running server-side and the
  collector fetches it later, so `OpencodeDeadlineExceeded` propagates unchanged
  and `POST /session/:id/abort` is never issued.
* **The collector must not lie twice.** It returns only text newer than
  `since_message_id`, it terminates on the idle condition, it is bounded by
  `timeout_s` even when a poll never answers, and a 404 mid-poll raises a typed
  error instead of spinning. Section 4.

The double is the REAL `OpencodeClient` and the REAL `OcSessionStore` over
`httpx.MockTransport` -- an HTTP-level fake, not a mocked client -- so the agent
name, the model, the `?directory=` and the request paths are all observed on the
wire. Nothing touches a network (proven by re-running under `-p no_net`).

Timing: three tests sleep for real, each inside a stated budget -- the 0.05 s
voice deadline, the 1 s collector ceiling and the 0.05 s collector poll interval.
No test sleeps to "let something finish".
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

import httpx
import pytest

from app.config import Config
from core.backends.base import Backend, Choice
from core.backends.config_loader import BackendChain, BackendConfigError, BackendSpec
from core.backends.opencode_session import (
    NoCurrentApplicationId,
    OpencodeEmptyReply,
    OpencodeSessionBackend,
    OpencodeWiring,
    current_application_id,
)
from core.backends.registry import build_backends, build_chain
from core.memory import Memory
from core.opencode.client import OpencodeClient, OpencodeDeadlineExceeded, OpencodeStatusError
from core.opencode.models import (
    OpencodeModelCheckFailed,
    OpencodeModelUnconfigured,
    OpencodeModelUnknown,
)
from core.opencode.session_store import OcSessionStore, title_for
from core.opencode.sse import EventSource
from core.opencode.wire import OpencodeError

# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------

BASE_URL = "http://127.0.0.1:4599"
APP = "alice-123"
OTHER_APP = "bob-456"
SESSION_ID = "ses_1"
MODEL = "opencode/space-bunny-free"
VOICE_AGENT = "r2d2-voice"
TASK_AGENT = "r2d2-agent"
ANSWER = "Квантовая точка — это наночастица."
#: The poll interval for the tests about the IDLE condition. The shipped default
#: is 2.0 s, and that one is exercised by the `timeout_s` test on purpose.
POLL_S = 0.05
#: The sentinel the plan greps for: if it appears in a log record, an exception
#: message or a transcript, the adapter leaked the opencode credential.
PASSWORD = "R2D2_OC_PASSWORD_VALUE"
#: A 200 whose `info.error` is a refusal -- C1, measured on this machine.
FREE_TIER_ERROR = {
    "name": "APIError",
    "data": {
        "message": "OpenCode's free tier can only be used from within OpenCode",
        "statusCode": 403,
        "isRetryable": False,
    },
}
MISSING_MODEL = "opencode/muse-spark-1.3-contributor-free"
NOT_YET = "opencode_session backend not yet available"


class _EndlessStream(httpx.AsyncByteStream):
    """A body that never ends, so a deadline or a cancel is the only way out."""

    async def __aiter__(self):
        await asyncio.sleep(3600)
        yield b""  # pragma: no cover - the wait above never returns


def providers_document(*models: str) -> dict[str, object]:
    """`GET /config/providers` as the server answers it: providers with `models`."""
    return {
        "providers": [
            {"id": "opencode", "models": {model: {"status": "active"} for model in models}}
        ],
        "default": {},
    }


def said(text: str, message_id: str, role: str = "assistant") -> dict[str, object]:
    """One `GET /session/:id/message` entry: `{info, parts}` (C7)."""
    return {
        "info": {"id": message_id, "role": role},
        "parts": [{"type": "text", "text": text}],
    }


class FakeOpencode:
    """`opencode serve` as a `MockTransport` handler -- the routes this adapter uses.

    The knobs exist for the cases the real server produced in the spike: an
    `info.error` inside a 200 (C1), a turn that never answers, a message list
    that 404s on the second poll. Requests are recorded BEFORE the handler runs,
    so "the abort path was never hit" means the transport never saw an abort.
    """

    def __init__(self) -> None:
        self.sessions: dict[str, dict[str, str]] = {}
        self.message_posts: list[tuple[str, str]] = []
        self.requests: list[httpx.Request] = []
        self.aborted: list[str] = []
        self.answer = ANSWER
        self.message_error: dict[str, object] | None = None
        self.answer_empty = False
        self.prompt_async_status = 204
        self.hang: set[str] = set()
        self.providers = providers_document("space-bunny-free")
        self.providers_status = 200
        #: Scripted `GET /session/:id/message` bodies, one per poll; the last repeats.
        self.polls: list[list[dict[str, object]]] | None = None
        self.polls_gone = False
        self.polls_seen = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        method, path = request.method, request.url.path
        if method == "GET" and path == "/session":
            return httpx.Response(200, json=list(self.sessions.values()))
        if method == "POST" and path == "/session":
            return self._create(request)
        if method == "POST" and path.endswith("/prompt_async"):
            return httpx.Response(self.prompt_async_status)
        if method == "POST" and path.endswith("/message"):
            if "turn" in self.hang:
                return httpx.Response(200, stream=_EndlessStream())
            return self._turn(request)
        if method == "GET" and path.endswith("/message"):
            return self._poll(path)
        if method == "POST" and path.endswith("/abort"):
            self.aborted.append(path.split("/")[2])
            return httpx.Response(200, json=True)
        if method == "GET" and path == "/config/providers":
            return httpx.Response(self.providers_status, json=self.providers)
        return httpx.Response(404, json={"name": "NotFoundError", "data": {"message": path}})

    def client(self, workspace: str, spec: BackendSpec | None = None) -> OpencodeClient:
        return OpencodeClient(
            spec if spec is not None else spec_for(),
            workspace,
            client=httpx.AsyncClient(transport=httpx.MockTransport(self)),
        )

    # -- what the tests read off the wire ----------------------------------

    def routes(self) -> list[tuple[str, str]]:
        return [(request.method, request.url.path) for request in self.requests]

    def body_of(self, method: str, suffix: str) -> dict[str, object]:
        """The JSON body of the single request matching `method` and path `suffix`."""
        matches = [
            request
            for request in self.requests
            if request.method == method and request.url.path.endswith(suffix)
        ]
        assert len(matches) == 1, f"expected one {method} ...{suffix}, saw {self.routes()}"
        decoded: dict[str, object] = json.loads(matches[0].content)
        return decoded

    def poll_count(self) -> int:
        """How many times the collector asked the server for the message list."""
        return len(
            [r for r in self.requests if r.method == "GET" and r.url.path.endswith("/message")]
        )

    def titles(self) -> dict[str, str]:
        return {sid: entry["title"] for sid, entry in self.sessions.items()}

    # -- routes -------------------------------------------------------------

    def _create(self, request: httpx.Request) -> httpx.Response:
        session_id = f"ses_{len(self.sessions) + 1}"
        self.sessions[session_id] = {
            "id": session_id,
            "title": json.loads(request.content)["title"],
            "directory": request.url.params.get("directory", ""),
        }
        return httpx.Response(200, json=self.sessions[session_id])

    def _turn(self, request: httpx.Request) -> httpx.Response:
        session_id = request.url.path.split("/")[2]
        body: dict[str, object] = json.loads(request.content)
        self.message_posts.append((session_id, body["parts"][0]["text"]))
        if self.message_error is not None:
            return httpx.Response(
                200, json={"info": {"id": "msg_1", "error": self.message_error}, "parts": []}
            )
        parts = [] if self.answer_empty else [{"type": "text", "text": self.answer}]
        return httpx.Response(200, json={"info": {"id": "msg_1", "role": "assistant"}, "parts": parts})

    def _poll(self, path: str) -> httpx.Response:
        if "poll" in self.hang:
            return httpx.Response(200, stream=_EndlessStream())
        if self.polls_gone and self.polls_seen >= 1:
            return httpx.Response(404, json={"name": "NotFoundError", "data": {"message": "gone"}})
        entries = (
            self.polls[min(self.polls_seen, len(self.polls) - 1)] if self.polls is not None else []
        )
        self.polls_seen += 1
        return httpx.Response(200, json=entries)


class RecordingClient(OpencodeClient):
    """The real client with `aclose` counted.

    Closing is the one thing a test must observe and the real client hides it
    behind its ownership rule, so exactly one method is instrumented; the request
    behaviour is the real one.
    """

    def __init__(self, spec: BackendSpec, directory: str, *, client: httpx.AsyncClient) -> None:
        super().__init__(spec, directory, client=client)
        self.closes = 0

    async def aclose(self) -> None:
        self.closes += 1
        await super().aclose()


class RecordingEventSource(EventSource):
    """The real `EventSource` with `aclose` counted, for the same reason."""

    def __init__(
        self, spec: BackendSpec, directory: str, *, session_id: str, client: httpx.AsyncClient
    ) -> None:
        super().__init__(spec, directory, session_id=session_id, client=client)
        self.closes = 0

    async def aclose(self) -> None:
        self.closes += 1
        await super().aclose()


def spec_for(
    *,
    fast_model: str = MODEL,
    task_model: str = MODEL,
    summarize_model: str = MODEL,
    password: str = PASSWORD,
) -> BackendSpec:
    """The `opencode_session` spec shape a test varies, and nothing else."""
    return BackendSpec(
        name="opencode",
        kind="opencode_session",
        base_url=BASE_URL,
        username="opencode",
        password=password,
        voice_agent=VOICE_AGENT,
        task_agent=TASK_AGENT,
        fast_model=fast_model,
        task_model=task_model,
        summarize_model=summarize_model,
        timeout=3.2,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def no_leaked_application() -> object:
    """No application is current except where a test sets one.

    `current_application_id` is process-wide state, and a test that leaked a
    value would make the next test's "unset" case pass for the wrong reason.
    """
    current_application_id.set(None)
    yield
    current_application_id.set(None)


@pytest.fixture
def workspace(tmp_path: Path) -> str:
    path = tmp_path / "r2d2-workspace"
    path.mkdir()
    return str(path)


@pytest.fixture
def server() -> FakeOpencode:
    return FakeOpencode()


@pytest.fixture
async def memory(tmp_path: Path):
    connected = await Memory(str(tmp_path / "sessions.db")).connect()
    try:
        yield connected
    finally:
        await connected.close()


@pytest.fixture
def build(server: FakeOpencode, workspace: str, memory: Memory):
    """`build()` -> an adapter over the real client and store; kwargs vary it.

    The poll interval is the knob the collector tests turn, because the idle
    condition and the `timeout_s` bound are two different clocks.
    """

    def factory(
        *, poll_s: float = POLL_S, spec: BackendSpec | None = None
    ) -> OpencodeSessionBackend:
        chosen = spec if spec is not None else spec_for()
        cfg = Config(r2d2_workspace=workspace, r2d2_event_poll_interval=poll_s)
        client = server.client(workspace, chosen)
        store = OcSessionStore(memory, client, cfg)
        return OpencodeSessionBackend(chosen, OpencodeWiring(client=client, store=store, cfg=cfg))

    return factory


@pytest.fixture
def backend(build) -> OpencodeSessionBackend:
    return build()


def question(text: str = "что такое квантовые точки") -> list[dict[str, str]]:
    """The single user message todo 15's brain passes to `complete()`."""
    return [{"role": "user", "content": text}]


async def as_user(backend: OpencodeSessionBackend, application_id: str, text: str) -> Choice:
    """One `complete()` call as a given user, the way todo 15's brain does it."""
    current_application_id.set(application_id)
    return await backend.complete(question(text))


# ---------------------------------------------------------------------------
# 1. complete() -- the fast voice path
# ---------------------------------------------------------------------------


async def test_complete_returns_the_assistant_text_with_no_tool_calls(
    backend: OpencodeSessionBackend,
):
    # Given a user whose session is bound, with the ContextVar set
    current_application_id.set(APP)
    # When the voice path is called
    choice = await backend.complete(question())
    # Then the answer is the assistant's text and there is nothing to dispatch:
    # the voice agent's tools are opencode's business (todo 10), never a ToolCall
    assert choice == Choice(content=ANSWER, tool_calls=[], provider="opencode", model=MODEL)


async def test_the_voice_turn_reaches_the_wire_with_the_voice_agent_and_fast_model(
    backend: OpencodeSessionBackend, server: FakeOpencode
):
    # Given
    current_application_id.set(APP)
    # When
    await backend.complete(question())
    # Then the body is the one the contract pins: the EXACT agent name (C2 keeps
    # 11 foreign agents visible) and the model split on the FIRST "/"
    body = server.body_of("POST", "/message")
    assert body["agent"] == VOICE_AGENT
    assert body["model"] == {"providerID": "opencode", "modelID": "space-bunny-free"}
    assert body["parts"] == [{"type": "text", "text": "что такое квантовые точки"}]


async def test_two_users_in_two_tasks_reach_two_different_sessions(
    backend: OpencodeSessionBackend, server: FakeOpencode
):
    # Given two Alice users answering at the same instant -- the load-bearing case
    # for a ContextVar, because a process-wide variable would cross-deliver one
    # user's turn into another user's context
    # When both are served concurrently
    alice, bob = await asyncio.gather(
        as_user(backend, APP, f"вопрос от {APP}"),
        as_user(backend, OTHER_APP, f"вопрос от {OTHER_APP}"),
    )
    # Then each turn landed in the session titled for ITS OWN user
    titles = server.titles()
    assert {(titles[session_id], text) for session_id, text in server.message_posts} == {
        (title_for(APP), f"вопрос от {APP}"),
        (title_for(OTHER_APP), f"вопрос от {OTHER_APP}"),
    }
    # ... so two distinct sessions were created, and both callers got an answer
    assert len({session_id for session_id, _text in server.message_posts}) == 2
    assert alice.content and bob.content


async def test_an_unset_application_id_raises_before_anything_is_sent(
    backend: OpencodeSessionBackend, server: FakeOpencode
):
    # Given a caller that forgot to set the ContextVar -- guessing a session here
    # would answer one user from another user's context
    # When / Then
    with pytest.raises(NoCurrentApplicationId) as excinfo:
        await backend.complete(question())
    # Then the failure is typed, and nothing reached the server
    assert isinstance(excinfo.value, OpencodeError)
    assert server.requests == []


@pytest.mark.parametrize("unset", (None, ""), ids=("never-set", "empty"))
async def test_an_empty_application_id_is_the_same_failure_as_an_unset_one(
    backend: OpencodeSessionBackend, server: FakeOpencode, unset: str | None
):
    # Given a ContextVar holding nothing that could be an application
    current_application_id.set(unset)
    # When / Then -- an empty id is not an application and must not be looked up
    with pytest.raises(NoCurrentApplicationId):
        await backend.complete(question())
    assert server.requests == []


async def test_a_200_carrying_a_free_tier_refusal_raises_instead_of_answering(
    backend: OpencodeSessionBackend, server: FakeOpencode
):
    """C1: the headline failure here is a REFUSAL SPOKEN AS AN ANSWER.

    A 200 whose `info.error` is a 403 `FreeTierError` is opencode's way of saying
    "this model is not usable from here". If that became a `Choice`, Alice would
    read an apologetic answer and nobody would ever learn the model is refused.
    """
    # Given a server that refuses the model inside a 200
    server.message_error = FREE_TIER_ERROR
    current_application_id.set(APP)
    # When the voice path is called
    with pytest.raises(OpencodeError) as excinfo:
        await backend.complete(question())
    # Then it RAISED, carrying the INNER status: no Choice exists, so the refusal
    # text has no path to the speaker
    assert getattr(excinfo.value, "status_code", None) == 403
    assert "free tier" in str(excinfo.value)


async def test_a_turn_that_silently_substituted_a_model_yields_no_answer(
    backend: OpencodeSessionBackend, server: FakeOpencode
):
    """C1, second half: an unknown modelID is answered 200 by ANOTHER model.

    The spike measured that substitution as HTTP 200 with `parts` carrying no
    text. `Choice(content="")` would be a misleading success the speaker cannot
    report, so a turn with no text is a failure.
    """
    # Given a 200 whose parts hold nothing -- what silent substitution looks like
    server.answer_empty = True
    current_application_id.set(APP)
    # When / Then
    with pytest.raises(OpencodeEmptyReply):
        await backend.complete(question())


async def test_a_deadline_propagates_unchanged_and_is_never_aborted(
    backend: OpencodeSessionBackend, server: FakeOpencode
):
    # Given a voice turn the server will not finish
    server.hang.add("turn")
    current_application_id.set(APP)
    # When the caller's own deadline elapses
    started = time.monotonic()
    with pytest.raises(OpencodeDeadlineExceeded):
        await backend.complete(question(), timeout=0.05)
    # Then the deadline arrives as the caller's own signal, and the turn is left
    # running for the collector to fetch: aborting destroys work already paid for
    assert time.monotonic() - started < 0.5
    assert server.aborted == []
    assert ("POST", f"/session/{SESSION_ID}/abort") not in server.routes()


async def test_the_model_argument_overrides_the_configured_fast_model(
    backend: OpencodeSessionBackend, server: FakeOpencode
):
    # Given a caller that insists on a different model
    current_application_id.set(APP)
    # When
    choice = await backend.complete(question(), model="opencode/another-model")
    # Then the wire and the returned Choice both report the model that was used
    assert server.body_of("POST", "/message")["model"] == {
        "providerID": "opencode",
        "modelID": "another-model",
    }
    assert choice.model == "opencode/another-model"


async def test_only_the_new_utterance_is_sent_because_the_session_already_has_the_history(
    backend: OpencodeSessionBackend, server: FakeOpencode
):
    # Given a message list shaped like the fallback path's: a system prompt plus a
    # transcript the opencode session has already read
    current_application_id.set(APP)
    messages = [
        {"role": "system", "content": "Ты -- Р2Д2"},
        {"role": "user", "content": "старый вопрос"},
        {"role": "assistant", "content": "старый ответ"},
        {"role": "user", "content": "новый вопрос"},
    ]
    # When
    await backend.complete(messages)
    # Then only the new utterance goes on the wire: resending the transcript would
    # duplicate the context the session already holds
    assert server.body_of("POST", "/message")["parts"] == [{"type": "text", "text": "новый вопрос"}]


async def test_a_call_with_nothing_to_say_is_refused_before_any_request(
    backend: OpencodeSessionBackend, server: FakeOpencode
):
    # Given a message list with no user text at all
    current_application_id.set(APP)
    # When / Then
    with pytest.raises(OpencodeError, match="user"):
        await backend.complete([{"role": "system", "content": "Ты -- Р2Д2"}])
    assert server.requests == []


# ---------------------------------------------------------------------------
# 2. validate_models() -- the C1 gate at startup
# ---------------------------------------------------------------------------


async def test_validate_models_passes_when_every_configured_model_is_listed(
    backend: OpencodeSessionBackend, server: FakeOpencode
):
    # Given a server that lists the configured model
    # When startup validation runs
    validated = await backend.validate_models()
    # Then it passes, and the catalogue was fetched ONCE for all three roles
    assert validated == (MODEL,)
    assert server.routes() == [("GET", "/config/providers")]


async def test_a_model_the_server_does_not_list_is_named_and_refused(
    build, server: FakeOpencode
):
    # Given a config with a model id opencode has never heard of. The spike
    # measured that this is NOT an error there: it silently answers 200 from
    # another model, so nothing downstream would ever notice.
    backend = build(
        spec=spec_for(fast_model=MISSING_MODEL, summarize_model="opencode/also-missing")
    )
    # When startup validation runs
    with pytest.raises(OpencodeModelUnknown) as excinfo:
        await backend.validate_models()
    # Then every offending role is named, and the route that proves the point
    message = str(excinfo.value)
    assert MISSING_MODEL in message
    assert "opencode/also-missing" in message
    assert "/config/providers" in message
    assert isinstance(excinfo.value, OpencodeError)


async def test_a_role_with_no_model_is_refused_rather_than_left_to_opencode(build):
    # Given a config whose task role is empty
    backend = build(spec=spec_for(task_model=""))
    # When startup validation runs
    # Then it fails loudly: an absent model would let opencode answer from its own
    # default, which is the same trap as an unknown id
    with pytest.raises(OpencodeModelUnconfigured, match="task_model"):
        await backend.validate_models()


async def test_an_unreadable_catalogue_refuses_to_assume_the_model_is_fine(
    backend: OpencodeSessionBackend, server: FakeOpencode
):
    # Given a server that cannot answer the catalogue at all
    server.providers_status = 500
    # When startup validation runs
    with pytest.raises(OpencodeModelCheckFailed) as excinfo:
        await backend.validate_models()
    # Then the failure says the CHECK could not be made. "I could not check, so I
    # assume it is fine" is exactly the misleading success this gate exists to stop
    assert "/config/providers" in str(excinfo.value)
    assert not isinstance(excinfo.value, OpencodeModelUnknown)


async def test_a_catalogue_in_an_unreadable_shape_is_not_reported_as_a_missing_model(
    backend: OpencodeSessionBackend, server: FakeOpencode
):
    # Given a 200 whose shape this client does not understand -- claiming
    # "unknown model" there would send the operator hunting a config typo
    server.providers = {"providers": "not-a-list"}
    # When / Then
    with pytest.raises(OpencodeModelCheckFailed):
        await backend.validate_models()


async def test_a_catalogue_without_our_provider_is_reported_as_a_missing_model(
    backend: OpencodeSessionBackend, server: FakeOpencode
):
    # Given a readable catalogue that lists no "opencode" provider at all
    server.providers = {"providers": [{"id": "openrouter", "models": {}}]}
    # When / Then -- the catalogue IS readable here, so an absent model is the
    # honest diagnosis rather than a failure to read
    with pytest.raises(OpencodeModelUnknown, match="opencode"):
        await backend.validate_models()


# ---------------------------------------------------------------------------
# 3. submit_task -- the agent path
# ---------------------------------------------------------------------------


async def test_submit_task_posts_prompt_async_with_the_task_agent_and_model(
    backend: OpencodeSessionBackend, server: FakeOpencode
):
    # Given a session the user already has
    # When the agent turn is submitted
    submitted = await backend.submit_task(
        SESSION_ID, "Пользователь попросил голосом: сделай сводку"
    )
    # Then the submission carries the AGENT agent and the task model, and no reply
    # is awaited: nothing polls for an answer on this path
    assert submitted is None
    body = server.body_of("POST", "/prompt_async")
    assert body["agent"] == TASK_AGENT
    assert body["model"] == {"providerID": "opencode", "modelID": "space-bunny-free"}
    assert body["parts"][0]["text"].startswith("Пользователь попросил голосом:")
    assert ("GET", f"/session/{SESSION_ID}/message") not in server.routes()


async def test_submit_task_refuses_a_server_that_does_not_accept_the_submission(
    backend: OpencodeSessionBackend, server: FakeOpencode
):
    # Given a server answering the async route with anything other than 204
    server.prompt_async_status = 200
    # When / Then -- a silently dropped task is worse than a loud failure
    with pytest.raises(OpencodeStatusError):
        await backend.submit_task(SESSION_ID, "сделай сводку")


# ---------------------------------------------------------------------------
# 4. collect_reply -- the F1 polling fallback collector
# ---------------------------------------------------------------------------


async def test_collect_reply_returns_the_accumulated_text_and_stops_when_idle(
    backend: OpencodeSessionBackend, server: FakeOpencode
):
    # Given a session whose turn has produced one message and then goes quiet
    server.polls = [
        [said("Собираю", "msg_1")],
        [said("Собираю", "msg_1"), said("Готово: 12 статей", "msg_2")],
    ]
    # When the collector runs with the production poll interval
    started = time.monotonic()
    text = await backend.collect_reply(SESSION_ID, "", timeout_s=30.0)
    # Then it returned once no new assistant text had appeared for one interval
    assert text == "Собираю\nГотово: 12 статей"
    assert time.monotonic() - started < 1.0
    assert server.poll_count() >= 2


async def test_collect_reply_of_a_silent_session_returns_empty_at_the_timeout(
    build, server: FakeOpencode
):
    # Given a session whose assistant never produces text, a 2.0 s poll interval
    # and a 1 s ceiling: the shipped poll interval, so the BOUND has to be the
    # thing that ends the wait rather than the idle condition
    backend = build(poll_s=2.0)
    server.polls = [[]]
    # When
    started = time.monotonic()
    text = await backend.collect_reply(SESSION_ID, "", timeout_s=1.0)
    elapsed = time.monotonic() - started
    # Then it is an empty answer: no exception, no hang
    assert text == ""
    assert 0.9 <= elapsed < 2.0, f"the ceiling was not enforced ({elapsed:.3f}s)"


async def test_collect_reply_is_bounded_even_when_a_poll_never_answers(
    build, server: FakeOpencode
):
    # Given a message route that takes the request and then goes silent forever
    backend = build()
    server.hang.add("poll")
    # When
    started = time.monotonic()
    text = await backend.collect_reply(SESSION_ID, "", timeout_s=1.0)
    # Then the collector's own bound is what ended the wait
    assert text == ""
    assert time.monotonic() - started < 2.0


async def test_collect_reply_returns_only_text_newer_than_the_given_message(
    backend: OpencodeSessionBackend, server: FakeOpencode
):
    # Given a session that already delivered one message to this user
    server.polls = [[said("уже доставлено", "msg_1"), said("новый ответ", "msg_2")]]
    # When the collector resumes after that message
    text = await backend.collect_reply(SESSION_ID, "msg_1", timeout_s=1.0)
    # Then the older text is not delivered twice (Telegram would show it twice)
    assert text == "новый ответ"


async def test_collect_reply_reads_everything_in_a_list_the_marker_is_not_in(
    backend: OpencodeSessionBackend, server: FakeOpencode
):
    # Given a marker the server no longer returns -- the window has been truncated
    # at the front, so every message still listed is newer than the marker
    server.polls = [[said("первый", "msg_1"), said("второй", "msg_2")]]
    # When / Then -- losing the whole reply is the worse failure, so the collector
    # delivers the window it can see rather than nothing
    assert await backend.collect_reply(SESSION_ID, "msg_pruned", timeout_s=1.0) == "первый\nвторой"


async def test_collect_reply_ignores_the_users_own_messages(
    backend: OpencodeSessionBackend, server: FakeOpencode
):
    # Given a turn that has only echoed the request back so far
    server.polls = [[said("сделай сводку", "msg_1", role="user")]]
    # When / Then -- only the ASSISTANT speaks; the user's own text is not an answer
    assert await backend.collect_reply(SESSION_ID, "", timeout_s=1.0) == ""


async def test_a_session_that_disappears_mid_poll_raises_instead_of_spinning(
    backend: OpencodeSessionBackend, server: FakeOpencode
):
    # Given a session the server drops between two polls -- the reaper's race,
    # seen from the collector's side
    server.polls = [[said("первый", "msg_1")]]
    server.polls_gone = True
    # When / Then
    with pytest.raises(OpencodeStatusError) as excinfo:
        await backend.collect_reply(SESSION_ID, "", timeout_s=30.0)
    assert excinfo.value.status_code == 404
    # ... and the loop stopped at the failure instead of retrying to the ceiling
    assert server.poll_count() == 2


async def test_a_cancelled_collector_leaves_no_dangling_task(
    backend: OpencodeSessionBackend, server: FakeOpencode
):
    # Given a collector waiting on a poll that will never answer
    server.hang.add("poll")
    before = asyncio.all_tasks()
    task = asyncio.create_task(backend.collect_reply(SESSION_ID, "", timeout_s=600.0))
    await asyncio.sleep(0.05)
    # When it is cancelled mid-poll, the way a worker shutdown cancels a job
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.05)
    # Then nothing outlived it: the poll runs inline, so cancelling the collector
    # cancels the request with it
    assert asyncio.all_tasks() == before


# ---------------------------------------------------------------------------
# 5. protocol, lifecycle and registration
# ---------------------------------------------------------------------------


def test_the_adapter_satisfies_the_backend_protocol(backend: OpencodeSessionBackend):
    # Given / When / Then -- the registry hands this to the brain as a Backend
    assert isinstance(backend, Backend)
    assert backend.name == "opencode"


@pytest.mark.parametrize("with_events", (True, False), ids=("sse", "sse-absent"))
async def test_aclose_closes_what_it_owns_and_is_safe_twice(
    server: FakeOpencode, workspace: str, memory: Memory, with_events: bool
):
    # Given an adapter over a recording client, with an event source only when the
    # caller wired one (`EVENT_MODE == "sse"` is todo 18's business)
    cfg = Config(r2d2_workspace=workspace)
    spec = spec_for()
    client = RecordingClient(
        spec, workspace, client=httpx.AsyncClient(transport=httpx.MockTransport(server))
    )
    events = (
        RecordingEventSource(
            spec,
            workspace,
            session_id=SESSION_ID,
            client=httpx.AsyncClient(transport=httpx.MockTransport(server)),
        )
        if with_events
        else None
    )
    backend = OpencodeSessionBackend(
        spec,
        OpencodeWiring(
            client=client, store=OcSessionStore(memory, client, cfg), cfg=cfg, events=events
        ),
    )
    # When the lifespan closes it -- twice, because shutdown paths are not ordered
    await backend.aclose()
    await backend.aclose()
    # Then the closeables were released, and the store this adapter does NOT own
    # is still open and usable
    assert client.closes == 2
    assert events is None or events.closes == 2
    assert await memory.all_oc_sessions() == []


async def test_the_registry_builds_the_adapter_when_the_wiring_is_supplied(
    server: FakeOpencode, workspace: str, memory: Memory
):
    # Given a credentialed opencode_session spec plus the wiring the composition
    # root assembles (todo 18)
    loaded = {"opencode": spec_for()}
    cfg = Config(r2d2_workspace=workspace)
    client = server.client(workspace)
    wiring = OpencodeWiring(client=client, store=OcSessionStore(memory, client, cfg), cfg=cfg)
    # When the registry dispatches on the kind
    built = build_backends(loaded, opencode=wiring)
    chain = build_chain(loaded, BackendChain(order=("opencode",)), opencode=wiring)
    # Then the real adapter comes back, no longer the "not yet available" stub
    assert isinstance(built["opencode"], OpencodeSessionBackend)
    assert [backend.name for backend in chain] == ["opencode"]
    assert isinstance(chain[0], Backend)


def test_without_a_wiring_the_registry_keeps_the_plan_error():
    # Given a credentialed opencode_session spec and NO wiring -- a composition
    # root that never built the client, store and event source
    # When / Then -- the plan's error string is kept for exactly this case, which
    # is a real misconfiguration rather than "the feature is unwritten"
    with pytest.raises(BackendConfigError, match=NOT_YET) as excinfo:
        build_backends({"opencode": spec_for()})
    assert "opencode" in str(excinfo.value)
    assert "client" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 6. adversarial: config-controlled strings and credentials
# ---------------------------------------------------------------------------


async def test_a_title_and_workspace_full_of_metacharacters_never_reach_the_path(
    tmp_path: Path, server: FakeOpencode, memory: Memory
):
    # Given a workspace path and an application id that both look like an attack.
    # Both are config/remote controlled, and the only place a URL path could be
    # corrupted is a string interpolated into one: `?directory=` (C6) and the
    # JSON body are the only places either is allowed to travel.
    workspace = tmp_path / "ws; rm -rf ~ $(id) `whoami`"
    workspace.mkdir()
    application_id = "alice/../;rm -rf /"
    cfg = Config(r2d2_workspace=str(workspace))
    client = server.client(str(workspace))
    backend = OpencodeSessionBackend(
        spec_for(),
        OpencodeWiring(client=client, store=OcSessionStore(memory, client, cfg), cfg=cfg),
    )
    # When the session is created and the turn is sent
    current_application_id.set(application_id)
    await backend.complete(question())
    # Then every path on the wire is one of the two the protocol defines, with
    # neither hostile string anywhere in it
    assert set(server.routes()) == {
        ("POST", "/session"),
        ("POST", "/session/ses_1/message"),
    }
    # ... the directory travelled as the query parameter and decodes back to itself
    create = next(
        request
        for request in server.requests
        if request.method == "POST" and request.url.path == "/session"
    )
    assert create.url.params["directory"] == str(workspace)
    # ... and the title travelled in the body, never interpolated into a URL
    assert json.loads(create.content) == {"title": f"r2d2:alice:{application_id}"}


async def test_the_password_never_reaches_a_log_record_or_an_exception(
    build, server: FakeOpencode, caplog: pytest.LogCaptureFixture
):
    # Given every failure path this module can take, on one wiring whose password
    # is the sentinel
    backend = build(spec=spec_for(password=PASSWORD))
    messages: list[str] = []

    async def failure(what) -> None:
        with pytest.raises((OpencodeError, httpx.HTTPError)) as excinfo:
            await what
        messages.append(str(excinfo.value))

    with caplog.at_level(logging.DEBUG, logger="core.backends.opencode_session"):
        await failure(backend.complete(question()))  # nothing set the application
        current_application_id.set(APP)
        server.message_error = FREE_TIER_ERROR
        await failure(backend.complete(question()))
        server.message_error = None
        server.answer_empty = True
        await failure(backend.complete(question()))
        server.answer_empty = False
        server.prompt_async_status = 500
        await failure(backend.submit_task(SESSION_ID, "сделай сводку"))
        server.providers_status = 500
        await failure(backend.validate_models())
        server.polls_gone = True
        await failure(backend.collect_reply(SESSION_ID, "", timeout_s=1.0))

    # Then no failure message and no log record carries the credential
    assert len(messages) == 6
    assert all(PASSWORD not in message for message in messages)
    assert PASSWORD not in caplog.text
