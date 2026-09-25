"""opencode's wire vocabulary: the value types, the failure types, and the one
place its JSON is read into them.

Nothing here performs I/O. `core.opencode.client` owns the conversation with
an `opencode serve` process -- which route, with which body, under which
timeout -- and this module owns what the bytes that come back *mean*, so a
second client (the SSE reader of plan todo 7) can share the same reading of the
same documents instead of inventing its own.

Three things are load-bearing and were all measured in todo 1
(`docs/11-opencode-contract.md`):

* **The failure hierarchy hangs off `BackendError`** (moved to
  `core/backends/base.py` in this todo). The brain branches on that ONE root
  for every backend, and `core/backcode` todo 9's adapter must not have to
  import from `openai_compatible` to find an error type.
* **A 200 is not a reply (spike correction C1).** `info.error` carries the real
  failure -- an `APIError` with `statusCode` 403 for every free Zen model except
  `space-bunny-free`, and 402 for every paid one -- inside an HTTP 200. That is
  `OpencodeErrorEnvelope`, a subclass of the protocol error, and it is the
  reason `envelope_detail` exists.
* **`{info, parts}` is the message shape (C7).** `role`, `model`, `agent` and
  `error` live inside `info`; reading them at the top level silently yields
  `None`, which is exactly how the first version of the spike probe broke.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, TypeVar

import httpx

from core.backends.base import BackendError

__all__ = [
    "BODY_EXCERPT_CHARS",
    "DEFAULT_PROVIDER_ID",
    "MessageRecord",
    "OpencodeDeadlineExceeded",
    "OpencodeError",
    "OpencodeErrorEnvelope",
    "OpencodeHealth",
    "OpencodeProtocolError",
    "OpencodeReply",
    "OpencodeStatusError",
    "SessionInfo",
    "decode",
    "envelope_detail",
    "excerpt",
    "records",
    "reply_of",
    "required_text",
    "split_model",
    "text_of_parts",
]

#: opencode's own provider id, used when a configured model string carries none.
DEFAULT_PROVIDER_ID: Final = "opencode"
BODY_EXCERPT_CHARS: Final = 240
_T = TypeVar("_T")


# ---------------------------------------------------------------------------
# Failures
# ---------------------------------------------------------------------------


class OpencodeError(BackendError):
    """opencode could not produce a reply, so the caller should fall back.

    A subclass of `core.backends.base.BackendError`, so the brain branches on
    ONE error root for every backend and never has to reach into a sibling
    backend's module to find it.
    """


class OpencodeStatusError(OpencodeError):
    """The server answered with a non-2xx status."""

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class OpencodeProtocolError(OpencodeError):
    """A 2xx body that is not a usable opencode response."""


class OpencodeErrorEnvelope(OpencodeProtocolError):
    """A 200 whose `info.error` carries the real failure (spike correction C1).

    `status_code` is the INNER code from the envelope (403, 402), not the 200 the
    server answered with: the whole point of this class is that the transport
    said everything was fine.
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class OpencodeDeadlineExceeded(OpencodeError):
    """The caller's deadline elapsed; the turn keeps running server-side."""


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OpencodeHealth:
    reachable: bool
    version: str | None = None


@dataclass(frozen=True, slots=True)
class SessionInfo:
    id: str
    title: str
    directory: str = ""


@dataclass(frozen=True, slots=True)
class MessageRecord:
    id: str
    role: str
    text: str


@dataclass(frozen=True, slots=True)
class OpencodeReply:
    text: str
    message_id: str = ""


# ---------------------------------------------------------------------------
# Reading the documents
# ---------------------------------------------------------------------------


def split_model(model: str) -> tuple[str, str]:
    """`(providerID, modelID)` for a configured model string, split on the FIRST `/`.

    `opencode/space-bunny-free` -> `("opencode", "space-bunny-free")` and
    `a/b/c` -> `("a", "b/c")`. A bare id defaults to opencode's own provider,
    because every Zen model in `config/backends.json` is named `<provider>/<id>`.

    A *missing* provider defaults the same way, and that includes the leading
    `/` of a `"/x"` typo: an empty `providerID` in the turn body is a request the
    server rejects, and this is the only provider the deployment has, so
    inferring it is strictly better than shipping `("", "x")` on the wire. The
    function is total -- no input yields an empty provider.
    """
    provider, separator, model_id = model.partition("/")
    if not separator:
        return DEFAULT_PROVIDER_ID, provider
    return provider or DEFAULT_PROVIDER_ID, model_id


def required_text(entry: object, key: str, context: str) -> str:
    """`entry[key]` as a string, or a typed failure naming what arrived instead.

    `entry` is `object`, not `Mapping`, because `GET /session/status` maps a
    session id to a status this client has not seen the shape of yet: a bare
    string there is exactly the kind of value a reaper must not misread.
    """
    value = entry.get(key) if isinstance(entry, Mapping) else entry
    if not isinstance(value, str):
        raise OpencodeProtocolError(
            f"opencode: {context} must carry a string {key!r}, got {type(value).__name__}"
        )
    return value


def text_of_parts(parts: list[object]) -> str:
    """The `text` parts concatenated in order; `reasoning` and `tool` are not answers."""
    chunks: list[str] = []
    for part in parts:
        if not isinstance(part, Mapping):
            raise OpencodeProtocolError(
                f"opencode: a message part must be a JSON object, got {type(part).__name__}"
            )
        if part.get("type") != "text":
            continue
        text = part.get("text")
        if not isinstance(text, str):
            raise OpencodeProtocolError(
                f"opencode: a text part must carry a string 'text', got {type(text).__name__}"
            )
        chunks.append(text)
    return "".join(chunks)


def envelope_detail(error: object) -> tuple[str, int | None]:
    """`info.error` as (readable detail, inner status code).

    The documented shape is `{name, data: {message, statusCode}}`, but the union
    also admits error types this client has never seen, so an unreadable shape
    degrades to a bounded JSON dump rather than to silence.
    """
    data = error.get("data") if isinstance(error, Mapping) else None
    name = error.get("name") if isinstance(error, Mapping) else None
    message = data.get("message") if isinstance(data, Mapping) else None
    status = data.get("statusCode") if isinstance(data, Mapping) else None
    detail = ": ".join(str(part) for part in (name, message) if isinstance(part, str))
    if not detail:
        detail = json.dumps(error, ensure_ascii=False)[:BODY_EXCERPT_CHARS]
    return detail, status if isinstance(status, int) else None


def decode(response: httpx.Response, shape: type[_T]) -> _T:
    """A 2xx body parsed and checked against `shape` (`dict`, `list` or `bool`)."""
    try:
        document = response.json()
    except ValueError as exc:
        raise OpencodeProtocolError(
            f"opencode: HTTP {response.status_code} body is not JSON: {exc}"
        ) from exc
    if not isinstance(document, shape):
        raise OpencodeProtocolError(
            f"opencode: HTTP {response.status_code} body is a {type(document).__name__}, "
            f"expected a JSON {shape.__name__}"
        )
    return document


def records(response: httpx.Response, context: str) -> list[Mapping[str, object]]:
    """A 2xx body as a list of JSON objects -- the shape every list route uses."""
    entries = decode(response, list)
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise OpencodeProtocolError(
                f"opencode: {context} must be a list of JSON objects, "
                f"got a {type(entry).__name__} entry"
            )
    return entries


def excerpt(response: httpx.Response, secret: str) -> str:
    """A short, credential-free slice of a failing body, for an error message."""
    text = response.text.replace("\n", " ")[:BODY_EXCERPT_CHARS]
    return text.replace(secret, "[redacted]") if secret else text


def reply_of(document: Mapping[str, object]) -> OpencodeReply:
    """`{info, parts}` as a reply -- after the C1 check, which comes first.

    A refusal and an empty turn are both "no text here", so `info.error` is read
    BEFORE the parts: otherwise the most important failure in the whole client
    arrives as a silent empty answer.
    """
    info = document.get("info")
    if not isinstance(info, Mapping):
        raise OpencodeProtocolError(
            f"opencode: the reply has no 'info' object, got {type(info).__name__}"
        )
    failure = info.get("error")
    if failure is not None:
        detail, inner_status = envelope_detail(failure)
        status = f"HTTP {inner_status}" if inner_status is not None else "an unstated status"
        raise OpencodeErrorEnvelope(
            f"opencode: HTTP 200 with info.error ({status}): {detail}",
            status_code=inner_status,
        )
    parts = document.get("parts")
    if not isinstance(parts, list):
        raise OpencodeProtocolError(
            f"opencode: the reply has no 'parts' list, got {type(parts).__name__}"
        )
    return OpencodeReply(text=text_of_parts(parts), message_id=required_text(info, "id", "an assistant message"))
