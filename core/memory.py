import asyncio
import json
import time
import uuid
from dataclasses import dataclass

import aiosqlite


@dataclass(frozen=True)
class OcSession:
    application_id: str
    session_id: str
    title: str
    message_count: int
    created_at: float
    last_used_at: float


def _oc_session(row: aiosqlite.Row) -> OcSession:
    return OcSession(
        application_id=str(row["application_id"]),
        session_id=str(row["session_id"]),
        title=str(row["title"]),
        message_count=int(row["message_count"]),
        created_at=float(row["created_at"]),
        last_used_at=float(row["last_used_at"]),
    )


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
        await self._db.execute(
            "CREATE TABLE IF NOT EXISTS oc_sessions ("
            " application_id TEXT PRIMARY KEY,"
            " session_id TEXT NOT NULL,"
            " title TEXT NOT NULL,"
            " message_count INTEGER NOT NULL DEFAULT 0,"
            " created_at REAL NOT NULL,"
            " last_used_at REAL NOT NULL)"
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

    async def all_pending_ids(self) -> list[str]:
        """Every application that has a pending action of any kind.

        A feature that owns one kind of row (the opencode permission broker) has to
        find its rows again after a restart, and the row is its own source of truth:
        an ask can outlive the session binding it was raised in, and outlive the
        session on the server. Callers re-read each row with `get_pending` under
        their own lock, so this returns the population and nothing more.
        """
        async with self._lock:
            cur = await self._db.execute(
                "SELECT application_id FROM pending_actions ORDER BY application_id"
            )
            rows = await cur.fetchall()
        return [str(row["application_id"]) for row in rows]

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

    async def bind_oc_session(self, application_id: str, session_id: str, title: str) -> None:
        """Upsert the binding between an R2D2 user and their opencode session.

        Rebinding a *different* session resets `message_count`: the new session
        holds none of the old context, and a stale count would make a cold first
        turn (15.5-18.6s, C8) look warm. Rebinding the *same* session keeps it.
        """
        now = time.time()
        async with self._lock:
            await self._db.execute(
                "INSERT INTO oc_sessions (application_id, session_id, title, message_count, "
                "created_at, last_used_at) VALUES (?, ?, ?, 0, ?, ?) "
                "ON CONFLICT(application_id) DO UPDATE SET "
                " session_id = excluded.session_id,"
                " title = excluded.title,"
                " message_count = CASE WHEN oc_sessions.session_id = excluded.session_id "
                "   THEN oc_sessions.message_count ELSE 0 END,"
                " last_used_at = excluded.last_used_at",
                (application_id, session_id, title, now, now),
            )
            await self._db.commit()

    async def get_oc_session(self, application_id: str) -> "OcSession | None":
        async with self._lock:
            cur = await self._db.execute(
                "SELECT * FROM oc_sessions WHERE application_id = ?", (application_id,)
            )
            row = await cur.fetchone()
        return _oc_session(row) if row is not None else None

    async def touch_oc_session(self, application_id: str, *, message_delta: int) -> int:
        """Count messages into a session, refresh its clock, return the NEW count.

        `message_delta=0` refreshes the clock without counting, which is how a
        reused session stays out of the reaper's reach. An unbound application
        returns 0 rather than raising: the caller only wants to know whether the
        session is brand new. A negative delta is refused, because a count
        below zero reads as "brand new" and would put the cold first turn on the
        voice path.
        """
        if message_delta < 0:
            raise ValueError(
                f"message_delta must be >= 0, got {message_delta}: a negative count would read "
                "as a brand-new session"
            )
        async with self._lock:
            cur = await self._db.execute(
                "UPDATE oc_sessions SET message_count = message_count + ?, last_used_at = ? "
                "WHERE application_id = ? RETURNING message_count",
                (message_delta, time.time(), application_id),
            )
            row = await cur.fetchone()
            await self._db.commit()
        return int(row["message_count"]) if row is not None else 0

    async def all_oc_sessions(self) -> list[OcSession]:
        async with self._lock:
            cur = await self._db.execute("SELECT * FROM oc_sessions ORDER BY last_used_at, application_id")
            rows = await cur.fetchall()
        return [_oc_session(row) for row in rows]

    async def unbind_oc_session(self, application_id: str) -> None:
        async with self._lock:
            await self._db.execute("DELETE FROM oc_sessions WHERE application_id = ?", (application_id,))
            await self._db.commit()
