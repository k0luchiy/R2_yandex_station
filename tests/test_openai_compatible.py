"""Behavioural tests for `core.backends.openai_compatible` (plan todo 4).

One class replaces `core/providers/openrouter.py` and
`core/providers/yandexgpt.py`, so these tests pin the *wire behaviour* both of
them had, plus the three things the plan adds on top:

* the two Yandex auth modes, selected by `auth_style` + `auth_mode`;
* exactly one retry on 429/502/503 behind a real 0.4 s backoff, and **no** retry
  on `httpx.TimeoutException` -- the caller owns the deadline and must be able to
  tell a timeout from an overload;
* a 200 that carries `{"error": ...}` is NOT a reply (spike correction C1: a 200
  status is not success, the error lives in the body).

Every request goes through `httpx.MockTransport`; nothing here touches a
network. Requests are asserted on the *bytes that reached the transport* rather
than on a call count wherever a byte-level claim is stronger.

Timing: the single real-sleep test is
`test_retry_backoff_is_a_real_0_4_second_sleep`. Every other retry test
monkeypatches `asyncio.sleep` with a recorder, so the suite sleeps for real at
most 0.4 s in total.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import textwrap
import time
from collections.abc import Callable, Sequence
from typing import NoReturn

import httpx
import pytest

from core.backends.base import Backend, Choice, ToolCall
from core.backends.config_loader import BackendSpec
from core.backends.openai_compatible import (
    MAX_ATTEMPTS,
    RETRY_BACKOFF_S,
    RETRY_STATUSES,
    BackendError,
    BackendErrorEnvelope,
    BackendProtocolError,
    BackendStatusError,
    OpenAICompatibleBackend,
)

API_KEY = "sk-or-v1-SECRET-do-not-log-0123456789"
REDACTED = "[redacted]"
YANDEX_MODEL = "gpt://b1ghsjum2v37c2un1h4/yandexgpt-lite-5"
MESSAGES = [{"role": "system", "content": "be brief"}, {"role": "user", "content": "hi"}]

# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class Recorder:
    """A `MockTransport` handler that remembers every request it saw.

    Responses are produced by *factories* rather than values: httpx marks a
    `Response`'s stream as consumed once read, so handing back the same object
    twice (as a retry does) would fail in the transport rather than at the
    assertion. When the list runs dry the last factory repeats.
    """

    def __init__(self, factories: Sequence[Callable[[], httpx.Response]]) -> None:
        self._factories = list(factories)
        self.requests: list[httpx.Request] = []

    @property
    def count(self) -> int:
        return len(self.requests)

    @property
    def first(self) -> httpx.Request:
        assert self.requests, "the transport was never reached"
        return self.requests[0]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        index = min(len(self.requests) - 1, len(self._factories) - 1)
        return self._factories[index]()


class FakeSleep:
    """Records requested delays instead of serving them, so retry tests are instant."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def _spec(
    name: str = "openrouter",
    base_url: str = "https://openrouter.ai/api/v1",
    model: str = "inclusionai/ling-3.0-flash:free",
    auth_style: str = "bearer",
    auth_mode: str = "",
    api_key: str = API_KEY,
) -> BackendSpec:
    """A spec-shaped builder: the knobs are the spec fields a test varies, nothing else."""
    return BackendSpec(
        name=name,
        kind="openai_compatible",
        base_url=base_url,
        api_key=api_key,
        model=model,
        auth_style=auth_style,
        auth_mode=auth_mode,
    )


def _ok(
    *,
    content: object = "Готово",
    tool_calls: list[dict[str, object]] | None = None,
    extra: dict[str, object] | None = None,
) -> Callable[[], httpx.Response]:
    message: dict[str, object] = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    payload: dict[str, object] = {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "model": "server-side-model-name",
        "choices": [{"index": 0, "finish_reason": "stop", "message": message}],
    }
    if extra:
        payload.update(extra)
    return lambda: httpx.Response(200, json=payload)


def _status(code: int, *, body: object = None) -> Callable[[], httpx.Response]:
    return lambda: httpx.Response(code, json=body if body is not None else {"error": {"message": "nope"}})


def _timeout() -> NoReturn:
    """A transport that never answers. Typed `NoReturn`, so it composes as a factory."""
    raise httpx.ReadTimeout("timed out")


def _backend(
    recorder: Recorder | None = None,
    *,
    spec: BackendSpec | None = None,
    client: httpx.AsyncClient | None = None,
) -> OpenAICompatibleBackend:
    """Build a backend whose HTTP client talks to `recorder` and nothing else."""
    resolved_spec = spec if spec is not None else _spec()
    resolved_client = client
    if resolved_client is None:
        resolved_client = httpx.AsyncClient(transport=httpx.MockTransport(recorder or Recorder([_ok()])))
    return OpenAICompatibleBackend(resolved_spec, client=resolved_client)


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> FakeSleep:
    """Replace `asyncio.sleep` with a recorder: retries stay instant."""
    fake = FakeSleep()
    monkeypatch.setattr(asyncio, "sleep", fake)
    return fake


@pytest.fixture
def client_factory(monkeypatch: pytest.MonkeyPatch) -> list[httpx.AsyncClient]:
    """Make every client the backend builds itself a `MockTransport` client.

    Returns the list of clients handed out, so a test can assert how many were
    created and whether they were closed. This is also what guarantees the
    "no network anywhere" rule for the code paths that construct their own
    client instead of receiving one.
    """
    created: list[httpx.AsyncClient] = []
    real_client = httpx.AsyncClient
    recorder = Recorder([_ok()])

    def factory(*, timeout: float = 5.0) -> httpx.AsyncClient:
        client = real_client(transport=httpx.MockTransport(recorder), timeout=timeout)
        created.append(client)
        return client

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return created


# ---------------------------------------------------------------------------
# Auth styles
# ---------------------------------------------------------------------------


async def test_bearer_auth_style_sends_an_authorization_bearer_header() -> None:
    """Given `auth_style="bearer"`: the header carries the api key as a bearer token."""
    # Given
    recorder = Recorder([_ok()])
    # When
    await _backend(recorder).complete(MESSAGES)
    # Then
    assert recorder.first.headers["Authorization"] == f"Bearer {API_KEY}"
    assert recorder.first.headers["Content-Type"] == "application/json"


async def test_yandex_auth_style_in_api_key_mode_sends_an_authorization_api_key_header() -> None:
    """Given Yandex + `auth_mode="api_key"`: the folder api key uses the `Api-Key` scheme."""
    # Given
    recorder = Recorder([_ok()])
    backend = _backend(
        recorder,
        spec=_spec(
            name="yandexgpt",
            base_url="https://llm.api.cloud.yandex.net/foundationModels/v1",
            model=YANDEX_MODEL,
            auth_style="yandex",
            auth_mode="api_key",
        ),
    )
    # When
    await backend.complete(MESSAGES)
    # Then
    assert recorder.first.headers["Authorization"] == f"Api-Key {API_KEY}"


@pytest.mark.parametrize("auth_mode", ["iam_token", "iam", "IAM_Token"])
async def test_yandex_auth_style_in_iam_token_mode_travels_as_the_bearer_credential(
    auth_mode: str,
) -> None:
    """Given Yandex + an IAM-token mode: the token goes out as `Bearer <token>`.

    The legacy spelling `iam` is accepted because `YandexGPTProvider` compared
    `cfg.yandex_auth_mode.lower() == "iam"`, so dropping it would silently switch
    a working deployment's header scheme.
    """
    # Given
    recorder = Recorder([_ok()])
    backend = _backend(
        recorder,
        spec=_spec(name="yandexgpt", auth_style="yandex", auth_mode=auth_mode, model=YANDEX_MODEL),
    )
    # When
    await backend.complete(MESSAGES)
    # Then
    assert recorder.first.headers["Authorization"] == f"Bearer {API_KEY}"


async def test_unknown_auth_style_is_rejected_before_any_request_is_sent() -> None:
    """Given an auth style that is neither `bearer` nor `yandex`: fail loudly, send nothing."""
    # Given
    recorder = Recorder([_ok()])
    backend = _backend(recorder, spec=_spec(name="zen", auth_style="basic"))
    # When / Then
    with pytest.raises(BackendError) as excinfo:
        await backend.complete(MESSAGES)
    assert "basic" in str(excinfo.value)
    assert "zen" in str(excinfo.value)
    assert recorder.count == 0


# ---------------------------------------------------------------------------
# Request shape
# ---------------------------------------------------------------------------


async def test_happy_path_request_is_byte_identical_on_the_wire() -> None:
    """Given a plain completion: URL, method, headers and body match, byte for byte.

    The body literal is written out by hand rather than produced by the same
    encoder the implementation uses, so a change in key order or spacing fails.
    """
    # Given
    recorder = Recorder([_ok()])
    backend = _backend(recorder)
    # When
    await backend.complete(MESSAGES)
    # Then
    request = recorder.first
    assert request.method == "POST"
    assert str(request.url) == "https://openrouter.ai/api/v1/chat/completions"
    assert request.headers["Authorization"] == f"Bearer {API_KEY}"
    assert request.headers["Content-Type"] == "application/json"
    assert request.content == (
        b'{"model":"inclusionai/ling-3.0-flash:free",'
        b'"messages":[{"role":"system","content":"be brief"},{"role":"user","content":"hi"}],'
        b'"max_tokens":400,"temperature":0.7}'
    )


async def test_model_uri_is_passed_through_verbatim() -> None:
    """Given a `gpt://folder/model` model: it reaches the body as the exact same bytes."""
    # Given
    recorder = Recorder([_ok()])
    backend = _backend(
        recorder, spec=_spec(name="yandexgpt", base_url="https://llm.api.cloud.yandex.net/foundationModels/v1",
                             model=YANDEX_MODEL, auth_style="yandex", auth_mode="api_key")
    )
    # When
    choice = await backend.complete(MESSAGES)
    # Then
    assert f'"model":"{YANDEX_MODEL}"'.encode() in recorder.first.content
    assert json.loads(recorder.first.content)["model"] == YANDEX_MODEL
    assert choice.model == YANDEX_MODEL


async def test_explicit_model_argument_overrides_the_spec_model() -> None:
    """Given `model=`: it wins over `spec.model`, and it is echoed in the choice."""
    # Given
    recorder = Recorder([_ok()])
    # When
    choice = await _backend(recorder).complete(MESSAGES, model="gpt://override/folder/model")
    # Then
    assert json.loads(recorder.first.content)["model"] == "gpt://override/folder/model"
    assert choice.model == "gpt://override/folder/model"


async def test_tools_are_serialised_into_the_request_body_unchanged() -> None:
    """Given tool schemas: they land in the body exactly as passed."""
    # Given
    recorder = Recorder([_ok()])
    tools = [
        {
            "type": "function",
            "function": {
                "name": "system_status",
                "description": "заряд и температура",
                "parameters": {"type": "object", "properties": {"verbose": {"type": "boolean"}}},
            },
        }
    ]
    # When
    await _backend(recorder).complete(MESSAGES, tools)
    # Then
    assert json.loads(recorder.first.content)["tools"] == tools
    assert f'"tools":{json.dumps(tools, ensure_ascii=False, separators=(",", ":"))}'.encode() in recorder.first.content


async def test_tools_key_is_absent_when_no_tools_are_supplied() -> None:
    """Given no tools: the key is omitted, not sent as null or an empty list."""
    # Given
    recorder = Recorder([_ok()])
    backend = _backend(recorder)
    # When
    await backend.complete(MESSAGES)
    await backend.complete(MESSAGES, [])
    # Then
    for request in recorder.requests:
        assert "tools" not in json.loads(request.content)


async def test_max_tokens_and_temperature_are_sent_verbatim() -> None:
    """Given sampling parameters: they are passed through, not clamped or renamed."""
    # Given
    recorder = Recorder([_ok()])
    # When
    await _backend(recorder).complete(MESSAGES, max_tokens=57, temperature=0.0)
    # Then
    body = json.loads(recorder.first.content)
    assert body["max_tokens"] == 57
    assert body["temperature"] == 0.0


async def test_non_ascii_message_content_is_sent_as_utf8_not_escaped() -> None:
    """Given a Russian question: the body carries UTF-8 bytes, no `\\uXXXX` escapes."""
    # Given
    recorder = Recorder([_ok()])
    # When
    await _backend(recorder).complete([{"role": "user", "content": "Привет"}])
    # Then
    assert '"Привет"'.encode() in recorder.first.content


async def test_base_url_with_a_trailing_slash_does_not_double_the_path_separator() -> None:
    """Given a base url ending in `/`: the endpoint is `{base}/chat/completions`."""
    # Given
    recorder = Recorder([_ok()])
    backend = _backend(recorder, spec=_spec(base_url="https://openrouter.ai/api/v1/"))
    # When
    await backend.complete(MESSAGES)
    # Then
    assert str(recorder.first.url) == "https://openrouter.ai/api/v1/chat/completions"


async def test_missing_model_is_rejected_before_any_request_is_sent() -> None:
    """Given no spec model and no `model=` argument: fail instead of sending an empty model."""
    # Given
    recorder = Recorder([_ok()])
    backend = _backend(recorder, spec=_spec(model=""))
    # When / Then
    with pytest.raises(BackendError, match="model"):
        await backend.complete(MESSAGES)
    assert recorder.count == 0


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


async def test_content_maps_to_choice_content_with_provider_and_model_populated() -> None:
    """Given a plain answer: content, provider and model all reach the Choice."""
    # Given
    recorder = Recorder([_ok(content="Заряд 87%")])
    # When
    choice = await _backend(recorder).complete(MESSAGES)
    # Then
    assert isinstance(choice, Choice)
    assert choice.content == "Заряд 87%"
    assert choice.provider == "openrouter"
    assert choice.model == "inclusionai/ling-3.0-flash:free"
    assert choice.tool_calls == []


async def test_null_content_degrades_to_an_empty_string() -> None:
    """Given `content: null` (some gateways omit it on tool calls): the content is ""."""
    # Given
    recorder = Recorder([_ok(content=None)])
    # When
    choice = await _backend(recorder).complete(MESSAGES)
    # Then
    assert choice.content == ""


async def test_tool_call_arguments_given_as_a_json_string_are_parsed() -> None:
    """Given `arguments` as a JSON string: it is decoded into a dict."""
    # Given
    recorder = Recorder(
        [_ok(tool_calls=[{"id": "call_1", "type": "function", "function": {"name": "send_tg", "arguments": '{"chat_id": 1, "text": "привет"}'}}])]
    )
    # When
    choice = await _backend(recorder).complete(MESSAGES)
    # Then
    assert choice.tool_calls == [ToolCall(id="call_1", name="send_tg", arguments={"chat_id": 1, "text": "привет"})]


async def test_tool_call_arguments_already_an_object_are_used_as_is() -> None:
    """Given `arguments` as an object (some gateways pre-parse it): the dict survives.

    The verbatim `parse_choice` would `json.loads({...})` and swallow the
    TypeError, silently yielding `{}` -- so the backend must hand the parser a
    string, not leave the object in place.
    """
    # Given
    recorder = Recorder(
        [_ok(tool_calls=[{"id": "call_2", "type": "function", "function": {"name": "open_app", "arguments": {"app": "browser", "args": {"a": 1}}}}])]
    )
    # When
    choice = await _backend(recorder).complete(MESSAGES)
    # Then
    assert choice.tool_calls == [ToolCall(id="call_2", name="open_app", arguments={"app": "browser", "args": {"a": 1}})]


async def test_malformed_json_arguments_degrade_to_an_empty_dict() -> None:
    """Given `arguments` that is not JSON at all: `{}` rather than an exception."""
    # Given
    recorder = Recorder(
        [_ok(tool_calls=[{"id": "call_3", "function": {"name": "system_status", "arguments": "{not json"}}])]
    )
    # When
    choice = await _backend(recorder).complete(MESSAGES)
    # Then
    assert choice.tool_calls == [ToolCall(id="call_3", name="system_status", arguments={})]


@pytest.mark.parametrize(
    "arguments",
    ["[]", "null", "5", '"text"', "true", "[]", ""],
    ids=["empty-array", "null", "number", "bare-string", "bool", "array-again", "empty-string"],
)
async def test_non_object_arguments_degrade_to_an_empty_dict(arguments: str) -> None:
    """Given arguments that parse to something other than an object: `{}`.

    `ToolCall.arguments` is typed `dict`; handing the tool executor a list or a
    None would be a type lie that only explodes three layers up.
    """
    # Given
    recorder = Recorder([_ok(tool_calls=[{"id": "c", "function": {"name": "f", "arguments": arguments}}])])
    # When
    choice = await _backend(recorder).complete(MESSAGES)
    # Then
    assert choice.tool_calls == [ToolCall(id="c", name="f", arguments={})]
    assert isinstance(choice.tool_calls[0].arguments, dict)


async def test_non_string_non_object_arguments_degrade_to_an_empty_dict() -> None:
    """Given `arguments` as a JSON array (not a string, not an object): `{}`."""
    # Given
    recorder = Recorder([_ok(tool_calls=[{"id": "c", "function": {"name": "f", "arguments": ["a", "b"]}}])])
    # When
    choice = await _backend(recorder).complete(MESSAGES)
    # Then
    assert choice.tool_calls[0].arguments == {}


async def test_tool_call_without_a_function_degrades_to_an_empty_name() -> None:
    """Given a tool call whose `function` is missing: no crash, empty name, no arguments."""
    # Given
    recorder = Recorder([_ok(tool_calls=[{"id": "call_4", "type": "function"}])])
    # When
    choice = await _backend(recorder).complete(MESSAGES)
    # Then
    assert choice.tool_calls == [ToolCall(id="call_4", name="", arguments={})]


async def test_tool_call_without_an_id_defaults_to_an_empty_string() -> None:
    """Given a tool call with no `id`: the id defaults to "" (verbatim parser behaviour)."""
    # Given
    recorder = Recorder([_ok(tool_calls=[{"function": {"name": "f", "arguments": "{}"}}])])
    # When
    choice = await _backend(recorder).complete(MESSAGES)
    # Then
    assert choice.tool_calls[0].id == ""


async def test_several_tool_calls_are_all_parsed_in_order() -> None:
    """Given three tool calls: all three are returned, in wire order."""
    # Given
    calls = [
        {"id": "a", "function": {"name": "one", "arguments": '{"n": 1}'}},
        {"id": "b", "function": {"name": "two", "arguments": {"n": 2}}},
        {"id": "c", "function": {"name": "three", "arguments": "oops"}},
    ]
    recorder = Recorder([_ok(tool_calls=calls)])
    # When
    choice = await _backend(recorder).complete(MESSAGES)
    # Then
    assert [call.name for call in choice.tool_calls] == ["one", "two", "three"]
    assert [call.arguments for call in choice.tool_calls] == [{"n": 1}, {"n": 2}, {}]


async def test_raw_carries_the_response_body_for_inspection() -> None:
    """Given a well-formed response: `raw` holds the decoded body, choices intact."""
    # Given
    recorder = Recorder([_ok(extra={"usage": {"total_tokens": 12}})])
    # When
    choice = await _backend(recorder).complete(MESSAGES)
    # Then
    assert choice.raw is not None
    assert choice.raw["usage"] == {"total_tokens": 12}
    assert choice.raw["id"] == "chatcmpl-1"


# ---------------------------------------------------------------------------
# A 200 is not necessarily a success (spike correction C1)
# ---------------------------------------------------------------------------


async def test_http_200_carrying_an_error_envelope_raises_instead_of_returning_a_choice() -> None:
    """Given a 200 whose body is `{"error": {...}}`: never reported as a choice.

    opencode, Zen and OpenRouter all answer a refused model with HTTP 200 and the
    real failure inside the body, so a status check alone is a misleading success.
    """
    # Given
    body: dict[str, object] = {"error": {"message": "FreeTierError: only from within OpenCode", "code": 403}}
    recorder = Recorder([lambda: httpx.Response(200, json=body)])
    # When / Then
    with pytest.raises(BackendErrorEnvelope) as excinfo:
        await _backend(recorder).complete(MESSAGES)
    assert "FreeTierError" in str(excinfo.value)
    assert isinstance(excinfo.value, BackendError)
    assert recorder.count == 1


async def test_http_200_with_both_choices_and_an_error_is_still_an_error() -> None:
    """Given a 200 that carries an `error` next to a `choices` array: the error wins.

    Reporting the choice here would let a refused-but-shaped response through as a
    real answer, which is exactly the failure mode C1 warns about.
    """
    # Given
    recorder = Recorder([_ok(extra={"error": {"message": "Insufficient account funds", "code": 402}})])
    # When / Then
    with pytest.raises(BackendErrorEnvelope, match="Insufficient account funds"):
        await _backend(recorder).complete(MESSAGES)


async def test_http_200_with_a_null_error_key_is_not_an_error_envelope() -> None:
    """Given `"error": null` alongside a real answer: the answer is returned."""
    # Given
    recorder = Recorder([_ok(extra={"error": None})])
    # When
    choice = await _backend(recorder).complete(MESSAGES)
    # Then
    assert choice.content == "Готово"


@pytest.mark.parametrize(
    ("label", "response_factory"),
    [
        ("not-json", lambda: httpx.Response(200, content=b"<html>proxy error</html>")),
        ("json-array", lambda: httpx.Response(200, json=[1, 2, 3])),
        ("json-null", lambda: httpx.Response(200, json=None)),
        ("empty-choices", lambda: httpx.Response(200, json={"choices": []})),
        ("no-choices-key", lambda: httpx.Response(200, json={"id": "x"})),
        ("choice-without-message", lambda: httpx.Response(200, json={"choices": [{"index": 0}]})),
        ("message-not-an-object", lambda: httpx.Response(200, json={"choices": [{"message": "hi"}]})),
        ("tool-calls-not-a-list", lambda: httpx.Response(200, json={"choices": [{"message": {"tool_calls": {"id": "a"}}}]})),
        ("tool-call-not-an-object", lambda: httpx.Response(200, json={"choices": [{"message": {"tool_calls": ["oops"]}}]})),
    ],
    ids=["not-json", "json-array", "json-null", "empty-choices", "no-choices", "no-message", "message-str", "tool-calls-obj", "tool-call-str"],
)
async def test_malformed_success_body_raises_a_protocol_error(response_factory: Callable[[], httpx.Response], label: str) -> None:
    """Given a 200 that is not a usable completion: a typed protocol error, never a Choice."""
    # Given
    recorder = Recorder([response_factory])
    # When / Then
    with pytest.raises(BackendProtocolError) as excinfo:
        await _backend(recorder).complete(MESSAGES)
    assert excinfo.value.args, f"{label}: the error must say what was wrong"
    assert isinstance(excinfo.value, BackendError)


async def test_non_2xx_raises_a_status_error_carrying_the_code() -> None:
    """Given a 404: `BackendStatusError` with `.status_code` and the backend name."""
    # Given
    recorder = Recorder([_status(404, body={"error": {"message": "no such model"}})])
    # When / Then
    with pytest.raises(BackendStatusError) as excinfo:
        await _backend(recorder).complete(MESSAGES)
    assert excinfo.value.status_code == 404
    assert excinfo.value.backend == "openrouter"
    assert "no such model" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Retries
# ---------------------------------------------------------------------------


async def test_429_is_retried_exactly_once_then_succeeds(no_sleep: FakeSleep) -> None:
    """Given one 429 then a 200: the choice is returned and the backoff was requested once."""
    # Given
    recorder = Recorder([_status(429), _ok(content="второй раз")])
    # When
    choice = await _backend(recorder).complete(MESSAGES)
    # Then
    assert recorder.count == 2
    assert choice.content == "второй раз"
    assert no_sleep.delays == [RETRY_BACKOFF_S]
    assert recorder.requests[0].content == recorder.requests[1].content


async def test_429_twice_raises_after_exactly_two_requests(no_sleep: FakeSleep) -> None:
    """Given a permanent 429: exactly one retry, then `BackendStatusError`."""
    # Given
    recorder = Recorder([_status(429)])
    # When / Then
    with pytest.raises(BackendStatusError) as excinfo:
        await _backend(recorder).complete(MESSAGES)
    assert excinfo.value.status_code == 429
    assert recorder.count == MAX_ATTEMPTS == 2
    assert no_sleep.delays == [RETRY_BACKOFF_S]


@pytest.mark.parametrize("code", sorted(RETRY_STATUSES))
async def test_every_declared_retryable_status_is_retried_once(code: int, no_sleep: FakeSleep) -> None:
    """Given a retryable status: the second attempt is made and its answer is used."""
    # Given
    recorder = Recorder([_status(code), _ok()])
    # When
    choice = await _backend(recorder).complete(MESSAGES)
    # Then
    assert recorder.count == 2
    assert choice.content == "Готово"
    assert no_sleep.delays == [RETRY_BACKOFF_S]


def test_the_retryable_set_is_exactly_429_502_and_503() -> None:
    """Given the module constant: it is the set the plan names, no more."""
    # When / Then
    assert RETRY_STATUSES == frozenset({429, 502, 503})


@pytest.mark.parametrize("code", [400, 401, 403, 404, 500])
async def test_a_non_retryable_status_is_not_retried(code: int, no_sleep: FakeSleep) -> None:
    """Given 400/401/403/404/500: one request only -- retrying a rejection is waste."""
    # Given
    recorder = Recorder([_status(code)])
    # When / Then
    with pytest.raises(BackendStatusError) as excinfo:
        await _backend(recorder).complete(MESSAGES)
    assert excinfo.value.status_code == code
    assert recorder.count == 1
    assert no_sleep.delays == []


async def test_timeout_propagates_untouched_and_without_retry(no_sleep: FakeSleep) -> None:
    """Given a transport timeout: it propagates, and exactly one request was made.

    The brain breaks out of its own fallback loop on `httpx.TimeoutException`
    because the deadline, not the provider, is the problem; a wrapper exception
    would defeat that branch, and a retry would burn a third of the budget.
    """
    # Given
    recorder = Recorder([_timeout])
    # When / Then
    with pytest.raises(httpx.TimeoutException):
        await _backend(recorder).complete(MESSAGES)
    assert recorder.count == 1
    assert no_sleep.delays == []


async def test_retry_backoff_is_a_real_0_4_second_sleep() -> None:
    """Given a permanent 429: the second attempt waits a real 0.4 s, not a no-op.

    The only test in the suite that sleeps for real.
    """
    # Given
    recorder = Recorder([_status(429)])
    started = time.perf_counter()
    # When / Then
    with pytest.raises(BackendStatusError):
        await _backend(recorder).complete(MESSAGES)
    elapsed = time.perf_counter() - started
    assert RETRY_BACKOFF_S == 0.4
    assert elapsed >= RETRY_BACKOFF_S
    assert recorder.count == 2


# ---------------------------------------------------------------------------
# Secret hygiene
# ---------------------------------------------------------------------------


async def test_api_key_never_appears_in_an_exception_message() -> None:
    """Given a server that echoes the credential in its error body: it is redacted."""
    # Given
    recorder = Recorder([_status(401, body={"error": {"message": f"bad key {API_KEY}"}})])
    # When
    with pytest.raises(BackendStatusError) as excinfo:
        await _backend(recorder).complete(MESSAGES)
    # Then
    assert API_KEY not in str(excinfo.value)
    assert API_KEY not in repr(excinfo.value)
    # The surrounding, non-secret text survives, so the assertion above is not
    # passing merely because the message is empty.
    assert f"bad key {REDACTED}" in str(excinfo.value)


async def test_api_key_never_appears_in_a_log_record(caplog: pytest.LogCaptureFixture, no_sleep: FakeSleep) -> None:
    """Given a failure that logs a warning: no log record carries the credential."""
    # Given
    recorder = Recorder([_status(429), _status(429)])
    # When / Then
    with caplog.at_level(logging.DEBUG), pytest.raises(BackendStatusError):
        await _backend(recorder).complete(MESSAGES)
    assert "backend" in caplog.text
    assert API_KEY not in caplog.text


async def test_api_key_never_appears_in_choice_raw() -> None:
    """Given a success body that echoes the credential: `raw` is scrubbed."""
    # Given
    recorder = Recorder([_ok(extra={"echoed": f"Authorization: Bearer {API_KEY}"})])
    # When
    choice = await _backend(recorder).complete(MESSAGES)
    # Then
    assert choice.content == "Готово"
    dumped = json.dumps(choice.raw, ensure_ascii=False)
    assert API_KEY not in dumped
    assert f"Authorization: Bearer {REDACTED}" in dumped


async def test_an_uncredentialed_backend_does_not_mangle_a_literal_redaction_token() -> None:
    """Given an empty api key: nothing is replaced, so `[redacted]` in a body survives."""
    # Given
    recorder = Recorder([_ok(content=f"literal {REDACTED} token")])
    backend = _backend(recorder, spec=_spec(api_key=""))
    # When
    choice = await backend.complete(MESSAGES)
    # Then
    assert choice.content == f"literal {REDACTED} token"


# ---------------------------------------------------------------------------
# Client ownership
# ---------------------------------------------------------------------------


async def test_aclose_does_not_close_a_client_it_did_not_create() -> None:
    """Given an injected client: `aclose()` leaves it open and usable."""
    # Given
    recorder = Recorder([_ok(), _ok()])
    client = httpx.AsyncClient(transport=httpx.MockTransport(recorder))
    backend = OpenAICompatibleBackend(_spec(), client=client)
    await backend.complete(MESSAGES)
    # When
    await backend.aclose()
    # Then
    assert client.is_closed is False
    assert (await backend.complete(MESSAGES)).content == "Готово"
    await client.aclose()


async def test_aclose_closes_a_client_it_created(client_factory: list[httpx.AsyncClient]) -> None:
    """Given no injected client: the lazily created one is closed by `aclose()`."""
    # Given
    backend = OpenAICompatibleBackend(_spec())
    # When
    await backend.complete(MESSAGES)
    await backend.aclose()
    # Then
    assert len(client_factory) == 1
    assert client_factory[0].is_closed is True


async def test_aclose_before_the_first_request_creates_no_client(
    client_factory: list[httpx.AsyncClient],
) -> None:
    """Given a backend that never ran: `aclose()` is a no-op -- clients are lazy."""
    # Given / When
    backend = OpenAICompatibleBackend(_spec())
    await backend.aclose()
    # Then
    assert client_factory == []


async def test_the_created_client_is_reused_across_completions(
    client_factory: list[httpx.AsyncClient],
) -> None:
    """Given two completions: one client, so connections are pooled across calls."""
    # Given
    backend = OpenAICompatibleBackend(_spec())
    # When
    await backend.complete(MESSAGES)
    await backend.complete(MESSAGES)
    # Then
    assert len(client_factory) == 1
    await backend.aclose()


# ---------------------------------------------------------------------------
# Protocol conformance, verbatim reuse and the shipped config
# ---------------------------------------------------------------------------


def test_the_backend_satisfies_the_backend_protocol() -> None:
    """Given the class: it structurally conforms to `core.backends.base.Backend`."""
    # When / Then
    assert isinstance(_backend(), Backend)
    assert OpenAICompatibleBackend(_spec()).name == "openrouter"


def test_parse_choice_is_reused_verbatim_from_the_legacy_provider_base() -> None:
    """Given the parser: its source is byte-identical to the deleted one.

    The plan requires verbatim reuse. Pinning the source (not just the
    behaviour) is what stops a later "small" refactor from silently changing the
    semantics every downstream tool call depends on.
    """
    # When
    actual = textwrap.dedent(inspect.getsource(OpenAICompatibleBackend.parse_choice))
    # Then
    assert actual.strip() == textwrap.dedent(LEGACY_PARSE_CHOICE_SOURCE).strip()


def test_parse_choice_keeps_the_legacy_signature() -> None:
    """Given the parser: `(data, provider, model)` positional, keyword-callable."""
    # When / Then
    signature = inspect.signature(OpenAICompatibleBackend.parse_choice)
    assert list(signature.parameters) == ["data", "provider", "model"]
    assert isinstance(inspect.getattr_static(OpenAICompatibleBackend, "parse_choice"), staticmethod)


def test_the_three_shipped_openai_compatible_backends_all_construct() -> None:
    """Given `config/backends.json`: zen, openrouter and yandexgpt each build a backend.

    Proves the one class really does cover all three, with the auth scheme each
    spec asks for -- no network, construction only.
    """
    # Given
    from app.config import Config  # local import: keeps this file free of config side effects
    from core.backends.config_loader import load_backend_specs

    for variable, value in {
        "R2D2_ZEN_KEY": "zen-key",
        "OPENROUTER_API_KEY": "openrouter-key",
        "YANDEX_API_KEY": "yandex-key",
        "YANDEX_FOLDER_ID": "b1ghsjum2v37c2un1h4",
        "R2D2_OC_USERNAME": "opencode",
        "R2D2_OC_PASSWORD": "oc-password",
    }.items():
        os.environ[variable] = value
    # When
    _chain, specs = load_backend_specs(Config.load())
    # Then
    openai_compatible = {name: spec for name, spec in specs.items() if spec.kind == "openai_compatible"}
    assert set(openai_compatible) == {"zen", "openrouter", "yandexgpt"}
    for name, spec in openai_compatible.items():
        backend = OpenAICompatibleBackend(spec)
        assert backend.name == name
        assert spec.model
    yandex = specs["yandexgpt"]
    assert yandex.model == YANDEX_MODEL
    assert yandex.auth_style == "yandex"
    assert yandex.auth_mode == "api_key"


# The `parse_choice` half of the deleted `core/providers/base.py`, kept verbatim so
# the parity assertion above compares against the original code, not a restatement.
LEGACY_PARSE_CHOICE_SOURCE = """\
    @staticmethod
    def parse_choice(data: dict, provider: str, model: str) -> Choice:
        msg = data["choices"][0]["message"]
        content = msg.get("content") or ""
        tool_calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except (TypeError, ValueError):
                args = {}
            tool_calls.append(ToolCall(id=tc.get("id", ""), name=fn.get("name", ""), arguments=args))
        return Choice(content=content, tool_calls=tool_calls, provider=provider, model=model, raw=data)
"""
