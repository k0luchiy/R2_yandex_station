"""Behavioural tests for `core.opencode.client` (plan todo 6).

The client is the only thing that will talk to a real `opencode serve`, so
these tests encode the three measured facts that a naive HTTP wrapper gets
wrong. All three come from `docs/11-opencode-contract.md` and the spike
corrections C1/C6/C7, and each has its own section below:

* **C1** -- opencode answers HTTP **200 with the failure in the body**
  (`info.error`, e.g. `APIError` / `statusCode` 403 `FreeTierError` or 402
  `Insufficient account funds`). A 200 is not a reply.
* **C6** -- the session directory is the **query parameter** `?directory=<abs
  path>`, never a body key (a body key is silently ignored with a 200), and the
  server does **not** validate it, so the client checks the path itself.
* **C7** -- `GET /session/:id/message` returns `[{info, parts}]`, not a flat
  list, and an unknown `modelID` is not an error: opencode silently substitutes
  a different model and still answers 200.

Everything goes through `httpx.MockTransport`; nothing here touches a network
(proven by re-running the suite under `-p no_net`).

Timing: exactly one test sleeps for real --
`test_send_message_deadline_raises_and_never_aborts` waits out a 0.05 s
deadline against a transport that never answers. No other test sleeps.
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Literal, get_args, get_type_hints
from urllib.parse import unquote

import httpx
import pytest

from core.backends.base import BackendError
from core.backends.config_loader import BackendSpec
from core.routing import TranscriptSweep, for_human, transcript_sweep

from core.opencode.client import (
    OpencodeClient,
    OpencodeDeadlineExceeded,
    OpencodeError,
    OpencodeErrorEnvelope,
    OpencodeHealth,
    OpencodeProtocolError,
    OpencodeReply,
    OpencodeStatusError,
)

# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------

BASE_URL = "http://127.0.0.1:4599"
#: An existing absolute path. The client refuses to send `?directory=` for a
#: path that does not exist (C6), so the happy path needs a real one; the repo
#: root always exists and needs no cleanup.
WORKSPACE = str(Path(__file__).resolve().parents[1])
MODEL = "opencode/space-bunny-free"
TITLE = "r2d2:alice:alice-123"
SESSION_ID = "ses_1"
#: The sentinel the plan greps for: if this string ever appears in a message, a
#: log record, a repr or a transcript, the client leaked the credential.
PASSWORD = "R2D2_OC_PASSWORD_VALUE"
#: The escalation sentinel, the routing sweep's only subject. It is passed in, so
#: this file states the value `app/config.py` ships rather than a literal of its own.
SENTINEL = "[[NEEDS_AGENT]]"
FREE_TIER_ERROR = {
    "name": "APIError",
    "data": {
        "message": "OpenCode's free tier can only be used from within OpenCode",
        "statusCode": 403,
        "isRetryable": False,
    },
}
INSUFFICIENT_FUNDS_ERROR = {
    "name": "APIError",
    "data": {"message": "Insufficient account funds", "statusCode": 402, "isRetryable": False},
}


class Recorder:
    """A `MockTransport` handler that remembers every request it saw.

    Requests are recorded before the handler runs, so a request that raised
    still counts -- "the abort path was never hit" has to mean the transport
    never saw an abort, not that the abort call returned quietly.
    """

    def __init__(self, respond: Callable[[httpx.Request], httpx.Response] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self._respond = respond if respond is not None else (lambda _r: httpx.Response(200, json={}))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._respond(request)

    @property
    def count(self) -> int:
        return len(self.requests)

    def only(self) -> httpx.Request:
        """The single request that reached the transport."""
        assert self.count == 1, f"expected exactly one request, saw {self.routes()}"
        return self.requests[0]

    def routes(self) -> list[tuple[str, str]]:
        return [(request.method, request.url.path) for request in self.requests]

    def on_route(self, method: str, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method and r.url.path == path]


def canned_body(body: object) -> Recorder:
    """A transport answering 200 with exactly `body`; `bytes` go out verbatim."""
    if isinstance(body, bytes):
        return Recorder(lambda _r: httpx.Response(200, content=body))
    return Recorder(lambda _r: httpx.Response(200, json=body))


def canned_status(status: int, *, json_body: object = None, content: bytes | None = None) -> Recorder:
    """A transport answering every request with the same status and body."""
    return Recorder(
        lambda _r: httpx.Response(status, content=content)
        if content is not None
        else httpx.Response(status, json=json_body)
    )


def _refuse(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused", request=request)


def _message_document(
    text: str = "Готово",
    *,
    parts: list[dict[str, object]] | None = None,
    info: dict[str, object] | None = None,
) -> dict[str, object]:
    """A `{info, parts}` assistant turn exactly as the live server returns it."""
    document_info: dict[str, object] = {
        "id": "msg_1",
        "sessionID": SESSION_ID,
        "role": "assistant",
        "providerID": "opencode",
        "modelID": "space-bunny-free",
        "error": None,
    }
    if info is not None:
        document_info = info
    document_parts: list[dict[str, object]] = [{"id": "prt_1", "type": "text", "text": text}]
    if parts is not None:
        document_parts = parts
    return {"info": document_info, "parts": document_parts}


class FakeOpencode(Recorder):
    """A stateful stand-in for the opencode routes this client calls.

    Stateful rather than a canned-response list because `resolve_session` is
    only correct if a session created by the first call is FOUND by the second
    -- that is a property of the server's state, not of the client's parsing.
    """

    def __init__(self, *, version: str = "1.18.32") -> None:
        super().__init__(self._route)
        self.version = version
        self.healthy = True
        self.sessions: dict[str, dict[str, object]] = {}
        self.turns: list[dict[str, object]] = []
        self.aborted: list[str] = []
        self.summaries: list[dict[str, object]] = []
        self.permissions: list[tuple[str, object]] = []
        self.session_status: dict[str, object] = {}
        self.reply: object = _message_document()
        self.reply_status = 200
        self.sessions_payload: object = None
        self.create_status = 200
        self.async_status = 204
        self.permission_status = 200
        self.permission_body: object = True
        self.abort_body: object = True

    def _route(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        if path == "/global/health":
            return httpx.Response(200, json={"healthy": self.healthy, "version": self.version})
        if path == "/session/status":
            return httpx.Response(200, json=self.session_status)
        if path == "/config/providers":
            return httpx.Response(200, json={"providers": [], "default": {}})
        if path == "/agent":
            return httpx.Response(
                200,
                json=[{"name": "r2d2-voice", "mode": "primary", "permission": {}, "options": {}}],
            )
        if path == "/session" and method == "POST":
            if self.create_status != 200:
                return httpx.Response(self.create_status, json={"name": "UnknownError"})
            body = json.loads(request.read())
            session: dict[str, object] = {
                "id": f"ses_{len(self.sessions) + 1}",
                "slug": "r2d2",
                "projectID": "global",
                "directory": request.url.params.get("directory", ""),
                "title": body.get("title", ""),
                "version": self.version,
                "time": {"created": 0, "updated": 0},
            }
            self.sessions[str(session["id"])] = session
            return httpx.Response(200, json=session)
        if path == "/session" and method == "GET":
            payload = self.sessions_payload
            return httpx.Response(
                200, json=list(self.sessions.values()) if payload is None else payload
            )
        if path.endswith("/message") and method == "GET":
            return httpx.Response(200, json=[_message_document()])
        if path.endswith("/message") and method == "POST":
            self.turns.append(json.loads(request.read()))
            return httpx.Response(self.reply_status, json=self.reply)
        if path.endswith("/prompt_async"):
            self.turns.append(json.loads(request.read()))
            return httpx.Response(self.async_status)
        if path.endswith("/abort"):
            self.aborted.append(path.split("/")[-2])
            return httpx.Response(200, json=self.abort_body)
        if path.endswith("/summarize"):
            self.summaries.append(json.loads(request.read()))
            return httpx.Response(200, json=True)
        if "/permissions/" in path:
            self.permissions.append((path.rsplit("/", 1)[-1], json.loads(request.read())))
            return httpx.Response(self.permission_status, json=self.permission_body)
        raise AssertionError(f"FakeOpencode has no route for {method} {path}")


class HangingServer(Recorder):
    """A transport that accepts the request and then goes silent forever."""

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, stream=_EndlessStream())


class _EndlessStream(httpx.AsyncByteStream):
    async def __aiter__(self):  # type: ignore[override]
        await asyncio.sleep(3600)
        yield b""  # pragma: no cover - the wait above never returns


def _spec(
    *,
    base_url: str = BASE_URL,
    username: str = "opencode",
    password: str = PASSWORD,
    timeout: float = 3.2,
) -> BackendSpec:
    """The `opencode_session` spec shape a test varies, and nothing else."""
    return BackendSpec(
        name="opencode",
        kind="opencode_session",
        base_url=base_url,
        username=username,
        password=password,
        voice_agent="r2d2-voice",
        task_agent="r2d2-agent",
        fast_model=MODEL,
        task_model=MODEL,
        summarize_model=MODEL,
        timeout=timeout,
    )


def _harness(
    recorder: Recorder,
    *,
    directory: str = WORKSPACE,
    spec: BackendSpec | None = None,
) -> tuple[OpencodeClient, httpx.AsyncClient]:
    """An `OpencodeClient` plus the injected client, so a test can inspect both."""
    transport_client = httpx.AsyncClient(transport=httpx.MockTransport(recorder))
    client = OpencodeClient(
        spec if spec is not None else _spec(), directory=directory, client=transport_client
    )
    return client, transport_client


def _client(
    recorder: Recorder, *, directory: str = WORKSPACE, spec: BackendSpec | None = None
) -> OpencodeClient:
    return _harness(recorder, directory=directory, spec=spec)[0]


async def _turn(
    client: OpencodeClient, *, text: str = "привет", deadline_s: float = 1.0
) -> OpencodeReply:
    return await client.send_message(
        SESSION_ID, text, agent="r2d2-voice", model=MODEL, deadline_s=deadline_s
    )


@pytest.fixture
def built_clients(monkeypatch: pytest.MonkeyPatch) -> list[httpx.AsyncClient]:
    """Make every client the opencode client builds for itself a `MockTransport` one.

    Also the proof that the no-network rule holds for the code paths that
    construct their own client instead of receiving one.
    """
    created: list[httpx.AsyncClient] = []
    real_client = httpx.AsyncClient
    fake = FakeOpencode()

    def factory(*, timeout: float = 5.0, **kwargs: object) -> httpx.AsyncClient:
        client = real_client(transport=httpx.MockTransport(fake), timeout=timeout, **kwargs)
        created.append(client)
        return client

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return created


# ---------------------------------------------------------------------------
# 1-2. health: a liveness probe that cannot raise
# ---------------------------------------------------------------------------


async def test_health_reports_reachable_and_the_server_version_when_healthy() -> None:
    """Given `GET /global/health` -> `{healthy, version}`: both are reported."""
    # Given
    recorder = FakeOpencode(version="1.18.32")
    # When
    health = await _client(recorder).health()
    # Then
    assert health == OpencodeHealth(reachable=True, version="1.18.32")
    assert recorder.only().url.path == "/global/health"


async def test_health_reports_unreachable_instead_of_raising_on_a_transport_error() -> None:
    """Given the server is not listening: `reachable=False`, no exception escapes."""
    # Given
    recorder = Recorder(_refuse)
    # When
    health = await _client(recorder).health()
    # Then
    assert health.reachable is False
    assert health.version is None


async def test_health_reports_unreachable_when_the_body_is_not_json() -> None:
    """Given a 200 that is not JSON: unusable health data is `reachable=False`."""
    # Given / When
    health = await _client(canned_status(200, content=b"<html>proxy error</html>")).health()
    # Then
    assert health.reachable is False


async def test_health_reports_unreachable_when_the_body_says_unhealthy() -> None:
    """Given `{healthy: false}`: the server answered, but R2D2 cannot use it."""
    # Given
    recorder = FakeOpencode()
    recorder.healthy = False
    # When
    health = await _client(recorder).health()
    # Then
    assert health.reachable is False
    assert health.version == "1.18.32"


# ---------------------------------------------------------------------------
# 3-4. sessions: find, create, resolve
# ---------------------------------------------------------------------------


async def test_find_session_returns_the_id_of_the_session_with_that_exact_title() -> None:
    """Given a stored session: `find_session` returns its id, creating nothing."""
    # Given
    recorder = FakeOpencode()
    client = _client(recorder)
    created = await client.create_session(TITLE)
    # When
    found = await client.find_session(TITLE)
    # Then
    assert found == created
    assert recorder.routes() == [("POST", "/session"), ("GET", "/session")]


async def test_find_session_returns_none_when_no_session_carries_that_title() -> None:
    """Given a miss: `None`, and still no session is created."""
    # Given
    recorder = FakeOpencode()
    # When
    found = await _client(recorder).find_session("r2d2:alice:nobody")
    # Then
    assert found is None
    assert recorder.routes() == [("GET", "/session")]


async def test_find_session_matches_the_title_exactly_and_not_by_prefix() -> None:
    """Given a session whose title merely starts with the wanted one: no match."""
    # Given
    recorder = FakeOpencode()
    client = _client(recorder)
    await client.create_session(f"{TITLE}:extra")
    # When
    found = await client.find_session(TITLE)
    # Then
    assert found is None


async def test_resolve_session_creates_exactly_one_session_across_two_calls() -> None:
    """Given the first call misses and the second hits: exactly one `POST /session`."""
    # Given
    recorder = FakeOpencode()
    client = _client(recorder)
    # When
    first = await client.resolve_session(TITLE)
    second = await client.resolve_session(TITLE)
    # Then
    assert first == second == SESSION_ID
    assert len(recorder.on_route("POST", "/session")) == 1
    assert recorder.routes() == [
        ("GET", "/session"),
        ("POST", "/session"),
        ("GET", "/session"),
    ]


async def test_resolve_session_sends_the_title_in_the_create_body() -> None:
    """Given a miss: the created session carries the resolved title."""
    # Given
    recorder = FakeOpencode()
    # When
    await _client(recorder).resolve_session(TITLE)
    # Then
    sent = recorder.on_route("POST", "/session")[0]
    assert json.loads(sent.read()) == {"title": TITLE}
    assert recorder.sessions[SESSION_ID]["title"] == TITLE


# ---------------------------------------------------------------------------
# 5. create_session failures
# ---------------------------------------------------------------------------


async def test_create_session_raises_with_the_status_code_and_without_the_password() -> None:
    """Given `POST /session` -> 500: `OpencodeError` naming 500, never the password."""
    # Given
    recorder = FakeOpencode()
    recorder.create_status = 500
    # When
    with pytest.raises(OpencodeError) as excinfo:
        await _client(recorder).create_session(TITLE)
    # Then
    assert isinstance(excinfo.value, OpencodeStatusError)
    assert excinfo.value.status_code == 500
    assert "500" in str(excinfo.value)
    assert PASSWORD not in str(excinfo.value)


# ---------------------------------------------------------------------------
# 6. the model split
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("configured", "provider", "model_id"),
    [
        pytest.param("opencode/space-bunny-free", "opencode", "space-bunny-free", id="provider-and-model"),
        pytest.param("a/b/c", "a", "b/c", id="first-slash-only"),
        pytest.param("x", "opencode", "x", id="bare-id-defaults-to-opencode"),
    ],
)
async def test_send_message_splits_the_configured_model_on_the_first_slash(
    configured: str, provider: str, model_id: str
) -> None:
    """Given a model string: `model.providerID`/`modelID` is the FIRST-slash split."""
    # Given
    recorder = FakeOpencode()
    # When
    await _client(recorder).send_message(
        SESSION_ID, "привет", agent="r2d2-voice", model=configured, deadline_s=1.0
    )
    # Then
    assert recorder.turns[0]["model"] == {"providerID": provider, "modelID": model_id}
    assert recorder.only().url.path == f"/session/{SESSION_ID}/message"


async def test_send_message_sends_the_agent_and_a_single_text_part() -> None:
    """Given a turn: the body is the agent plus exactly one `text` part."""
    # Given
    recorder = FakeOpencode()
    # When
    await _client(recorder).send_message(
        SESSION_ID, "что такое квантовые точки", agent="r2d2-voice", model=MODEL, deadline_s=1.0
    )
    # Then
    assert recorder.turns[0] == {
        "model": {"providerID": "opencode", "modelID": "space-bunny-free"},
        "agent": "r2d2-voice",
        "parts": [{"type": "text", "text": "что такое квантовые точки"}],
    }


# ---------------------------------------------------------------------------
# 7. text extraction
# ---------------------------------------------------------------------------


async def test_send_message_concatenates_text_parts_in_order_and_ignores_the_rest() -> None:
    """Given reasoning/tool parts around text parts: only `text` is the answer."""
    # Given
    recorder = FakeOpencode()
    recorder.reply = _message_document(
        parts=[
            {"id": "prt_1", "type": "reasoning", "text": "Сначала подумаю"},
            {"id": "prt_2", "type": "text", "text": "Первая мысль. "},
            {"id": "prt_3", "type": "tool", "tool": "bash", "text": "ls -la"},
            {"id": "prt_4", "type": "text", "text": "Вторая мысль."},
        ]
    )
    # When
    reply = await _turn(_client(recorder))
    # Then
    assert reply.text == "Первая мысль. Вторая мысль."
    assert reply.message_id == "msg_1"


async def test_a_turn_with_no_text_part_is_an_empty_reply_and_not_an_error() -> None:
    """Given a tool-only turn: empty text is a fact, not a malformed response."""
    # Given
    recorder = FakeOpencode()
    recorder.reply = _message_document(parts=[{"id": "prt_1", "type": "tool", "tool": "bash"}])
    # When
    reply = await _turn(_client(recorder))
    # Then
    assert reply.text == ""


# ---------------------------------------------------------------------------
# 8. C1 -- a 200 that carries the failure in the body
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("inner", "status_code", "marker"),
    [
        pytest.param(FREE_TIER_ERROR, 403, "free tier", id="403-free-tier"),
        pytest.param(INSUFFICIENT_FUNDS_ERROR, 402, "Insufficient account funds", id="402-no-funds"),
    ],
)
async def test_send_message_raises_when_a_200_carries_an_inner_error(
    inner: dict[str, object], status_code: int, marker: str
) -> None:
    """Given HTTP 200 with `info.error` non-null: a refusal, never a reply (C1)."""
    # Given
    recorder = FakeOpencode()
    recorder.reply = {
        "info": {"id": "msg_1", "role": "assistant", "error": inner, "tokens": {"input": 0, "output": 0}},
        "parts": [],
    }
    # When
    with pytest.raises(OpencodeError) as excinfo:
        await _turn(_client(recorder))
    # Then
    assert isinstance(excinfo.value, OpencodeErrorEnvelope)
    assert excinfo.value.status_code == status_code
    assert str(status_code) in str(excinfo.value)
    assert marker in str(excinfo.value)


async def test_an_envelope_refusal_is_never_reported_as_an_empty_reply() -> None:
    """Given the C1 shape: no `OpencodeReply` with empty text ever comes back."""
    # Given
    recorder = FakeOpencode()
    recorder.reply = {"info": {"id": "msg_1", "role": "assistant", "error": FREE_TIER_ERROR}, "parts": []}
    # When
    with pytest.raises(OpencodeError) as excinfo:
        await _turn(_client(recorder))
    # Then
    assert not isinstance(excinfo.value, OpencodeReply)
    assert recorder.count == 1


# ---------------------------------------------------------------------------
# 9. deadline: bounded, and never an abort
# ---------------------------------------------------------------------------


async def test_send_message_deadline_raises_and_never_aborts() -> None:
    """Given a hung server: `OpencodeDeadlineExceeded`, and NO `POST .../abort`.

    The turn is still running server-side, so aborting it would destroy work
    the background collector is going to fetch.
    """
    # Given
    recorder = HangingServer()
    # When
    started = time.monotonic()
    with pytest.raises(OpencodeDeadlineExceeded):
        await _turn(_client(recorder), deadline_s=0.05)
    elapsed = time.monotonic() - started
    # Then
    assert elapsed < 0.4, f"the deadline was not enforced in bounded time ({elapsed:.3f}s)"
    assert recorder.routes() == [("POST", f"/session/{SESSION_ID}/message")]


async def test_a_deadline_exceeded_is_a_backend_error_so_the_brain_falls_back() -> None:
    """Given the brain branches on `BackendError`: a deadline is one of them."""
    # Then
    assert issubclass(OpencodeDeadlineExceeded, OpencodeError)
    assert issubclass(OpencodeError, BackendError)


# ---------------------------------------------------------------------------
# 10. prompt_async
# ---------------------------------------------------------------------------


async def test_send_message_async_returns_none_on_204() -> None:
    """Given `POST /session/:id/prompt_async` -> 204: the submission is accepted."""
    # Given
    recorder = FakeOpencode()
    # When
    result = await _client(recorder).send_message_async(
        SESSION_ID, "сделай сводку", agent="r2d2-agent", model=MODEL
    )
    # Then
    assert result is None
    assert recorder.only().url.path == f"/session/{SESSION_ID}/prompt_async"
    assert recorder.turns[0]["agent"] == "r2d2-agent"


@pytest.mark.parametrize("status", [200, 202, 400, 404, 500])
async def test_send_message_async_raises_when_the_status_is_not_204(status: int) -> None:
    """Given anything but 204: the async submit did not happen, so say so."""
    # Given
    recorder = FakeOpencode()
    recorder.async_status = status
    # When / Then
    with pytest.raises(OpencodeError) as excinfo:
        await _client(recorder).send_message_async(
            SESSION_ID, "сделай сводку", agent="r2d2-agent", model=MODEL
        )
    assert str(status) in str(excinfo.value)


# ---------------------------------------------------------------------------
# 11. permissions: exact body, and "always" is impossible
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("response", ["once", "reject"])
async def test_respond_permission_sends_a_byte_exact_body(response: str) -> None:
    """Given an answer to a permission request: the body is `{"response": ...}` only."""
    # Given
    recorder = FakeOpencode()
    # When
    result = await _client(recorder).respond_permission(SESSION_ID, "per_1", response)
    # Then
    assert result is True
    sent = recorder.only()
    assert sent.method == "POST"
    assert sent.url.path == f"/session/{SESSION_ID}/permissions/per_1"
    assert sent.read() == f'{{"response":"{response}"}}'.encode()
    assert recorder.permissions == [("per_1", {"response": response})]


def test_the_response_annotation_forbids_always() -> None:
    """Given the declared type of `response`: `"always"` is not among the values.

    One `always` grants the whole command mask in `properties.always` (e.g.
    `["echo *"]`) for good, so it must be unrepresentable here, not merely
    unused: a type checker rejects the call before it can reach the wire.
    """
    # When
    hints = get_type_hints(OpencodeClient.respond_permission)
    annotation = inspect.signature(OpencodeClient.respond_permission).parameters["response"].annotation
    # Then
    assert hints["response"] == Literal["once", "reject"]
    assert get_args(hints["response"]) == ("once", "reject")
    assert "always" not in str(annotation)


@pytest.mark.parametrize("permission_id", ["per_a/b", "per_ünïcode", "per a b"])
async def test_respond_permission_escapes_the_permission_id_into_one_path_segment(
    permission_id: str,
) -> None:
    """Given an id with a slash or non-ASCII: it stays one segment of the path."""
    # Given
    recorder = FakeOpencode()
    # When
    await _client(recorder).respond_permission(SESSION_ID, permission_id, "once")
    # Then
    sent = recorder.only()
    path = sent.url.raw_path.split(b"?", 1)[0]
    tail = path.split(b"/permissions/")[1]
    assert b"/" not in tail, f"{permission_id!r} split the path into two segments"
    assert unquote(tail.decode()) == permission_id


# ---------------------------------------------------------------------------
# 12-13. C6 -- `?directory=` is a query parameter, and the client checks the path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda c: c.create_session(TITLE), id="POST /session"),
        pytest.param(
            lambda c: c.send_message(
                SESSION_ID, "привет", agent="r2d2-voice", model=MODEL, deadline_s=1.0
            ),
            id="POST /session/:id/message",
        ),
        pytest.param(lambda c: c.list_messages(SESSION_ID), id="GET /session/:id/message"),
    ],
)
async def test_the_directory_travels_as_a_query_parameter_and_never_as_a_body_key(
    call: Callable[[OpencodeClient], object],
) -> None:
    """Given a session-scoped call: `?directory=` is in the query, not the body (C6).

    A `directory` body key is accepted with a 200 and silently ignored, so a
    client that puts it there silently gets the server's cwd instead.
    """
    # Given
    recorder = FakeOpencode()
    # When
    await call(_client(recorder))
    # Then
    sent = recorder.only()
    assert sent.url.params["directory"] == WORKSPACE
    assert sent.url.query.decode().startswith("directory=")
    assert b"directory" not in sent.read()


async def test_the_directory_is_also_passed_to_the_listing_and_status_routes() -> None:
    """Given the routes that address sessions by listing: they carry it too."""
    # Given
    recorder = FakeOpencode()
    client = _client(recorder)
    # When
    await client.find_session(TITLE)
    await client.list_sessions()
    await client.session_status()
    # Then
    assert all(request.url.params["directory"] == WORKSPACE for request in recorder.requests)
    assert recorder.routes() == [("GET", "/session"), ("GET", "/session"), ("GET", "/session/status")]


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda c: c.create_session(TITLE), id="POST /session"),
        pytest.param(lambda c: c.find_session(TITLE), id="GET /session"),
        pytest.param(lambda c: c.list_sessions(), id="GET /session"),
        pytest.param(lambda c: c.session_status(), id="GET /session/status"),
        pytest.param(
            lambda c: c.send_message(
                SESSION_ID, "привет", agent="r2d2-voice", model=MODEL, deadline_s=1.0
            ),
            id="POST /session/:id/message",
        ),
    ],
)
async def test_a_workspace_that_does_not_exist_stops_every_session_scoped_call(
    call: Callable[[OpencodeClient], object], tmp_path: Path
) -> None:
    """Given `?directory=` the server does NOT validate: the client refuses first (C6)."""
    # Given
    recorder = FakeOpencode()
    missing = str(tmp_path / "no-such-workspace")
    # When
    with pytest.raises(OpencodeError) as excinfo:
        await call(_client(recorder, directory=missing))
    # Then
    assert missing in str(excinfo.value)
    assert recorder.requests == [], "a request was sent for a workspace that does not exist"


async def test_health_still_answers_when_the_workspace_is_missing() -> None:
    """Given health takes no `directory`: a missing workspace is not "unreachable"."""
    # Given
    recorder = FakeOpencode()
    # When
    health = await _client(recorder, directory="/no-such-workspace").health()
    # Then
    assert health.reachable is True
    assert recorder.only().url.path == "/global/health"


# ---------------------------------------------------------------------------
# 14-15. summarize, status, and the remaining routes
# ---------------------------------------------------------------------------


async def test_summarize_sends_the_provider_and_model_id_as_declared() -> None:
    """Given `POST /session/:id/summarize`: the body is `{providerID, modelID}`."""
    # Given
    recorder = FakeOpencode()
    # When
    result = await _client(recorder).summarize(
        SESSION_ID, provider="opencode", model="space-bunny-free"
    )
    # Then
    assert result is True
    assert recorder.only().read() == b'{"providerID":"opencode","modelID":"space-bunny-free"}'
    assert recorder.summaries == [{"providerID": "opencode", "modelID": "space-bunny-free"}]


async def test_session_status_parses_a_session_id_to_status_mapping() -> None:
    """Given `GET /session/status` -> `{"ses_x": {"type": "idle"}}`: the reaper's view."""
    # Given
    recorder = FakeOpencode()
    recorder.session_status = {"ses_1": {"type": "idle"}, "ses_2": {"type": "busy"}}
    # When
    status = await _client(recorder).session_status()
    # Then
    assert status == {"ses_1": "idle", "ses_2": "busy"}


async def test_list_sessions_projects_id_title_and_directory() -> None:
    """Given `GET /session`: each entry becomes a `SessionInfo`."""
    # Given
    recorder = FakeOpencode()
    client = _client(recorder)
    await client.create_session(TITLE)
    # When
    sessions = await client.list_sessions()
    # Then
    assert [(session.id, session.title, session.directory) for session in sessions] == [
        (SESSION_ID, TITLE, WORKSPACE)
    ]


async def test_list_messages_reads_the_info_parts_envelope() -> None:
    """Given C7's `[{info, parts}]`: the role lives in `info`, not at the top level."""
    # Given
    recorder = FakeOpencode()
    # When
    messages = await _client(recorder).list_messages(SESSION_ID)
    # Then
    assert [(m.id, m.role, m.text) for m in messages] == [("msg_1", "assistant", "Готово")]


async def test_abort_reports_the_servers_bool_answer() -> None:
    """Given `POST /session/:id/abort` -> `true`: the turn was aborted."""
    # Given
    recorder = FakeOpencode()
    # When
    result = await _client(recorder).abort(SESSION_ID)
    # Then
    assert result is True
    assert recorder.aborted == [SESSION_ID]


async def test_providers_and_agents_pass_the_servers_documents_through() -> None:
    """Given the two diagnostic routes: the parsed documents reach the caller."""
    # Given
    recorder = FakeOpencode()
    client = _client(recorder)
    # When
    providers = await client.providers()
    agents = await client.agents()
    # Then
    assert providers == {"providers": [], "default": {}}
    assert [agent["name"] for agent in agents] == ["r2d2-voice"]
    assert recorder.routes() == [("GET", "/config/providers"), ("GET", "/agent")]


# ---------------------------------------------------------------------------
# Adversarial: malformed bodies must raise, never degrade
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(b"not json at all", id="not-json"),
        pytest.param({"info": {"id": "msg_1"}, "parts": {"nope": 1}}, id="parts-not-a-list"),
        pytest.param({"parts": [{"type": "text", "text": "hi"}]}, id="info-missing"),
        pytest.param({"info": "msg_1", "parts": []}, id="info-not-an-object"),
        pytest.param([{"info": {}, "parts": []}], id="body-not-an-object"),
        pytest.param({"info": {"id": "msg_1"}, "parts": ["not an object"]}, id="part-not-an-object"),
    ],
)
async def test_send_message_raises_on_a_wrong_shaped_success_body(body: object) -> None:
    """Given a 200 whose shape is not `{info, parts}`: raise, do not return empty."""
    # Given
    recorder = canned_body(body)
    # When / Then
    with pytest.raises(OpencodeProtocolError):
        await _turn(_client(recorder))


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"not": "a list"}, id="object"),
        pytest.param(["not an object"], id="entries-not-objects"),
        pytest.param([{"title": "x"}], id="entry-without-id"),
    ],
)
async def test_list_sessions_raises_when_the_session_list_is_unusable(payload: object) -> None:
    """Given `GET /session` returning something that is not `[Session]`: raise."""
    # Given
    recorder = FakeOpencode()
    recorder.sessions_payload = payload
    # When / Then
    with pytest.raises(OpencodeProtocolError):
        await _client(recorder).list_sessions()


async def test_list_messages_raises_when_the_envelope_entry_is_not_an_object() -> None:
    """Given C7's envelope with a junk entry: raise instead of skipping silently."""
    # Given
    recorder = canned_body(["not an object"])
    # When / Then
    with pytest.raises(OpencodeProtocolError):
        await _client(recorder).list_messages(SESSION_ID)


# ---------------------------------------------------------------------------
# A stored permission refusal: what the reader can see of it, and what it cannot
# ---------------------------------------------------------------------------

#: The whole refusal opencode stored, BYTE FOR BYTE off a live `opencode serve`
#: 1.18.32 (`docs/11-opencode-contract.md`, U8).  It is kept whole and unedited
#: because the defect is in its shape: the rules it enumerates are the refused
#: agent's matrix, and in the one-session-per-human design the OTHER agent reads
#: this exact text as a statement about its own tools.
DENIED_BASH_ERROR = (
    'The user has specified a rule which prevents you from using this specific tool call. Here are so'
    'me of the relevant rules [{"permission":"*","action":"allow","pattern":"*"},{"permission":"*","a'
    'ction":"deny","pattern":"*"},{"permission":"*","action":"deny","pattern":"*"},{"permission":"bas'
    'h","pattern":"*","action":"deny"},{"permission":"bash","pattern":"/home/koluchiy/.r2d2/r2d2_do.p'
    'y *","action":"allow"},{"permission":"bash","pattern":"python3 /home/koluchiy/.r2d2/r2d2_do.py *'
    '","action":"allow"},{"permission":"bash","pattern":"/home/koluchiy/Documents/R2_yandex_station/.'
    'venv/bin/python /home/koluchiy/.r2d2/r2d2_do.py *","action":"allow"},{"permission":"bash","patte'
    'rn":"upower *","action":"allow"},{"permission":"bash","pattern":"cat /sys/class/power_supply/*",'
    '"action":"allow"},{"permission":"bash","pattern":"df *","action":"allow"},{"permission":"bash","'
    'pattern":"free *","action":"allow"},{"permission":"bash","pattern":"uname *","action":"allow"},{'
    '"permission":"bash","pattern":"hostname *","action":"allow"},{"permission":"bash","pattern":"ps '
    '*","action":"allow"},{"permission":"bash","pattern":"uptime","action":"allow"},{"permission":"ba'
    'sh","pattern":"date","action":"allow"},{"permission":"bash","pattern":"/home/koluchiy/.r2d2/r2d2'
    '_do.py shell *","action":"deny"},{"permission":"bash","pattern":"python3 /home/koluchiy/.r2d2/r2'
    'd2_do.py shell *","action":"deny"},{"permission":"bash","pattern":"/home/koluchiy/Documents/R2_y'
    'andex_station/.venv/bin/python /home/koluchiy/.r2d2/r2d2_do.py shell *","action":"deny"}]'
)


def _refused_tool_message(message_id: str, tool: str, error: str) -> dict[str, object]:
    """The stored assistant message opencode writes when it refuses a tool call.

    Measured: `step-start`, the model's reasoning, the refused tool part, and
    `step-finish` -- and not one `text` part, because the refusal is the tool
    part's `state.error` and the refusal itself is never spoken.
    """
    return {
        "info": {"id": message_id, "role": "assistant", "agent": "r2d2-voice", "error": None},
        "parts": [
            {"id": f"prt_{message_id}_1", "type": "step-start"},
            {"id": f"prt_{message_id}_2", "type": "reasoning", "time": {"start": 1, "end": 2}},
            {
                "id": f"prt_{message_id}_3",
                "type": "tool",
                "tool": tool,
                "callID": f"call_{message_id}",
                "state": {
                    "status": "error",
                    "input": {"command": "ls -la /tmp/r2d2-workspace"},
                    "error": error,
                },
            },
            {"id": f"prt_{message_id}_4", "type": "step-finish", "reason": "tool-calls"},
        ],
    }


async def test_a_stored_permission_refusal_reaches_the_caller_as_empty_text() -> None:
    """The refusal is a `tool` part, and `list_messages` reads TEXT parts only."""
    # Given: a session holding a refusal opencode stored for the voice agent
    recorder = canned_body([_refused_tool_message("msg_d1", "bash", DENIED_BASH_ERROR)])
    # When: the transcript is read the one way production reads it
    messages = await _client(recorder).list_messages(SESSION_ID)
    # Then: the message is there and its rule enumeration is NOT in its text,
    # which is why no text-keyed sweep can ever see a refusal
    assert [(m.id, m.role, m.text) for m in messages] == [("msg_d1", "assistant", "")]


async def test_the_transcript_sweep_leaves_a_refusal_alone_and_keeps_its_anchor() -> None:
    """The sweep's contract on a refusal: not deleted, and the anchor unmoved."""
    # Given: the refusal as the newest message, after the user asked for the work
    recorder = canned_body(
        [
            _message_document("Найди статьи про RAG.", info={"id": "msg_u1", "role": "user"}),
            _refused_tool_message("msg_d1", "bash", DENIED_BASH_ERROR),
        ]
    )
    records = await _client(recorder).list_messages(SESSION_ID)
    # When: the collector's sweep runs over the very records it will act on
    sweep = transcript_sweep(records, sentinel=SENTINEL)
    # Then: nothing is deleted -- a refusal is the record that opencode refused a
    # command the user asked for, and this module may not take that away -- and
    # the anchor is the newest surviving message, which here IS the refusal
    assert sweep.signal_ids == ()
    assert sweep.since_message_id == "msg_d1"


async def test_an_empty_assistant_message_is_not_a_marker_of_a_refusal() -> None:
    """Why no sweep may key on "empty text": a SUCCESSFUL tool turn looks the same.

    Measured in the same live session: four `glob` calls that all completed are
    stored as an assistant message with no text part either.  Deleting empty-text
    assistant messages would therefore delete the record of every successful tool
    call, which costs the user far more than the refusal it was meant to remove.
    """
    recorder = canned_body(
        [
            _refused_tool_message("msg_d1", "bash", DENIED_BASH_ERROR),
            {
                "info": {"id": "msg_ok", "role": "assistant", "agent": "r2d2-agent"},
                "parts": [
                    {"id": "prt_ok_1", "type": "step-start"},
                    {
                        "id": "prt_ok_2",
                        "type": "tool",
                        "tool": "glob",
                        "callID": "call_ok",
                        "state": {
                            "status": "completed",
                            "input": {"pattern": "*.json", "path": "/tmp"},
                            "output": "a.json",
                        },
                    },
                    {"id": "prt_ok_3", "type": "step-finish", "reason": "tool-calls"},
                ],
            },
        ]
    )
    records = await _client(recorder).list_messages(SESSION_ID)
    # Then: the two messages are indistinguishable through the text channel, so
    # "empty" cannot mean "refused" -- and the sweep deletes neither
    assert [m.text for m in records] == ["", ""]
    assert transcript_sweep(records, sentinel=SENTINEL) == TranscriptSweep(
        since_message_id="msg_ok", signal_ids=()
    )


async def test_a_refusal_reaches_no_human_through_the_telegram_boundary() -> None:
    """`for_human` is the only outbound text path, and a refusal never enters it."""
    # Given: the user's own words next to the stored refusal
    recorder = canned_body(
        [
            _refused_tool_message("msg_d1", "bash", DENIED_BASH_ERROR),
            _message_document(
                "Команда выполнена, вот результат.",
                info={"id": "msg_a2", "role": "assistant", "agent": "r2d2-agent"},
            ),
        ]
    )
    records = await _client(recorder).list_messages(SESSION_ID)
    # When: the collected answer is put through the Telegram boundary
    shipped = for_human(records[-1].text, sentinel=SENTINEL)
    # Then: the answer is delivered whole, and no rule enumeration rides along
    # with it -- the refusal was never in the text channel to begin with
    assert shipped == "Команда выполнена, вот результат."
    assert all(DENIED_BASH_ERROR not in m.text for m in records)


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"ses_1": 42}, id="a-number"),
        pytest.param({"ses_1": None}, id="a-null"),
        pytest.param({"ses_1": {"type": 5}}, id="a-non-string-type"),
        pytest.param({"ses_1": {}}, id="no-type-at-all"),
    ],
)
async def test_session_status_raises_when_a_status_carries_no_readable_type(
    payload: object,
) -> None:
    """Given a status the reaper could misread: raise, do not stringify a guess."""
    # Given
    recorder = canned_body(payload)
    # When / Then
    with pytest.raises(OpencodeProtocolError):
        await _client(recorder).session_status()


async def test_a_bare_string_status_is_taken_as_the_status_itself() -> None:
    """Given `{"ses_1": "idle"}` instead of `{"ses_1": {"type": "idle"}}`: same fact."""
    # Given
    recorder = canned_body({"ses_1": "idle", "ses_2": {"type": "busy"}})
    # When
    status = await _client(recorder).session_status()
    # Then
    assert status == {"ses_1": "idle", "ses_2": "busy"}


async def test_a_boolean_route_raises_when_the_body_is_not_a_bool() -> None:
    """Given `abort` answering `{"ok": true}` instead of `true`: raise."""
    # Given
    recorder = FakeOpencode()
    recorder.abort_body = {"ok": True}
    # When / Then
    with pytest.raises(OpencodeProtocolError):
        await _client(recorder).abort(SESSION_ID)


# ---------------------------------------------------------------------------
# Adversarial: hung commands, auth, secrets, repeated interruption
# ---------------------------------------------------------------------------


async def test_every_request_carries_an_explicit_timeout() -> None:
    """Given any call: the timeout reaches the transport, so no call is unbounded."""
    # Given
    recorder = FakeOpencode()
    client = _client(recorder)
    # When
    await client.health()
    await client.resolve_session(TITLE)
    await _turn(client, deadline_s=1.25)
    await client.session_status()
    # Then
    assert recorder.requests
    for request in recorder.requests:
        timeouts = request.extensions.get("timeout", {})
        assert all(value is not None for value in timeouts.values()), (
            f"{request.method} {request.url.path} went out unbounded: {timeouts}"
        )


async def test_a_self_built_client_carries_the_configured_timeout(
    built_clients: list[httpx.AsyncClient],
) -> None:
    """Given no injected client: the one this client builds is bounded too."""
    # Given
    client = OpencodeClient(_spec(timeout=3.2), directory=WORKSPACE)
    # When
    await client.health()
    # Then
    assert [c.timeout for c in built_clients] == [httpx.Timeout(3.2)]


async def test_basic_auth_is_sent_when_both_username_and_password_are_present() -> None:
    """Given the opencode server's HTTP basic: the header is the expected one."""
    # Given
    recorder = FakeOpencode()
    # When
    await _client(recorder).health()
    # Then
    expected = base64.b64encode(f"opencode:{PASSWORD}".encode()).decode()
    assert recorder.only().headers["Authorization"] == f"Basic {expected}"


@pytest.mark.parametrize(
    ("username", "password"),
    [("", PASSWORD), ("opencode", ""), ("", "")],
    ids=["no-username", "no-password", "no-credentials"],
)
async def test_basic_auth_is_omitted_unless_both_halves_are_present(
    username: str, password: str
) -> None:
    """Given a half-empty credential: no `Authorization` header at all."""
    # Given
    recorder = FakeOpencode()
    # When
    await _client(recorder, spec=_spec(username=username, password=password)).health()
    # Then
    assert "Authorization" not in recorder.only().headers


async def test_a_non_2xx_body_is_never_echoed_with_the_password_in_it() -> None:
    """Given an error body that quotes the credential: it is scrubbed first."""
    # Given
    recorder = canned_status(401, content=f"unauthorized for {PASSWORD}".encode())
    # When
    with pytest.raises(OpencodeError) as excinfo:
        await _client(recorder).create_session(TITLE)
    # Then
    assert PASSWORD not in str(excinfo.value)
    assert "401" in str(excinfo.value)


async def test_the_password_never_reaches_a_log_record(caplog: pytest.LogCaptureFixture) -> None:
    """Given a failing call: the base_url is logged, the password never is."""
    # Given
    recorder = FakeOpencode()
    recorder.create_status = 503
    # When
    with caplog.at_level(logging.DEBUG), pytest.raises(OpencodeError):
        await _client(recorder).create_session(TITLE)
    # Then
    assert BASE_URL in caplog.text
    assert PASSWORD not in caplog.text
    assert "Authorization" not in caplog.text


def test_the_repr_of_a_client_shows_its_identity_and_not_its_credential() -> None:
    """Given a repr in a traceback: it names the server, never the password."""
    # Given
    client = _client(FakeOpencode())
    # When
    text = repr(client)
    # Then
    assert BASE_URL in text
    assert WORKSPACE in text
    assert PASSWORD not in text


async def test_aclose_leaves_an_injected_client_open_and_attached() -> None:
    """Given an injected client: `aclose()` must not close it, nor drop it."""
    # Given
    recorder = FakeOpencode()
    client, transport_client = _harness(recorder)
    # When
    await client.aclose()
    # Then
    assert not transport_client.is_closed
    # And the reference is still live: a detached client would rebuild a real one.
    assert await client.health() == OpencodeHealth(reachable=True, version="1.18.32")
    assert recorder.only().url.path == "/global/health"


async def test_aclose_called_twice_is_safe() -> None:
    """Given repeated interruption: closing an already-closed client is not an error."""
    # Given
    client, transport_client = _harness(FakeOpencode())
    # When
    await client.aclose()
    await client.aclose()
    # Then
    assert not transport_client.is_closed


async def test_aclose_is_safe_when_no_request_ever_built_a_client(
    built_clients: list[httpx.AsyncClient],
) -> None:
    """Given a client that never opened a socket: closing it still must not raise."""
    # Given
    client = OpencodeClient(_spec(), directory=WORKSPACE)
    # When / Then
    await client.aclose()
    await client.aclose()
    # And no client was built at all -- so nothing was opened either.
    assert built_clients == []


async def test_a_self_built_client_is_closed_and_the_second_close_is_safe(
    built_clients: list[httpx.AsyncClient],
) -> None:
    """Given a client this object created: `aclose()` owns it and must close it."""
    # Given
    client = OpencodeClient(_spec(), directory=WORKSPACE)
    await client.health()
    # When
    await client.aclose()
    # Then
    assert [c.is_closed for c in built_clients] == [True]
    await client.aclose()
