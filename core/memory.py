import asyncio
import json
import time
import uuid

import aiosqlite


class Memory:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> "Memory":
        self._db = await aiosqlite.connect(self.db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute(
            "CREATE TABLE IF NOT EXISTS sessions ("
            " application_id TEXT PRIMARY KEY,"
            " history TEXT NOT NULL DEFAULT '[]',"
            " updated_at REAL NOT NULL)"
        )
        await self._db.execute(
            "CREATE TABLE IF NOT EXISTS pending_actions ("
            " application_id TEXT PRIMARY KEY,"
            " action_json TEXT NOT NULL,"
            " created_at REAL NOT NULL)"
        )
        await self._db.execute(
            "CREATE TABLE IF NOT EXISTS jobs ("
            " job_id TEXT PRIMARY KEY,"
            " type TEXT NOT NULL,"
            " params TEXT NOT NULL DEFAULT '{}',"
            " status TEXT NOT NULL DEFAULT 'pending',"
            " result TEXT,"
            " error TEXT,"
            " created_at REAL NOT NULL,"
            " updated_at REAL NOT NULL)"
        )
        await self._db.commit()
        return self

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    async def load_history(self, application_id: str) -> list[dict]:
        return await self._load_history(application_id)

    async def _load_history(self, application_id: str) -> list[dict]:
        async with self._lock:
            cur = await self._db.execute(
                "SELECT history FROM sessions WHERE application_id = ?",
                (application_id,),
            )
            row = await cur.fetchone()
        if row is None:
            return []
        try:
            return json.loads(row["history"])
        except (TypeError, ValueError):
            return []

    async def append_message(
        self, application_id: str, role: str, content: str, max_history: int = 20
    ) -> None:
        if not content:
            return
        history = await self._load_history(application_id)
        history.append({"role": role, "content": content[:4000]})
        history = history[-max_history:]
        async with self._lock:
            await self._db.execute(
                "INSERT INTO sessions (application_id, history, updated_at) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(application_id) DO UPDATE SET "
                " history = excluded.history, updated_at = excluded.updated_at",
                (application_id, json.dumps(history, ensure_ascii=False), time.time()),
            )
            await self._db.commit()

    async def set_pending(self, application_id: str, action: dict) -> None:
        async with self._lock:
            await self._db.execute(
                "INSERT INTO pending_actions (application_id, action_json, created_at) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(application_id) DO UPDATE SET action_json = excluded.action_json, "
                " created_at = excluded.created_at",
                (application_id, json.dumps(action, ensure_ascii=False), time.time()),
            )
            await self._db.commit()

    async def get_pending(self, application_id: str) -> dict | None:
        async with self._lock:
            cur = await self._db.execute(
                "SELECT action_json FROM pending_actions WHERE application_id = ?",
                (application_id,),
            )
            row = await cur.fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["action_json"])
        except (TypeError, ValueError):
            return None

    async def clear_pending(self, application_id: str) -> None:
        async with self._lock:
            await self._db.execute(
                "DELETE FROM pending_actions WHERE application_id = ?", (application_id,)
            )
            await self._db.commit()

    async def create_job(self, params: dict) -> str:
        job_id = uuid.uuid4().hex[:12]
        now = time.time()
        async with self._lock:
            await self._db.execute(
                "INSERT INTO jobs (job_id, type, params, status, created_at, updated_at) "
                "VALUES (?, ?, ?, 'pending', ?, ?)",
                (job_id, params.get("type", "unknown"), json.dumps(params, ensure_ascii=False), now, now),
            )
            await self._db.commit()
        return job_id

    async def set_job_status(self, job_id: str, status: str, result: str | None = None, error: str | None = None) -> None:
        now = time.time()
        async with self._lock:
            await self._db.execute(
                "UPDATE jobs SET status = ?, result = ?, error = ?, updated_at = ? WHERE job_id = ?",
                (status, result, error, now, job_id),
            )
            await self._db.commit()
