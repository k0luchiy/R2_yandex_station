"""D11: what opencode 1.18.32's frames really are, and the reader decoding them.

Every fixture in this module is a **verbatim `data:` body** captured from a live
`GET /event` on this build -- not a hand-written frame with an `event:` line added,
which is the whole reason the defect survived a green suite. The tap that produced
them is `qa/d11-wire-tap.py`; its output is `qa/d11-wire-tap.out`, and it records for
that run:

    lines starting with 'data:':      44
    lines starting with 'event:':      0
    bodies by their own "type":       server.connected, permission.asked,
                                       permission.replied, session.idle,
                                       message.part.delta, server.heartbeat, ...
    decode_frame() types, as shipped: {'message': 44}

Forty-four real frames, zero `event:` lines, and the shipped decoder typed every one
of them `"message"`. A 30-minute tap of the owner's own server saw 1090 `data:` lines
against 0 `event:` lines, so this is the build's shape and not a scratch-server
quirk.

**Nothing here is scrubbed.** The tap ran against a throwaway `OPENCODE_CONFIG_DIR`
in `/tmp` with a workspace of its own, on the scratch port 4613, and no credential,
owner session or TUI state is in any of these strings -- an SSE frame never carries
the server's basic-auth password, and the session id, message id and call id in them
are all minted by that throwaway server for that throwaway run.

**The precedence rule this pins**, in the order the reader applies it:

1. the SSE `event:` field, when present and non-empty -- the only place the SSE
   specification lets a server state the name, so a server that states it is not
   second-guessed;
2. otherwise the body's own top-level `"type"` -- where this build puts every name,
   and therefore the only path a real frame can take;
3. otherwise `SSE_DEFAULT_EVENT`, so a frame that names itself nowhere still decodes
   instead of being dropped.

A frame carrying both names with different values takes (1) and warns once: nothing
in this protocol expects a mismatch, and quietly preferring one of two names is how a
healthy-looking stream stops dispatching.

The transport double (`EventServer`) is imported from `tests.test_sse` rather than
copied, so these bytes travel through the same reader the rest of the suite uses.
"""
from __future__ import annotations

import asyncio
import sqlite3
import json
import logging
from pathlib import Path
from typing import Final

import httpx
import pytest

from app.opencode_route import SessionReaders
from core.backends.config_loader import BackendSpec
from core.opencode.sse import (
    CONNECTED,
    PERMISSION_ASKED,
    PERMISSION_REPLIED,
    SSE_DEFAULT_EVENT,
    TEXT_DELTA,
    TURN_COMPLETE,
    EventSource,
    OpencodeEvent,
    turn_is_complete,
)
from core.opencode.sse_frames import frames
from tests.test_sse import EventServer, _no_wait

# ---------------------------------------------------------------------------
# The captured wire, verbatim. Not one character added or removed.
# ---------------------------------------------------------------------------

#: The session the tap drove. Every body below belongs to it, so the reader's session
#: filter is exercised with a real id rather than a synthetic one.
SESSION_ID: Final = "ses_f237204c2ffew0YlIQMRoGkbrk"
FOREIGN_SESSION_ID: Final = "ses_someone_else"
PERMISSION_ID: Final = "per_0dc8e0d950015YF13XN1QQAEcz"
HEARTBEAT: Final = "server.heartbeat"
BASE_URL: Final = "http://127.0.0.1:4599"
WORKSPACE: Final = str(Path(__file__).resolve().parents[1])

CONNECTED_BODY: Final = (
    '{"id":"evt_0dc8df5060016UzTVXQwd1vYWS","type":"server.connected","properties":{}}'
)
ASKED_BODY: Final = (
    '{"id":"evt_0dc8e0d96001ENniA8TFQh84Kv","type":"permission.asked","properties":'
    '{"id":"per_0dc8e0d950015YF13XN1QQAEcz","sessionID":"ses_f237204c2ffew0YlIQMRoGkbrk",'
    '"permission":"bash","patterns":["echo R2D2D11TAP"],"metadata":{"command":"echo R2D2D11TAP"},'
    '"always":["echo *"],"tool":{"messageID":"msg_0dc8dfc20001Ay94pxsy3GeYG4",'
    '"callID":"call_function_rds009nllcpm_1"}}}'
)
REPLIED_BODY: Final = (
    '{"id":"evt_0dc8e0ddf001LGahccPzDQvuh7","type":"permission.replied","properties":'
    '{"sessionID":"ses_f237204c2ffew0YlIQMRoGkbrk","requestID":"per_0dc8e0d950015YF13XN1QQAEcz",'
    '"reply":"once"}}'
)
IDLE_BODY: Final = (
    '{"id":"evt_0dc8e15b70018OjNOY8rXtt22q","type":"session.idle","properties":'
    '{"sessionID":"ses_f237204c2ffew0YlIQMRoGkbrk"}}'
)
DELTA_BODY: Final = (
    '{"id":"evt_0dc8e1571001BFI36rsRcdO7ul","type":"message.part.delta","properties":'
    '{"sessionID":"ses_f237204c2ffew0YlIQMRoGkbrk","messageID":"msg_0dc8e0f0a001ph7zUZCNIhzq4F",'
    '"partID":"prt_0dc8e1567001rUrLg9he5sRlNi","field":"text","delta":"Raw output: `R2D2"}}'
)
HEARTBEAT_BODY: Final = (
    '{"id":"evt_0dc8e1c44001tbKs2xV3zMvYOZ","type":"server.heartbeat","properties":{}}'
)
#: One whole turn, in the order that run sent it, plus the heartbeat after it.
TURN: Final = (
    CONNECTED_BODY, ASKED_BODY, REPLIED_BODY, DELTA_BODY, IDLE_BODY, HEARTBEAT_BODY,
)


def wire(body: str) -> bytes:
    """One body as the server put it on the wire: `data:` and nothing else.

    No `event:` line, because the tap counted zero of them. This is the fixture the
    whole defect hid behind.
    """
    return f"data: {body}\n\n".encode()


def _spec() -> BackendSpec:
    return BackendSpec(
        name="opencode",
        kind="opencode_session",
        base_url=BASE_URL,
        username="opencode",
        password="R2D2_OC_PASSWORD_VALUE",
        voice_agent="r2d2-voice",
        task_agent="r2d2-agent",
        fast_model="opencode/space-bunny-free",
        task_model="opencode/space-bunny-free",
        summarize_model="opencode/space-bunny-free",
        timeout=3.2,
    )


async def _events_of(chunks: list[bytes], *, session_id: str = SESSION_ID) -> list[OpencodeEvent]:
    """Every event one connection delivers, read by the real reader."""
    client = httpx.AsyncClient(transport=httpx.MockTransport(EventServer([chunks])))
    source = EventSource(
        _spec(), WORKSPACE, session_id=session_id, client=client, sleep=_no_wait
    )
    async with client:
        return [event async for event in source.events()]


async def _from_bodies(*bodies: str) -> list[OpencodeEvent]:
    return await _events_of([wire(body) for body in bodies])


async def _from_field(event_field: str, body: str) -> list[OpencodeEvent]:
    """One frame with an `event:` line -- the spec-compliant shape."""
    return await _events_of([f"event: {event_field}\ndata: {body}\n\n".encode()])


async def _name_of(event_field: str, body: str) -> str:
    """The single `type` one frame decodes to, read through the reader rather than
    through `decode_frame`, so the precedence tests cannot pass on a decoder quirk."""
    names = [event.type for event in await _from_field(event_field, body)]
    assert len(names) == 1, f"expected one event, decoded {names}"
    return names[0]


class RecordingBroker:
    """A stand-in for `PermissionBroker` that only records what it was handed.

    Real enough for the call: `on_permission_requested`'s signature and argument order
    are the contract `SessionReaders` is written against, and the third argument is
    the id the broker answers `{"response": "once"}` with.
    """

    def __init__(self) -> None:
        self.asks: list[tuple[str, str, str, str, tuple[str, ...]]] = []

    async def on_permission_requested(
        self,
        application_id: str,
        session_id: str,
        permission_id: str,
        title: str,
        always: tuple[str, ...],
    ) -> None:
        self.asks.append((application_id, session_id, permission_id, title, always))


# ---------------------------------------------------------------------------
# The regression: real frames typed by the names they carry
# ---------------------------------------------------------------------------


async def test_a_real_permission_ask_off_the_wire_decodes_to_its_own_name() -> None:
    """The defect in one test. The body carries `"type":"permission.asked"` and no
    `event:` line exists anywhere on the stream, so a decoder reading only the field
    calls this a `"message"` -- and `app/opencode_route.py` drops it on its first
    line, so `on_permission_requested` is never called however healthy the socket."""
    # Given / When
    events = await _from_bodies(ASKED_BODY)
    # Then
    assert [event.type for event in events] == [PERMISSION_ASKED]
    assert events[0].permission_id == PERMISSION_ID
    assert events[0].session_id == SESSION_ID


async def test_every_frame_type_the_reader_dispatches_on_is_recognised_from_a_real_body() -> None:
    """One turn, six real bodies. Five names come through, and the sixth -- the
    heartbeat -- is dropped by the session filter, which is the only thing standing
    between a 10 s event and a handler; it is proved to decode by its own name in the
    test below."""
    # Given / When
    events = await _from_bodies(*TURN)
    # Then
    assert [event.type for event in events] == [
        CONNECTED,
        PERMISSION_ASKED,
        PERMISSION_REPLIED,
        TEXT_DELTA,
        TURN_COMPLETE,
    ]
    # And the accessors read the real payload rather than a fixture's idea of one
    assert events[0].session_id is None                     # server.connected has none
    assert events[1].permission_id == PERMISSION_ID
    assert events[1].always == ("echo *",)
    assert events[1].title == "echo R2D2D11TAP"             # `metadata.command`
    assert events[2].request_id == PERMISSION_ID            # matches the ask's `id`
    assert events[4].session_id == SESSION_ID


async def test_a_real_permission_ask_reaches_the_brokers_own_entry_point() -> None:
    """The headline, on captured bytes: reader, session filter, the handler the route
    installs, then `on_permission_requested` with the id the server really sent.

    `_handler` is the private seam on purpose: it IS the dispatch `app/opencode_route`
    installs per session, and nothing public hands it an event, so a test that went
    around it would prove the decoder and not the route.
    """
    # Given
    broker = RecordingBroker()
    readers = SessionReaders(_spec(), WORKSPACE, broker)  # type: ignore[arg-type]
    handle = readers._handler("r2d2:alice:owner", SESSION_ID)  # type: ignore[attr-defined]
    # When
    for event in await _from_bodies(ASKED_BODY, REPLIED_BODY, IDLE_BODY):
        await handle(event)
    # Then
    assert broker.asks == [
        ("r2d2:alice:owner", SESSION_ID, PERMISSION_ID, "echo R2D2D11TAP", ("echo *",))
    ]


async def test_a_turn_off_the_wire_is_complete_only_once_the_reply_is_there() -> None:
    """`turn_is_complete` needs the three real frames to mean anything, and with every
    frame typed `"message"` it could see neither the idle nor the reply."""
    # Given / When / Then
    assert turn_is_complete(await _from_bodies(ASKED_BODY, IDLE_BODY)) is False
    assert turn_is_complete(await _from_bodies(ASKED_BODY, REPLIED_BODY, IDLE_BODY)) is True


# ---------------------------------------------------------------------------
# The filter and the heartbeat, on real bytes
# ---------------------------------------------------------------------------


async def test_a_real_frame_of_another_session_is_still_dropped_by_the_filter() -> None:
    """Typing the frame correctly must not weaken the one filter that cannot move.
    Changing the session id is the only edit, and it is the edit a stranger's frame
    already carries."""
    # Given
    foreign = ASKED_BODY.replace(SESSION_ID, FOREIGN_SESSION_ID)
    # When
    events = await _from_bodies(foreign)
    # Then
    assert events == []


async def test_the_heartbeat_body_decodes_to_its_own_name() -> None:
    """Read by `frames()` alone, with no session filter in the way, so the name is
    shown to be read rather than inferred from what got dropped."""
    # Given
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(EventServer([[wire(HEARTBEAT_BODY)]]))
    )
    # When
    async with client:
        response = await client.get(f"{BASE_URL}/event")
        names = [event.type async for event in frames(response)]
    # Then
    assert names == [HEARTBEAT]


async def test_a_real_heartbeat_never_reaches_a_handler_and_never_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Its body is `properties:{}` -- an object, so it decodes -- and it carries no
    `sessionID`, so the filter drops it at DEBUG. It must not warn every 10 s.

    Driven through `events()` rather than `run()`: `run()` reconnects forever, so a
    regressed decoder (which would type `session.idle` as `"message"` and never set
    the stop flag) would hang this test instead of failing it.
    """
    # Given / When
    with caplog.at_level(logging.INFO):
        events = await asyncio.wait_for(
            _events_of([wire(HEARTBEAT_BODY), wire(CONNECTED_BODY), wire(IDLE_BODY)]), 2.0
        )
    # Then
    assert [event.type for event in events] == [CONNECTED, TURN_COMPLETE]
    assert [record for record in caplog.records if record.levelno >= logging.WARNING] == []


# ---------------------------------------------------------------------------
# The precedence rule, one case per test
# ---------------------------------------------------------------------------


async def test_the_event_field_names_the_frame_when_the_server_sends_one() -> None:
    """Rule 1, and the shape every pre-D11 unit test fed the decoder."""
    # Given: a body with no `type` of its own, exactly as such a server sends it
    body = f'{{"id":"evt_1","properties":{{"sessionID":"{SESSION_ID}"}}}}'
    # When
    events = await _from_field(PERMISSION_ASKED, body)
    # Then
    assert [event.type for event in events] == [PERMISSION_ASKED]


async def test_the_bodies_own_type_names_the_frame_when_the_event_field_is_absent() -> None:
    """Rule 2, and the only rule a real opencode frame reaches."""
    # Given / When
    events = await _from_bodies(ASKED_BODY)
    # Then
    assert [event.type for event in events] == [PERMISSION_ASKED]


async def test_a_frame_named_the_same_in_both_places_takes_that_name_and_stays_quiet(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Rule 1 over rule 2, agreeing: the same answer either way, and nothing to say."""
    # Given / When
    with caplog.at_level(logging.INFO):
        name = await _name_of(PERMISSION_ASKED, ASKED_BODY)
    # Then
    assert name == PERMISSION_ASKED
    assert [record for record in caplog.records if record.levelno >= logging.WARNING] == []


async def test_a_frame_that_contradicts_itself_takes_the_event_field_and_says_so(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The mismatch case. `event:` wins -- a server stating the name in the field the
    spec defines is not to be second-guessed -- and the contradiction is reported at
    WARNING, because silently preferring one of two names is how a stream that looks
    healthy stops dispatching."""
    # Given
    with caplog.at_level(logging.INFO):
        name = await _name_of("permission.v2.asked", ASKED_BODY)
    # Then
    assert name == "permission.v2.asked"
    warned = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warned) == 1
    assert "permission.v2.asked" in warned[0].getMessage()
    assert PERMISSION_ASKED in warned[0].getMessage()


async def test_a_frame_that_names_itself_nowhere_takes_the_sse_default_and_stays_quiet(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Rule 3, and the no-crash case: neither name is legal input from this server,
    but an unnamed frame is still a frame, and a reader that raised here would take
    down the listener that answers permission requests."""
    # Given
    body = json.dumps({"id": "evt_1", "properties": {"sessionID": SESSION_ID}})
    # When
    with caplog.at_level(logging.INFO):
        name = await _name_of("", body)
    # Then
    assert name == SSE_DEFAULT_EVENT
    assert [record for record in caplog.records if record.levelno >= logging.WARNING] == []


@pytest.mark.parametrize(
    "named",
    ["7", "[]", "{}", "null", '""', '" "'],
    ids=["number", "array", "object", "null", "empty-string", "blank-string"],
)
async def test_a_body_type_of_the_wrong_json_type_is_never_taken_as_a_name(named: str) -> None:
    """`payload["type"]` is read as text or not at all: a number, a list, an object or
    a blank string must fall through to the field or the default rather than become an
    event type that no handler and no constant can ever match."""
    # Given
    body = f'{{"id":"evt_1","type":{named},"properties":{{"sessionID":"{SESSION_ID}"}}}}'
    # When / Then
    assert await _name_of("", body) == SSE_DEFAULT_EVENT
    assert await _name_of(TURN_COMPLETE, body) == TURN_COMPLETE


# ---------------------------------------------------------------------------
# The three unreadable-frame paths, on real-shaped bodies
# ---------------------------------------------------------------------------


async def test_the_three_unreadable_frame_paths_still_drop_a_body_only_frame(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Unchanged, now on frames shaped like the ones the server sends: an empty `data`
    buffer dispatches nothing at all, a body that is not JSON is dropped, and a body
    whose `properties` is not an object is dropped. The next readable frame survives
    all three, which is what "a server mid-deploy must not take down the listener"
    means in practice."""
    # Given
    empty = b"data: \n\n"
    unreadable = (
        b"data: {not json\n\n",
        b'data: {"type":"permission.asked","properties":[1,2]}\n\n',
        b'data: {"type":"permission.asked","properties":null}\n\n',
    )
    # When
    with caplog.at_level(logging.INFO):
        events = await _events_of([empty, *unreadable, wire(IDLE_BODY)])
    # Then: only the one readable frame survived
    assert [event.type for event in events] == [TURN_COMPLETE]
    # And each malformed frame was reported exactly once...
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == len(unreadable)
    # ...while the empty `data` buffer stayed silent, because per the SSE dispatch
    # rules it is not a malformed frame and must not cry wolf.
    assert "unreadable JSON" in caplog.text
    assert "'properties' is not an object" in caplog.text


async def test_a_reader_that_ended_is_replaced_rather_than_counted_forever() -> None:
    """A finished reader must not hold the slot that would restart it.

    `EventSource.run` lets anything that is not an `httpx.HTTPError` or an
    `OpencodeError` escape -- `sqlite3.OperationalError` from the broker's write
    does -- and the finished task stayed in `_tasks`. `ensure` then skipped that
    session forever, so its permission asks were never brokered again, while
    `count` -- the one number an operator reads -- still said 1.
    """
    readers = SessionReaders(_spec(), WORKSPACE, RecordingBroker())  # type: ignore[arg-type]
    started: list[int] = []

    async def dead(self: object, app_id: str, session_id: str) -> None:
        started.append(1)
        raise sqlite3.OperationalError("database is locked")

    readers._read = dead.__get__(readers)  # type: ignore[attr-defined]
    readers.ensure("r2d2:alice:owner", SESSION_ID)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    # Then the reader is not being watched, and `count` says so
    assert readers.count == 0
    # ... and the next turn starts a new one
    readers.ensure("r2d2:alice:owner", SESSION_ID)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert len(started) == 2
    await readers.aclose()


async def test_a_rebound_session_does_not_orphan_the_previous_reader() -> None:
    """Readers are keyed by session id, so a rebind used to leak the old one.

    `SessionReaders._tasks` maps SESSION -> task. When `resolve` replaced a dead
    binding the new session got a new key and the previous reader stayed in the dict
    under the old one, alive and reconnecting to a session nobody was bound to. It
    was reachable only from `aclose` at shutdown, and `count` counted it, so an
    operator watching that number saw readers for sessions that no longer existed.
    """
    # Given a reader running for the session a user is currently bound to
    readers = SessionReaders(_spec(), WORKSPACE, RecordingBroker())  # type: ignore[arg-type]
    readers.ensure("alice", "ses_old")
    await asyncio.sleep(0)
    assert readers.count == 1
    # When the binding is replaced and the store drops the old session
    readers.discard("alice", "ses_old")
    readers.ensure("alice", "ses_new")
    await asyncio.sleep(0)
    # Then only the new session is watched, and the old task is finished
    assert readers.count == 1
    assert set(readers._tasks) == {"ses_new"}
    old = [t for t in asyncio.all_tasks() if "ses_old" in (t.get_name() or "")]
    assert all(t.done() for t in old)
    # The surviving reader is closed here rather than left to the loop teardown:
    # it opens a real connection, and a cancelled one surfaces as an unraisable
    # "coroutine was never awaited" warning in whichever test runs next.
    await readers.aclose()
