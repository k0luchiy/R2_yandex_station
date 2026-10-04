"""Behavioural tests for the opencode session store (plan todo 8).

R2D2 keeps **one opencode session per end user**, created on the first turn and
reused forever, so the conversation survives across turns. These tests lock the
three things that make that true, and each one is a way the store can lie:

* **One session per user.** Two `resolve()` calls -- sequential *and* concurrent
  -- must reach the server as exactly one `POST /session`. Two Alice requests
  arriving at once is the normal case, not an edge case, so the concurrent test
  is the load-bearing one here.
* **A binding is a claim about the server, not about the database.** A row whose
  `session_id` the server has never heard of is a wedge: every later turn would
  be posted into a session that does not exist. `resolve` re-verifies against
  `GET /session` and replaces a dead binding instead of returning its id.
* **A crashed run must not wedge a session forever.** `reap()` aborts a session
  that is *busy* and *stale*, and nothing else: never an idle session, never a
  fresh one, and never a turn that is merely long.

**C6** -- `?directory=` is a QUERY parameter the server does not validate, so
`resolve()` refuses a missing workspace itself, before any HTTP call, instead of
trusting the client's per-request check. **C7** -- `GET /session/:id/message`
returns `[{info, parts}]`; nothing here reads messages, but the route is listed
so the fake stays an honest stand-in for the server.

The double is the **real** `OpencodeClient` over `httpx.MockTransport` (an
HTTP-level fake, not a mocked client), so the title that goes out, the
`?directory=` that goes out and the number of `POST /session` calls are all
observed on the wire. Nothing touches a network -- proven by re-running the
suite under `-p no_net`.

Timing: no test sleeps. A stale binding is produced by moving `last_used_at` in
the database, which is the only clock input the reaper reads.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from dataclasses import FrozenInstanceError

import httpx
import pytest

from app.config import Config
from core.backends.config_loader import BackendSpec
from core.memory import Memory, OcSession
from core.opencode.client import (
    CATALOGUE_TIMEOUT_S,
    TRANSCRIPT_TIMEOUT_S,
    OpencodeClient,
    OpencodeError,
)
from core.opencode.session_store import BUSY, OcSessionStore

BASE_URL = "http://127.0.0.1:4599"
APP = "alice-123"
OTHER_APP = "bob-456"
STALE_AFTER_S = 900.0


class FakeOpencode:
    """`opencode serve` as a `MockTransport` handler -- the routes the store uses.

    `GET /session`, `POST /session`, `GET /session/status` and
    `POST /session/:id/abort` behave like the real server, including the 404 an
    abort gets for a session the server has since dropped. `status` is kept
    separate from `sessions` on purpose: a session can be *reported busy* and
    still be unknown to `GET /session`, which is exactly the race the reaper
    has to survive.
    """

    def __init__(self) -> None:
        self.sessions: dict[str, dict[str, str]] = {}
        self.status: dict[str, str] = {}
        self.creates = 0
        self.aborted: list[str] = []
        self.requests: list[httpx.Request] = []
        self.failing = False
        self.fail_status = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        method, path = request.method, request.url.path
        if self.failing:
            return httpx.Response(500, json={"error": "server exploded"})
        if method == "GET" and path == "/session":
            return httpx.Response(200, json=list(self.sessions.values()))
        if method == "POST" and path == "/session":
            return self._create(request)
        if method == "GET" and path == "/session/status":
            if self.fail_status:
                return httpx.Response(500, json={"error": "no status for you"})
            return httpx.Response(200, json={sid: {"type": st} for sid, st in self.status.items()})
        if method == "POST" and path.startswith("/session/") and path.endswith("/abort"):
            return self._abort(path[len("/session/") : -len("/abort")])
        return httpx.Response(404, json={"error": f"no route for {method} {path}"})

    def client(self, workspace: str) -> OpencodeClient:
        spec = BackendSpec(name="opencode", kind="opencode_session", base_url=BASE_URL, timeout=1.0)
        transport = httpx.MockTransport(self)
        return OpencodeClient(spec, workspace, client=httpx.AsyncClient(transport=transport))

    def put(self, session_id: str, title: str, *, status: str = "idle") -> str:
        """A session that already exists server-side, with a projected status."""
        self.sessions[session_id] = {"id": session_id, "title": title, "directory": ""}
        self.status[session_id] = status
        return session_id

    def creates_on_wire(self) -> list[httpx.Request]:
        return [
            request
            for request in self.requests
            if request.method == "POST" and request.url.path == "/session"
        ]

    def sent_titles(self) -> list[str]:
        return [json.loads(request.content)["title"] for request in self.creates_on_wire()]

    def _create(self, request: httpx.Request) -> httpx.Response:
        self.creates += 1
        session_id = f"ses_{self.creates}"
        self.sessions[session_id] = {
            "id": session_id,
            "title": json.loads(request.content)["title"],
            "directory": request.url.params.get("directory", ""),
        }
        return httpx.Response(200, json=self.sessions[session_id])

    def _abort(self, session_id: str) -> httpx.Response:
        if session_id not in self.sessions:
            return httpx.Response(404, json={"error": "session not found"})
        self.aborted.append(session_id)
        return httpx.Response(200, json=True)


def age(memory: Memory, application_id: str, *, seconds: float) -> None:
    """Move a binding's `last_used_at` into the past -- the reaper's only clock."""
    with sqlite3.connect(memory.db_path) as database:
        database.execute(
            "UPDATE oc_sessions SET last_used_at = ? WHERE application_id = ?",
            (time.time() - seconds, application_id),
        )


@pytest.fixture
def db_path(tmp_path) -> str:
    return str(tmp_path / "sessions.db")


@pytest.fixture
async def memory(db_path: str):
    connected = await Memory(db_path).connect()
    try:
        yield connected
    finally:
        await connected.close()


@pytest.fixture
def server() -> FakeOpencode:
    return FakeOpencode()


@pytest.fixture
def workspace(tmp_path) -> str:
    path = tmp_path / "r2d2-workspace"
    path.mkdir()
    return str(path)


@pytest.fixture
def cfg(workspace: str) -> Config:
    return Config(r2d2_workspace=workspace, r2d2_stale_session_seconds=STALE_AFTER_S)


@pytest.fixture
def store(memory: Memory, server: FakeOpencode, cfg: Config) -> OcSessionStore:
    return OcSessionStore(memory, server.client(cfg.r2d2_workspace), cfg)


# ---------------------------------------------------------------------------
# core.memory -- the table and its accessors
# ---------------------------------------------------------------------------


async def test_bind_then_get_returns_the_same_session_id(memory: Memory):
    # Given: nothing bound yet
    # When: an application is bound to a session
    await memory.bind_oc_session(APP, "ses_1", f"r2d2:alice:{APP}")
    # Then: the binding reads back with that exact id
    binding = await memory.get_oc_session(APP)
    assert binding is not None
    assert binding.session_id == "ses_1"
    assert binding.title == f"r2d2:alice:{APP}"
    assert binding.message_count == 0


async def test_get_oc_session_of_an_unbound_application_is_none(memory: Memory):
    # Given: an empty table
    # When: an unknown application is read
    binding = await memory.get_oc_session(APP)
    # Then: there is no binding, not a zeroed one
    assert binding is None


async def test_touch_increments_and_returns_one_two_three(memory: Memory):
    # Given: a fresh binding
    await memory.bind_oc_session(APP, "ses_1", f"r2d2:alice:{APP}")
    # When: three messages land in it
    counts = [await memory.touch_oc_session(APP, message_delta=1) for _ in range(3)]
    # Then: each call returns the NEW count, and the stored count agrees
    assert counts == [1, 2, 3]
    binding = await memory.get_oc_session(APP)
    assert binding is not None and binding.message_count == 3


async def test_touch_with_a_zero_delta_refreshes_the_clock_without_counting(memory: Memory):
    # Given: a binding that already holds two messages
    await memory.bind_oc_session(APP, "ses_1", f"r2d2:alice:{APP}")
    await memory.touch_oc_session(APP, message_delta=2)
    before = await memory.get_oc_session(APP)
    assert before is not None
    # When: the session is merely re-verified (no message sent)
    count = await memory.touch_oc_session(APP, message_delta=0)
    after = await memory.get_oc_session(APP)
    # Then: the count is unchanged but the session is no longer stale
    assert count == 2
    assert after is not None
    assert after.message_count == 2
    assert after.last_used_at >= before.last_used_at


async def test_touch_of_an_unbound_application_returns_zero(memory: Memory):
    # Given: an application that was never bound
    # When: a message count is asked for anyway
    count = await memory.touch_oc_session(APP, message_delta=1)
    # Then: nothing was invented -- a caller routing the first turn learns the count is zero
    assert count == 0
    assert await memory.all_oc_sessions() == []


async def test_touch_rejects_a_negative_message_delta(memory: Memory):
    # Given: a real binding
    await memory.bind_oc_session(APP, "ses_1", f"r2d2:alice:{APP}")
    # When: a caller claims messages were removed
    # Then: it is refused, because a negative count would read as "brand new session"
    with pytest.raises(ValueError, match="message_delta"):
        await memory.touch_oc_session(APP, message_delta=-1)
    binding = await memory.get_oc_session(APP)
    assert binding is not None and binding.message_count == 0


async def test_bind_resets_the_count_when_the_session_id_changes(memory: Memory):
    # Given: a replacement for a session that carried a full context
    await memory.bind_oc_session(APP, "ses_old", f"r2d2:alice:{APP}")
    await memory.touch_oc_session(APP, message_delta=7)
    # When: the dead session is replaced by a new one under the same application
    await memory.bind_oc_session(APP, "ses_new", f"r2d2:alice:{APP}")
    binding = await memory.get_oc_session(APP)
    # Then: the fresh session starts empty, so it is not mistaken for a warm one
    assert binding is not None
    assert binding.session_id == "ses_new"
    assert binding.message_count == 0


async def test_rebinding_the_same_session_keeps_its_count(memory: Memory):
    # Given: a live session that has taken messages
    await memory.bind_oc_session(APP, "ses_1", f"r2d2:alice:{APP}")
    await memory.touch_oc_session(APP, message_delta=3)
    # When: the same session is bound again (an idempotent re-verify)
    await memory.bind_oc_session(APP, "ses_1", f"r2d2:alice:{APP}")
    binding = await memory.get_oc_session(APP)
    # Then: the accumulated context is not thrown away
    assert binding is not None and binding.message_count == 3


async def test_all_oc_sessions_returns_every_binding(memory: Memory):
    # Given: two users with sessions
    await memory.bind_oc_session(APP, "ses_1", f"r2d2:alice:{APP}")
    await memory.bind_oc_session(OTHER_APP, "ses_2", f"r2d2:alice:{OTHER_APP}")
    # When: the reaper asks for the whole population
    bindings = await memory.all_oc_sessions()
    # Then: both are there, each with its own application id
    assert {binding.session_id for binding in bindings} == {"ses_1", "ses_2"}
    assert {binding.application_id for binding in bindings} == {APP, OTHER_APP}


async def test_unbind_removes_the_row_and_get_returns_none(memory: Memory):
    # Given: a bound application
    await memory.bind_oc_session(APP, "ses_1", f"r2d2:alice:{APP}")
    # When: the binding is released
    await memory.unbind_oc_session(APP)
    # Then: the row is gone, not merely emptied
    assert await memory.get_oc_session(APP) is None
    assert await memory.all_oc_sessions() == []


async def test_bindings_survive_a_reconnect(db_path: str):
    # Given: a binding written by one process
    first = await Memory(db_path).connect()
    await first.bind_oc_session(APP, "ses_1", f"r2d2:alice:{APP}")
    await first.touch_oc_session(APP, message_delta=2)
    await first.close()
    # When: a new process opens the same file (CREATE TABLE IF NOT EXISTS, no migrations)
    second = await Memory(db_path).connect()
    try:
        binding = await second.get_oc_session(APP)
    finally:
        await second.close()
    # Then: the table and the row are both still there
    assert binding is not None
    assert (binding.application_id, binding.session_id, binding.message_count) == (APP, "ses_1", 2)


def test_oc_session_is_a_frozen_row_value():
    # Given: a binding read out of the table
    binding = OcSession(APP, "ses_1", f"r2d2:alice:{APP}", 0, created_at=1.0, last_used_at=2.0)
    # When: a caller tries to rewrite it in place
    # Then: it cannot -- two holders of the same row can never disagree
    with pytest.raises(FrozenInstanceError):
        binding.session_id = "ses_other"


# ---------------------------------------------------------------------------
# OcSessionStore.resolve -- one session per user, forever
# ---------------------------------------------------------------------------


async def test_two_resolves_for_one_application_create_exactly_one_session(
    store: OcSessionStore, server: FakeOpencode
):
    # Given: a user R2D2 has never talked to
    # When: the same user is resolved twice
    first = await store.resolve(APP)
    second = await store.resolve(APP)
    # Then: the server saw one creation and both callers got that id
    assert first == second
    assert len(server.creates_on_wire()) == 1
    assert (await store.message_count(APP)) == 0


async def test_concurrent_resolves_for_one_application_create_exactly_one_session(
    store: OcSessionStore, server: FakeOpencode
):
    # Given: Alice delivers two requests at the same instant
    # When: both are resolved concurrently
    first, second = await asyncio.gather(store.resolve(APP), store.resolve(APP))
    # Then: they share one opencode session, and the loser re-verified instead of creating
    assert first == second
    assert len(server.creates_on_wire()) == 1
    assert [request.method for request in server.requests if request.url.path == "/session"] == [
        "POST",
        "GET",
    ]


async def test_the_created_session_is_titled_after_the_application(
    store: OcSessionStore, server: FakeOpencode
):
    # Given: an application id from Yandex
    # When: its session is created
    await store.resolve(APP)
    # Then: the title on the wire is exactly the agreed one
    assert server.sent_titles() == ["r2d2:alice:alice-123"]


async def test_the_workspace_is_sent_as_the_directory_query_parameter(
    store: OcSessionStore, server: FakeOpencode, workspace: str
):
    # Given: R2D2's configured workspace
    # When: a session is created in it
    await store.resolve(APP)
    # Then: it travels as `?directory=` (C6), never as a body key
    create = server.creates_on_wire()[0]
    assert create.url.params["directory"] == workspace
    assert json.loads(create.content) == {"title": f"r2d2:alice:{APP}"}


async def test_resolve_replaces_a_dead_session_once_and_names_it_in_a_warning(
    store: OcSessionStore, server: FakeOpencode, caplog, memory: Memory
):
    # Given: a binding whose session the server no longer has. The listing is left
    # POPULATED on purpose -- an empty listing is not proof of death (see the test
    # below), so "dead" has to be expressed the way a real server expresses it:
    # a list of sessions that does not include ours.
    await store.resolve(APP)
    dead_id = (await memory.get_oc_session(APP)).session_id
    server.sessions.clear()
    server.status.clear()
    server.put("ses_unrelated", "someone else's session")
    # When: the user speaks again
    with caplog.at_level(logging.WARNING, logger="core.opencode.session_store"):
        replacement = await store.resolve(APP)
    # Then: a fresh session replaces the dead one, once, and the loss is logged
    assert replacement != dead_id
    assert len(server.creates_on_wire()) == 2
    binding = await memory.get_oc_session(APP)
    assert binding is not None and binding.session_id == replacement
    assert dead_id in caplog.text


async def test_an_empty_session_listing_does_not_replace_a_bound_session(
    store: OcSessionStore, server: FakeOpencode, memory: Memory
):
    """`GET /session` returning nothing is not evidence that our session died.

    A server that has not finished restoring, a `?directory=` scope that does not
    match, and a truncated answer all look exactly like an empty list. Replacing
    the binding on that evidence reset `message_count` and dropped the user's
    conversation without a word, so the binding is trusted instead.
    """
    # Given: a live binding, and a server that suddenly lists no sessions at all
    bound = await store.resolve(APP)
    server.sessions.clear()
    server.status.clear()
    # When: the user speaks again
    resolved = await store.resolve(APP)
    # Then: the same session is returned, and nothing was created
    assert resolved == bound
    assert len(server.creates_on_wire()) == 1
    binding = await memory.get_oc_session(APP)
    assert binding is not None and binding.session_id == bound


async def test_resolve_adopts_a_server_session_rebound_under_our_title(
    store: OcSessionStore, server: FakeOpencode, memory: Memory
):
    # Given: our title exists server-side under a different id (the DB was lost, not the session)
    server.put("ses_moved", f"r2d2:alice:{APP}")
    await memory.bind_oc_session(APP, "ses_gone", f"r2d2:alice:{APP}")
    # When: the user speaks
    resolved = await store.resolve(APP)
    # Then: the live session is adopted rather than duplicated
    assert resolved == "ses_moved"
    assert server.creates_on_wire() == []
    binding = await memory.get_oc_session(APP)
    assert binding is not None and binding.session_id == "ses_moved"


async def test_resolve_refreshes_the_clock_of_a_reused_session(
    store: OcSessionStore, server: FakeOpencode, memory: Memory
):
    # Given: a session that has gone stale in the table
    await store.resolve(APP)
    age(memory, APP, seconds=STALE_AFTER_S * 10)
    stale = await memory.get_oc_session(APP)
    # When: the user speaks again without the session having been re-created
    await store.resolve(APP)
    refreshed = await memory.get_oc_session(APP)
    # Then: it is fresh again, so the reaper will not judge a live session stale
    assert stale is not None and refreshed is not None
    assert refreshed.last_used_at > stale.last_used_at
    assert refreshed.session_id == stale.session_id


async def test_resolve_refuses_a_missing_workspace_before_any_http_call(
    memory: Memory, server: FakeOpencode, tmp_path
):
    # Given: a workspace path that does not exist -- the server does not validate it (C6)
    cfg = Config(r2d2_workspace=str(tmp_path / "absent"), r2d2_stale_session_seconds=STALE_AFTER_S)
    store = OcSessionStore(memory, server.client(cfg.r2d2_workspace), cfg)
    # When: a session is resolved
    with pytest.raises(OpencodeError, match="absent"):
        await store.resolve(APP)
    # Then: nothing was sent, and nothing was bound
    assert server.requests == []
    assert await memory.all_oc_sessions() == []


async def test_resolve_propagates_a_server_failure_and_binds_nothing(
    store: OcSessionStore, server: FakeOpencode, memory: Memory
):
    # Given: a server that fails every call
    server.failing = True
    # When: a session is resolved
    with pytest.raises(OpencodeError):
        await store.resolve(APP)
    # Then: there is no half-created state -- the table is still empty
    assert await memory.all_oc_sessions() == []


# ---------------------------------------------------------------------------
# OcSessionStore.reap -- the crashed-run reaper
# ---------------------------------------------------------------------------


async def test_reap_aborts_a_busy_stale_session(
    store: OcSessionStore, server: FakeOpencode, memory: Memory
):
    # Given: a turn that started long ago and is still running
    await store.resolve(APP)
    session_id = (await memory.get_oc_session(APP)).session_id
    server.status[session_id] = "busy"
    age(memory, APP, seconds=STALE_AFTER_S + 1)
    # When: the reaper sweeps
    aborted = await store.reap()
    # Then: the wedged turn is stopped
    assert aborted == 1
    assert server.aborted == [session_id]


async def test_reap_leaves_a_busy_fresh_session_running(
    store: OcSessionStore, server: FakeOpencode, memory: Memory
):
    # Given: a long turn that started a moment ago (C8: the first turn is slow by nature)
    await store.resolve(APP)
    session_id = (await memory.get_oc_session(APP)).session_id
    server.status[session_id] = "busy"
    # When: the reaper sweeps
    aborted = await store.reap()
    # Then: a slow turn is not a dead turn
    assert aborted == 0
    assert server.aborted == []


async def test_reap_aborts_nothing_when_every_session_is_idle(
    store: OcSessionStore, server: FakeOpencode, memory: Memory
):
    # Given: a user between turns
    await store.resolve(APP)
    age(memory, APP, seconds=STALE_AFTER_S * 10)
    # When: the reaper sweeps
    aborted = await store.reap()
    # Then: an idle session is left alone
    assert aborted == 0
    assert server.aborted == []


async def test_reap_returns_the_number_of_aborts(
    store: OcSessionStore, server: FakeOpencode, memory: Memory
):
    # Given: three wedged sessions across three users
    for application_id in ("a-1", "b-2", "c-3"):
        await store.resolve(application_id)
        age(memory, application_id, seconds=STALE_AFTER_S + 1)
    for session_id in server.sessions:
        server.status[session_id] = "busy"
    # When: the reaper sweeps
    aborted = await store.reap()
    # Then: every one of them is counted
    assert aborted == 3
    assert len(server.aborted) == 3


async def test_reap_with_no_bound_sessions_never_calls_the_server(
    store: OcSessionStore, server: FakeOpencode
):
    # Given: no user has ever been bound
    # When: the reaper sweeps
    aborted = await store.reap()
    # Then: it is a no-op that costs no request
    assert aborted == 0
    assert server.requests == []


async def test_reap_survives_a_session_the_server_has_never_heard_of(
    store: OcSessionStore, server: FakeOpencode, memory: Memory, caplog
):
    # Given: one session the server reports busy but no longer has, and one real wedged session
    server.status["ses_ghost"] = "busy"
    await memory.bind_oc_session(APP, "ses_ghost", f"r2d2:alice:{APP}")
    await store.resolve(OTHER_APP)
    live = (await memory.get_oc_session(OTHER_APP)).session_id
    server.status[live] = "busy"
    age(memory, APP, seconds=STALE_AFTER_S + 1)
    age(memory, OTHER_APP, seconds=STALE_AFTER_S + 1)
    # When: the reaper sweeps
    with caplog.at_level(logging.WARNING, logger="core.opencode.session_store"):
        aborted = await store.reap()
    # Then: the dead one is logged, the loop continues, and the other row survives
    assert aborted == 1
    assert server.aborted == [live]
    assert "ses_ghost" in caplog.text
    assert len(await memory.all_oc_sessions()) == 2


async def test_reap_returns_zero_when_the_status_call_fails(
    store: OcSessionStore, server: FakeOpencode, memory: Memory, caplog
):
    # Given: a server that cannot report any status
    await store.resolve(APP)
    age(memory, APP, seconds=STALE_AFTER_S + 1)
    server.fail_status = True
    # When: the reaper sweeps
    with caplog.at_level(logging.WARNING, logger="core.opencode.session_store"):
        aborted = await store.reap()
    # Then: the sweep reports that it did nothing instead of aborting on a guess
    assert aborted == 0
    assert server.aborted == []
    assert "/session/status" in caplog.text


# ---------------------------------------------------------------------------
# message_count -- what todo 15 needs to know before its first turn
# ---------------------------------------------------------------------------


async def test_message_count_is_zero_for_a_brand_new_session(
    store: OcSessionStore, memory: Memory
):
    # Given: a session created this instant (C8: its first turn costs 15.5-18.6s)
    await store.resolve(APP)
    # When: the caller asks how much context it has
    count = await store.message_count(APP)
    # Then: zero, which is the signal to route the first turn asynchronously
    assert count == 0
    binding = await memory.get_oc_session(APP)
    assert binding is not None and binding.message_count == 0


async def test_message_count_of_an_unbound_application_is_zero(
    store: OcSessionStore, memory: Memory
):
    # Given: an application with no session at all
    # When: the caller asks how much context it has
    count = await store.message_count(APP)
    # Then: zero rather than an error -- the fast path must not be blocked by bookkeeping
    assert count == 0
    assert await memory.all_oc_sessions() == []


async def test_the_provider_catalogue_read_does_not_borrow_the_voice_deadline(
    tmp_path: Path,
) -> None:
    """A cold catalogue is a startup read, not a voice turn, and it is not on Alice's clock.

    Measured, not reasoned: with the bound taken from `spec.timeout` -- the shipped
    3.2 s, which is the answer to "how long may I wait to SPEAK" -- a server started
    36 s earlier accepted `GET /config/providers` and did not answer inside it.
    Startup then logged `catalogue-unreadable`, left the brain route UNWIRED, and
    every question fell through to a chain with no model behind it.

    The control assertion matters as much as the first: a call that *is* a voice
    turn must still get the small bound, or this would pass for the wrong reason --
    a client whose every call got 20 s would satisfy it too.
    """
    seen: dict[str, float | None] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        seen[path] = request.extensions.get("timeout", {}).get("read")
        if path == "/config/providers":
            return httpx.Response(200, json={"providers": [], "default": {}})
        return httpx.Response(200, json=[])  # /agent is a bare list route

    # A bound so small that borrowing it would be unmistakable, and so small that
    # a real request would fail if it were actually applied to the socket.
    spec = BackendSpec(name="opencode", kind="opencode_session", base_url=BASE_URL, timeout=0.01)
    client = OpencodeClient(
        spec, str(tmp_path), client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    # When: the startup catalogue read
    await client.providers()
    # Then: it carries its own bound, not the voice deadline
    assert seen["/config/providers"] == CATALOGUE_TIMEOUT_S
    # ... and a voice turn still gets the small one, so only the catalogue moved
    await client.agents()
    assert seen["/agent"] == spec.timeout


async def test_the_collector_transcript_read_does_not_borrow_the_voice_deadline(
    tmp_path: Path,
) -> None:
    """A transcript read is a collector read, not a voice turn, and it is not on Alice's clock.

    Measured twice in one live run (`qa/live-run-v12.md` finding 2):
    `GET /session/:id/message` inside the collector did not answer inside the
    voice bound, so the whole job failed with `did not answer within ...s` and a
    body saying so. The same defect already fixed for the provider catalogue
    (`CATALOGUE_TIMEOUT_S`): a read that is not a voice turn must not borrow the
    voice deadline, because it is not on Alice's clock -- the collector runs in
    the background with a 600 s ceiling of its own.

    The control assertion matters as much as the first: a call that *is* a voice
    turn must still get the small bound, or this would pass for the wrong reason --
    a client whose every call got 20 s would satisfy it too.
    """
    seen: dict[str, float | None] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        seen[path] = request.extensions.get("timeout", {}).get("read")
        if path.endswith("/message") and request.method == "GET":
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=[])  # /agent is a bare list route

    # A bound so small that borrowing it would be unmistakable, and so small that
    # a real request would fail if it were actually applied to the socket.
    spec = BackendSpec(name="opencode", kind="opencode_session", base_url=BASE_URL, timeout=0.01)
    client = OpencodeClient(
        spec, str(tmp_path), client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    # When: the collector's transcript read
    await client.list_messages("ses_1")
    # Then: it carries its own bound, not the voice deadline
    assert seen["/session/ses_1/message"] == TRANSCRIPT_TIMEOUT_S
    # ... and a voice-path read still gets the small one, so only the transcript moved
    await client.agents()
    assert seen["/agent"] == spec.timeout


async def test_append_message_takes_the_lock_once_for_the_whole_update(tmp_path) -> None:
    """The read and the write must be ONE critical section.

    Two turns for one person can be in flight at once -- a Station and the phone,
    or a webhook retry arriving beside the original -- and the history is stored as
    one JSON blob, so appending is a read-modify-write. Taking the lock for the
    read and again for the write leaves a window between them in which both turns
    read the same history and the second write drops the first turn's message.
    Counting acquisitions pins the invariant without depending on interleaving.
    """
    memory = await Memory(str(tmp_path / "sessions.db")).connect()
    try:
        acquisitions = 0
        real = memory._lock

        class Counting:
            async def __aenter__(self) -> None:
                nonlocal acquisitions
                await real.acquire()
                acquisitions += 1

            async def __aexit__(self, *exc: object) -> None:
                real.release()

        memory._lock = Counting()  # type: ignore[assignment]
        await memory.append_message("u", "user", "привет")
        # Then one acquisition, not two
        assert acquisitions == 1
        assert [e["content"] for e in await memory.load_history("u")] == ["привет"]
    finally:
        memory._lock = real  # type: ignore[assignment]
        await memory.close()


async def test_two_concurrent_appends_both_land(tmp_path) -> None:
    # Given one person whose gateway is answering two turns at once
    memory = await Memory(str(tmp_path / "sessions.db")).connect()
    try:
        # When both append before either is read back
        await asyncio.gather(
            memory.append_message("u", "user", "первый"),
            memory.append_message("u", "assistant", "второй"),
        )
        # Then the history holds both, in some order
        assert {e["content"] for e in await memory.load_history("u")} == {"первый", "второй"}
    finally:
        await memory.close()


async def test_a_binding_unused_past_retention_is_forgotten(
    store: OcSessionStore, server: FakeOpencode, memory: Memory
):
    """`oc_sessions` grew one row per identity forever: nothing ever unbound.

    `unbind_oc_session` existed and had no production caller, so every test run,
    every throwaway identity and every abandoned experiment left its row behind.
    This is the only caller, and the sweep is the only thing that makes forgetting
    possible.
    """
    # Given a binding nobody has spoken through in longer than the window
    await store.resolve(APP)
    binding = await memory.get_oc_session(APP)
    assert binding is not None
    age(memory, APP, seconds=40 * 86_400)
    # When the sweep runs
    forgotten = await store.forget_unused()
    # Then the row is gone
    assert forgotten == 1
    assert await memory.get_oc_session(APP) is None


async def test_retention_never_unbinds_a_session_that_is_still_busy(
    store: OcSessionStore, server: FakeOpencode, memory: Memory
):
    # Given a long-idle binding whose session is BUSY on the server
    await store.resolve(APP)
    binding = await memory.get_oc_session(APP)
    assert binding is not None
    age(memory, APP, seconds=40 * 86_400)
    server.status[binding.session_id] = BUSY
    # When the sweep runs
    forgotten = await store.forget_unused()
    # Then nothing was forgotten: `last_used_at` is refreshed by reuse but a turn
    # in flight has not reused it yet, and dropping that binding orphans a live turn
    assert forgotten == 0
    assert await memory.get_oc_session(APP) is not None


async def test_an_ordinary_gap_between_two_questions_keeps_the_session(
    store: OcSessionStore, server: FakeOpencode, memory: Memory
):
    # Given a binding last used a month ago -- well inside the default window
    await store.resolve(APP)
    binding = await memory.get_oc_session(APP)
    assert binding is not None
    age(memory, APP, seconds=29 * 86_400)
    # When the sweep runs
    forgotten = await store.forget_unused()
    # Then the user keeps their conversation
    assert forgotten == 0
    assert await memory.get_oc_session(APP) is not None
