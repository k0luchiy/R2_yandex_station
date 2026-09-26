"""Behavioural tests for the worker's `opencode_reply` branch (plan todo 16).

Alice's webhook has **4.5 s**, and `docs/11-opencode-contract.md` C8 measured a
cold opencode turn at **15.5-18.6 s**. So a voice turn that outruns
`r2d2_fast_deadline` is acknowledged immediately and the real answer is fetched
in the background -- `core/brain.py` (todo 15) enqueues one `opencode_reply`
job for it, and this file proves the worker that receives the job does four
things it must not get wrong:

* **the collected text reaches the user exactly once** -- the whole point of the
  ack is that Telegram carries the answer, so a second send would double it and
  a missing one loses the answer;
* **a collector that returns nothing is not a success** -- an empty body is a
  misleading answer, so it becomes a stated `"Агент не ответил."`;
* **a collector that never returns cannot hold the pool** -- `Worker` runs at
  most two jobs at a time and the arxiv digest shares that pool, so an
  unbounded collector would starve the feature this worker exists for. The
  ceiling is the job's own `timeout_s`, enforced with `asyncio.wait_for`;
* **nothing typed leaks** -- the opencode password reaches neither Telegram nor
  the `jobs` table nor a log record, even when the server's own error body
  quotes it back.

**What is real here.** The real `Worker`, the real `Memory` over a temp SQLite
file, the real `core.tools.arxiv_tool.build_digest`, the real
`OpencodeSessionBackend` + `OpencodeClient` over `httpx.MockTransport`, and the
real Telegram call through `respx`. Only the collector and arxiv's HTTP/LLM
call are doubles, because those are the two boundaries the tests drive.

**Why some tests call `_run_job` and others go through `enqueue`.** The happy
paths go through `enqueue` + the real `_loop`, so the queue, the task fan-out
and the single Telegram send are observed end to end. The failure paths call
`_run_job` directly, because "it does not raise out of `_run_job`" is only
observable if the test is the awaiter: a task the loop created would swallow the
exception and the assertion would pass for the wrong reason.

**Timing.** No test sleeps to "let something finish". A hung collector is driven
by a fake that never resolves, bounded by a per-job `timeout_s` of 50 ms, and
every wait is `asyncio.wait_for` on an `asyncio.Event` the double sets, so a
missing release is a failure rather than a flake.
"""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import sqlite3
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

import httpx
import pytest
import respx

import core.async_worker as async_worker
import core.brain
from app.config import Config
from core.async_worker import Worker
from core.backends.config_loader import BackendSpec
from core.backends.opencode_session import OpencodeSessionBackend, OpencodeWiring
from core.memory import Memory
from core.opencode.client import OpencodeClient
from core.opencode.session_store import OcSessionStore
from core.opencode.wire import OpencodeStatusError
from core.tools import arxiv_tool

# ---------------------------------------------------------------------------
# The contract, read off the producer
# ---------------------------------------------------------------------------

#: The job type `core/brain.py` writes (todo 15, `JOB_OPENCODE_REPLY`) and this
#: todo dispatches. The brain is the spec: a rename there must fail here.
JOB_TYPE: Final = "opencode_reply"
#: The exact key set `SessionCollector.hand_to_agent` enqueues (via
#: `core/session_collector.py:_collect_later`). Anything else this worker
#: reads is a key nobody writes.
JOB_KEYS: Final = frozenset(
    {"type", "application_id", "session_id", "since_message_id", "timeout_s"}
)
#: The collector ceiling the brain states in the job.
COLLECT_TIMEOUT_S: Final = 600.0
#: The state a job may be observed in, and the three the worker writes.
PENDING: Final = "pending"
RUNNING: Final = "running"
DONE: Final = "done"
ERROR: Final = "error"
#: The prefix `_run_job` puts in front of every failure it reports. Asserted so a
#: raw traceback in the user's Telegram is a test failure, not a surprise.
FAILURE_PREFIX: Final = "Задача \u04202D2 завершилась ошибкой: "  # the first letter of R2D2 here is CYRILLic U+0420, as it is in the worker

APP: Final = "alice-app-1"
SESSION: Final = "ses_opencode_1"
MARKER: Final = "msg_before_the_hung_turn"
TELEGRAM_PREFIX: Final = "https://api.telegram.org/"
OC_BASE_URL: Final = "http://127.0.0.1:4599"
TELEGRAM_TOKEN: Final = "TESTTOKEN"

AGENT_REPLY: Final = "Сводка готова: три статьи про RAG."
#: What the user is told when the collector ends with no assistant text. Pinned
#: here as a literal and checked against the module's own constant below, so the
#: assertion can never pass by comparing the worker to itself.
NO_REPLY: Final = "Агент не ответил."
#: `r2d2_needs_agent_sentinel` as the app ships it, read off the dataclass so a
#: rename in `app/config.py` fails here instead of silently un-guarding the send.
SENTINEL: Final = Config().r2d2_needs_agent_sentinel
#: The voice agent's protocol answer, in the exact shape the live run put into the
#: user's chat eleven times: the marker on its own line, then the one short task
#: line `docs/11-opencode-backend.md` §4.1 allows after it.
PROTOCOL_TASK_LINE: Final = "Выполнить в терминале `ls -la /tmp` и пересказать каталог."
PROTOCOL_REPLY: Final = f"{SENTINEL}\n{PROTOCOL_TASK_LINE}"
#: What the full agent answers, and the reason the whole collector exists: a
#: result far too long for Alice's 1024 characters.
AGENT_DIGEST: Final = "Сводка готова: 60 статей про RAG, три лучшие в приложении."
#: The pre-existing default for a job type nothing dispatches.
UNKNOWN_TEXT: Final = "Неизвестная задача."
#: The password lives in exactly one place -- the spec handed to the real client
#: -- so "it is in neither the Telegram body, the `jobs` row nor the log" is an
#: assertion about something that really exists, not about a string nobody uses.
PASSWORD: Final = "R2D2_OC_PASSWORD_VALUE-9f3a"
MODEL: Final = "opencode/space-bunny-free"

#: A collector ceiling small enough to keep the suite fast and long enough to be
#: a real `asyncio.wait_for`, chosen per test rather than slept on.
HANG_S: Final = 0.05
#: A ceiling on "this must happen now", generous enough that a loaded machine
#: cannot trip it and short enough that a held semaphore fails inside the test.
NOW_S: Final = 5.0

#: The behaviour a scripted collector call does: return this text, raise this,
#: or never resolve. `type` is a str, an exception, or this marker.
NEVER: Final = object()


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Call:
    """One `collect_reply` invocation, exactly as the worker made it."""

    session_id: str
    since_message_id: str
    timeout_s: float


@dataclass
class FakeCollector:
    """A scripted `collect_reply`: one behaviour per call, all of them recorded.

    `NEVER` is the interesting one. It awaits an `asyncio.Event` that is never
    set, so the coroutine is only ever left by cancellation -- the exact shape of
    a turn the opencode server acknowledges and never finishes, and the only way
    to test a timeout without a wall clock.
    """

    script: deque[str | BaseException | object] = field(default_factory=deque)
    calls: list[Call] = field(default_factory=list)
    #: collectors currently inside `collect_reply`
    inside: int = 0
    #: set when the FIRST collector entered -- "the job has started"
    started: asyncio.Event = field(default_factory=asyncio.Event)
    #: set when TWO collectors are inside at once -- "both pool slots are busy"
    busy: asyncio.Event = field(default_factory=asyncio.Event)
    #: how many calls were cancelled rather than answered
    cancelled: int = 0

    async def collect_reply(self, session_id: str, since_message_id: str, timeout_s: float) -> str:
        self.calls.append(Call(session_id, since_message_id, timeout_s))
        self.inside += 1
        self.started.set()
        if self.inside == 2:
            self.busy.set()
        try:
            behaviour = self.script.popleft()
            if behaviour is NEVER:
                await asyncio.Event().wait()  # pragma: no cover - leaves by cancel
            if isinstance(behaviour, BaseException):
                raise behaviour
            return str(behaviour)
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        finally:
            self.inside -= 1


class Net:
    """`respx` in front of Telegram -- the message the user would actually read.

    The route records the text of every send, so "exactly once" and "never the
    password" are observations. An unmocked call raises rather than escaping to
    the network, and `assert_all_called` is off because several tests here must
    send nothing at all -- which they assert.
    """

    def __init__(self) -> None:
        self.router = respx.mock(assert_all_called=False)
        self.route = self.router.post(url__startswith=TELEGRAM_PREFIX).mock(
            side_effect=self._telegram
        )
        self.sent: list[str] = []
        self.delivered = asyncio.Event()

    def _telegram(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(str(json.loads(request.content)["text"]))
        self.delivered.set()
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(self.sent)}})

    def clear(self) -> None:
        self.sent.clear()
        self.delivered.clear()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def job(**overrides: object) -> dict[str, object]:
    """A job shaped exactly as `SessionCollector.hand_to_agent` enqueues one."""
    base: dict[str, object] = {
        "type": JOB_TYPE,
        "application_id": APP,
        "session_id": SESSION,
        "since_message_id": MARKER,
        "timeout_s": COLLECT_TIMEOUT_S,
    }
    base.update(overrides)
    return base


def job_row(db_path: str, job_id: str) -> tuple[str, str | None, str | None]:
    """`(status, result, error)` as the `jobs` table holds it.

    A second connection on purpose: the assertion is about what R2D2 persisted,
    not about what the worker believes it persisted.
    """
    with sqlite3.connect(db_path) as database:
        row = database.execute(
            "SELECT status, result, error FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
    assert row is not None, f"job {job_id} is not in the jobs table"
    return str(row[0]), row[1], row[2]


def oc_spec() -> BackendSpec:
    """The shipped opencode spec, carrying the password the hygiene tests hunt."""
    return BackendSpec(
        name="opencode",
        kind="opencode_session",
        base_url=OC_BASE_URL,
        username="opencode",
        password=PASSWORD,
        voice_agent="r2d2-voice",
        task_agent="r2d2-agent",
        fast_model=MODEL,
        task_model=MODEL,
        summarize_model=MODEL,
        timeout=1.0,
    )


def real_backend(spec: BackendSpec, cfg: Config, memory: Memory, handler) -> OpencodeSessionBackend:
    """The real adapter over a `MockTransport`, as todo 18 will assemble it."""
    client = OpencodeClient(
        spec, str(Path(cfg.r2d2_workspace)), client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    store = OcSessionStore(memory, client, cfg)
    return OpencodeSessionBackend(spec, OpencodeWiring(client=client, store=store, cfg=cfg))


@pytest.fixture
def net() -> Net:
    double = Net()
    with double.router:
        yield double


@pytest.fixture
async def cfg(tmp_path: Path) -> Config:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return Config(
        telegram_bot_token=TELEGRAM_TOKEN,
        telegram_chat_id="42",
        r2d2_workspace=str(workspace),
        r2d2_event_poll_interval=0.05,
    )


@pytest.fixture
async def memory(tmp_path: Path):
    store = await Memory(str(tmp_path / "jobs.db")).connect()
    try:
        yield store
    finally:
        await store.close()


@pytest.fixture
def log() -> logging.Logger:
    return logging.getLogger("r2d2.test.worker")


# ---------------------------------------------------------------------------
# 1. The happy path: the agent's own text reaches the user, once
# ---------------------------------------------------------------------------


async def test_a_collected_reply_is_the_job_result_and_reaches_telegram_once(
    net: Net, cfg: Config, memory: Memory, log: logging.Logger
) -> None:
    # Given a worker wired to a collector that answers with the agent's own text
    collector = FakeCollector(deque([AGENT_REPLY]))
    worker = Worker(cfg, memory, log, opencode_backend=collector)
    # When the job the brain enqueues goes through the real queue and the real loop
    await worker.start()
    try:
        net.clear()
        job_id = await worker.enqueue(job())
        await asyncio.wait_for(net.delivered.wait(), timeout=NOW_S)
    finally:
        await worker.stop()
    # Then the job is DONE with exactly the text the collector produced ...
    status, result, error = job_row(memory.db_path, job_id)
    assert (status, result, error) == (DONE, AGENT_REPLY, None)
    # ... that text is what the user reads, and it was sent ONCE: a second send
    # would be the answer arriving twice, which reads as two answers
    assert net.sent == [AGENT_REPLY]
    assert net.route.call_count == 1
    # ... and the collector was asked for exactly what the job said, with the
    # brain's own ceiling -- the worker neither invented a session nor a marker
    assert collector.calls == [Call(SESSION, MARKER, COLLECT_TIMEOUT_S)]


async def test_an_empty_reply_is_stated_not_reported_as_an_empty_success(
    net: Net, cfg: Config, memory: Memory, log: logging.Logger
) -> None:
    # Given a collector that ends without any assistant text -- the agent really
    # said nothing, which `collect_reply` reports as ""
    collector = FakeCollector(deque([""]))
    worker = Worker(cfg, memory, log, opencode_backend=collector)
    # When
    await worker.start()
    try:
        net.clear()
        job_id = await worker.enqueue(job())
        await asyncio.wait_for(net.delivered.wait(), timeout=NOW_S)
    finally:
        await worker.stop()
    # Then the user is told the agent did not answer ...
    assert net.sent == [NO_REPLY]
    # ... it is never an EMPTY message body, which a phone renders as nothing at
    # all and an operator reads as a delivered job with no reason in it
    assert net.sent[0].strip() != ""
    assert NO_REPLY.strip() != ""
    # ... and the job says the same thing, so the `jobs` table never records a
    # successful empty answer
    status, result, _ = job_row(memory.db_path, job_id)
    assert (status, result) == (DONE, NO_REPLY)
    # ... and the collector WAS asked: the emptiness is its verdict, not a skipped job
    assert len(collector.calls) == 1


# ---------------------------------------------------------------------------
# 2. A collector that never returns: bounded, and the pool survives it
# ---------------------------------------------------------------------------


async def test_a_collector_that_never_returns_marks_the_job_a_collector_timeout(
    net: Net, cfg: Config, memory: Memory, log: logging.Logger
) -> None:
    # Given a turn the server acknowledged and never finishes, with a ceiling small
    # enough to keep the suite fast
    collector = FakeCollector(deque([NEVER]))
    worker = Worker(cfg, memory, log, opencode_backend=collector)
    job_id = await memory.create_job(job(timeout_s=HANG_S))
    # When
    await worker._run_job(job_id, job(timeout_s=HANG_S))
    # Then the job is ERROR, naming the ceiling as a timeout -- and not `done`:
    # a job marked done with no text is the misleading success this branch exists
    # to prevent. The bare `str(TimeoutError())` is "", so the reason must be stated
    status, result, error = job_row(memory.db_path, job_id)
    assert status == ERROR
    assert result is None
    assert error is not None and "collector timeout" in error
    # ... it did NOT raise out of `_run_job` -- the await above is the assertion
    # ... and the failure the user sees is the stated reason, not a traceback
    assert net.sent == [f"{FAILURE_PREFIX}{error}"]
    assert "Traceback" not in net.sent[0]


async def test_a_collector_that_never_returns_frees_the_pool_for_the_next_jobs(
    net: Net, cfg: Config, memory: Memory, log: logging.Logger
) -> None:
    # Given a hung collector that has just timed out and released its slot, and two
    # follow-up collectors that also never return but on a long ceiling
    collector = FakeCollector(deque([NEVER, NEVER, NEVER]))
    worker = Worker(cfg, memory, log, opencode_backend=collector)
    hung = job(timeout_s=HANG_S)
    hung_id = await memory.create_job(hung)
    await worker._run_job(hung_id, hung)
    # When both remaining slots are asked for at once
    busy = [job(timeout_s=NOW_S * 4) for _ in range(2)]
    first = asyncio.create_task(worker._run_job(await memory.create_job(busy[0]), busy[0]))
    second = asyncio.create_task(worker._run_job(await memory.create_job(busy[1]), busy[1]))
    try:
        # Then BOTH are inside the collector simultaneously -- which is only
        # possible if the timed-out job handed its slot back. `asyncio.Event` plus
        # `wait_for`, never a sleep: a leaked slot leaves the event unset and the
        # next line raises TimeoutError, which is the failure we want to see.
        await asyncio.wait_for(collector.busy.wait(), timeout=NOW_S)
    finally:
        for task in (first, second):
            task.cancel()
        await asyncio.gather(first, second, return_exceptions=True)


async def test_stopping_the_worker_mid_job_cancels_it_and_reports_no_success(
    net: Net, cfg: Config, memory: Memory, log: logging.Logger
) -> None:
    # Given a job already inside a collector that never returns
    collector = FakeCollector(deque([NEVER]))
    worker = Worker(cfg, memory, log, opencode_backend=collector)
    await worker.start()
    job_id = await worker.enqueue(job(timeout_s=NOW_S * 4))
    await asyncio.wait_for(collector.started.wait(), timeout=NOW_S)
    # When the worker is stopped mid-job -- shutdown, not a hang
    await worker.stop()
    # Then the collector was cancelled rather than left running: a job still
    # pending when the event loop closes is a `Task was destroyed but it is
    # pending` warning and, worse, an HTTP request nobody owns
    assert collector.cancelled == 1
    # ... and the job was never reported as finished: a shutdown is not a `done`
    status, _, _ = job_row(memory.db_path, job_id)
    assert status != DONE
    assert net.sent == []


# ---------------------------------------------------------------------------
# 3. Missing wiring, and a loop that outlives one failure
# ---------------------------------------------------------------------------


async def test_a_job_without_a_wired_backend_is_an_error_naming_the_missing_wiring(
    net: Net, cfg: Config, memory: Memory, log: logging.Logger
) -> None:
    # Given the worker `app/main.py` still builds -- three arguments, no backend
    worker = Worker(cfg, memory, log)
    job_id = await memory.create_job(job())
    # When
    await worker._run_job(job_id, job())
    # Then it is an ERROR that says WHICH wiring is missing, so the fix is
    # readable from the `jobs` table without reading this file
    status, _, error = job_row(memory.db_path, job_id)
    assert status == ERROR
    assert error is not None and "opencode_backend" in error
    # ... the user gets that reason rather than an AttributeError traceback
    assert net.sent == [f"{FAILURE_PREFIX}{error}"]
    assert "Traceback" not in net.sent[0]
    assert "AttributeError" not in net.sent[0]


async def test_the_worker_loop_keeps_dispatching_after_a_job_fails(
    net: Net, cfg: Config, memory: Memory, log: logging.Logger
) -> None:
    # Given a collector whose FIRST call refuses and whose second answers: the loop
    # is about to see one failure and one success in a row
    collector = FakeCollector(
        deque([OpencodeStatusError("opencode is down", status_code=500), AGENT_REPLY])
    )
    worker = Worker(cfg, memory, log, opencode_backend=collector)
    await worker.start()
    try:
        net.clear()
        failed = await worker.enqueue(job())
        await asyncio.wait_for(net.delivered.wait(), timeout=NOW_S)
        # When the next job arrives
        net.clear()
        job_id = await worker.enqueue(job())
        await asyncio.wait_for(net.delivered.wait(), timeout=NOW_S)
    finally:
        await worker.stop()
    # Then the failure did not end the loop, and the second job is DONE
    assert job_row(memory.db_path, failed)[0] == ERROR
    assert job_row(memory.db_path, job_id)[0] == DONE
    assert net.sent == [AGENT_REPLY]


# ---------------------------------------------------------------------------
# 4. A job that does not match the contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("missing", ("session_id", "since_message_id"))
async def test_a_job_missing_a_required_key_is_an_error_naming_that_key(
    net: Net, cfg: Config, memory: Memory, log: logging.Logger, missing: str
) -> None:
    # Given a job the producer would never write -- a key is absent, not empty
    collector = FakeCollector(deque([AGENT_REPLY]))
    worker = Worker(cfg, memory, log, opencode_backend=collector)
    malformed = job()
    del malformed[missing]
    job_id = await memory.create_job(malformed)
    # When
    await worker._run_job(job_id, malformed)
    # Then the missing KEY is named -- `KeyError: 'session_id'` in the user's
    # Telegram is a stack trace dressed as an answer
    status, _, error = job_row(memory.db_path, job_id)
    assert status == ERROR
    assert error is not None and missing in error
    assert "KeyError" not in error
    assert "Traceback" not in net.sent[0]
    # ... and the collector was never asked, so no session was touched at all
    assert collector.calls == []


async def test_a_collector_typed_error_becomes_a_job_error_and_not_a_crash(
    net: Net, cfg: Config, memory: Memory, log: logging.Logger
) -> None:
    # Given a collector that refuses with the adapter's own typed error -- what
    # `collect_reply` raises when the session is gone. A `since_message_id` the
    # session dropped is the same shape: the marker may be gone, the session is not.
    collector = FakeCollector(
        deque([OpencodeStatusError("opencode: no such session", status_code=404)])
    )
    worker = Worker(cfg, memory, log, opencode_backend=collector)
    job_id = await memory.create_job(job())
    # When
    await worker._run_job(job_id, job())
    # Then the job is ERROR with the collector's own reason -- the worker did not
    # swallow it, retry it, or invent a success
    status, result, error = job_row(memory.db_path, job_id)
    assert status == ERROR
    assert result is None
    assert error is not None and "no such session" in error
    # ... and nothing escaped: this line is reached only if `_run_job` returned
    assert net.sent == [f"{FAILURE_PREFIX}opencode: no such session"]


# ---------------------------------------------------------------------------
# 5. Secret hygiene: a server that quotes the password back
# ---------------------------------------------------------------------------


async def test_a_refusing_opencode_server_leaks_no_credential_to_telegram_the_row_or_the_log(
    net: Net, cfg: Config, memory: Memory, log: logging.Logger, caplog: pytest.LogCaptureFixture
) -> None:
    # Given the REAL adapter over a server that answers 401 with the password
    # quoted in its own body -- the shape of a misconfigured `OPENCODE_SERVER_PASSWORD`
    def refuse(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": f"bad basic auth: {PASSWORD}"})

    backend = real_backend(oc_spec(), cfg, memory, refuse)
    worker = Worker(cfg, memory, log, opencode_backend=backend)
    job_id = await memory.create_job(job(timeout_s=HANG_S * 10))
    # When the job is dispatched for real
    with caplog.at_level(logging.DEBUG):
        await worker._run_job(job_id, job(timeout_s=HANG_S * 10))
    # Then the failure is reported -- the redaction must not swallow the reason
    status, _, error = job_row(memory.db_path, job_id)
    assert status == ERROR
    assert error is not None and "[redacted]" in error
    # ... and the credential is in NONE of the three places a failure escapes to
    assert PASSWORD not in error
    assert all(PASSWORD not in text for text in net.sent)
    assert PASSWORD not in caplog.text


# ---------------------------------------------------------------------------
# 6. The feature this worker existed for must not regress
# ---------------------------------------------------------------------------


async def test_the_arxiv_digest_still_dispatches_through_the_same_worker(
    net: Net, cfg: Config, memory: Memory, log: logging.Logger, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given the arxiv tool's HTTP and LLM calls doubled, its real digest builder kept
    entries = [
        {"title": "Retrieval-Augmented Generation", "summary": "RAG survey", "link": "http://arxiv.org/1", "published": ""},
        {"title": "Agents that plan", "summary": "planning", "link": "http://arxiv.org/2", "published": ""},
    ]
    fetched: list[tuple[str, int, int]] = []

    async def fake_fetch(query: str, max_results: int = 5, days: int | None = None) -> list[dict]:
        fetched.append((query, max_results, days or 0))
        return entries

    async def fake_summarize(_cfg: Config, _query: str, _entries: list[dict]) -> str:
        return "Коротко: две статьи про RAG."

    monkeypatch.setattr(arxiv_tool, "arxiv_fetch", fake_fetch)
    monkeypatch.setattr(arxiv_tool, "summarize_entries", fake_summarize)
    # And a worker whose opencode backend is wired, so the digest is proven to
    # coexist with the new branch rather than to work only without it
    worker = Worker(cfg, memory, log, opencode_backend=FakeCollector(deque([AGENT_REPLY])))
    arxiv_job = {"type": "arxiv", "query": "RAG", "days": 3, "max_results": 2}
    # When
    await worker.start()
    try:
        net.clear()
        job_id = await worker.enqueue(arxiv_job)
        await asyncio.wait_for(net.delivered.wait(), timeout=NOW_S)
    finally:
        await worker.stop()
    # Then the real digest -- header, summary and links -- is the job result and the
    # Telegram body, so the arxiv path is untouched by adding a second branch
    digest = arxiv_tool.build_digest("RAG", entries, "Коротко: две статьи про RAG.")
    assert net.sent == [digest]
    assert job_row(memory.db_path, job_id) == (DONE, digest, None)
    # ... and the job's own fields still reach the tool unchanged
    assert fetched == [("RAG", 2, 3)]


async def test_an_unknown_job_type_is_still_answered_with_the_unknown_text(
    net: Net, cfg: Config, memory: Memory, log: logging.Logger
) -> None:
    # Given a worker wired to a collector, and a job type nothing dispatches
    collector = FakeCollector(deque([AGENT_REPLY]))
    worker = Worker(cfg, memory, log, opencode_backend=collector)
    unknown = {"type": "не-известно"}
    job_id = await memory.create_job(unknown)
    # When
    await worker._run_job(job_id, unknown)
    # Then the pre-existing answer is unchanged -- adding a second branch must not
    # swallow the default, or a typo in a job type would look like a working job
    status, result, error = job_row(memory.db_path, job_id)
    assert (status, result, error) == (DONE, UNKNOWN_TEXT, None)
    assert net.sent == [UNKNOWN_TEXT]
    # ... and nothing was collected: the unknown type never reached the collector
    assert collector.calls == []


# ---------------------------------------------------------------------------
# 7. The escalation marker is machine protocol, not a message for a human
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "collected",
    (
        pytest.param(SENTINEL, id="a-body-that-was-nothing-but-the-token"),
        pytest.param(PROTOCOL_REPLY, id="the-token-line-and-the-task-line"),
        pytest.param(
            f"{PROTOCOL_REPLY}\n{AGENT_DIGEST}",
            id="the-token-followed-by-something-else",
        ),
    ),
)
async def test_a_body_that_begins_with_the_marker_is_never_delivered_as_an_answer(
    net: Net, cfg: Config, memory: Memory, log: logging.Logger, collected: str
) -> None:
    """The D6 harm, and the reason the two cases above are separated.

    **What changed, and why the old expectation was wrong.** This file used to assert
    that a collected body carrying `[[NEEDS_AGENT]]` is delivered with the token line
    removed -- `PROTOCOL_REPLY` arriving as its own task line, and a digest arriving
    behind the signal. That is precisely the failure the third live run measured: on the
    `deadline` branch the sweep cannot see the voice agent's marker (it is still being
    written), the agent reads that marker in its own history and emits one itself, and
    that echo sits after the collector's anchor -- so the user received a stripped echo
    of their own question instead of the result (`qa/live-run-v3.md` §3, job
    `e0974a5334ac`). `routing.transcript_sweep` already deletes such a message out of
    the session, deliberately, so a collector that ships one is shipping protocol the
    project has already decided is not conversation.

    The delivery is therefore the stated one -- `NO_REPLY`, with a WARNING naming the
    session -- which is the honest report: the agent did not answer. `for_human` is
    untouched and still guards every OTHER outbound body, which
    `test_a_digest_that_mentions_the_marker_still_arrives` below now pins in its place.
    """
    # Given a collector that ends with a routing signal rather than a result
    collector = FakeCollector(deque([collected]))
    worker = Worker(cfg, memory, log, opencode_backend=collector)
    document = job()
    job_id = await memory.create_job(document)
    # When the job runs
    await worker._run_job(job_id, document)
    # Then the user is told the agent did not answer, and the protocol never reaches them
    assert net.sent == [NO_REPLY]
    assert all(SENTINEL not in text for text in net.sent)
    # ... and the job says the same thing, so no `done` row claims a signal was an answer
    assert job_row(memory.db_path, job_id) == (DONE, NO_REPLY, None)


async def test_a_digest_that_mentions_the_marker_still_arrives(
    net: Net, cfg: Config, memory: Memory, log: logging.Logger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`routing.for_human` is still the guard on every OTHER body a person may read.

    The gate above withholds a COLLECTED answer that carries the token, because
    `core.routing.transcript_sweep` treats such an assistant message as a control signal
    rather than as conversation. A digest is not a collected answer: it is R2D2's own text,
    and a mention of the token in one sentence of it must cost the token and nothing else.
    """
    # Given: a digest whose summary quotes the machine token in a sentence
    entries = [
        {"title": "RAG", "summary": "survey", "link": "http://arxiv.org/1", "published": ""},
    ]

    async def fake_fetch(query: str, max_results: int = 5, days: int | None = None) -> list[dict]:
        return entries

    async def fake_summarize(_cfg: Config, _query: str, _entries: list[dict]) -> str:
        return f"Коротко: метка {SENTINEL} — служебная, статья одна."

    monkeypatch.setattr(arxiv_tool, "arxiv_fetch", fake_fetch)
    monkeypatch.setattr(arxiv_tool, "summarize_entries", fake_summarize)
    worker = Worker(cfg, memory, log, opencode_backend=FakeCollector(deque([AGENT_REPLY])))
    # When
    await worker.start()
    try:
        net.clear()
        await worker.enqueue({"type": "arxiv", "query": "RAG", "days": 3, "max_results": 1})
        await asyncio.wait_for(net.delivered.wait(), timeout=NOW_S)
    finally:
        await worker.stop()
    # Then: the digest arrived whole, with only the token removed
    digest = arxiv_tool.build_digest("RAG", entries, "Коротко: метка — служебная, статья одна.")
    assert net.sent == [digest]
    assert SENTINEL not in net.sent[0]
    assert "служебная, статья одна." in net.sent[0]


# ---------------------------------------------------------------------------
# 8. The dispatch itself: explicit, not a dynamic attribute lookup
# ---------------------------------------------------------------------------


def test_dispatch_names_every_job_type_it_handles() -> None:
    # Given the module as it stands
    source = Path(async_worker.__file__).read_text(encoding="utf-8")
    # Then the dynamic-dispatch shape the rules forbid is absent: no
    # `getattr(self._backend, f"_{kind}")`, which would dispatch on a job-supplied
    # string and turn a typo in the brain into an AttributeError at run time
    assert "getattr(self._backend" not in source
    assert 'getattr(self, f"' not in source
    # ... and no `getattr` at all: the module has no use for one, so any future one
    # is a decision to make in review rather than a leftover
    calls = {
        node.func.id
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "getattr" not in calls


def test_the_job_type_the_brain_writes_is_the_branch_the_worker_has() -> None:
    # Then the worker dispatches exactly the name the producer writes -- a rename in
    # `core/brain.py` fails here instead of silently turning every agent reply into
    # "Неизвестная задача." in production -- and the text it reports for an empty
    # collector is the one pinned above
    assert async_worker.NO_REPLY == NO_REPLY
    assert async_worker.JOB_OPENCODE_REPLY == JOB_TYPE
    assert core.brain.JOB_OPENCODE_REPLY == JOB_TYPE
    assert JOB_KEYS == {"type", "application_id", "session_id", "since_message_id", "timeout_s"}
