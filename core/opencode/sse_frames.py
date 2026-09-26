"""The opencode event vocabulary, and the SSE frames it is decoded from.

`GET /event` is a global SSE stream, and everything hard about it was measured
rather than guessed (`docs/11-opencode-contract.md` U4, spike correction C5). What
arrives on the wire and what a handler is allowed to see are two separable
concerns, and this module is the first of them:

* **The event name is in the BODY, not in an `event:` line.** Measured, not inferred:
  a tap of `GET /event` on 1.18.32 recorded **44 `data:` lines and 0 `event:`
  lines** (`qa/d11-wire-tap.out`), and every name U4 lists -- including
  `permission.asked` -- arrives as a top-level `"type"` key of the JSON document.
  Reading only the SSE `event:` field types all 44 of them `"message"`, which is
  how the whole permission feature stayed dead behind a permanently healthy
  connection. `decode_frame` therefore reads the body's `type`, with the `event:`
  field taking precedence when a spec-compliant server does send one.
* **It carries EVERY session.** A `permission.asked` for somebody else's session
  arrives on the same socket, so nothing reaches a handler without a
  `properties.sessionID` match -- the one defect here that could do real damage,
  since answering a stranger's permission is an irreversible act on a stranger's
  turn. That filter belongs to the reader, `EventSource.events()` in
  `core/opencode/sse.py`, and it sits above this module: the vocabulary has no way
  to route anything anywhere.
* **There is no "turn completed" event.** Not `turn.completed`, not
  `message.completed`: `message.updated` fires ~10x per turn while the answer is
  still assembling, and only `session.idle` closes it -- and only once every
  `permission.asked` has a matching `permission.replied`. That conjunction is
  `turn_is_complete`, a pure function, so the collector and the broker hand it
  whatever their per-turn consumer collected.
* **`properties.always` is a trap, not a feature.** The server offers command masks
  there (`["echo *"]`) and one grant is permanent, so this module exposes the list
  for logging and holds no code path that could answer with it; answering lives in
  `core.opencode.client`, whose `respond_permission` does not accept `"always"` in
  its type at all.
* **`EVENT_MODE` is `"sse"`** because U4 resolved cleanly to a live stream, and it
  keeps the literal type of the three modes so the polled and deny-only
  degradations stay reachable instead of becoming folklore.

No third-party SSE client: the stdlib has no parser and the project adds no
dependency, so `frames` is the WHATWG dispatch algorithm over httpx's
`aiter_lines()` -- accumulate to a blank line, ignore `:` comments, ignore fields
this reader does not use, join multi-line `data:` with newlines, drop a frame the
stream cut in half. httpx's own line decoder already handles CRLF and a frame split
across TCP chunks. A frame this module cannot read is dropped with a WARNING,
never raised: a server mid-deploy must not take down the listener that answers
permission requests.

The split is by dependency, not by convenience: this half knows nothing about a
socket, a timeout or a reconnect, so it is testable without one -- which is what
`tests/test_sse.py` does -- while `core/opencode/sse.py` keeps the connection and
re-exports every name below, so `EventSource` stays the single import the rest of
R2D2 uses.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal, TypeAlias

import httpx

__all__ = [
    "CONNECTED", "EVENT_MODE", "PERMISSION_ASKED", "PERMISSION_REPLIED",
    "SSE_DEFAULT_EVENT", "TEXT_DELTA", "TITLE_CHARS", "TURN_COMPLETE", "EventMode",
    "OpencodeEvent", "decode_frame", "frames", "turn_is_complete",
]

log = logging.getLogger(__name__)

#: The event names U4 recorded, verbatim (C5). `GET /doc` also advertises V2
#: spellings (`permission.v2.asked`, `session.next.*`) which this build was never
#: seen sending, so they are not defined here and must not be used.
CONNECTED: Final = "server.connected"
PERMISSION_ASKED: Final = "permission.asked"
PERMISSION_REPLIED: Final = "permission.replied"
TURN_COMPLETE: Final = "session.idle"
TEXT_DELTA: Final = "message.part.delta"

#: The three ways R2D2 can learn what a turn did, per U4's ADOPTED line: only "sse"
#: is live, and "poll" (todo 9's collector) and "deny" (refuse every mutation) are
#: the pre-agreed degradations, kept typed so they stay reachable.
EventMode: TypeAlias = Literal["sse", "poll", "deny"]
EVENT_MODE: Final[EventMode] = "sse"
#: SSE's own type for a frame that named itself in neither place. opencode names
#: every frame in its body and never in the `event:` field (U4, re-measured), so
#: this is the shape of a frame from a third server, not of opencode's -- decoded
#: rather than discarded, because an unnamed frame is still a frame.
SSE_DEFAULT_EVENT: Final = "message"
#: A logged title is a preview, never a command to run; whoever acts reads
#: `properties`, and a payload can be kilobytes long.
TITLE_CHARS: Final = 120


@dataclass(frozen=True, slots=True)
class OpencodeEvent:
    """One decoded frame: its `type` and its read-only `properties` object.

    Frozen because an event is a report about the past -- a handler that could
    rewrite the session id it was dispatched for would defeat the filter in
    `EventSource`. Every accessor is TOTAL: opencode's own `server.connected` frame
    carries no `sessionID`, so a missing key answers `None` instead of raising in
    the middle of a live stream.
    """

    type: str
    properties: Mapping[str, object]

    @property
    def session_id(self) -> str | None:
        """`properties.sessionID` -- the key the global stream is filtered by."""
        return _text(self.properties.get("sessionID"))

    @property
    def permission_id(self) -> str | None:
        """`properties.id` on a `permission.asked`; what the broker answers with."""
        return _text(self.properties.get("id"))

    @property
    def request_id(self) -> str | None:
        """`properties.requestID` on a `permission.replied`; the ask it answers."""
        return _text(self.properties.get("requestID"))

    @property
    def always(self) -> tuple[str, ...]:
        """The command masks the server offers in `properties.always`, e.g. `("echo *",)`.

        Exposed to be LOGGED and nothing else -- one `always` grants that mask for good.
        """
        masks = self.properties.get("always")
        if not isinstance(masks, list):
            return ()
        return tuple(mask for mask in masks if isinstance(mask, str))

    @property
    def title(self) -> str:
        """A short human label for a log line or a spoken prompt; `""` if there is none.

        opencode puts no `title` on a permission event: the thing a user has to
        confirm is `metadata.command` (bash) or the first `patterns` entry (edit).
        """
        metadata = self.properties.get("metadata")
        patterns = self.properties.get("patterns")
        for candidate in (
            self.properties.get("title"),
            metadata.get("command") if isinstance(metadata, Mapping) else None,
            patterns[0] if isinstance(patterns, list) and patterns else None,
        ):
            text = _text(candidate)
            if text is not None:
                return text[:TITLE_CHARS]
        return ""


def turn_is_complete(events: Iterable[OpencodeEvent]) -> bool:
    """Whether `events` end a finished turn -- the C5 rule 2 conjunction.

    `session.idle` alone is not enough: the server sends it even while a tool is
    blocked on a permission answer, so idle alone would declare the turn over before
    it is and the reply would never be collected. A reply matches one ask, by
    `requestID` against that ask's `id`. Single-pass over any iterable, because
    todos 9 and 14 hand it whatever their consumer collected.
    """
    idle = False
    waiting: set[str] = set()
    for event in events:
        if event.type == PERMISSION_ASKED:
            asked = event.permission_id
            if asked is not None:
                waiting.add(asked)
        elif event.type == PERMISSION_REPLIED:
            answered = event.request_id
            if answered is not None:
                waiting.discard(answered)
        elif event.type == TURN_COMPLETE:
            idle = True
    return idle and not waiting


async def frames(response: httpx.Response) -> AsyncIterator[OpencodeEvent]:
    """The readable frames of ONE response, in arrival order.

    `aiter_lines()` is httpx's line decoder, so CRLF and a frame split across TCP
    chunks are already handled; what is left is the SSE dispatch algorithm itself.
    """
    event_type = ""
    data: list[str] = []
    async for line in response.aiter_lines():
        if line == "":
            decoded = decode_frame(event_type, data)
            if decoded is not None:
                yield decoded
            event_type, data = "", []
        elif line.startswith(":"):
            continue
        else:
            field, _, value = line.partition(":")
            if value.startswith(" "):
                value = value[1:]
            if field == "event":
                event_type = value
            elif field == "data":
                data.append(value)


def decode_frame(event_type: str, data: Sequence[str]) -> OpencodeEvent | None:
    """One accumulated frame as an event, or `None` when it carries nothing to decode.

    **Where the name comes from, and in what order.** The SSE `event:` field wins
    whenever it is present, because that field is the only place the SSE
    specification allows a server to state the name, and a server that states it
    should not be second-guessed by a reader. opencode 1.18.32 sends none -- 44
    `data:` lines against 0 `event:` lines in `qa/d11-wire-tap.out`, and 1090 against 0
    in a 30-minute tap -- and puts the name in the body's own top-level `"type"`, so
    that is the fallback and the only path a real frame can take on this build. A
    frame that names itself in neither place decodes as `SSE_DEFAULT_EVENT` rather
    than being dropped, because an unnamed frame is still a frame. A frame that
    names itself in BOTH places and disagrees is a WARNING and takes the `event:`
    field: nothing in this protocol expects a mismatch, and silently preferring one
    of two names is how a stream that looks perfectly alive stops dispatching
    anything at all.

    Three ways a frame can be unreadable, all of which must leave the stream alive:
    an empty `data` buffer (SSE says dispatch nothing), a body that is not JSON, and
    a body whose `properties` is not an object. The last is the misleading success --
    `{"properties": [1, 2]}` is valid JSON and would hand a caller a mapping that
    reports every field as absent.
    """
    body = "\n".join(data)
    if not body:
        return None
    try:
        payload = json.loads(body)
    except ValueError as exc:
        # `str(exc)` is a position ("... line 1 column 3"), never the body itself.
        log.warning("opencode sse: dropping a %s frame: unreadable JSON (%s)", event_type, exc)
        return None
    mapped: Mapping[str, object] = payload if isinstance(payload, Mapping) else {}
    properties = mapped.get("properties")
    if not isinstance(properties, Mapping):
        log.warning("opencode sse: dropping a %s frame: 'properties' is not an object", event_type)
        return None
    return OpencodeEvent(
        type=_name(event_type, mapped.get("type")),
        properties=MappingProxyType(dict(properties)),
    )


def _name(event_type: str, named: object) -> str:
    """The frame's event name; never raises and never returns an empty string.

    `named` is the body's `"type"` of whatever type JSON handed back, so a non-string
    there reads as "this frame did not name itself" rather than as a name -- and a
    name that is only whitespace is the same thing, because no constant and no
    handler can ever match it.
    """
    body_type = _text(named)
    if body_type is not None and not body_type.strip():
        body_type = None
    if not event_type:
        return body_type or SSE_DEFAULT_EVENT
    if body_type is not None and body_type != event_type:
        log.warning(
            "opencode sse: the event: field says %r and the body says %r; the event: field wins",
            event_type, body_type,
        )
    return event_type


def _text(value: object) -> str | None:
    """A `str`, or nothing: a wrong-typed value in `properties` must read as absent
    rather than reach a caller as truthy."""
    return value if isinstance(value, str) else None
