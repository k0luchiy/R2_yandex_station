"""The opencode event stream reader: one connection per session, kept open.

`GET /event` is a global SSE stream, and what reaches a handler is decided here and
nowhere else. Three things in this module are decisions, not plumbing:

* **The stream has its own read bound, and it is not the voice deadline.** This was
  measured, not guessed: opencode 1.18.32 heartbeats every **10.0 s**
  (`docs/11-opencode-contract.md` U4, gaps `[10.0, 10.0]`), while the opencode
  backend's `timeout` is **3.2 s** -- `R2D2_FAST_DEADLINE`, correct for
  `POST /session/:id/message` and wrong for a socket meant to stay open for the
  life of the process. A reader whose read timeout is the request deadline can
  never survive to the next heartbeat: it walks a `3.2 s connect / 5 s sleep`
  ladder, is blind for most of every window, and misses `permission.asked` --
  which is the one event that must not be missed. So the client this module builds
  bounds CONNECTING, WRITING and POOLING by `spec.timeout` and READING by
  `spec.event_read_timeout` (30 s: three heartbeats of slack, and still far below
  the 300 s the broker waits before rejecting an unanswered ask). `tests/
  test_sse_stream_timeout.py` proves the behaviour over a real socket, because a
  `MockTransport` has no timeouts and could never have caught this.
* **Nothing reaches a handler without a `properties.sessionID` match.** A
  `permission.asked` for somebody else's session arrives on the same socket, and
  answering it is an irreversible act performed on a stranger's turn. The filter
  lives inside `events()` rather than in `run()` because an unfiltered iterator
  handed to a caller is one unguarded `await handler(...)` away from doing exactly
  that. `CONNECTED` is the documented exception: it has no `sessionID` because it
  describes the connection, not a session, and it is the only proof that a
  reconnect succeeded.
* **A failure inside `handler` is the caller's, not a dead stream.** Todos 9 and 14
  answer permissions from inside the handler and `respond_permission` raises
  `OpencodeError` on a real 404, which says nothing about the connection; mistaking
  it for one would drop the connection and hide the caller's own error.

The event vocabulary and the frame decoder are not here: they are in
`core/opencode.sse_frames`, which knows nothing about sockets, and every name it
defines is re-exported below so `EventSource` stays the single import the rest of
R2D2 uses.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Final

import httpx

from core.backends.config_loader import BackendSpec
from core.opencode.sse_frames import (
    CONNECTED,
    EVENT_MODE,
    PERMISSION_ASKED,
    PERMISSION_REPLIED,
    QUESTION_ASKED,
    SSE_DEFAULT_EVENT,
    TEXT_DELTA,
    TITLE_CHARS,
    TURN_COMPLETE,
    EventMode,
    OpencodeEvent,
    frames,
    turn_is_complete,
)
from core.opencode.wire import OpencodeError, OpencodeStatusError

__all__ = [
    "CONNECTED", "EVENT_MODE", "EventMode", "EventSource", "OpencodeEvent",
    "PERMISSION_ASKED", "PERMISSION_REPLIED", "QUESTION_ASKED", "RECONNECT_MAX_S",
    "RECONNECT_MIN_S", "SSE_DEFAULT_EVENT", "TEXT_DELTA", "TITLE_CHARS", "TURN_COMPLETE",
    "turn_is_complete",
]

log = logging.getLogger(__name__)

#: The reconnect ladder: 1s, 2s, 4s, 5s, 5s... It grows over CONSECUTIVE
#: connections that delivered nothing, and a connection that delivered a frame
#: starts it over, so a long-lived reader cannot inherit a delay from a blip it
#: already recovered from -- and a server that is down, which never delivers a
#: frame, still cannot be hot-looped.
RECONNECT_MIN_S: Final = 1.0
RECONNECT_MAX_S: Final = 5.0


class EventSource:
    """One session's view of `GET /event`, with reconnects.

    Five constructor arguments, and only three are this object's business: the spec
    (server, credentials, and the two timeout bounds), the workspace directory (C6)
    and the session this reader may see (C5). The other two -- an injected
    `httpx.AsyncClient` and an injected `sleep` -- exist so the suite runs offline
    and in milliseconds, because a reconnect-ladder test would otherwise spend 17
    seconds asleep. Nothing is opened at construction; `aclose()` closes exactly what
    this object built.
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
        self._deadline_s = spec.timeout
        self._read_timeout_s = spec.event_read_timeout
        self._auth: httpx.Auth | None = (
            httpx.BasicAuth(spec.username, spec.password)
            if spec.username and spec.password
            else None
        )
        self._client = client
        self._owns_client = client is None
        self._sleep = sleep
        log.info(
            "opencode sse: base_url=%s workspace=%s session=%s auth=%s deadline=%ss "
            "event_read_timeout=%ss",
            self._base_url, directory, session_id, "basic" if self._auth is not None else "none",
            self._deadline_s, self._read_timeout_s,
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
        would make the next call build a real one and hit the network. This is also
        the only close path a shutdown needs besides cancelling the task: a reader
        blocked in a silent read is released by the cancellation, which closes the
        response, and the long read bound does not delay either step.
        """
        client = self._client
        if client is not None and self._owns_client:
            self._client = None
            await client.aclose()

    async def events(self) -> AsyncIterator[OpencodeEvent]:
        """This session's events from ONE connection, until that stream ends.

        `?directory=` is passed through (C6); the client is where that path is
        validated, and what reaches a handler here is filtered by `sessionID`
        whatever the server scoped the stream to.
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
            async for event in frames(response):
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

        `stop` is honoured before every dispatch and every reconnect, so a shutdown
        never waits out the ladder; a `run()` idle inside a silent stream is
        released by cancelling its task, which closes the response.
        """
        delay = RECONNECT_MIN_S
        while not stop.is_set():
            served = False
            handler_failed = False
            try:
                async for event in self.events():
                    if stop.is_set():
                        return
                    served = True
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
            delay = RECONNECT_MIN_S if served else min(delay * 2, RECONNECT_MAX_S)

    # -- internals ---------------------------------------------------------

    def _mine(self, event: OpencodeEvent) -> bool:
        """Whether `event` belongs to this reader's session (C5); `CONNECTED` excepted."""
        return event.type == CONNECTED or event.session_id == self._session_id

    def _timeouts(self) -> httpx.Timeout:
        """The request deadline for everything that is a request, and the stream's own
        bound for the one read that is supposed to wait.

        Sharing a single number here is what made the reader blind: the read bound
        has to outlast a 10.0 s heartbeat, and the request deadline must not, because
        a caller waiting on a turn has 3.2 s of Alice budget. httpx applies the read
        timeout per socket read, so one long bound is exactly the patience an idle
        stream needs and never a ceiling on the whole connection.
        """
        return httpx.Timeout(
            connect=self._deadline_s,
            read=self._read_timeout_s,
            write=self._deadline_s,
            pool=self._deadline_s,
        )

    def _http(self) -> httpx.AsyncClient:
        """The shared client, built on first use so construction opens no socket."""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeouts())
        return self._client
