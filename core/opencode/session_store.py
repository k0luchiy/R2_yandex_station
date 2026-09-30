"""One opencode session per R2D2 user, and the reaper for the ones a crashed run
left busy (plan todo 8).

R2D2 has to keep **one session per end user forever**: created on the first turn
and reused afterwards, so Alice's context survives between turns. The mapping
from Alice's `application_id` to an opencode `session_id` therefore lives in
SQLite, and this module is the only thing allowed to change it.

Three decisions are load-bearing:

* **A binding is a claim about the server, not about the database.** A row whose
  `session_id` the server has never heard of is a wedge: every later turn would
  be posted into a session that does not exist, and the failure would surface as
  a silent empty answer much later. `resolve` re-checks the binding against
  `GET /session` and replaces a dead one instead of returning its id.
* **`resolve` is the only path that creates a session.** Two Alice requests can
  arrive at the same instant, so the whole read-verify-create-bind sequence runs
  under a lock keyed by `application_id`; the second caller waits and then finds
  the session the first one created. The lock is per application rather than
  global so two users never block each other.
* **The reaper only kills what is provably stuck.** A session is aborted when the
  server reports it `busy` AND it has not been used for
  `r2d2_stale_session_seconds`. A slow turn is not a dead turn: the first message
  in a fresh session measured 15.5-18.6s (C8) and must never be cut off.

Per **C6** the server does not validate `?directory=`, so this module checks the
workspace itself rather than relying on the client's per-request check -- todo 18
builds the client at startup, and a bad workspace has to fail there too.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Final

import httpx

from app.config import Config
from core.memory import Memory
from core.opencode.client import OpencodeClient, OpencodeError

log = logging.getLogger(__name__)

#: The prefix of the one title R2D2 gives a user's session. It carries the
#: application id, so a session is findable again even if the database is lost.
TITLE_PREFIX: Final = "r2d2:alice:"
#: The only `GET /session/status` value that may be aborted.
BUSY: Final = "busy"


def title_for(application_id: str) -> str:
    """`f"r2d2:alice:{application_id}"` -- the only title R2D2 ever creates."""
    return f"{TITLE_PREFIX}{application_id}"


class OcSessionStore:
    def __init__(self, memory: Memory, client: OpencodeClient, cfg: Config) -> None:
        self._memory = memory
        self._client = client
        self._workspace = cfg.resolved_workspace()
        self._stale_after_s = cfg.r2d2_stale_session_seconds
        self._resolving: dict[str, asyncio.Lock] = {}

    async def resolve(self, application_id: str) -> str:
        """The session id for this user, created on the first call and reused after.

        The workspace is checked before anything else: a session created against
        a directory that is not there would hand the agent's file tools a root
        that does not exist, and the server answers 200 either way (C6).
        """
        self._require_workspace()
        async with self._lock_for(application_id):
            return await self._resolve_locked(application_id)

    async def reap(self) -> int:
        """Abort every session that is busy AND past its staleness window.

        Returns how many sessions the server actually stopped. Failures are
        contained: a session the server has already dropped answers the abort
        with a 404, and a status call that cannot be made at all means the sweep
        learned nothing. Either way the sweep is incomplete rather than wrong --
        aborting on a guess would kill live turns -- and the next sweep retries.
        """
        bindings = await self._memory.all_oc_sessions()
        if not bindings:
            return 0
        try:
            statuses = await self._client.session_status()
        except (httpx.HTTPError, OpencodeError) as exc:
            log.warning("opencode session reaper: swept nothing: %s", exc)
            return 0
        now = time.time()
        aborted = 0
        for binding in bindings:
            if statuses.get(binding.session_id) != BUSY:
                continue
            idle_for = now - binding.last_used_at
            if idle_for <= self._stale_after_s:
                continue
            try:
                stopped = await self._client.abort(binding.session_id)
            except (httpx.HTTPError, OpencodeError) as exc:
                log.warning(
                    "opencode session reaper: cannot abort the stale busy session %s of %s: %s",
                    binding.session_id, binding.application_id, exc,
                )
                continue
            aborted += int(stopped)
            log.warning(
                "opencode session reaper: asked to abort the stale busy session %s of %s "
                "(unused for %.0fs); the server %s",
                binding.session_id, binding.application_id, idle_for,
                "stopped it" if stopped else "did not stop it",
            )
        return aborted

    async def message_count(self, application_id: str) -> int:
        """How many messages this user's session holds; 0 means brand new.

        Todo 15 needs this before the first turn: a cold session costs 15.5-18.6s
        and must go async, a warm one answers inside the voice budget.
        """
        binding = await self._memory.get_oc_session(application_id)
        return binding.message_count if binding is not None else 0

    # -- internals ---------------------------------------------------------

    async def _resolve_locked(self, application_id: str) -> str:
        title = title_for(application_id)
        binding = await self._memory.get_oc_session(application_id)
        if binding is None:
            return await self._create(application_id, title)
        live = await self._client.find_session(title)
        if live == binding.session_id:
            await self._memory.touch_oc_session(application_id, message_delta=0)
            return live
        # The bound session is gone, and it is NOT unbound before the create: a
        # create that then fails must leave a row that still reads as dead, not a
        # user with no session at all. `bind_oc_session` replaces it atomically.
        if live is None:
            log.warning(
                "opencode session store: %s is bound to session %s, which the server no longer "
                "has; creating a replacement",
                application_id, binding.session_id,
            )
            return await self._create(application_id, title)
        log.warning(
            "opencode session store: %s is bound to session %s, but the server holds that title "
            "as %s now; re-binding",
            application_id, binding.session_id, live,
        )
        await self._memory.bind_oc_session(application_id, live, title)
        return live

    async def _create(self, application_id: str, title: str) -> str:
        session_id = await self._client.create_session(title)
        await self._memory.bind_oc_session(application_id, session_id, title)
        log.info(
            "opencode session store: %s -> new session %s (%s)", application_id, session_id, title
        )
        return session_id

    def _require_workspace(self) -> None:
        if not os.path.isdir(self._workspace):
            raise OpencodeError(
                f"opencode session store: workspace {self._workspace!r} does not exist and the "
                "server does not validate ?directory= (C6), so no session was created and "
                "nothing was sent"
            )

    def _lock_for(self, application_id: str) -> asyncio.Lock:
        # No lock is needed to fill this dict: the lookup and the store happen
        # with no await in between, and asyncio runs one task at a time.
        lock = self._resolving.get(application_id)
        if lock is None:
            lock = self._resolving[application_id] = asyncio.Lock()
        return lock
