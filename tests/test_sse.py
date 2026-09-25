"""Behavioural tests for `core.opencode.sse` -- the SSE event reader (plan todo 7).

This is the only place R2D2 learns that opencode is waiting for a permission, so
these tests encode the two measured facts from `docs/11-opencode-contract.md`
(U4) and spike correction C5 that a naive `aiter_lines()` loop gets wrong:

* **`GET /event` is a GLOBAL stream.** One socket carries every session on the
  server, so a `permission.asked` belonging to somebody else's session arrives on
  the same connection. Every frame is filtered by `properties.sessionID` before it
  reaches a handler, or R2D2 answers a stranger's request -- that is
  `test_a_permission_ask_for_another_session_never_reaches_the_handler`, and the
  payloads are the verbatim ones U4 recorded.
* **There is no "turn completed" event.** Not `turn.completed`, not
  `message.completed` -- `message.updated` fires ~10x per turn while the answer is
  still assembling. A turn ends at `session.idle` AND only once no
  `permission.asked` is left unanswered. That conjunction is the exported
  `turn_is_complete`, which plan todos 9 and 14 import, so its semantics are
  pinned here instead of being left to whoever writes them.

`properties.always` is the reason a permission is the one event logged at INFO: the
server offers a list of command masks there (`["echo *"]`) and one grant is
permanent, so the reader surfaces the command and never acts on the list.

Everything goes through `httpx.MockTransport`; nothing here touches a network
(proven by re-running the suite under `-p no_net`).

Timing: two tests sleep for real, 0.05 s each -- one to let a cancel land
mid-stream, one to prove a hung transport never returns. Every other test that
could block carries an explicit `asyncio.wait_for`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from pathlib import Path
from typing import Final, get_args

import httpx
import pytest

from core.backends.base import BackendError
from core.backends.config_loader import BackendSpec
from core.opencode import sse
from core.opencode.sse import (
    CONNECTED,
    EVENT_MODE,
    PERMISSION_ASKED,
    PERMISSION_REPLIED,
    TEXT_DELTA,
    TURN_COMPLETE,
    EventSource,
    OpencodeEvent,
    turn_is_complete,
)
from core.opencode.wire import OpencodeError, OpencodeStatusError

# ---------------------------------------------------------------------------
# Doubles and fixtures
# ---------------------------------------------------------------------------

BASE_URL = "http://127.0.0.1:4599"
#: An existing absolute path. `?directory=` is not validated server-side, so the
#: reader checks it locally; the repo root always exists and needs no cleanup.
WORKSPACE: Final = str(Path(__file__).resolve().parents[1])
#: The session id U4 recorded inside its verbatim `permission.asked` payload.
SESSION_ID: Final = "ses_f266522f4ffee31XNquJ1D3qjC"
FOREIGN_SESSION_ID: Final = "ses_someone_else"
PERMISSION_ID: Final = "per_0d99aed41001iOUiwXDd6vx2NP"
#: The sentinel: a server-controlled value that must never reach a log record.
SENTINEL: Final = "R2D2SSE-SECRET-8f3a"
#: The credential sentinel, as in the todo 6 suite.
PASSWORD: Final = "R2D2_OC_PASSWORD_VALUE"
MODEL: Final = "opencode/space-bunny-free"

#: U4's `permission.asked`, verbatim.
ASKED_PROPERTIES: Final[dict[str, object]] = {
    "id": PERMISSION_ID,
    "sessionID": SESSION_ID,
    "permission": "bash",
    "patterns": ["echo R2D2SPIKERUN3K"],
    "metadata": {"command": "echo R2D2SPIKERUN3K"},
    "always": ["echo *"],
    "tool": {
        "messageID": "msg_0d99ae16a0015kUjEPJoOW15k8",
        "callID": "call_function_98vuii2wlh91_1",
    },
}
#: U4's `permission.replied`, verbatim. The id of the ask it answers is spelled
#: `requestID` here, not `id`.
REPLIED_PROPERTIES: Final[dict[str, object]] = {
    "sessionID": SESSION_ID,
    "requestID": PERMISSION_ID,
    "reply": "once",
}


def payload(event_type: str, properties: dict[str, object]) -> dict[str, object]:
    """The whole `data` document opencode sends: `{id, type, properties}`."""
    return {"id": f"evt_{event_type}", "type": event_type, "properties": properties}


def frame(event_type: str | None, properties: object, *, data: str | None = None) -> bytes:
    """One wire frame. `data` overrides the body; `event_type=None` drops the
    `event:` line, which is how the SSE default type is exercised."""
    body = json.dumps(properties, ensure_ascii=False) if data is None else data
    head = b"" if event_type is None else f"event: {event_type}\n".encode()
    return head + f"data: {body}\n\n".encode()


def connected_frame() -> bytes:
    """`server.connected` -- the first frame of EVERY connection, and the one frame
    on the stream that carries no `sessionID` because it describes the connection
    rather than a session."""
    return frame(CONNECTED, payload(CONNECTED, {}))


def asked_frame(*, session_id: str = SESSION_ID, permission_id: str = PERMISSION_ID) -> bytes:
    return frame(
        PERMISSION_ASKED,
        payload(
            PERMISSION_ASKED, {**ASKED_PROPERTIES, "id": permission_id, "sessionID": session_id}
        ),
    )


def replied_frame(*, request_id: str = PERMISSION_ID) -> bytes:
    return frame(
        PERMISSION_REPLIED,
        payload(PERMISSION_REPLIED, {**REPLIED_PROPERTIES, "requestID": request_id}),
    )


def idle_frame(*, session_id: str = SESSION_ID) -> bytes:
    return frame(TURN_COMPLETE, payload(TURN_COMPLETE, {"sessionID": session_id}))


def delta_frame(*, session_id: str = SESSION_ID) -> bytes:
    return frame(
        TEXT_DELTA,
        payload(TEXT_DELTA, {"sessionID": session_id, "messageID": "msg_1", "text": "привет"}),
    )


class TrackedStream(httpx.AsyncByteStream):
    """A response body that hands out `chunks` and can then block forever.

    The chunks are separate yields on purpose: that is the only way to reproduce a
    frame split across two TCP reads, which is the classic SSE boundary bug.
    """

    def __init__(self, chunks: Sequence[bytes], *, hold: asyncio.Event | None = None) -> None:
        self._chunks = list(chunks)
        self._hold = hold
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk
        if self._hold is not None:
            await self._hold.wait()

    async def aclose(self) -> None:
        self.closed = True


class EventServer:
    """`GET /event` over `MockTransport`: one scripted connection per request.

    `scripts` and `failures` are read by connection index and the last entry
    repeats, so a test writes only the connections it cares about. A connection
    with no script at all is a stream that ends immediately having said nothing.
    """

    def __init__(
        self,
        scripts: Sequence[Sequence[bytes]] = (),
        *,
        failures: Sequence[Exception | None] = (),
        hold: asyncio.Event | None = None,
        status: int = 200,
    ) -> None:
        self.requests: list[httpx.Request] = []
        self.streams: list[TrackedStream] = []
        self._scripts = [list(script) for script in scripts]
        self._failures = list(failures)
        self._hold = hold
        self._status = status

    def __call__(self, request: httpx.Request) -> httpx.Response:
        attempt = len(self.requests)
        self.requests.append(request)
        failure = self._failures[min(attempt, len(self._failures) - 1)] if self._failures else None
        if failure is not None:
            raise failure
        script = self._scripts[min(attempt, len(self._scripts) - 1)] if self._scripts else []
        stream = TrackedStream(script, hold=self._hold)
        self.streams.append(stream)
        if self._status != 200:
            return httpx.Response(self._status, json={"name": "UnauthorizedError"})
        return httpx.Response(
            200, stream=stream, headers={"content-type": "text/event-stream; charset=utf-8"}
        )

    @property
    def connections(self) -> int:
        return len(self.requests)

    def only(self) -> httpx.Request:
        assert self.connections == 1, f"expected one connection, saw {len(self.requests)}"
        return self.requests[0]


def _spec(*, password: str = PASSWORD) -> BackendSpec:
    """The `opencode_session` spec shape a test varies, and nothing else."""
    return BackendSpec(
        name="opencode",
        kind="opencode_session",
        base_url=BASE_URL,
        username="opencode",
        password=password,
        voice_agent="r2d2-voice",
        task_agent="r2d2-agent",
        fast_model=MODEL,
        task_model=MODEL,
        summarize_model=MODEL,
        timeout=3.2,
    )


def _source(
    server: EventServer,
    *,
    session_id: str = SESSION_ID,
    directory: str = WORKSPACE,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> tuple[EventSource, httpx.AsyncClient]:
    """An `EventSource` plus the injected client, so a test can inspect both."""
    client = httpx.AsyncClient(transport=httpx.MockTransport(server))
    source = EventSource(
        _spec(), directory, session_id=session_id, client=client, sleep=sleep or _no_wait
    )
    return source, client


async def _no_wait(_delay: float) -> None:
    """The default injected clock: waits for nothing. The tests that care about the
    delays inject their own recorder instead."""


async def _collect(source: EventSource) -> list[OpencodeEvent]:
    """Every event one connection delivers, in order."""
    return [event async for event in source.events()]


async def _events_of(raw: bytes) -> list[OpencodeEvent]:
    """Parse `raw` as one whole connection, the way the live server would send it.

    The turn helper is pure, but feeding it real parsed frames means its cases
    cannot drift from the reader's actual reading of the same bytes.
    """
    return await _collect(_source(EventServer([[raw]]))[0])


def _collector() -> tuple[list[OpencodeEvent], Callable[[OpencodeEvent], Awaitable[None]]]:
    """A recording handler -- `seen` and the handler that fills it."""
    seen: list[OpencodeEvent] = []

    async def handler(event: OpencodeEvent) -> None:
        seen.append(event)

    return seen, handler


@pytest.fixture
def built_clients(monkeypatch: pytest.MonkeyPatch) -> list[httpx.AsyncClient]:
    """Make every client the reader builds for itself a `MockTransport` one.

    Also the proof that the no-network rule holds for the path that constructs its
    own client instead of receiving one. (Same fixture as the todo 6 suite -- the
    tests directory carries no `conftest.py` by design.)
    """
    created: list[httpx.AsyncClient] = []
    real_client = httpx.AsyncClient
    fake = EventServer([[connected_frame()]])

    def factory(*, timeout: float = 5.0, **kwargs: object) -> httpx.AsyncClient:
        client = real_client(transport=httpx.MockTransport(fake), timeout=timeout, **kwargs)
        created.append(client)
        return client

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return created


# ---------------------------------------------------------------------------
# The vocabulary: exact event names, and an event mode that stays reachable
# ---------------------------------------------------------------------------


def test_the_event_names_are_the_ones_measured_on_this_machine() -> None:
    """C5: these five strings, spelled exactly as the live server spells them."""
    # Given / When
    names = (CONNECTED, PERMISSION_ASKED, PERMISSION_REPLIED, TURN_COMPLETE, TEXT_DELTA)
    # Then
    assert names == (
        "server.connected",
        "permission.asked",
        "permission.replied",
        "session.idle",
        "message.part.delta",
    )


def test_the_event_mode_is_live_sse_and_stays_one_of_the_three_degradations() -> None:
    """U4 resolved cleanly to SSE, but the polled and deny-only paths stay typed so
    they remain reachable: `EVENT_MODE` carries the `EventMode` literal set, not a
    bare `str`, so `todo 9` can branch on it and get a type error when it typos."""
    # Given
    modes = get_args(sse.EventMode)
    # When / Then
    assert EVENT_MODE == "sse"
    assert modes == ("sse", "poll", "deny")
    assert EVENT_MODE in modes


def test_event_properties_are_read_without_raising_when_a_key_is_absent() -> None:
    """Every accessor is total: a frame with no `sessionID` must not explode the
    reader, and that frame is the one the global stream sends for its own
    connection."""
    # Given
    event = OpencodeEvent(type="session.updated", properties={})
    # When / Then
    assert (event.session_id, event.permission_id, event.request_id) == (None, None, None)
    assert event.always == ()
    assert event.title == ""


def test_an_accessor_never_passes_through_a_value_of_the_wrong_type() -> None:
    """A JSON server can put anything in `properties`; a non-string has to read as
    absent rather than as a truthy value a caller would act on -- and a bare string
    in `always` must not be iterated character by character."""
    # Given
    event = OpencodeEvent(
        type=PERMISSION_ASKED,
        properties={"sessionID": 7, "id": ["per_1"], "requestID": None, "always": "echo *"},
    )
    # When / Then
    assert (event.session_id, event.permission_id, event.request_id) == (None, None, None)
    assert event.always == ()


async def test_the_always_list_is_exposed_for_logging_and_nothing_can_answer_with_it() -> None:
    """C5: `properties.always` lists command masks (`["echo *"]`) and one grant is
    permanent. The reader surfaces it and is structurally unable to act on it -- it
    has no way to answer a permission at all."""
    # Given
    source, _ = _source(EventServer([[asked_frame()]]))
    # When
    asked = (await _collect(source))[0]
    # Then
    assert asked.always == ("echo *",)
    assert asked.title == "echo R2D2SPIKERUN3K"
    assert not hasattr(sse, "respond_permission")


async def test_the_always_list_keeps_only_the_strings_the_server_actually_sent() -> None:
    """Given a mixed list: the log line gets the masks, not `repr` of junk."""
    # Given
    mixed = {**ASKED_PROPERTIES, "always": ["echo *", 7, None]}
    source, _ = _source(EventServer([[frame(PERMISSION_ASKED, payload(PERMISSION_ASKED, mixed))]]))
    # When
    asked = (await _collect(source))[0]
    # Then
    assert asked.always == ("echo *",)


# ---------------------------------------------------------------------------
# Parsing: the frames of one connection
# ---------------------------------------------------------------------------


async def test_the_first_event_of_a_connection_is_server_connected() -> None:
    """U4 measured `server.connected` as the first frame of every `GET /event`."""
    # Given
    source, _ = _source(EventServer([[connected_frame()]]))
    # When
    events = await _collect(source)
    # Then
    assert [event.type for event in events] == [CONNECTED]
    assert events[0].session_id is None


async def test_a_two_line_data_payload_is_joined_and_json_decoded() -> None:
    """SSE joins multi-line `data:` with newlines, and JSON tolerates the newline."""
    # Given
    raw = (
        f"event: {TEXT_DELTA}\n".encode()
        + b'data: {"properties":\ndata: '
        + json.dumps({"sessionID": SESSION_ID}).encode()
        + b"}\n\n"
    )
    # When
    events = await _events_of(raw)
    # Then
    assert [(event.type, event.session_id) for event in events] == [(TEXT_DELTA, SESSION_ID)]


async def test_a_comment_line_is_skipped_and_never_becomes_an_event() -> None:
    """`: keepalive` is a comment, and opencode sends it constantly."""
    # Given
    source, _ = _source(EventServer([[b": keepalive\n\n" + connected_frame()]]))
    # When
    events = await _collect(source)
    # Then
    assert [event.type for event in events] == [CONNECTED]


async def test_a_data_payload_that_is_not_json_is_dropped_with_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The reader must survive a broken frame, and must say so out loud."""
    # Given
    source, _ = _source(
        EventServer([[f"event: {TEXT_DELTA}\ndata: {{not json\n\n".encode() + connected_frame()]])
    )
    # When
    with caplog.at_level(logging.INFO):
        events = await _collect(source)
    # Then the next event still arrives
    assert [event.type for event in events] == [CONNECTED]
    # And the drop was reported at WARNING, naming the frame it dropped
    warned = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING and TEXT_DELTA in record.getMessage()
    ]
    assert len(warned) == 1


async def test_an_event_split_across_two_chunks_is_reassembled() -> None:
    """The classic SSE boundary bug: a frame cut in half by the network."""
    # Given
    raw = asked_frame()
    source, _ = _source(EventServer([[raw[: len(raw) // 2], raw[len(raw) // 2 :]]]))
    # When
    events = await _collect(source)
    # Then
    assert [event.permission_id for event in events] == [PERMISSION_ID]


async def test_mixed_crlf_and_lf_line_endings_produce_the_same_events() -> None:
    """A server may end some lines with CRLF and others with a bare LF."""
    # Given
    raw = connected_frame() + idle_frame() + delta_frame()
    # When
    lf_events = await _events_of(raw)
    crlf_events = await _events_of(raw.replace(b"\n", b"\r\n"))
    # Then
    assert [event.type for event in lf_events] == [event.type for event in crlf_events]
    assert [event.type for event in lf_events] == [CONNECTED, TURN_COMPLETE, TEXT_DELTA]


async def test_a_frame_without_a_data_field_never_becomes_an_event() -> None:
    """`event:` alone carries nothing to decode, and the next frame is unaffected."""
    # Given
    source, _ = _source(EventServer([[b"event: session.idle\n\n" + connected_frame()]]))
    # When
    events = await _collect(source)
    # Then
    assert [event.type for event in events] == [CONNECTED]


async def test_an_empty_data_field_dispatches_nothing_and_warns_about_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Per the SSE dispatch rules a bare `data:` resets the buffer and emits no
    event -- so it is not a malformed frame and must not cry wolf."""
    # Given
    source, _ = _source(EventServer([[b"event: session.idle\ndata:\n\n" + connected_frame()]]))
    # When
    with caplog.at_level(logging.INFO):
        events = await _collect(source)
    # Then
    assert [event.type for event in events] == [CONNECTED]
    assert [record for record in caplog.records if record.levelno >= logging.WARNING] == []


async def test_a_frame_without_an_event_field_arrives_as_the_sse_default_type() -> None:
    """SSE's own default. The stream is decoded, not second-guessed."""
    # Given
    source, _ = _source(EventServer([[frame(None, payload("message", {"sessionID": SESSION_ID}))]]))
    # When
    events = await _collect(source)
    # Then
    assert [(event.type, event.session_id) for event in events] == [("message", SESSION_ID)]


async def test_a_field_line_without_a_colon_is_ignored(caplog: pytest.LogCaptureFixture) -> None:
    """`garbage` is an unknown field with an empty value: legal, and inert. `id` and
    `retry` are known SSE fields this reader does not use."""
    # Given
    source, _ = _source(EventServer([[b"garbage\nid: evt_9\nretry: 3000\n" + connected_frame()]]))
    # When
    with caplog.at_level(logging.INFO):
        events = await _collect(source)
    # Then
    assert [event.type for event in events] == [CONNECTED]
    assert [record for record in caplog.records if record.levelno >= logging.WARNING] == []


@pytest.mark.parametrize(
    "data",
    [
        "[1, 2]",
        '"just a string"',
        "42",
        "null",
        "true",
        "{}",
        '{"properties": null}',
        '{"properties": [1, 2]}',
        '{"properties": "nope"}',
    ],
    ids=[
        "json-array",
        "json-string",
        "json-number",
        "json-null",
        "json-bool",
        "no-properties",
        "null-properties",
        "array-properties",
        "string-properties",
    ],
)
async def test_a_frame_that_parses_but_carries_no_object_properties_is_dropped(
    data: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A misleading success: JSON that decodes into something that is not an event.
    It must never become an `OpencodeEvent` with a broken mapping, and the stream
    must keep going."""
    # Given
    raw = f"event: {TEXT_DELTA}\ndata: {data}\n\n".encode() + connected_frame()
    source, _ = _source(EventServer([[raw]]))
    # When
    with caplog.at_level(logging.INFO):
        events = await _collect(source)
    # Then
    assert [event.type for event in events] == [CONNECTED]
    assert [record for record in caplog.records if record.levelno == logging.WARNING]


async def test_a_frame_the_stream_cut_in_half_is_discarded() -> None:
    """Per the SSE dispatch rules an unterminated trailing frame is dropped, and
    the rest of the connection is unaffected."""
    # Given: a complete frame, then a `data:` line with no blank line after it
    truncated = (
        b"event: session.idle\ndata: " + json.dumps({"sessionID": SESSION_ID}).encode() + b"\n"
    )
    source, _ = _source(EventServer([[connected_frame() + truncated]]))
    # When
    events = await _collect(source)
    # Then
    assert [event.type for event in events] == [CONNECTED]


# ---------------------------------------------------------------------------
# C5: the global stream, filtered to one session (the headline)
# ---------------------------------------------------------------------------


async def test_a_permission_ask_for_another_session_never_reaches_the_handler() -> None:
    """`GET /event` is global. Answering `ses_someone_else`'s permission would be a
    real, irreversible act performed on a stranger's turn, so the foreign frame is
    dropped before dispatch -- never filtered after."""
    # Given
    seen, record = _collector()
    source, _ = _source(
        EventServer([[asked_frame(session_id=FOREIGN_SESSION_ID), connected_frame()]])
    )
    stop = asyncio.Event()

    async def stop_on_first(event: OpencodeEvent) -> None:
        await record(event)
        stop.set()

    # When
    await asyncio.wait_for(source.run(stop_on_first, stop=stop), 2.0)
    # Then
    assert [event.type for event in seen] == [CONNECTED]
    assert all(event.session_id != FOREIGN_SESSION_ID for event in seen)


async def test_an_event_with_no_session_id_at_all_is_dropped() -> None:
    """A frame whose `properties` is a fine object but carries no `sessionID`
    belongs to no session, and reading it must not raise."""
    # Given
    heartbeat = frame("server.heartbeat", payload("server.heartbeat", {}))
    seen, record = _collector()
    source, _ = _source(EventServer([[heartbeat, connected_frame()]]))
    stop = asyncio.Event()

    async def stop_on_first(event: OpencodeEvent) -> None:
        await record(event)
        stop.set()

    # When
    await asyncio.wait_for(source.run(stop_on_first, stop=stop), 2.0)
    # Then
    assert [event.type for event in seen] == [CONNECTED]


async def test_the_connection_scoped_connected_event_does_reach_the_handler() -> None:
    """The documented exception to the filter above. `server.connected` has no
    `sessionID` because it describes the CONNECTION, and it is the only reliable
    proof that a reconnect succeeded -- so the handler sees it and may reset its
    per-connection state."""
    # Given
    seen, record = _collector()
    source, _ = _source(EventServer([[connected_frame()]]))
    stop = asyncio.Event()

    async def stop_on_first(event: OpencodeEvent) -> None:
        await record(event)
        stop.set()

    # When
    await asyncio.wait_for(source.run(stop_on_first, stop=stop), 2.0)
    # Then
    assert [event.type for event in seen] == [CONNECTED]
    assert seen[0].session_id is None


async def test_our_own_events_arrive_in_order_and_foreign_ones_are_never_seen() -> None:
    """The mixed stream as the live server sends it: our turn interleaved with
    somebody else's."""
    # Given
    raw = (
        connected_frame()
        + delta_frame()
        + asked_frame(session_id=FOREIGN_SESSION_ID)
        + idle_frame()
        + frame("session.updated", payload("session.updated", {}))
        + delta_frame(session_id=FOREIGN_SESSION_ID)
        + delta_frame()
    )
    seen, record = _collector()
    source, _ = _source(EventServer([[raw]]))
    stop = asyncio.Event()

    async def stop_at_four(event: OpencodeEvent) -> None:
        await record(event)
        if len(seen) == 4:
            stop.set()

    # When
    await asyncio.wait_for(source.run(stop_at_four, stop=stop), 2.0)
    # Then
    assert [event.type for event in seen] == [CONNECTED, TEXT_DELTA, TURN_COMPLETE, TEXT_DELTA]
    assert all(event.session_id in (None, SESSION_ID) for event in seen)


# ---------------------------------------------------------------------------
# Reconnect, backoff and failures
# ---------------------------------------------------------------------------


async def test_a_stream_that_ends_is_reconnected_and_a_later_event_is_delivered() -> None:
    """The server closes idle connections; the reader must come back on its own."""
    # Given
    server = EventServer([[connected_frame()], [connected_frame() + idle_frame()]])
    seen, record = _collector()
    source, _ = _source(server)
    stop = asyncio.Event()

    async def stop_on_idle(event: OpencodeEvent) -> None:
        await record(event)
        if event.type == TURN_COMPLETE:
            stop.set()

    # When
    await asyncio.wait_for(source.run(stop_on_idle, stop=stop), 2.0)
    # Then
    assert server.connections == 2
    assert [event.type for event in seen] == [CONNECTED, CONNECTED, TURN_COMPLETE]


async def test_the_backoff_ladder_grows_to_the_cap_and_stays_there() -> None:
    """Every connection fails, so the reader waits 1, 2, 4, 5, 5 seconds: it must
    back off -- a hot reconnect loop would hammer a server that is down -- and it
    must be capped, or an unlucky run would never come back at all."""
    # Given
    delays: list[float] = []
    stop = asyncio.Event()

    async def sleep(delay: float) -> None:
        delays.append(delay)
        if len(delays) == 5:
            stop.set()

    server = EventServer([[]], failures=[httpx.ReadError("connection reset") for _ in range(9)])
    seen, handler = _collector()
    source, _ = _source(server, sleep=sleep)
    # When
    await asyncio.wait_for(source.run(handler, stop=stop), 2.0)
    # Then
    assert delays == [1.0, 2.0, 4.0, 5.0, 5.0]
    assert server.connections == 5
    assert seen == []


async def test_a_transport_error_is_retried_and_the_next_connection_delivers() -> None:
    """`httpx.ReadError` on the first call, a real stream on the second -- the
    reader retries, delivers, and raises nothing."""
    # Given
    failures: list[Exception | None] = [httpx.ReadError("boom"), None]
    server = EventServer([[connected_frame() + idle_frame()]], failures=failures)
    seen, record = _collector()
    source, _ = _source(server)
    stop = asyncio.Event()

    async def stop_on_idle(event: OpencodeEvent) -> None:
        await record(event)
        if event.type == TURN_COMPLETE:
            stop.set()

    # When
    await asyncio.wait_for(source.run(stop_on_idle, stop=stop), 2.0)
    # Then
    assert server.connections == 2
    assert [event.type for event in seen] == [CONNECTED, TURN_COMPLETE]


async def test_a_handler_failure_is_never_mistaken_for_a_stream_failure() -> None:
    """Todos 9 and 14 answer permissions from inside the handler, and
    `respond_permission` raises `OpencodeError` on a real 404. If that were caught
    as a stream failure the reader would drop the connection and the caller would
    never learn its own handler threw."""
    # Given
    source, _ = _source(EventServer([[connected_frame()]]))

    async def handler(event: OpencodeEvent) -> None:
        raise OpencodeError("permission not found")

    # When / Then
    with pytest.raises(OpencodeError, match="permission not found"):
        await asyncio.wait_for(source.run(handler, stop=asyncio.Event()), 2.0)


async def test_a_non_2xx_answer_raises_the_opencode_error_family_without_the_password() -> None:
    """A wrong password answers 401 on the event route too, and the message has to
    be safe to put in a log."""
    # Given
    source, _ = _source(EventServer([[]], status=401))
    # When
    with pytest.raises(OpencodeStatusError) as caught:
        await _collect(source)
    # Then
    assert "401" in str(caught.value)
    assert PASSWORD not in str(caught.value)
    assert isinstance(caught.value, OpencodeError)
    assert isinstance(caught.value, BackendError)


async def test_the_event_route_is_basic_authenticated_and_carries_the_directory() -> None:
    """C6: `?directory=` is a QUERY parameter, and the server needs the password."""
    # Given
    server = EventServer([[connected_frame()]])
    source, _ = _source(server)
    # When
    await _collect(source)
    # Then
    request = server.only()
    assert request.url.path == "/event"
    assert request.url.params["directory"] == WORKSPACE
    assert request.headers["authorization"].startswith("Basic ")


async def test_the_scope_on_the_wire_does_not_replace_the_session_filter() -> None:
    """The stream is global whatever `?directory=` said, so the session id decides
    what a handler sees: two sessions on one stream, one survivor."""
    # Given
    raw = (
        connected_frame()
        + delta_frame()
        + delta_frame(session_id=FOREIGN_SESSION_ID)
        + idle_frame()
    )
    server = EventServer([[raw]])
    seen, record = _collector()
    source, _ = _source(server, directory="/somewhere/else")
    stop = asyncio.Event()

    async def stop_on_idle(event: OpencodeEvent) -> None:
        await record(event)
        if event.type == TURN_COMPLETE:
            stop.set()

    # When
    await asyncio.wait_for(source.run(stop_on_idle, stop=stop), 2.0)
    # Then
    assert server.only().url.params["directory"] == "/somewhere/else"
    assert [event.type for event in seen] == [CONNECTED, TEXT_DELTA, TURN_COMPLETE]
    assert all(event.session_id in (None, SESSION_ID) for event in seen)


# ---------------------------------------------------------------------------
# Stopping: cleanly, early and mid-stream
# ---------------------------------------------------------------------------


async def test_stop_set_before_the_first_event_exits_the_loop_without_raising() -> None:
    """The plan's failure-path case: stop is already set, so not one request is
    made and nothing is raised."""
    # Given
    server = EventServer([[connected_frame()]])
    seen, handler = _collector()
    stop = asyncio.Event()
    stop.set()
    source, _ = _source(server)
    # When / Then
    await asyncio.wait_for(source.run(handler, stop=stop), 1.0)
    assert seen == []
    assert server.connections == 0


async def test_stop_mid_stream_exits_the_loop_without_raising() -> None:
    # Given
    seen, record = _collector()
    source, _ = _source(EventServer([[connected_frame() + idle_frame() + delta_frame()]]))
    stop = asyncio.Event()

    async def stop_on_first(event: OpencodeEvent) -> None:
        await record(event)
        stop.set()

    # When / Then
    await asyncio.wait_for(source.run(stop_on_first, stop=stop), 1.0)
    assert [event.type for event in seen] == [CONNECTED]


async def test_a_transport_that_never_answers_leaves_the_loop_waiting() -> None:
    """A hung server must not be mistaken for a finished one -- and must not hang a
    test either, so the explicit timeout is the assertion."""
    # Given
    server = EventServer([[connected_frame()]], hold=asyncio.Event())
    seen, handler = _collector()
    source, _ = _source(server)
    # When
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(source.run(handler, stop=asyncio.Event()), 0.2)
    # Then
    assert [event.type for event in seen] == [CONNECTED]


async def test_cancelling_the_loop_mid_stream_closes_the_response() -> None:
    """Repeated interruption: a cancelled supervisor must not leave the opencode
    connection hanging, and what it already dispatched stays dispatched."""
    # Given
    server = EventServer([[connected_frame() + idle_frame()]], hold=asyncio.Event())
    seen, handler = _collector()
    source, _ = _source(server)
    task = asyncio.create_task(source.run(handler, stop=asyncio.Event()))
    await asyncio.sleep(0.05)
    # When
    task.cancel()
    # Then
    with pytest.raises(asyncio.CancelledError):
        await task
    assert server.streams[0].closed is True
    assert [event.type for event in seen] == [CONNECTED, TURN_COMPLETE]


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


async def test_the_context_manager_closes_the_client_it_built(
    built_clients: list[httpx.AsyncClient],
) -> None:
    """An `async with` must not leave an httpx client open on the floor."""
    # Given
    source = EventSource(_spec(), WORKSPACE, session_id=SESSION_ID)
    # When
    async with source:
        assert [event.type for event in await _collect(source)] == [CONNECTED]
    # Then
    assert [client.is_closed for client in built_clients] == [True]


async def test_aclose_is_idempotent_and_safe_with_the_stream_never_opened() -> None:
    """Given repeated interruption: closing twice, and closing a source that never
    opened anything, are both no-ops -- and neither may close a client somebody
    else injected."""
    # Given
    source, client = _source(EventServer([[connected_frame()]]))
    # When
    await source.aclose()
    await source.aclose()
    async with source:
        pass
    await source.aclose()
    # Then
    assert not client.is_closed
    # And the injected client is still attached, not detached: a dropped reference
    # would make the next call build a real one and hit the network.
    assert [event.type for event in await _collect(source)] == [CONNECTED]


# ---------------------------------------------------------------------------
# Turn completion: session.idle AND no unanswered permission (C5 rule 2)
# ---------------------------------------------------------------------------


async def test_a_turn_with_an_unanswered_permission_ask_is_not_complete() -> None:
    """The premature-completion trap: `session.idle` arrives even while a tool is
    blocked on a permission answer, so idle alone would tell the brain the turn is
    over and the reply would be lost."""
    # Given
    events = await _events_of(asked_frame() + idle_frame())
    # When / Then
    assert turn_is_complete(events) is False


async def test_a_turn_whose_permission_was_answered_is_complete() -> None:
    # Given
    events = await _events_of(
        connected_frame() + asked_frame() + delta_frame() + replied_frame() + idle_frame()
    )
    # When / Then
    assert turn_is_complete(events) is True


async def test_no_idle_event_means_the_turn_is_not_complete() -> None:
    """A stream that only carried deltas has produced nothing final."""
    # Given / When / Then
    assert turn_is_complete([]) is False
    assert turn_is_complete(await _events_of(delta_frame())) is False
    assert turn_is_complete(await _events_of(asked_frame() + replied_frame())) is False


async def test_a_permission_asked_after_the_idle_belongs_to_the_next_turn() -> None:
    """Asking again after idle reopens the turn: the ask is unanswered, so the
    sequence is not a completed turn."""
    # Given / When / Then
    assert turn_is_complete(await _events_of(idle_frame() + asked_frame())) is False


async def test_a_reply_that_matches_no_ask_leaves_the_turn_complete() -> None:
    """A reply with nothing to answer cannot be holding a turn open."""
    # Given / When / Then
    assert turn_is_complete(await _events_of(idle_frame() + replied_frame())) is True


async def test_two_asks_are_only_complete_once_both_are_answered() -> None:
    """A reply answers ONE ask, matched by `requestID` against the ask's `id`."""
    # Given
    second_ask = asked_frame(permission_id="per_2")
    second_reply = replied_frame(request_id="per_2")
    # When / Then
    assert turn_is_complete(await _events_of(asked_frame() + second_ask + idle_frame())) is False
    assert (
        turn_is_complete(
            await _events_of(asked_frame() + second_ask + replied_frame() + idle_frame())
        )
        is False
    )
    assert (
        turn_is_complete(
            await _events_of(
                asked_frame() + second_ask + replied_frame() + second_reply + idle_frame()
            )
        )
        is True
    )


async def test_the_turn_helper_reads_its_events_once_so_a_live_iterator_works() -> None:
    """Todos 9 and 14 will feed it what their consumer collected, so it must accept
    a one-shot iterator: no `len()`, no indexing, no second pass."""
    # Given
    events = iter(await _events_of(asked_frame() + replied_frame() + idle_frame()))
    # When
    verdict = turn_is_complete(events)
    # Then
    assert verdict is True
    assert list(events) == []


# ---------------------------------------------------------------------------
# Secret hygiene
# ---------------------------------------------------------------------------


async def test_a_sentinel_hidden_in_a_payload_never_reaches_the_log_at_info(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Event payloads come from the server and can echo anything the user typed. At
    INFO the reader logs the event TYPE and, for a permission, the title -- never
    the properties blob."""
    # Given
    leaky = frame(
        PERMISSION_ASKED,
        payload(
            PERMISSION_ASKED,
            {
                **ASKED_PROPERTIES,
                "metadata": {"command": "ls -la", "token": SENTINEL},
                "previous": SENTINEL,
            },
        ),
    )
    seen, record = _collector()
    source, _ = _source(EventServer([[connected_frame() + leaky + idle_frame()]]))
    stop = asyncio.Event()

    async def stop_on_idle(event: OpencodeEvent) -> None:
        await record(event)
        if event.type == TURN_COMPLETE:
            stop.set()

    # When
    with caplog.at_level(logging.INFO):
        await asyncio.wait_for(source.run(stop_on_idle, stop=stop), 2.0)
    # Then -- the sentinel is nowhere in anything the reader logged
    assert [event.type for event in seen] == [CONNECTED, PERMISSION_ASKED, TURN_COMPLETE]
    assert SENTINEL not in caplog.text
    # And the record that IS written is the type plus the title, so this test is
    # not passing vacuously on an empty log.
    assert PERMISSION_ASKED in caplog.text
    assert "ls -la" in caplog.text
