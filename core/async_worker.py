import asyncio
import logging

from app.config import Config
from core.memory import Memory
from core.tools import arxiv_tool
from core.tools.telegram_tool import send_message


class Worker:
    def __init__(self, cfg: Config, memory: Memory, logger: logging.Logger | None = None):
        self.cfg = cfg
        self.memory = memory
        self.logger = logger or logging.getLogger("r2d2.worker")
        self._queue: asyncio.Queue = asyncio.Queue()
        self._tasks: list[asyncio.Task] = []
        self._sem = asyncio.Semaphore(2)

    async def start(self) -> None:
        self._tasks.append(asyncio.create_task(self._loop()))

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def enqueue(self, job: dict) -> str:
        job_id = await self.memory.create_job(job)
        await self._queue.put((job_id, job))
        return job_id

    async def _loop(self) -> None:
        while True:
            job_id, job = await self._queue.get()
            asyncio.create_task(self._run_job(job_id, job))

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
                await send_message(self.cfg, f"Задача R2D2 завершилась ошибкой: {exc}")

    async def _dispatch(self, job: dict) -> str:
        kind = job.get("type")
        if kind == "arxiv":
            return await self._arxiv(job)
        return "Неизвестная задача."

    async def _arxiv(self, job: dict) -> str:
        query = job.get("query", "")
        days = int(job.get("days") or 7)
        max_results = int(job.get("max_results") or 5)
        entries = await arxiv_tool.arxiv_fetch(query, max_results=max_results, days=days)
        if not entries:
            return f"По запросу «{query}» за последние {days} дней статей на arxiv не нашёл."
        summary = await arxiv_tool.summarize_entries(self.cfg, query, entries)
        return arxiv_tool.build_digest(query, entries, summary)
