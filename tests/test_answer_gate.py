"""What the worker may show a person as "the agent's answer" -- D13 and D6-residue.

Alice's webhook is gone by the time an escalating turn's answer exists, so the answer
travels through `opencode_reply` and this file is the gate on that path. It is the pair of
defects the third live run measured on one turn, in one session, against a real
`opencode serve` v1.18.32 and the real model:

* **D13 -- the plan shipped instead of the result.** `r2d2-agent` wrote `"Сделаю. Сначала
  посмотрю, что доступно в окружении."`, then reached for a tool, and opencode raised
  `permission.asked` and blocked the turn. The broker put the question in Telegram and the
  collector read the silence as the end of the turn: the job closed after 6.5 s with the
  narration, the digest the agent went on to write reached nobody, and two `bash` tools
  were left in `error` because their answers were refused (`qa/live-run-v3.md` §D13, job
  `720cdccb64b7`). A silence rule cannot tell *finished* from *blocked on a question R2D2
  itself raised*, and that is the one situation this project creates.
* **D6-residue -- a routing signal shipped as an answer.** On the `deadline` branch the
  sweep cannot see the voice agent's `[[NEEDS_AGENT]]` (it is still being written), the
  agent reads it as its own history and emits one itself, and that echo was the first
  message after the collector's anchor. The user received a stripped echo of their own
  question instead of the requested essay (job `e0974a5334ac`).

**What is real here.** The real `Worker`, the real `OpencodeSessionBackend` and the real
`OpencodeClient` over an `httpx.MockTransport` that serves a transcript which changes as
the turn proceeds, and the real `TurnWatch` fed frames in the shape the live server sends
them (D11: the name is in the body's `type`, and `properties.tool.messageID` is present).
Telegram is the only double, and it is the boundary being asserted. Nothing sleeps: the
transcript advances on poll count, exactly as the real one advances on the agent's work,
and every wait is a subscription to a message the gate sent.

**The signal, and why it is not `session.idle`.** `TurnWatch` records the ask/reply pair
and nothing else. `session.idle` is the only event that closes a turn, but the contract
records that the server sends it *even while a tool is blocked on a permission answer* --
which is why `core/opencode/sse_frames.turn_is_complete` needs a conjunction -- so an idle
frame cannot answer the question this gate has, which is "is the text in hand the plan or
the result?". An unanswered ask answers it exactly.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterator
from types import MappingProxyType
from typing import Final

import httpx
import pytest
import respx

from app.config import Config
from core.answer_gate import NO_REPLY
from core.async_worker import Worker
from core.backends.config_loader import BackendSpec
from core.backends.opencode_session import OpencodeSessionBackend, OpencodeWiring
from core.memory import Memory
from core.opencode.client import OpencodeClient
from core.opencode.sse import OpencodeEvent
from core.opencode.turn_watch import TurnWatch

# ---------------------------------------------------------------------------
# The vocabulary, read off the shipped config
# ---------------------------------------------------------------------------

SENTINEL: Final = Config().r2d2_needs_agent_sentinel
#: The agent's plan, in the message that also holds the blocked tool call. Verbatim from
#: the live run: the model writes English even when the user writes Russian.
NARRATION: Final = "I'll run those two commands."
#: The result the user actually asked for, and the only thing they may be shown.
REPORT: Final = "Обе команды выполнены: каталог пуст, uptime 4 минуты."
#: The routing signal the voice agent's dead turn left behind, and the agent's echo of it.
ECHO: Final = f"{SENTINEL}\nНаписать развёрнутый обзор из пяти абзацев."

APP: Final = "alice-app-1"
SESSION: Final = "ses_gate"
ASKED: Final = "per_1"
#: The live frame's `properties.tool.messageID`: the assistant message that holds the call.
BLOCKED_MESSAGE: Final = "msg_narration"
USER_QUESTION: Final = "msg_question"
TASK_MESSAGE: Final = "msg_task"

#: Short enough that the suite pays nothing, and the parked wait ends when the test wants
#: it to end. The production value is 300 s and the gate adds one poll interval to it.
PERMISSION_TIMEOUT_S: Final = 0.2
POLL_S: Final = 0.02
NOW_S: Final = 5.0


def frame(kind: str, properties: dict[str, object]) -> OpencodeEvent:
    """One decoded frame, as the reader hands it over: the name the body carried."""
    return OpencodeEvent(type=kind, properties=MappingProxyType(dict(properties)))


def ask_frame(message_id: str = BLOCKED_MESSAGE) -> OpencodeEvent:
    """A `permission.asked` exactly as the tap recorded it, tool message and all."""
    return frame(
        "permission.asked",
        {
            "id": ASKED,
            "sessionID": SESSION,
            "permission": "bash",
            "patterns": ["ls -la /tmp"],
            "metadata": {"command": "ls -la /tmp"},
            "always": ["ls *"],
            "tool": {"messageID": message_id, "callID": "call_1"},
        },
    )


def replied_frame() -> OpencodeEvent:
    """A `permission.replied`, which is what the broker's answer produces on the wire."""
    return frame("permission.replied", {"sessionID": SESSION, "requestID": ASKED, "reply": "once"})


def idle_frame() -> OpencodeEvent:
    """A `session.idle` for this session, verbatim in shape from the live tap.

    The only event that closes a turn, and the one the collector waits for: measured on
    1.18.32, it carries `properties.sessionID`, and the same tap showed the server sends
    it even while a tool is blocked -- which is why the gate needs it together with the
    ask/reply pair rather than instead of it.
    """
    return frame("session.idle", {"sessionID": SESSION})


def envelope(message_id: str, role: str, text: str) -> dict[str, object]:
    """One `{info, parts}` message, the shape C7 measured."""
    return {"info": {"id": message_id, "role": role}, "parts": [{"type": "text", "text": text}]}


class Telegram:
    """The real `send_message` behind `respx`: the message the user would read."""

    def __init__(self) -> None:
        self.router = respx.mock(assert_all_called=False, base_url="https://api.telegram.org")
        self.route = self.router.post(path="/botTESTTOKEN/sendMessage").mock(
            side_effect=self._reply
        )
        self.sent: list[str] = []
        self.delivered = asyncio.Event()

    def _reply(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(str(json.loads(request.content)["text"]))
        self.delivered.set()
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(self.sent)}})


class Transcript:
    """A session whose assistant messages appear as the turn proceeds.

    The live server holds a turn open and writes its answer afterwards, so the read the
    collector makes returns something different each time. `writes` is the schedule: poll N
    delivers `writes[N]` and everything before it, which is what lets a test put the
    narration in front of the collector and the report behind it without a sleep.
    """

    def __init__(self, writes: list[list[dict[str, object]]]) -> None:
        self.writes = writes
        self.polls = 0
        self.deleted: list[str] = []
        #: Set once a read has returned exactly what the read before it returned, which is
        #: how the real collector's silence rule ends. A test that must act BETWEEN what the
        #: agent wrote and what it wrote next waits for this instead of for a duration.
        self.quiet = asyncio.Event()
        #: Set when a new message has been published, i.e. when the agent has done
        #: something. A test that must act at that moment -- the turn ending -- waits for
        #: this rather than for a duration.
        self.published = asyncio.Event()
        self._last: object = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            self.deleted.append(request.url.path.rsplit("/", 1)[-1])
            return httpx.Response(200, json=True)
        if request.url.path.endswith("/message") and request.method == "GET":
            index = min(self.polls, len(self.writes) - 1)
            self.polls += 1
            payload = self.writes[index]
            if payload != self._last:
                self.published.set()
                self.quiet.clear()
            else:
                self.quiet.set()
            self._last = payload
            return httpx.Response(200, json=payload)
        return httpx.Response(200, json=True)

    def with_answer(self, poll: int) -> Transcript:
        """The same transcript, with the agent's report landing on read number `poll`."""
        return self._with(envelope("msg_report", "assistant", REPORT), poll)

    def with_text(self, poll: int, message_id: str, text: str) -> Transcript:
        """The same transcript, with one assistant message landing on read number `poll`."""
        return self._with(envelope(message_id, "assistant", text), poll)

    def _with(self, message: dict[str, object], poll: int) -> Transcript:
        """A copy whose read number `poll` is the first to carry `message`.

        Earlier reads repeat the last state rather than going empty: a transcript never
        gets shorter, and a read that returned nothing would let the collector's own
        silence rule end the collection early.
        """
        writes = [list(w) for w in self.writes]
        while len(writes) <= poll:
            writes.append(list(writes[-1]))
        writes[poll] = [*writes[poll], message]
        clone = self._clone(writes)
        clone.published = self.published
        return clone

    def _clone(self, writes: list[list[dict[str, object]]]) -> Transcript:
        clone = Transcript(writes)
        clone.polls = self.polls
        return clone


@pytest.fixture
def workspace(tmp_path) -> str:
    path = tmp_path / "workspace"
    path.mkdir()
    return str(path)


@pytest.fixture
async def memory(tmp_path) -> Iterator[Memory]:
    store = await Memory(str(tmp_path / "gate.db")).connect()
    try:
        yield store
    finally:
        await store.close()


@pytest.fixture
def cfg(workspace: str) -> Config:
    return Config(
        telegram_bot_token="TESTTOKEN",
        telegram_chat_id="42",
        r2d2_workspace=workspace,
        r2d2_event_poll_interval=POLL_S,
        r2d2_permission_timeout=PERMISSION_TIMEOUT_S,
    )


@pytest.fixture
def telegram() -> Iterator[Telegram]:
    channel = Telegram()
    with channel.router:
        yield channel


def build(cfg: Config, memory: Memory, transcript: Transcript) -> tuple[Worker, TurnWatch]:
    """The real worker over the real backend and the real client, plus its turn watch."""
    spec = BackendSpec(
        name="opencode", kind="opencode_session", base_url="http://127.0.0.1:4599",
        username="r2d2", password="pw", voice_agent="r2d2-voice", task_agent="r2d2-agent",
        fast_model="opencode/space-bunny-free", task_model="opencode/space-bunny-free",
        summarize_model="opencode/space-bunny-free", timeout=1.0,
    )
    client = OpencodeClient(
        spec, cfg.r2d2_workspace,
        client=httpx.AsyncClient(transport=httpx.MockTransport(transcript)),
    )
    backend = OpencodeSessionBackend(
        spec, OpencodeWiring(client=client, store=NoStore(), cfg=cfg)
    )
    worker = Worker(cfg, memory, logging.getLogger("r2d2.test.gate"), opencode_backend=backend)
    watch = TurnWatch()
    worker.arm_watch(watch)
    return worker, watch


class NoStore:
    """`OpencodeSessionBackend` wants a store in its wiring; the collector never calls it.

    `collect_reply` reads the transcript through the client, so a store is dead weight here
    -- and a double that is never touched is better than a real `OcSessionStore` whose
    `resolve` would re-verify the session this file is not about.
    """

    async def resolve(self, application_id: str) -> str:
        raise AssertionError(f"the collector must not resolve a session (asked for {application_id!r})")

    async def message_count(self, application_id: str) -> int:
        raise AssertionError("the collector must not count a session")


JOB: Final = {
    "type": "opencode_reply",
    "application_id": APP,
    "session_id": SESSION,
    "since_message_id": USER_QUESTION,
    "timeout_s": 5.0,
}
#: The session as the sweep left it: the user's question, the voice agent's marker (still
#: being written when the sweep ran, hence not swept) and the task message.
START: Final = [
    envelope(USER_QUESTION, "user", "Назови столицу Франции"),
    envelope(BLOCKED_MESSAGE, "assistant", NARRATION),
    envelope(TASK_MESSAGE, "user", "Пользователь попросил голосом: ..."),
]


async def _end_when_published(transcript: Transcript, watch: TurnWatch) -> None:
    """Publish `session.idle` as soon as the agent has written something, and never before.

    The real server sends it when the turn is over, which is the only thing that makes the
    text in hand an answer rather than a plan.
    """
    await asyncio.wait_for(transcript.published.wait(), timeout=NOW_S)
    watch.note(idle_frame())


async def deliver(worker: Worker, telegram: Telegram, job: dict | None = None) -> str:
    """Run the one job through the real pool and return what the user was sent."""
    await worker.start()
    telegram.delivered.clear()
    try:
        await worker.enqueue(dict(job or JOB))
        await asyncio.wait_for(telegram.delivered.wait(), timeout=NOW_S)
    finally:
        await worker.stop()
    return telegram.sent[-1]


# ---------------------------------------------------------------------------
# D13 -- the plan is not the result
# ---------------------------------------------------------------------------


async def test_the_result_is_delivered_and_the_plan_in_front_of_it_is_not(
    cfg: Config, memory: Memory, telegram: Telegram
) -> None:
    """The live sequence, whole: narrate, block on a permission, answer, report.

    This is what the live run did not deliver. The gate holds the collection while the ask
    of ours is unanswered -- the fact `TurnWatch` learned from the `permission.asked` frame
    -- and when the answer lands the anchor MOVES to the message the blocked tool call came
    from, so the next read starts at the tool and the narration in front of it is never
    collected at all. The report is what the user receives.
    """
    # Given: a turn that narrated, asked, and is waiting for the human
    transcript = Transcript([list(START)]).with_answer(2)
    worker, watch = build(cfg, memory, transcript)
    watch.note(ask_frame())

    async def the_user_answers() -> None:
        """The broker posts `once`, the server replies, the tool runs, the turn ends.

        Waits for the collector to have read the narration and found nothing new, which is
        the moment the live defect happened: the text in hand at that point is the plan, and
        it stays there until the turn itself is over.
        """
        await asyncio.wait_for(transcript.quiet.wait(), timeout=NOW_S)
        watch.note(replied_frame())

    async def the_turn_ends() -> None:
        """`session.idle`, which is what makes the text in hand the agent's final answer."""
        await transcript.published.wait()
        watch.note(idle_frame())

    # When
    answering = asyncio.create_task(the_user_answers())
    ending = asyncio.create_task(the_turn_ends())
    try:
        sent = await deliver(worker, telegram)
    finally:
        answering.cancel()
        ending.cancel()
    # Then: the RESULT, and nothing of the plan
    assert sent == REPORT
    assert NARRATION not in sent
    # ... and the turn is no longer recorded as parked
    assert not watch.state(SESSION).blocked


async def test_a_plan_is_never_delivered_while_the_question_is_still_open(
    cfg: Config, memory: Memory, telegram: Telegram, caplog: pytest.LogCaptureFixture
) -> None:
    """The same turn with nobody answering: the plan must not be what the user gets.

    The live defect in one assertion. The agent's result is minutes away at best, and the
    broker refuses the ask at its own window, so the wait is bounded and the delivery is the
    stated one -- never the sentence the agent said while it was still working.
    """
    # Given: the turn narrated and parked, on a server that named no tool message -- so
    # the gate has no anchor to move to and has to keep asking the collector instead
    worker, watch = build(cfg, memory, Transcript([list(START)]))
    watch.note(ask_frame(message_id=""))
    # When
    with caplog.at_level(logging.WARNING, logger="r2d2.test.gate"):
        sent = await deliver(worker, telegram)
    # Then
    assert sent == NO_REPLY
    assert NARRATION not in sent
    # ... and the bound that ended the wait is the broker's own window, on the record
    assert "still parked" in caplog.text
    assert f"at {PERMISSION_TIMEOUT_S + POLL_S:.0f}s" in caplog.text


async def test_a_turn_nobody_parked_on_is_still_delivered_unchanged(
    cfg: Config, memory: Memory, telegram: Telegram
) -> None:
    """The control: with the turn over and no ask outstanding, nothing is withheld.

    Without this the tests above would pass on a gate that refuses to deliver anything. The
    narration is still in the delivery, and it should be: the agent put it in the session in
    front of its result, and with no ask to say otherwise there is nothing that marks the
    difference.
    """
    # Given: a turn that answered with no permission involved, and that then ENDED
    transcript = Transcript([list(START)]).with_answer(1)
    worker, watch = build(cfg, memory, transcript)
    # When
    ending = asyncio.create_task(_end_when_published(transcript, watch))
    try:
        sent = await deliver(worker, telegram)
    finally:
        ending.cancel()
    # Then
    assert sent == f"{NARRATION}\n{REPORT}"
    assert watch.state(SESSION).blocked is False


# ---------------------------------------------------------------------------
# D6-residue -- a routing signal is not an answer
# ---------------------------------------------------------------------------


async def test_the_agents_own_echo_of_the_marker_is_never_delivered_as_an_answer(
    cfg: Config, memory: Memory, telegram: Telegram
) -> None:
    """What the user received on the live run: a stripped echo of their own question.

    The session holds the voice agent's marker, which the deadline branch's sweep could not
    see, and then the agent's own copy of it -- the first message after the anchor. Nothing
    here is blocked, so the silence rule would have shipped it, and `for_human` would have
    removed the token and left the copy of the user's request, which reads like an answer.
    The delivery is the stated one instead, and the reason is on record.
    """
    # Given: the agent answered with a routing signal rather than a result, and that turn
    # ENDED -- an agent that re-escalated and stopped is the honest worst case
    transcript = Transcript([list(START)]).with_text(1, "msg_echo", f"{NARRATION}\n{ECHO}")
    worker, watch = build(cfg, memory, transcript)
    # When
    ending = asyncio.create_task(_end_when_published(transcript, watch))
    try:
        sent = await deliver(worker, telegram)
    finally:
        ending.cancel()
    # Then
    assert sent == NO_REPLY
    assert SENTINEL not in sent
    assert "Написать развёрнутый обзор" not in sent


@pytest.mark.parametrize(
    "position",
    ("leading", "behind the plan"),
    ids=("the-echo-is-the-first-thing-the-agent-wrote", "the-echo-came-after-its-narration"),
)
async def test_a_body_carrying_the_token_anywhere_is_never_delivered_as_an_answer(
    cfg: Config, memory: Memory, telegram: Telegram, position: str
) -> None:
    """The predicate is the session sweep's, and the live harm is in BOTH positions.

    The recorded job result began with the token; what the user received did not, because
    `routing.for_human` removed the token line and left the agent's copy of their own
    question. The agent narrates first and echoes second, so a check that only looked at the
    leading position would have passed the one shape the evidence file shows and missed the
    one a person receives. `routing.transcript_sweep` already deletes such a message from the
    session -- deliberately, and with its reasoning written down -- so the gate asks that
    function rather than writing a second, looser reading of it.
    """
    # Given: the agent's own copy of the routing signal, alone or behind its narration
    echo = ECHO if position == "leading" else f"{NARRATION}\n{ECHO}"
    transcript = Transcript([list(START)]).with_text(1, "msg_echo", echo)
    worker, watch = build(cfg, memory, transcript)
    # When
    ending = asyncio.create_task(_end_when_published(transcript, watch))
    try:
        sent = await deliver(worker, telegram)
    finally:
        ending.cancel()
    # Then
    assert sent == NO_REPLY
    assert "Написать развёрнутый обзор" not in sent
