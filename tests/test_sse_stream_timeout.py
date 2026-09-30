"""D1: the event stream's read bound is its own, not the voice deadline.

`tests/test_sse.py` proves what the reader DOES with a frame, and proves it
entirely through `httpx.MockTransport`. That transport has no socket, so it has no
timeouts either -- measured here, not assumed: a client with a **0.2 s** read
timeout whose mock body stays silent for **0.6 s** still receives its frame
(`test_a_mock_transport_cannot_express_this_defect`). The defect this file exists
for was therefore invisible to the whole suite, and stayed invisible through a
live end-to-end run that missed 7 of 7 `permission.asked` events:

* `spec.timeout` is the **voice** deadline (`3.3 s` in `config/backends.json`,
  `R2D2_FAST_DEADLINE`). It is right for `POST /session/:id/message` and wrong
  for a socket that is meant to stay open for the life of the process.
* opencode 1.18.32's `server.heartbeat` interval was measured live at **10.0 s**
  (`docs/11-opencode-contract.md` U4, gaps `[10.0, 10.0]`). A reader whose read
  bound is 3.3 s cannot survive to the next heartbeat, so it walks a
  `3.3 s connect / 5 s sleep` ladder and is blind for most of every window.
* The consequence is not degraded liveness, it is a **dead permission broker**:
  the ask is the one event that must not be missed, and it is exactly the one
  that arrives inside a heartbeat gap.

So the server below is a raw asyncio listener on a loopback port. It is the only
place a read timeout exists at all, and the tests speak the measured surface: an
initial `server.connected`, then silence longer than the voice deadline, then a
`permission.asked`. What is asserted is BEHAVIOUR -- the reader is still connected
(`connection_count == 1`) and delivers the ask -- and only then the wiring that
makes it possible.

**Cancellation is pinned here too, not because it is a new path but because the
bound moved.** A 30 s read timeout must not become a 30 s shutdown: the reader is
cancelled while blocked in a silent read, and both the cancellation and the socket
going away are timed. That is the test that fails first if somebody later folds
the two bounds back into one.

The three tests that need a socket are skipped, never failed, when the run refuses
loopback sockets (`-p no_net`, as `tests/test_e2e_stack.py` does for its two SSE
tests); the wiring and the shipped-config assertions do not need one and always
run.
"""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Final

import httpx
import pytest

from app.config import Config
from core.backends.config_loader import BackendSpec, load_backend_specs
from core.opencode.sse import (
    CONNECTED,
    PERMISSION_ASKED,
    EventSource,
    OpencodeEvent,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SHIPPED: Final = str(REPO_ROOT / "config" / "backends.json")
#: An existing absolute path. `?directory=` is not validated server-side and the
#: reader does not check it either -- only session-scoped requests do (C6) -- and
#: the repo root always exists and needs no cleanup.
WORKSPACE: Final = str(REPO_ROOT)
SESSION_ID: Final = "ses_f266522f4ffee31XNquJ1D3qjC"
PERMISSION_ID: Final = "per_0d99aed41001iOUiwXDd6vx2NP"
PASSWORD: Final = "R2D2_OC_PASSWORD_VALUE"

#: The gap between `server.connected` and the ask. It is deliberately a little
#: LONGER than the shipped voice deadline, because that is the whole defect: a
#: reader that cannot stay quiet for 3.3 s is disconnected for every ask that
#: arrives in a heartbeat gap. The measured opencode heartbeat is 10.0 s, so a
#: real ask lands even deeper into a gap than this one does.
SILENCE_S: Final = 3.6
#: How long a test waits for the ask before calling it missed. Generous enough
#: that a loaded CI box does not fail a healthy reader, short enough that the red
#: phase is a red test rather than a hang.
DELIVERY_BUDGET_S: Final = 15.0
#: How long a cancellation of a reader blocked in a silent read may take. The
#: bound under test is 30 s, so anything near it is a wedged shutdown.
CANCEL_BUDGET_S: Final = 2.0

#: One `text/event-stream` response delimited by the connection close, the way a
#: live SSE server leaves it: the client's read bound is the only thing that can
#: end the body.
RESPONSE_HEAD: Final = (
    b"HTTP/1.1 200 OK\r\n"
    b"Content-Type: text/event-stream; charset=utf-8\r\n"
    b"Cache-Control: no-cache\r\n"
    b"Connection: close\r\n"
    b"\r\n"
)


def frame(event_type: str, properties: dict[str, object]) -> bytes:
    """One wire frame, in the shape opencode sends it (U4)."""
    body = json.dumps({"id": f"evt_{event_type}", "type": event_type, "properties": properties})
    return f"event: {event_type}\ndata: {body}\n\n".encode()


def connected_frame() -> bytes:
    """The first frame of EVERY connection, and the only one with no `sessionID`."""
    return frame(CONNECTED, {})


def asked_frame() -> bytes:
    """A `permission.asked` for this reader's session, in U4's payload shape."""
    return frame(
        PERMISSION_ASKED,
        {
            "id": PERMISSION_ID,
            "sessionID": SESSION_ID,
            "permission": "bash",
            "patterns": ["ls -la /tmp"],
            "metadata": {"command": "ls -la /tmp"},
            "always": ["ls *"],
        },
    )


def _one_connected_frame(_request: httpx.Request) -> httpx.Response:
    """A mock `GET /event` that answers once and ends -- for the wiring test."""
    return httpx.Response(200, content=connected_frame())


@dataclass
class _Connection:
    """One accepted socket, so a test can ask what the server saw."""

    writer: asyncio.StreamWriter
    closed_by_client: bool = False


@dataclass
class SilentStream:
    """`GET /event` on a real loopback socket, with the silence the tests need.

    A script is `[(delay_before, frame), ...]` per connection index, and the LAST
    script repeats -- so a test writes only the connections it cares about. A
    connection whose script is `server.connected` and nothing else is a stream
    that is silent for ever afterwards, which is what a heartbeat gap looks like
    from the reader's side. `hold` keeps the connection open after the script, so
    the reader can be cancelled while blocked in a read, and records whether the
    CLIENT was the one who closed it.
    """

    scripts: Sequence[Sequence[tuple[float, bytes]]]
    hold: bool = False
    requests: list[str] = field(default_factory=list)
    connections: list[_Connection] = field(default_factory=list)
    _server: asyncio.Server | None = field(default=None, init=False, repr=False)
    _tasks: list[asyncio.Task[None]] = field(default_factory=list, init=False, repr=False)
    _connected: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)
    _client_gone: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)

    async def __aenter__(self) -> "SilentStream":
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        for connection in self.connections:
            connection.writer.close()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    @property
    def base_url(self) -> str:
        assert self._server is not None, "the server is not running"
        return f"http://127.0.0.1:{self._server.sockets[0].getsockname()[1]}"

    @property
    def connection_count(self) -> int:
        """How many times the reader connected. `1` means "it never had to come back"."""
        return len(self.connections)

    async def wait_for_connection(self, timeout_s: float = 5.0) -> None:
        """Block until a reader is attached, so a test never races the dial."""
        await asyncio.wait_for(self._connected.wait(), timeout_s)

    async def wait_for_client_close(self, timeout_s: float = 5.0) -> None:
        """Block until the reader's socket is gone, i.e. the connection was closed."""
        await asyncio.wait_for(self._client_gone.wait(), timeout_s)

    def _script_for(self, index: int) -> Sequence[tuple[float, bytes]]:
        return self.scripts[min(index, len(self.scripts) - 1)]

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        connection = _Connection(writer)
        self.connections.append(connection)
        self._connected.set()
        self._tasks.append(asyncio.current_task())  # type: ignore[arg-type]
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            self.requests.append(head.split(b"\r\n", 1)[0].decode("latin-1"))
            writer.write(RESPONSE_HEAD)
            await writer.drain()
            for delay, payload in self._script_for(len(self.connections) - 1):
                await asyncio.sleep(delay)
                writer.write(payload)
                await writer.drain()
            if self.hold:
                while await reader.read(4096):
                    pass
                connection.closed_by_client = True
                self._client_gone.set()
        except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
            # The reader went away mid-script, which is what a cancellation looks
            # like from this side and is not a failure of the fake.
            return
        finally:
            writer.close()


@pytest.fixture
async def loopback_sockets() -> AsyncIterator[None]:
    """Fail loudly here, skip politely under a run that forbids sockets.

    The probe is the same thing these tests are about: can this process open a
    connection at all? `tests/test_e2e_stack.py` skips its two SSE tests the same
    way, and a hermeticity run (`-p no_net`) is exactly what forbids it.
    """
    probe = socket.socket()
    try:
        probe.bind(("127.0.0.1", 0))
    except OSError as exc:  # pragma: no cover -- only on a socketless run
        pytest.skip(f"this run refuses loopback sockets: {exc}")
    finally:
        probe.close()
    yield


@pytest.fixture
def shipped_spec(monkeypatch: pytest.MonkeyPatch) -> BackendSpec:
    """The `opencode` backend as the SHIPPED config declares it, not as a test imagines it.

    The numbers under test are the ones an operator runs, so they are read out of
    `config/backends.json` through the loader rather than typed into a fixture.
    """
    monkeypatch.setenv("YANDEX_FOLDER_ID", "b1gdocstestfolder")
    _chain, specs = load_backend_specs(Config(backends_path=SHIPPED))
    return specs["opencode"]


def _collector() -> tuple[list[OpencodeEvent], Callable[[OpencodeEvent], Awaitable[None]]]:
    """A recording handler, so a test can assert on what the reader delivered."""
    seen: list[OpencodeEvent] = []

    async def handler(event: OpencodeEvent) -> None:
        seen.append(event)

    return seen, handler


async def _run_until_the_first_ask(source: EventSource, seen: list[OpencodeEvent]) -> None:
    """Drive the reader until an ask arrives, then stop and close the client.

    The `asyncio.wait_for` IS the assertion: a reader that missed the ask keeps
    reconnecting until the budget expires, and that timeout is the failure.
    """
    stop = asyncio.Event()

    async def handler(event: OpencodeEvent) -> None:
        seen.append(event)
        if event.type == PERMISSION_ASKED:
            stop.set()

    try:
        await asyncio.wait_for(source.run(handler, stop=stop), DELIVERY_BUDGET_S)
    finally:
        await source.aclose()


# ---------------------------------------------------------------------------
# 1. The regression: a silence longer than the voice deadline
# ---------------------------------------------------------------------------


async def test_the_reader_keeps_its_connection_through_a_silence_the_voice_deadline_could_not_survive(
    loopback_sockets: None, shipped_spec: BackendSpec
) -> None:
    """D1, the whole defect in one test.

    Connection 1 sends `server.connected` and then says nothing for `SILENCE_S`
    (3.6 s, longer than the shipped 3.3 s voice deadline) before the ask. Every
    later connection sends `server.connected` and then nothing for ever, which is
    what the live run saw: the reader kept reconnecting, and the ask was already
    gone.
    """
    # Given: a server that answers with the ask only after a heartbeat-length silence
    async with SilentStream(
        scripts=[((0.0, connected_frame()), (SILENCE_S, asked_frame())), ((0.0, connected_frame()),)]
    ) as server:
        spec = replace(shipped_spec, base_url=server.base_url)
        # Only the shipped REQUEST deadline is named here, because that is what the
        # broken reader used for the stream too -- the assertion below has to be
        # reachable without the new key existing.
        assert spec.timeout < SILENCE_S, "the silence must outlast the request deadline"
        seen: list[OpencodeEvent] = []
        # When
        await _run_until_the_first_ask(EventSource(spec, WORKSPACE, session_id=SESSION_ID), seen)
        # Then: the ask arrived, and it arrived on the connection that was already open
        assert [event.type for event in seen] == [CONNECTED, PERMISSION_ASKED]
        assert seen[-1].permission_id == PERMISSION_ID
        assert server.connection_count == 1, "the silence disconnected the reader"
        # And it asked for exactly what it should: one `GET /event` scoped by the
        # workspace (C6), which is the only opencode traffic this test produces.
        assert len(server.requests) == 1
        assert server.requests[0].startswith("GET /event?directory=")


async def test_the_two_bounds_are_independent_rather_than_one_number_saying_it_twice(
    loopback_sockets: None, tmp_path: Path
) -> None:
    """The same behaviour at a scale a test can afford, which is the real claim.

    D1 was never "3.3 is too small", it was "one value is doing two jobs". So this
    test declares BOTH numbers in a config of its own and reads them back through
    the loader: a request deadline of **0.3 s** and a stream read bound of **2.0 s**,
    with the ask 0.9 s in. A reader that reads the stream with the request deadline
    loses the ask after 0.3 s; a reader that reads it with its own bound does not.
    """
    # Given a config that declares the two bounds separately
    path = tmp_path / "backends.json"
    path.write_text(
        json.dumps(
            {
                "chain": ["opencode"],
                "backends": [
                    {
                        "name": "opencode",
                        "kind": "opencode_session",
                        "base_url": "http://127.0.0.1:4599",
                        "username": "r2d2",
                        "timeout": 0.3,
                        "event_read_timeout": 2.0,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    _chain, specs = load_backend_specs(Config(backends_path=str(path)))
    spec = specs["opencode"]
    async with SilentStream(
        scripts=[((0.0, connected_frame()), (0.9, asked_frame())), ((0.0, connected_frame()),)]
    ) as server:
        # When
        seen: list[OpencodeEvent] = []
        await _run_until_the_first_ask(
            EventSource(replace(spec, base_url=server.base_url), WORKSPACE, session_id=SESSION_ID),
            seen,
        )
        # Then the ask survived a silence three times the request deadline
        assert [event.type for event in seen] == [CONNECTED, PERMISSION_ASKED]
        assert server.connection_count == 1
    # And the loader read both keys as fields: an unrecognised key is swallowed into
    # `extra` instead of raising, which is how a typo would pass unnoticed.
    assert spec.extra == {}


# ---------------------------------------------------------------------------
# 2. The wiring, and what the shipped config says
# ---------------------------------------------------------------------------


async def test_the_reader_keeps_the_request_deadline_for_connecting_and_its_own_for_reading(
    monkeypatch: pytest.MonkeyPatch, shipped_spec: BackendSpec
) -> None:
    """One `httpx.Timeout`, four knobs, and only the read one belongs to the stream.

    Everything that is a request -- connecting, writing, taking a connection from
    the pool -- is still bounded by `spec.timeout`, so a server that is down is
    still discovered in seconds. Only the read of an OPEN stream gets the long
    bound, because that is the only read that is supposed to wait for the next
    heartbeat.
    """
    # Given a reader that builds its own client, as it does in production
    built: list[httpx.Timeout] = []
    real_client = httpx.AsyncClient

    def factory(*, timeout: httpx.Timeout, **kwargs: object) -> httpx.AsyncClient:
        built.append(timeout)
        return real_client(
            transport=httpx.MockTransport(_one_connected_frame), timeout=timeout, **kwargs
        )

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    spec = replace(shipped_spec, timeout=0.4, event_read_timeout=7.5)
    source = EventSource(spec, WORKSPACE, session_id=SESSION_ID)
    # When
    try:
        async for _event in source.events():
            break
    finally:
        await source.aclose()
    # Then
    assert len(built) == 1
    assert built[0].read == 7.5
    assert (built[0].connect, built[0].write, built[0].pool) == (0.4, 0.4, 0.4)


def test_the_shipped_config_separates_the_two_bounds_and_leaves_the_others_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The numbers an operator runs, read from the file rather than restated.

    `timeout` keeps its meaning everywhere -- the voice deadline for opencode, the
    loader default for the three HTTP backends, which declare nothing and must
    keep declaring nothing -- and `event_read_timeout` is opencode's own key.
    """
    # Given
    monkeypatch.setenv("YANDEX_FOLDER_ID", "b1gdocstestfolder")
    _chain, specs = load_backend_specs(Config(backends_path=SHIPPED))
    document = json.loads(Path(SHIPPED).read_text(encoding="utf-8"))
    declared = {entry["name"]: entry for entry in document["backends"]}
    # When
    opencode = specs["opencode"]
    # Then: the voice deadline is untouched, and the stream has a bound of its own
    assert opencode.timeout == 3.3
    assert opencode.event_read_timeout == 30.0
    assert opencode.event_read_timeout > opencode.timeout
    assert declared["opencode"]["event_read_timeout"] == 30.0
    # And: the other three are exactly as they were -- a default they never declare
    for name in ("zen", "yandexgpt", "openrouter"):
        assert "event_read_timeout" not in declared[name], f"{name} was given a new key"
        assert "timeout" not in declared[name], f"{name}'s timeout was touched"
        assert specs[name].timeout == 3.0
        assert specs[name].event_read_timeout == 30.0


# ---------------------------------------------------------------------------
# 3. Cancellation: a long read bound must not become a long shutdown
# ---------------------------------------------------------------------------


async def test_cancelling_a_reader_blocked_on_a_silent_stream_is_prompt(
    loopback_sockets: None, shipped_spec: BackendSpec
) -> None:
    """The one thing a 30 s read bound could plausibly break.

    The reader is attached, has its `server.connected`, and is blocked in a read
    that will never be answered -- the exact state shutdown has to interrupt. Two
    things are timed: the cancellation, and the socket going away afterwards. A
    reader that treated its read bound as a shutdown deadline would take 30 s to
    do what `SessionReaders.aclose()` does by cancelling a task.
    """
    # Given: a connection that is attached and then silent for ever
    async with SilentStream(scripts=[((0.0, connected_frame()),)], hold=True) as server:
        spec = replace(shipped_spec, base_url=server.base_url)
        seen, handler = _collector()
        source = EventSource(spec, WORKSPACE, session_id=SESSION_ID)
        task = asyncio.create_task(source.run(handler, stop=asyncio.Event()))
        await server.wait_for_connection()
        await asyncio.sleep(0.1)  # the reader is now inside the silent read
        # When
        started = asyncio.get_running_loop().time()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, CANCEL_BUDGET_S)
        elapsed = asyncio.get_running_loop().time() - started
        # Then: the cancellation did not wait out the read bound
        assert elapsed < CANCEL_BUDGET_S
        assert [event.type for event in seen] == [CONNECTED]
        await source.aclose()
        # And the connection really was closed, not abandoned
        await server.wait_for_client_close()
        assert [c.closed_by_client for c in server.connections] == [True]


# ---------------------------------------------------------------------------
# 4. Why this file needs a socket at all
# ---------------------------------------------------------------------------


async def test_a_mock_transport_cannot_express_this_defect() -> None:
    """A mock body that is silent for 0.6 s still answers a 0.2 s read timeout.

    This is why the defect survived a green suite: `MockTransport` has no socket,
    so it enforces no timeout, and every test built on it would have passed with
    the voice deadline wired into the stream. It is the harness proving it is
    necessary -- and the reason the fix is proved behaviourally above.
    """
    # Given a stream that takes longer to answer than the client's read bound
    class Slow(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            await asyncio.sleep(0.6)
            yield connected_frame()

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, stream=Slow())),
        timeout=0.2,
    )
    source = EventSource(
        BackendSpec(
            name="opencode",
            kind="opencode_session",
            base_url="http://127.0.0.1:4599",
            username="opencode",
            password=PASSWORD,
            timeout=0.2,
            event_read_timeout=0.2,
        ),
        WORKSPACE,
        session_id=SESSION_ID,
        client=client,
    )
    # When / Then: the frame arrives anyway, so nothing on a mock can fail on a timeout
    assert [event.type async for event in source.events()] == [CONNECTED]
    await client.aclose()
