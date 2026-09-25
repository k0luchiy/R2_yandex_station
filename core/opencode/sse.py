"""The opencode event stream, read by hand (plan todo 7).

`GET /event` is a global SSE stream, and everything hard about it was measured in
todo 1 rather than guessed (`docs/11-opencode-contract.md` U4, spike correction C5):

* **It carries EVERY session.** A `permission.asked` for somebody else's session
  arrives on the same socket, so nothing reaches a handler without a
  `properties.sessionID` match -- the one defect here that could do real damage,
  since answering a stranger's permission is an irreversible act on a stranger's
  turn. The filter sits in `events`, below the parse and above every possible
  dispatch, where no consumer can route around it.
* **There is no "turn completed" event.** Not `turn.completed`, not
  `message.completed`: `message.updated` fires ~10x per turn while the answer is
  still assembling, and only `session.idle` closes it -- and only once every
  `permission.asked` has a matching `permission.replied`. That conjunction is
  `turn_is_complete`, a pure function, so todos 9 and 14 hand it whatever their
  per-turn consumer collected.
* **`properties.always` is a trap, not a feature.** The server offers command masks
  there (`["echo *"]`) and one grant is permanent, so this module exposes the list
  for logging and holds no code path that could answer with it; answering lives in
  `core.opencode.client`, whose `respond_permission` does not accept `"always"` in
  its type at all.
* **`EVENT_MODE` is `"sse"`** because U4 resolved cleanly to a live stream, and it
  keeps the literal type of the three modes so the polled and deny-only
  degradations stay reachable instead of becoming folklore.

No third-party SSE client: the stdlib has no parser and the plan adds no dependency,
so `_frames` is the WHATWG dispatch algorithm over httpx's `aiter_lines()` --
accumulate to a blank line, ignore `:` comments, ignore fields this reader does not
use, join multi-line `data:` with newlines, drop a frame the stream cut in half.
httpx's own line decoder already handles CRLF and a frame split across TCP chunks.
A frame the reader cannot read is dropped with a WARNING, never raised: a server
mid-deploy must not take down the listener that answers permission requests.

allow: SIZE_OK -- 315 pure LOC, 118 of them docstrings carrying the measured C5
contract that todos 9 and 14 cite. The event vocabulary (the five names, the frozen
`OpencodeEvent`, the pure `turn_is_complete`) is a separable concern from the
transport that reads frames off a socket, and `core/opencode/wire.py` shows where
this project puts that half. It stays here because plan todo 7 pins this file as the
reader's single import for todos 9 and 14 -- the trade `client.py` records for the
15-route wire contract -- and a split would give those todos an import whose only
caller is this one.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal, TypeAlias

import httpx

from core.backends.config_loader import BackendSpec
from core.opencode.wire import OpencodeError, OpencodeStatusError

__all__ = [
    "CONNECTED", "EVENT_MODE", "RECONNECT_MAX_S", "RECONNECT_MIN_S", "EventMode",
    "EventSource", "OpencodeEvent", "PERMISSION_ASKED", "PERMISSION_REPLIED",
    "TEXT_DELTA", "TURN_COMPLETE", "turn_is_complete",
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
#: SSE's own type for a frame that carried no `event:` line; opencode always sends
#: one, so an unnamed frame is decoded rather than discarded.
SSE_DEFAULT_EVENT: Final = "message"
#: The reconnect ladder: 1s, 2s, 4s, 5s, 5s... It does not reset while one `run()`
#: call lives, so a long-lived supervisor cannot hot-loop a server that is down.
RECONNECT_MIN_S: Final = 1.0
RECONNECT_MAX_S: Final = 5.0
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
        """`properties.id` on a `permission.asked`; what todo 14 answers with."""
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


class EventSource:
    """One session's view of `GET /event`, with reconnects.

    Five constructor arguments, and only three are this object's business: the spec
    (server and credentials), the workspace directory (C6) and the session this
    reader may see (C5). The other two -- an injected `httpx.AsyncClient` and an
    injected `sleep` -- exist so the suite runs offline and in milliseconds, because
    a reconnect-ladder test would otherwise spend 17 seconds asleep. Nothing is
    opened at construction; `aclose()` closes exactly what this object built.
    """

    def __init__(
        self,
        spec: BackendSpec,
        directory: str,
        *,
        session_id: str,
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._name = spec.name
        self._base_url = spec.base_url.rstrip("/")
        self._directory = directory
        self._session_id = session_id
        self._password = spec.password
        self._timeout = spec.timeout
        self._auth: httpx.Auth | None = (
            httpx.BasicAuth(spec.username, spec.password)
            if spec.username and spec.password
            else None
        )
        self._client = client
        self._owns_client = client is None
        self._sleep = sleep
        log.info(
            "opencode sse: base_url=%s workspace=%s session=%s auth=%s", self._base_url,
            directory, session_id, "basic" if self._auth is not None else "none",
        )

    def __repr__(self) -> str:
        return f"EventSource(base_url={self._base_url!r}, session_id={self._session_id!r})"

    async def __aenter__(self) -> EventSource:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the client only if this object created it.

        An injected client stays open AND stays attached: dropping the reference
        would make the next call build a real one and hit the network.
        """
        client = self._client
        if client is not None and self._owns_client:
            self._client = None
            await client.aclose()

    async def events(self) -> AsyncIterator[OpencodeEvent]:
        """This session's events from ONE connection, until that stream ends.

        The filter lives here rather than in `run()` because an unfiltered iterator
        handed to a caller is one unguarded `await handler(...)` away from answering
        somebody else's permission. `CONNECTED` is the documented exception: it has
        no `sessionID` because it describes the connection, not a session, and it is
        the only proof that a reconnect succeeded. `?directory=` is passed through
        (C6); the client is where that path is validated, and what reaches a handler
        here is filtered by `sessionID` whatever the server scoped the stream to.
        """
        async with self._http().stream(
            "GET",
            f"{self._base_url}/event",
            params={"directory": self._directory},
            auth=self._auth,
        ) as response:
            if response.status_code != 200:
                # The status alone is the diagnosis here (401 credentials, 404 route),
                # and reading an unbounded error body could hang the reader.
                raise OpencodeStatusError(
                    f"opencode {self._name}: GET /event -> HTTP {response.status_code}",
                    status_code=response.status_code,
                )
            async for event in self._frames(response):
                if not self._mine(event):
                    log.debug(
                        "opencode sse: dropped %s of session %r", event.type, event.session_id
                    )
                    continue
                yield event

    async def run(
        self, handler: Callable[[OpencodeEvent], Awaitable[None]], *, stop: asyncio.Event
    ) -> None:
        """Dispatch events to `handler` until `stop`, reconnecting on end or failure.

        A failure inside `handler` is the CALLER's and propagates out of `run()`: it
        must not be mistaken for a dead stream, because todos 9 and 14 answer
        permissions from inside the handler and `respond_permission` raises
        `OpencodeError` on a real 404, which says nothing about the connection.
        `stop` is honoured before every dispatch and every reconnect, so a shutdown
        never waits out the ladder; a `run()` idle inside a silent stream is
        released by cancelling its task, which closes the response.
        """
        delay = RECONNECT_MIN_S
        while not stop.is_set():
            handler_failed = False
            try:
                async for event in self.events():
                    if stop.is_set():
                        return
                    try:
                        if event.type == PERMISSION_ASKED:
                            # The one INFO: a permission is a user-visible moment and its
                            # command is what gets confirmed -- type and title only, never
                            # `properties`, which can echo anything the user typed.
                            log.info("opencode sse: %s %s", event.type, event.title)
                        else:
                            log.debug("opencode sse: %s", event.type)
                        await handler(event)
                    except BaseException:
                        handler_failed = True
                        raise
            except asyncio.CancelledError:
                raise
            except (httpx.HTTPError, OpencodeError) as exc:
                if handler_failed:
                    raise
                log.warning(
                    "opencode sse: stream for session %s failed (%s: %s), reconnecting in %ss",
                    self._session_id, type(exc).__name__, exc, delay,
                )
            else:
                log.info("opencode sse: stream ended, reconnecting in %ss", delay)
            if stop.is_set():
                return
            await self._sleep(delay)
            delay = min(delay * 2, RECONNECT_MAX_S)

    # -- internals ---------------------------------------------------------

    def _mine(self, event: OpencodeEvent) -> bool:
        """Whether `event` belongs to this reader's session (C5); `CONNECTED` excepted."""
        return event.type == CONNECTED or event.session_id == self._session_id

    async def _frames(self, response: httpx.Response) -> AsyncIterator[OpencodeEvent]:
        """The readable frames of ONE response, in arrival order."""
        event_type = ""
        data: list[str] = []
        async for line in response.aiter_lines():
            if line == "":
                decoded = _decode_frame(event_type, data)
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

    def _http(self) -> httpx.AsyncClient:
        """The shared client, built on first use so construction opens no socket."""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client


def _decode_frame(event_type: str, data: Sequence[str]) -> OpencodeEvent | None:
    """One accumulated frame as an event, or `None` when it carries nothing to decode.

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
    properties = payload.get("properties") if isinstance(payload, Mapping) else None
    if not isinstance(properties, Mapping):
        log.warning("opencode sse: dropping a %s frame: 'properties' is not an object", event_type)
        return None
    return OpencodeEvent(
        type=event_type or SSE_DEFAULT_EVENT, properties=MappingProxyType(dict(properties))
    )


def _text(value: object) -> str | None:
    """A `str`, or nothing: a wrong-typed value in `properties` must read as absent
    rather than reach a caller as truthy."""
    return value if isinstance(value, str) else None
