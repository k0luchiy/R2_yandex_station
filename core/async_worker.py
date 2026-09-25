"""The background job pool: everything too slow for Alice's 4.5 s webhook.

Two job types share this pool, and that sharing is the whole constraint. An
arxiv digest is a minute of network. An `opencode_reply` job -- enqueued by
`core/brain.py` when a voice turn outruns `r2d2_fast_deadline`, because a cold
opencode turn costs 15.5-18.6 s (C8) -- carries the ceiling the brain states in
it, ten minutes by default. `Semaphore(2)` therefore has to survive a collector
that never answers, or two of them stop the digest, which is the feature this
worker exists to serve. So the collector is bounded HERE, with
`asyncio.wait_for`, instead of being trusted to bound itself: `timeout_s` is a
promise about one user's session and the pool belongs to everyone.

The job dict is untrusted input -- a row in the `jobs` table written by another
module -- so every key read here is parsed and checked (`_text_field`,
`_timeout_field`), and a job that does not match the contract becomes a stated
job `error` rather than a `KeyError` rendered into the user's Telegram.

The dispatch is one explicit branch per job type, never a method name resolved
from the job's own `type`: that string crosses a module boundary, and a lookup
built from it turns a typo in `core/brain.py` into an AttributeError at run time,
in production, on the user's only copy of the answer.
"""

import asyncio
import logging
from collections.abc import Mapping
from typing import Final, Protocol

from app.config import Config
from core.memory import Memory
from core.tools import arxiv_tool
from core.tools.telegram_tool import send_message

#: The job type `core/brain.py` enqueues when a voice turn outruns the fast
#: deadline (todo 15). The brain owns the name; this module owns the branch.
JOB_OPENCODE_REPLY: Final = "opencode_reply"
#: The collector's ceiling for a job that states none. The same value
#: `core.brain.COLLECT_TIMEOUT_S` puts in a job it writes.
COLLECT_TIMEOUT_S: Final = 600.0
#: What the user is told when the collector ended without any assistant text. An
#: empty body would be a misleading success: the phone shows nothing at all and
#: the operator sees a delivered job with no reason in it.
NO_REPLY: Final = "Агент не ответил."


class WorkerJobError(Exception):
    """A job cannot be run as written: no wiring, a missing key, a dead collector.

    Its message is what the user ends up reading, so it has to STATE the reason.
    `str(TimeoutError())` is `""` and `str(KeyError("x"))` is `"'x'"`, which is
    how a failure becomes a job with no error text and a Telegram message that
    looks like an answer.
    """


class ReplyCollector(Protocol):
    """The one method `Worker` needs from the opencode backend.

    `OpencodeSessionBackend` satisfies it structurally, which is what lets todo
    18 hand the real adapter in without this module importing it -- and naming
    the method here is what keeps the branch a call rather than a lookup.
    """

    async def collect_reply(
        self, session_id: str, since_message_id: str, timeout_s: float
    ) -> str: ...


def _text_field(job: Mapping[str, object], key: str, *, allow_empty: bool = False) -> str:
    """One string field of the job, or a refusal naming the key that is wrong.

    `allow_empty` exists for `since_message_id` alone: the brain writes `""` when
    the session held no messages yet, and the collector reads that as "everything
    in the session is newer than the marker".
    """
    value = job.get(key)
    if isinstance(value, str) and (value or allow_empty):
        return value
    expected = "a string, possibly empty" if allow_empty else "a non-empty string"
    raise WorkerJobError(
        f"{JOB_OPENCODE_REPLY} job: {key!r} is missing or is not {expected}. It is a key "
        f"`core/brain.py` writes on every {JOB_OPENCODE_REPLY} job, so this job did not come "
        "from the brain."
    )


def _timeout_field(job: Mapping[str, object]) -> float:
    """The job's collector ceiling, or the shipped one.

    A job that states no usable ceiling gets `COLLECT_TIMEOUT_S` rather than zero:
    `wait_for(..., 0)` expires before the collector is ever awaited, which would
    report a timeout for a turn that was never given a chance.
    """
    value = job.get("timeout_s")
    if isinstance(value, int | float) and not isinstance(value, bool) and value > 0:
        return float(value)
    return COLLECT_TIMEOUT_S


class Worker:
    def __init__(
        self,
        cfg: Config,
        memory: Memory,
        logger: logging.Logger | None = None,
        opencode_backend: ReplyCollector | None = None,
    ) -> None:
        """`opencode_backend` is optional because todo 18 is what supplies it.

        Until then a `Worker` built with three arguments is a valid worker whose
        `opencode_reply` jobs end as a stated error -- the state `app/main.py` is
        in now, and the reason a job must never be lost to an AttributeError.
        """
        self.cfg = cfg
        self.memory = memory
        self.logger = logger or logging.getLogger("r2d2.worker")
        self._backend = opencode_backend
        self._queue: asyncio.Queue[tuple[str, dict]] = asyncio.Queue()
        self._tasks: list[asyncio.Task] = []
        self._jobs: set[asyncio.Task] = set()
        self._sem = asyncio.Semaphore(2)

    async def start(self) -> None:
        self._tasks.append(asyncio.create_task(self._loop()))

    async def stop(self) -> None:
        """Stop the loop and every job still running, so nothing outlives the worker.

        A job in flight holds one of two semaphore slots and, for an
        `opencode_reply`, an open request to opencode. Cancelling it is what stops
        `asyncio` reporting a task destroyed while pending, and `async with` in
        `_run_job` hands the slot back on the way out.
        """
        in_flight = [*self._jobs]
        for task in (*self._tasks, *in_flight):
            task.cancel()
        await asyncio.gather(*self._tasks, *in_flight, return_exceptions=True)

    async def enqueue(self, job: dict) -> str:
        job_id = await self.memory.create_job(job)
        await self._queue.put((job_id, job))
        return job_id

    async def _loop(self) -> None:
        while True:
            job_id, job = await self._queue.get()
            task = asyncio.create_task(self._run_job(job_id, job))
            self._jobs.add(task)
            task.add_done_callback(self._jobs.discard)

    async def _run_job(self, job_id: str, job: dict) -> None:
        async with self._sem:
            await self.memory.set_job_status(job_id, "running")
            try:
                text = await self._dispatch(job)
                await self.memory.set_job_status(job_id, "done", result=text[:2000])
                await send_message(self.cfg, text)
            except Exception as exc:
                self.logger.exception("job %s failed", job_id)
                await self.memory.set_job_status(job_id, "error", error=str(exc)[:500])
                await send_message(self.cfg, f"Задача Р2D2 завершилась ошибкой: {exc}")

    async def _dispatch(self, job: dict) -> str:
        kind = job.get("type")
        if kind == "arxiv":
            return await self._arxiv(job)
        if kind == JOB_OPENCODE_REPLY:
            return await self._opencode_reply(job)
        return "Неизвестная задача."

    async def _opencode_reply(self, job: dict) -> str:
        """The text the opencode agent produced after the marker, or `NO_REPLY`.

        The keys are checked before the backend is touched, so a malformed job
        cannot half-run. The ceiling is applied here rather than left to
        `collect_reply`, which ends on its own idle condition but is one
        implementation away from being the thing that holds a slot for ten
        minutes.
        """
        backend = self._backend
        if backend is None:
            raise WorkerJobError(
                f"{JOB_OPENCODE_REPLY} job: this Worker was built without an opencode backend, "
                "so there is nothing to collect the reply with. Pass "
                "opencode_backend=<OpencodeSessionBackend> when constructing it."
            )
        session_id = _text_field(job, "session_id")
        since_message_id = _text_field(job, "since_message_id", allow_empty=True)
        timeout_s = _timeout_field(job)
        try:
            text = await asyncio.wait_for(
                backend.collect_reply(session_id, since_message_id, timeout_s), timeout_s
            )
        except TimeoutError as exc:
            # Indistinguishable from a TimeoutError the collector raised itself, and
            # honestly so: either way no text arrived inside the ceiling, and
            # `str(TimeoutError())` is empty, so the reason is stated here.
            raise WorkerJobError(
                f"collector timeout: opencode produced no assistant text in session "
                f"{session_id!r} within {timeout_s:g}s. The turn is still running server-side "
                "and nothing aborts it, so its answer is still there to be asked for."
            ) from exc
        return text or NO_REPLY

    async def _arxiv(self, job: dict) -> str:
        query = job.get("query", "")
        days = int(job.get("days") or 7)
        max_results = int(job.get("max_results") or 5)
        entries = await arxiv_tool.arxiv_fetch(query, max_results=max_results, days=days)
        if not entries:
            return f"По запросу «{query}» за последние {days} дней статей на arxiv не нашёл."
        summary = await arxiv_tool.summarize_entries(self.cfg, query, entries)
        return arxiv_tool.build_digest(query, entries, summary)
