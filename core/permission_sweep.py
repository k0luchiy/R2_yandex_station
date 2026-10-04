"""The 30 s fail-safe: find every ask of R2D2's that nobody answered, on its own clock.

An ask opencode is waiting on is a turn that does not finish, so the broker owes the
user a decision even when the user says nothing at all. This module is the half of that
duty which is a matter of policy rather than of the wire: it walks the applications that
have a pending row, works out which of their asks are past `r2d2_permission_timeout`,
writes the queue back without them, and hands the broker the two lists it needs -- the
asks to refuse, and the asks that became the question while it was doing that. Posting
the refusal and telling the user stay in `core/permissions.py`, because that module is
the one place an answer can reach opencode, and a fail-safe that could answer on its own
would be a second place to get the two-value domain wrong.

Three properties are decisions rather than plumbing:

* **Every ask is judged on its OWN clock.** The row holds a queue
  (`core/permission_queue.py`), so a fresh ask that happened to be waiting behind an
  expired one is promoted and put to the user instead of being refused with it. Refusing
  a question the user was never asked is the failure the queue exists to remove, and a
  sweep that judged the queue as a unit would bring it straight back.
* **The row is the population.** An ask outlives a restart, its session binding and its
  server session, so nothing is held in a variable: every application with any pending
  row is re-read under the broker's per-application lock, which is also what keeps a
  user's own `да` from racing this loop, and what keeps the shell confirmation in the same
  table out of it.
* **One loop per broker, and a second `start()` is a no-op.** Two loops over one row would
  refuse the same ask twice, and the second refusal is a 404 the user does not need.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Final

from core.memory import Memory
from core.pending_permission import PendingPermission, PermissionQueue

__all__ = ["SWEEP_INTERVAL_S", "PermissionSweep", "SweepFound", "park_bound_s"]

#: The loop's own interval: short against a 300 s window, long against a server. A module
#: constant rather than a config field because nothing about it is a deployment choice.
SWEEP_INTERVAL_S: Final = 30.0


def park_bound_s(window_s: float, sweep_s: float | None = None) -> float:
    """How long an unanswered ask can keep a session parked: the window plus one sweep.

    The broker closes an ask at `window_s`, but it only LOOKS every `sweep_s`, so the
    refusal cannot land before the window is up AND a tick has gone by. Anything that
    waits out a park has to allow for both, or it gives up while the server is still
    blocked -- which is the difference between a delayed turn and a lost one.

    One function because there are two waiters and they must not disagree: the answer
    gate (`core/answer_gate.py`) decides when to stop believing a turn is still coming,
    and the hand-over wait (`core/session_settle.py`) decides how long to keep a task
    out of a parked session. A wait sized from the window alone would stop 30 s before
    the refusal that ends the park, and the turn behind it would be handed to a session
    the server still calls busy.

    `sweep_s` is READ at call time rather than bound as a default argument, so the
    cadence stays one fact in this module and a test that shrinks the sweep really
    shrinks it. A default captured at import would leave both waiters pinned to 30 s
    however the module is patched afterwards.
    """
    return window_s + (SWEEP_INTERVAL_S if sweep_s is None else sweep_s)


@dataclass(frozen=True, slots=True)
class SweepFound:
    """What one pass found: the asks past their window, and the ones promoted past them.

    Both lists carry the `application_id` they belong to, so the broker can log and post
    per user without reading the rows again. `promoted` is a separate list because a
    promotion is not a refusal: that ask was fresh, and the only thing this sweep did to
    it was make it the question.
    """

    refusals: tuple[tuple[str, PendingPermission], ...] = ()
    promoted: tuple[tuple[str, PendingPermission], ...] = ()


class PermissionSweep:
    """One loop per broker: find the expired asks and close their rows, and nothing else.

    `interval_s` is a parameter only so a test can prove the loop sweeps without spending
    30 s of wall clock; the window is the operator's, read from the config by the broker.
    `claim` is the broker's per-application lock, handed in so this loop and a user's own
    answer exclude each other instead of each keeping a lock of its own -- and the broker
    is what a tick calls back into, because refusing an ask and telling the user about it
    are its job and not this module's.
    """

    def __init__(
        self,
        memory: Memory,
        window_s: float,
        claim: Callable[[str], asyncio.Lock],
        *,
        interval_s: float = SWEEP_INTERVAL_S,
        logger: logging.Logger | None = None,
    ) -> None:
        self._memory = memory
        self._window_s = window_s
        self._claim = claim
        self._interval_s = interval_s
        self._log = logger or logging.getLogger("core.permissions")
        self._task: asyncio.Task[None] | None = None

    async def run_once(self) -> SweepFound:
        """Close out every ask past the window. Never posts and never sends anything."""
        refusals: list[tuple[str, PendingPermission]] = []
        promoted: list[tuple[str, PendingPermission]] = []
        for app_id in await self._memory.all_pending_ids():
            async with self._claim(app_id):
                queue = PermissionQueue.from_record(await self._memory.get_pending(app_id) or {})
                if queue is None or queue.head is None:
                    continue
                now = time.time()
                expired = tuple(
                    ask for ask in queue.outstanding if now - ask.requested_at > self._window_s
                )
                if not expired:
                    continue
                remaining = queue.without(expired)
                if remaining is None:
                    await self._memory.clear_pending(app_id)
                else:
                    await self._memory.set_pending(app_id, remaining.as_record())
                if remaining is not None and queue.head.is_same(expired[0]):
                    promoted.append((app_id, remaining.head))
            refusals.extend((app_id, ask) for ask in expired)
        return SweepFound(tuple(refusals), tuple(promoted))

    async def start(self, tick: Callable[[], Awaitable[None]]) -> bool:
        """Arm the loop over `tick`. `False` when already armed, so a second call is a no-op.

        The work of a pass stays the broker's: this loop only decides WHEN to look, and
        hands the tick the asks it found rather than posting or sending anything itself.
        """
        if self._task is not None:
            self._log.warning(
                "opencode permissions: already armed every %ss; not starting a second sweep",
                self._interval_s,
            )
            return False
        self._log.info(
            "opencode permissions: sweep armed every %ss; every ask older than %ss is refused",
            self._interval_s, self._window_s,
        )
        self._task = asyncio.create_task(self._forever(tick), name="r2d2-permission-sweep")
        return True

    async def stop(self) -> None:
        """Cancel the loop and wait for it, so shutdown leaves no pending task."""
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    # -- internals ---------------------------------------------------------

    async def _forever(self, tick: Callable[[], Awaitable[None]]) -> None:
        while True:
            try:
                await tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                # A tick that raises used to end this loop, and nothing restarts
                # it: the 300-second refusal that stops a parked session being
                # answerable FOREVER stopped happening, silently, for the life of
                # the process. One bad tick is a bad tick, not the end of the sweep.
                self._log.exception("permission sweep tick failed; the next one still runs")
            await asyncio.sleep(self._interval_s)
