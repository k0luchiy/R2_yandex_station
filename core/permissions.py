"""The permission broker: opencode asks, a human answers, nothing else (plan todo 14).

opencode raises `permission.asked` *before* it runs a risky command and blocks the
turn until somebody answers, so a wrong answer here is arbitrary code execution.

* **Two answers, one of them a trap.** The server also offers a durable grant in
  `properties.always` -- a RULE, not a per-turn answer -- so the domain is the
  two-value `Literal` below, the masks are LOGGED and never sent, no third typechecks.

* **Every ambiguous path refuses**, and the row is cleared even then. A replayed ask
  keeps the original clock; a different ask refuses the one it evicts.

* **One `application_id` per human**: the row is written and read under the id of
  the turn that raised it, which is why the ingress declares one id per chat.

Alice cannot push, so the question goes to Telegram and the answer returns as text;
no `core.render` is imported, and the ask is `core/pending_permission.py`'s value.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Sequence
from contextlib import suppress
from types import MappingProxyType
from typing import Final, Literal, TypeAlias

import httpx

from app.config import Config
from core.memory import Memory
from core.opencode.client import OpencodeClient, OpencodeError
from core.opencode.sse import EVENT_MODE
from core.pending_permission import KIND, PendingPermission
from core.permission_words import ACCEPTED, PREVIEW_CHARS, QUESTION, REFUSED, UNANSWERABLE, UNANSWERED, UNTITLED, preview
from core.policies import confirmation_verdict
from core.routing import for_human
from core.tools.telegram_tool import send_message

# `KIND` and `PREVIEW_CHARS` are imported for compatibility only: both were
# reachable as `core.permissions.NAME` before the split and are used by the two
# modules that now own them, so a caller reaching through this path still resolves.

log = logging.getLogger(__name__)

__all__ = [
    "ANSWERS", "APPROVE_ONCE", "ASK_SOURCE", "DURABLE_GRANT", "PendingPermission", "PermissionAnswer",
    "PermissionBroker", "PermissionVerdict", "REFUSE", "SWEEP_INTERVAL_S",
]

#: The whole answer domain. `respond_permission` carries the same `Literal` in
#: `core.opencode.client`, so a third value does not typecheck on the way to the wire
#: either -- the invariant lives in the type, and the test reads the source.
PermissionAnswer: TypeAlias = Literal["once", "reject"]
#: The one approval: run this once. It is forgotten with the turn.
APPROVE_ONCE: Final[PermissionAnswer] = "once"
#: The one refusal -- and the answer every timeout and every failure in this module
#: posts, because an unanswered risky command must default to not running.
REFUSE: Final[PermissionAnswer] = "reject"
#: What the server offers in `properties.always` and what this module refuses by
#: name, so the refusal is a value the guard test can check rather than a comment.
DURABLE_GRANT: Final = "always"
#: Everything this module may ever answer with, and the assertion of the invariant.
ANSWERS: Final[frozenset[str]] = frozenset({APPROVE_ONCE, REFUSE})
assert ANSWERS == frozenset({"once", "reject"}) and DURABLE_GRANT not in ANSWERS

#: What `resolve_from_text` reports. Only `approved` may let a tool run, and it is
#: returned only when the server confirmed the answer landed, so a caller gating on
#: it can never be told an approval the tool did not get.
PermissionVerdict: TypeAlias = Literal["approved", "rejected", "unrelated"]
APPROVED: Final[PermissionVerdict] = "approved"
REJECTED: Final[PermissionVerdict] = "rejected"
UNRELATED: Final[PermissionVerdict] = "unrelated"

#: The plan's sweep cadence, and the window the operator dials with
#: `r2d2_permission_timeout`.
SWEEP_INTERVAL_S: Final = 30.0
#: Where an ask comes from in each live `EVENT_MODE` -- the only thing the two live
#: modes differ in. A mode that is not in this table is the inert one, which is the
#: direction to be wrong in: a build that does not know its own mode must not answer
#: permissions.
ASK_SOURCE: Final[Mapping[str, str]] = MappingProxyType(
    {
        "sse": "as `permission.asked` on the opencode event stream",
        "poll": "from the polling collector that reads the same session",
    }
)


class PermissionBroker:
    """Turns an opencode permission ask into a human decision -- and only that.

    The four collaborators are the same ones todo 8 assembled: the pending row in
    `Memory` (`core/pending_permission.py` -- a value, not a variable), the opencode
    client, the operator's config, and Telegram. There is no second source of truth
    for "what is waiting", and no variable that could forget an ask on a restart.
    """

    def __init__(
        self,
        memory: Memory,
        client: OpencodeClient,
        cfg: Config,
        *,
        interval_s: float = SWEEP_INTERVAL_S,
    ) -> None:
        self._memory = memory
        self._client = client
        self._cfg = cfg
        self._window_s = cfg.r2d2_permission_timeout
        # The cadence is the plan's 30 s; it is a parameter only so a test can prove
        # the loop sweeps without spending 30 s of wall clock.
        self._interval_s = interval_s
        self._claims: dict[str, asyncio.Lock] = {}
        self._task: asyncio.Task[None] | None = None

    async def on_permission_requested(
        self, app_id: str,
        session_id: str,
        permission_id: str,
        title: str,
        always: Sequence[str],
    ) -> None:
        """Record an ask and put the question to the user in Telegram. Speaks nothing.

        Five parameters because four of them ARE the `permission.asked` event, which
        `OpencodeEvent` has already parsed, and the fifth is R2D2's own application
        id, which the event does not carry. The row is written BEFORE the question
        goes out -- a crash between the two must leave an ask the sweep can refuse,
        never a question whose answer has nowhere to land -- and the masks are
        logged here, which is the only thing done with them. Storing takes over the
        one row this user has, so a shell confirmation waiting there is replaced; that
        is the safe direction (the command does not run) and todo 15 reads the ask
        first, so the case is a lost confirmation and not a lost command.
        """
        ask = PendingPermission(
            session_id=session_id,
            permission_id=permission_id,
            title=title,
            masks=tuple(always),
            requested_at=time.time(),
        )
        async with self._claim_for(app_id):
            stored = PendingPermission.from_record(await self._memory.get_pending(app_id) or {})
            repeated = stored is not None and stored.is_same(ask)
            if stored is not None and not repeated:
                # The row holds one ask per user, so this one is now unanswerable:
                # refusing it is the only honest thing left to do with it.
                log.warning(
                    "opencode permissions: %s still has the unanswered ask %s of session %s; "
                    "refusing it, because the new ask replaces it",
                    app_id, stored.permission_id, stored.session_id,
                )
                await self._post(stored, REFUSE)
            if not repeated:
                await self._memory.set_pending(app_id, ask.as_record())
        log.info(
            "opencode permissions: %s must confirm %r; a durable grant (%s) would cover %s for "
            "the rest of the session and is never sent",
            app_id, preview(ask.title), DURABLE_GRANT, ", ".join(ask.masks) or "nothing",
        )
        await self._tell(QUESTION.format(title=ask.title or UNTITLED))

    async def resolve_from_text(self, app_id: str, text: str) -> PermissionVerdict:
        """The user's answer to whatever is pending, read as a verdict.

        `approved` means the SERVER took a one-time approval, nothing weaker.
        Everything else -- a refusal, a timeout, a 500, a `200 false`, a 404 -- is
        `rejected`, because the caller asks "may this tool run?" and only one answer
        to that may ever be yes; `unrelated` means there was no ask of ours, so the
        caller carries on as before. The words are `core/policies.py`'s call, matched
        as whole words on the affirmative side -- `да` is a substring of `дай`, and a
        sentence is not an answer -- with DENY read before AFFIRM ("да, не надо" is a
        refusal). The row is cleared even when the POST fails, so a server that forgot
        the session cannot re-ask for ever.
        """
        verdict = confirmation_verdict(text)
        if verdict is None:
            return UNRELATED
        async with self._claim_for(app_id):
            ask = PendingPermission.from_record(await self._memory.get_pending(app_id) or {})
            if ask is None:
                return UNRELATED
            if verdict == "yes":
                landed, notice = await self._post(ask, APPROVE_ONCE), ACCEPTED
            else:
                landed, notice = await self._post(ask, REFUSE), REFUSED
            await self._memory.clear_pending(app_id)
            if not landed:
                # The user is owed the truth: the server never took the answer, so
                # whatever it is holding is still blocked.
                notice = UNANSWERABLE
        await self._tell(notice.format(title=ask.title or UNTITLED))
        return APPROVED if landed and verdict == "yes" else REJECTED

    async def sweep_timeouts(self) -> int:
        """Refuse every ask that is still unanswered after the window. Fail-safe.

        The returned count is refusals THE SERVER TOOK, not rows cleared: a 500 or a
        `200 false` leaves an incomplete sweep, and a caller watching the number must
        not read it as "everything is fine now". The population is every application
        with a pending row of any kind -- the row is this module's own source of
        truth, so an ask outlives a restart, its session binding and its server
        session -- and each one is re-read under the per-application lock and filtered
        by `kind`, which keeps the shell confirmation out of this loop.
        """
        refused = 0
        for app_id in await self._memory.all_pending_ids():
            async with self._claim_for(app_id):
                ask = PendingPermission.from_record(await self._memory.get_pending(app_id) or {})
                if ask is None or time.time() - ask.requested_at <= self._window_s:
                    continue
                landed = await self._post(ask, REFUSE)
                await self._memory.clear_pending(app_id)
                refused += int(landed)
            log.warning(
                "opencode permissions: %s left the %r ask of session %s unanswered for %.0fs; %s",
                app_id, preview(ask.title), ask.session_id, self._window_s,
                "it was refused" if landed else "the server did NOT take the refusal",
            )
            await self._tell(UNANSWERED if landed else UNANSWERABLE.format(title=ask.title or UNTITLED))
        return refused

    async def start(self) -> None:
        """Arm the sweep, or say at INFO why this build stays inert. Called once.

        A mode missing from `ASK_SOURCE` is inert by construction: a build that does
        not recognise its own configuration must not answer permissions, because
        refusing is recoverable and answering wrongly is not.
        """
        source = ASK_SOURCE.get(EVENT_MODE)
        if source is None:
            log.info(
                "opencode permissions: broker is inert; the agent config already denies bash, edit "
                "and external_directory, so no ask can arrive"
            )
            return
        if self._task is not None:
            log.warning(
                "opencode permissions: already armed every %ss; not starting a second sweep",
                self._interval_s,
            )
            return
        log.info(
            "opencode permissions: sweep armed every %ss; asks arrive %s", self._interval_s, source
        )
        self._task = asyncio.create_task(self._sweep_forever(), name="r2d2-permission-sweep")

    async def stop(self) -> None:
        """Cancel the sweep and wait for it, so shutdown leaves no pending task."""
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    # -- internals ---------------------------------------------------------

    async def _sweep_forever(self) -> None:
        while True:
            await self.sweep_timeouts()
            await asyncio.sleep(self._interval_s)

    async def _post(self, ask: PendingPermission, answer: PermissionAnswer) -> bool:
        """The one place that answers a permission; whether the server took it.

        `answer` is the two-value domain above and all this call can send: a durable
        grant is not a value the parameter can hold. A failure is absorbed and
        reported as `False` rather than raised, because every caller must still clear
        its row and tell the user -- and a `200 false` is as much a refusal as a 500.
        """
        try:
            return await self._client.respond_permission(ask.session_id, ask.permission_id, answer)
        except (httpx.HTTPError, OpencodeError) as exc:
            log.warning(
                "opencode permissions: the server took no answer for the ask %s of session %s: %s",
                ask.permission_id, ask.session_id, exc,
            )
            return False

    async def _tell(self, text: str) -> None:
        """Send one Telegram message. A channel that is down must not stop the answer.

        The text is the user's and can be long and multi-line (a title is
        server-controlled), so it is never logged back -- only its size when Telegram
        refused it.

        The title is interpolated into the question, notice and refusal templates
        below, and it comes from the SERVER, so a command the user actually typed
        containing the routing token would otherwise reach their own chat as a raw
        protocol marker -- the leak D4 was opened for, arriving by a second door.
        `for_human` is the one outbound guard for the whole project, so this path
        uses it too. Every template carries prose of its own, so the `or text`
        fallback is unreachable in practice; it exists so an emptied body degrades
        to what it was rather than to an empty message.
        """
        text = for_human(text, sentinel=self._cfg.r2d2_needs_agent_sentinel) or text
        if not await send_message(self._cfg, text):
            log.warning("opencode permissions: Telegram took no message of %d chars", len(text))

    def _claim_for(self, app_id: str) -> asyncio.Lock:
        """The lock that makes an ask single-use: two answers arriving in the same
        instant must find the row claimed once, not answer one permission twice.

        Per application, so two users never wait on each other. No lock guards the
        dict: the lookup and the store have no await between them.
        """
        lock = self._claims.get(app_id)
        if lock is None:
            lock = self._claims[app_id] = asyncio.Lock()
        return lock
