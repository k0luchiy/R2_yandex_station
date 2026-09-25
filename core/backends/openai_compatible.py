"""One OpenAI-compatible chat backend for OpenRouter, YandexGPT and Zen (plan todo 4).

`core/providers/openrouter.py` and `core/providers/yandexgpt.py` were the same
request with two `Authorization` schemes and two model strings, so both
differences are `BackendSpec` fields now: `auth_style` (`"bearer"` | `"yandex"`)
and `auth_mode` (`"api_key"` | `"iam_token"`, plus the legacy `"iam"` spelling
`YandexGPTProvider` compared against). Preserved verbatim from them:
`POST {base_url}/chat/completions`, `Bearer {api_key}` for `bearer`, `Api-Key
{api_key}` for Yandex `api_key` mode and `Bearer {iam_token}` for `iam_token`
mode, the `model` string passed through untouched so `gpt://folder/model` URIs
work, `tools` serialised as given, and the deleted
`core/providers/base.py:parse_choice` reused as the response parser (its source
is pinned by `tests/test_openai_compatible.py`).

Added per the plan: one retry on 429/502/503 behind a real 0.4 s sleep, and NO
retry on `httpx.TimeoutException` -- it propagates untouched, because
`core/brain.py` breaks out of its own fallback loop on exactly that type: the
caller owns the deadline, so a second attempt would spend budget it no longer
has.

A 200 is not a success (spike correction C1): opencode, Zen and OpenRouter all
answer a refused model with HTTP 200 and the real failure inside the body, so a
body carrying a non-null `error` raises `BackendErrorEnvelope` instead of
becoming a `Choice`. The configured api key is scrubbed from everything this
backend emits -- `Choice.raw`, every exception message, the log line.

Callers branch on `BackendError` (any non-reply outcome) and on
`httpx.TimeoutException` (deadline).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping, Sequence
from typing import Final

import httpx

from core.backends.base import (
    BackendError,
    BackendErrorEnvelope,
    BackendProtocolError,
    BackendStatusError,
    ChatMessage,
    Choice,
    ToolCall,
    ToolSchema,
)
from core.backends.config_loader import BackendSpec

__all__ = [
    "BackendError",
    "BackendErrorEnvelope",
    "BackendProtocolError",
    "BackendStatusError",
    "OpenAICompatibleBackend",
]

log = logging.getLogger(__name__)

#: The only statuses worth a second attempt: throttling or a shedding edge.
#: 400/401/403/404/500 are verdicts, not weather.
RETRY_STATUSES: Final[frozenset[int]] = frozenset({429, 502, 503})
#: One retry, one pause: long enough to outlast the first burst of a throttling
#: window, short enough to stay inside the 3.2 s voice budget.
RETRY_BACKOFF_S: Final = 0.4
MAX_ATTEMPTS: Final = 2
BODY_EXCERPT_CHARS: Final = 240
REDACTED: Final = "[redacted]"
#: `YandexGPTProvider` compared `cfg.yandex_auth_mode.lower() == "iam"`, so that
#: spelling must keep working or a live deployment silently changes auth scheme.
IAM_MODES: Final[frozenset[str]] = frozenset({"iam", "iam_token"})


def _authorization(spec: BackendSpec) -> str:
    """The `Authorization` header value this spec's auth style demands."""
    match spec.auth_style:
        case "bearer":
            scheme = "Bearer"
        case "yandex":
            scheme = "Bearer" if spec.auth_mode.lower() in IAM_MODES else "Api-Key"
        case _:
            raise BackendError(f"backend {spec.name!r}: auth_style {spec.auth_style!r} is not supported; expected 'bearer' or 'yandex'")
    return f"{scheme} {spec.api_key}"


def _scrub_text(text: str, secret: str) -> str:
    return text.replace(secret, REDACTED) if secret else text


def _scrub_value(value: object, secret: str) -> object:
    """`value` with every occurrence of `secret` replaced, recursing into containers.

    An empty `secret` is the identity: an uncredentialed fallback must not rewrite
    a body that legitimately contains the marker text.
    """
    if not secret or not isinstance(value, (str, dict, list)):
        return value
    if isinstance(value, str):
        return _scrub_text(value, secret)
    if isinstance(value, dict):
        return {key: _scrub_value(item, secret) for key, item in value.items()}
    return [_scrub_value(item, secret) for item in value]


def _excerpt(response: httpx.Response, secret: str) -> str:
    """A short, credential-free slice of the body, for an error message."""
    return _scrub_text(response.text, secret).replace("\n", " ")[:BODY_EXCERPT_CHARS]


def _arguments(value: object) -> str:
    """`value` as the JSON object string `parse_choice` expects, or `"{}"`.

    Some OpenAI-compatible gateways pre-parse `arguments` into an object, and the
    verbatim parser would `json.loads` that object, swallow the `TypeError` and
    silently report empty arguments. So does anything that is not a JSON object
    at all -- `"[]"`, `"null"`, a number, a truncated string -- because
    `ToolCall.arguments` is typed `dict` and a list only fails later, in a tool
    executor that cannot say where it came from.
    """
    if isinstance(value, dict):
        return json.dumps(value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except ValueError:
            return "{}"
        return value if isinstance(decoded, dict) else "{}"
    return "{}"


def _prepared_call(call: object, backend: str) -> dict[str, object]:
    if not isinstance(call, dict):
        raise BackendProtocolError(
            f"backend {backend!r}: a 'tool_calls' entry must be a JSON object, got {type(call).__name__}"
        )
    function = call.get("function")
    if not isinstance(function, dict):
        return call
    return {**call, "function": {**function, "arguments": _arguments(function.get("arguments"))}}


def _prepared_message(message: Mapping[str, object], backend: str) -> Mapping[str, object]:
    calls = message.get("tool_calls")
    if calls is None:
        return message
    if not isinstance(calls, list):
        raise BackendProtocolError(
            f"backend {backend!r}: 'tool_calls' must be a list, got {type(calls).__name__}"
        )
    return {**message, "tool_calls": [_prepared_call(call, backend) for call in calls]}


class OpenAICompatibleBackend:
    """Chat completions against any OpenAI-shaped `/chat/completions` endpoint.

    Satisfies `core.backends.base.Backend` structurally; `name` is the spec name,
    which is also what lands in `Choice.provider`.
    """

    def __init__(self, spec: BackendSpec, *, client: httpx.AsyncClient | None = None) -> None:
        self._spec = spec
        self.name: str = spec.name
        self._client = client
        self._owns_client = client is None

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSchema] | None = None,
        *,
        max_tokens: int = 400,
        temperature: float = 0.7,
        timeout: float = 3.0,
        model: str | None = None,
    ) -> Choice:
        """One chat completion, or `BackendError` / `httpx.TimeoutException`."""
        target = model or self._spec.model
        if not target:
            raise BackendError(f"backend {self.name!r}: no model configured and no model argument given")
        headers = {
            "Authorization": _authorization(self._spec),
            "Content-Type": "application/json",
        }
        payload: dict[str, object] = {
            "model": target,
            "messages": [dict(message) for message in messages],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = [dict(tool) for tool in tools]
        url = f"{self._spec.base_url.rstrip('/')}/chat/completions"
        client = self._http()
        response = await client.post(url, headers=headers, json=payload, timeout=timeout)
        if response.status_code in RETRY_STATUSES:
            log.warning(
                "backend %s: HTTP %d, retrying once (attempt 2 of %d) after %.1fs",
                self.name, response.status_code, MAX_ATTEMPTS, RETRY_BACKOFF_S,
            )
            await asyncio.sleep(RETRY_BACKOFF_S)
            response = await client.post(url, headers=headers, json=payload, timeout=timeout)
        return self._choice(response, target)

    async def aclose(self) -> None:
        """Close the client only if this backend created it; an injected one stays
        open *and attached* -- dropping the reference would make the next
        `complete()` build a real client and hit the network.
        """
        client = self._client
        if client is not None and self._owns_client:
            self._client = None
            await client.aclose()

    def _http(self) -> httpx.AsyncClient:
        """The shared client, built on first use so construction opens no socket."""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._spec.timeout)
        return self._client

    def _choice(self, response: httpx.Response, model: str) -> Choice:
        """The parsed choice, or the typed failure this response represents."""
        if not response.is_success:
            raise BackendStatusError(self.name, response.status_code, _excerpt(response, self._spec.api_key))
        document = self._document(response)
        error = document.get("error")
        if error is not None:
            raise BackendErrorEnvelope(
                f"backend {self.name!r}: HTTP {response.status_code} carried an error envelope: "
                f"{json.dumps(error, ensure_ascii=False)[:BODY_EXCERPT_CHARS]}"
            )
        return self.parse_choice(self._prepared(document), self.name, model)

    def _document(self, response: httpx.Response) -> dict[str, object]:
        """The 2xx body as a JSON object, credential already scrubbed."""
        try:
            decoded = response.json()
        except ValueError as exc:
            raise BackendProtocolError(
                f"backend {self.name!r}: HTTP {response.status_code} body is not JSON: {exc}"
            ) from exc
        if not isinstance(decoded, dict):
            raise BackendProtocolError(
                f"backend {self.name!r}: HTTP {response.status_code} body is a "
                f"{type(decoded).__name__}, expected a JSON object"
            )
        return {key: _scrub_value(item, self._spec.api_key) for key, item in decoded.items()}

    def _prepared(self, document: dict[str, object]) -> dict[str, object]:
        """`document` shaped the way the verbatim parser needs it.

        The parser reads `choices[0].message` and `json.loads` every
        `function.arguments`, so both are normalised first: no malformed body can
        reach it and blow up as a bare `KeyError` three frames from the cause.
        """
        choices = document.get("choices")
        if not isinstance(choices, list) or not choices:
            raise BackendProtocolError(
                f"backend {self.name!r}: 'choices' must be a non-empty list, got {type(choices).__name__}"
            )
        choice = choices[0]
        if not isinstance(choice, dict):
            raise BackendProtocolError(
                f"backend {self.name!r}: 'choices[0]' must be a JSON object, got {type(choice).__name__}"
            )
        message = choice.get("message")
        if not isinstance(message, dict):
            raise BackendProtocolError(
                f"backend {self.name!r}: 'choices[0].message' must be a JSON object, "
                f"got {type(message).__name__}"
            )
        return {**document, "choices": [{**choice, "message": _prepared_message(message, self.name)}, *choices[1:]]}

    # VERBATIM from the deleted `core/providers/base.py:parse_choice`, as the plan
    # requires, bare `dict` annotation included. It can no longer raise: everything
    # it would have had to check for itself is now checked in `_prepared`/`_arguments`.
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
