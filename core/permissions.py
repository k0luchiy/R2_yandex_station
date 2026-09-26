"""The permission broker: opencode asks, a human answers, nothing else (plan todo 14).

opencode raises `permission.asked` *before* it runs a risky command and blocks the turn
until somebody answers, so a wrong answer here is arbitrary code execution.

* **Two answers, one of them a trap.** The server also offers a durable grant in
  `properties.always` -- a RULE, not a per-turn answer -- so the domain is the two-value
  `Literal` below, the masks are LOGGED and never sent, no third value typechecks.
* **More than one ask at a time, one QUESTION at a time.** Two asks 0.55 s apart used to
  mean the first was refused before the user could answer it (`qa/live-run-v3.md` §D14), so
  the row under `application_id` holds a queue whose head is the ask on screen.
  `core/pending_permission.py` owns that value, `core/permission_sweep.py` the 300 s
  fail-safe over it; what is left HERE touches the wire and the chat, so this file stays
  the single place an answer can reach opencode.
* **Every ambiguous path refuses.** An answer the server did not take reads as a refusal, an
  unreadable record is left alone, a full queue refuses the ask that does NOT fit, and a
  replayed ask keeps the original clock.
* **One `application_id` per human**: the row is written and read under the id of the turn
  that raised it, which is why the ingress declares one id per chat.

Alice cannot push, so the question goes to Telegram and the answer returns as text.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Sequence
from types import MappingProxyType
from typing import Final, Literal, TypeAlias

import httpx

from app.config import Config
from core.memory import Memory
from core.opencode.client import OpencodeClient, OpencodeError
from core.opencode.sse import EVENT_MODE
from core.pending_permission import KIND, PendingPermission, PermissionQueue
from core.permission_sweep import SWEEP_INTERVAL_S, PermissionSweep
from core.permission_words import ACCEPTED, OVERLOADED, PREVIEW_CHARS, QUESTION, REFUSED, UNANSWERABLE, UNANSWERED, UNTITLED, preview
from core.policies import confirmation_verdict
from core.routing import for_human
from core.tools.telegram_tool import send_message

# `KIND` and `PREVIEW_CHARS` are imported for compatibility only: both were
# reachable as `core.permissions.NAME` before the split and are used by the two
# modules that now own them, so a caller reaching through this path still resolves.

log = logging.getLogger(__name__)

__all__ = [
    "ANSWERS", "APPROVE_ONCE", "ASK_SOURCE", "DURABLE_GRANT", "PermissionAnswer", "PermissionBroker",
    "PermissionVerdict", "REFUSE", "SWEEP_INTERVAL_S",
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

# `SWEEP_INTERVAL_S` is re-exported from `core/permission_sweep.py`, which owns the loop
# as well as its cadence; it stays on this module's `__all__` because it was here first.
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

    The four collaborators are the same ones todo 8 assembled: the pending row in `Memory`
    (a value, not a variable), the opencode client, the operator's config, and Telegram.
    There is no second source of truth for "what is waiting", and no variable that could
    forget an ask on a restart.
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
        self._claims: dict[str, asyncio.Lock] = {}
        self._sweep = PermissionSweep(memory, self._window_s, self._claim_for,
                                      interval_s=interval_s, logger=log)

    async def on_permission_requested(
        self,
        app_id: str,
        session_id: str,
        permission_id: str,
        title: str,
        always: Sequence[str],
    ) -> None:
        """Record an ask and put the question to the user in Telegram. Speaks nothing.

        Five parameters because four of them ARE the `permission.asked` event and the fifth
        is R2D2's own application id, which the event does not carry. The row is written
        BEFORE the question goes out -- a crash between the two must leave an ask the sweep
        can refuse, never a question whose answer has nowhere to land -- and the masks are
        logged here, which is the only thing done with them. An ask arriving while another
        is open joins the queue behind the one on screen, so only one question is ever on
        screen and no ask becomes unanswerable.
        """
        ask = PendingPermission(
            session_id=session_id, permission_id=permission_id, title=title,
            masks=tuple(always), requested_at=time.time(),
        )
        ask_human, unreachable = await self._store(app_id, ask)
        if unreachable is not None:
            await self._post(unreachable, REFUSE)
            await self._tell(OVERLOADED.format(title=unreachable.title or UNTITLED))
        log.info("opencode permissions: %s must confirm %r; a durable grant (%s) would cover %s "
                 "for the rest of the session and is never sent",
                 app_id, preview(ask.title), DURABLE_GRANT, ", ".join(ask.masks) or "nothing")
        if ask_human:
            await self._tell(QUESTION.format(title=ask.title or UNTITLED))

    async def resolve_from_text(self, app_id: str, text: str) -> PermissionVerdict:
        """The user's answer to whatever is pending, read as a verdict.

        `approved` means the SERVER took a one-time approval, nothing weaker. Everything
        else -- a refusal, a timeout, a 500, a `200 false`, a 404 -- is `rejected`, because
        the caller asks "may this tool run?" and only one answer to that may ever be yes;
        `unrelated` means there was no ask of ours. The head is popped even when the POST
        fails, so a server that forgot the session cannot re-ask for ever, and the ask
        behind it is then put to the user as the new question.
        """
        verdict = confirmation_verdict(text)
        if verdict is None:
            return UNRELATED
        async with self._claim_for(app_id):
            queue = PermissionQueue.from_record(await self._memory.get_pending(app_id) or {})
            if queue is None or queue.head is None:
                return UNRELATED
            ask = queue.head
            if verdict == "yes":
                landed, notice = await self._post(ask, APPROVE_ONCE), ACCEPTED
            else:
                landed, notice = await self._post(ask, REFUSE), REFUSED
            remaining = queue.answered()
            await self._save(app_id, remaining)
            if not landed:
                # The user is owed the truth: the server never took the answer, so
                # whatever it is holding is still blocked.
                notice = UNANSWERABLE
        await self._tell(notice.format(title=ask.title or UNTITLED))
        await self._ask(remaining.head if remaining is not None else None)
        return APPROVED if landed and verdict == "yes" else REJECTED

    async def sweep_timeouts(self) -> int:
        """Refuse every ask that is still unanswered after the window. Fail-safe.

        The window, the population and the per-ask clock are `core/permission_sweep.py`'s. The
        returned count is refusals THE SERVER TOOK, not rows closed: a 500 or a `200 false`
        leaves an incomplete sweep, and a caller watching the number must not read it as
        "everything is fine now".

        Every fact the log and the notice need is bound in this body, not read out of the
        per-application block above: that read worked only because the `continue` which
        skipped the log also skipped it, and a variable that exists because a loop did not
        execute is one whose value nobody chose.
        """
        found = await self._sweep.run_once()
        refused = 0
        for app_id, expired in found.refusals:
            landed = await self._post(expired, REFUSE)
            refused += int(landed)
            log.warning("opencode permissions: %s left the %r ask of session %s unanswered for "
                        "%.0fs; %s", app_id, preview(expired.title), expired.session_id,
                        self._window_s,
                        "it was refused" if landed else "the server did NOT take the refusal")
            await self._tell(
                UNANSWERED if landed else UNANSWERABLE.format(title=expired.title or UNTITLED)
            )
        for _app_id, promoted in found.promoted:
            await self._ask(promoted)
        return refused

    async def start(self) -> None:
        """Arm the sweep, or say at INFO why this build stays inert. Called once. A mode
        missing from `ASK_SOURCE` is inert by construction: a build that does not
        recognise its own configuration must not answer permissions."""
        source = ASK_SOURCE.get(EVENT_MODE)
        if source is None:
            log.info("opencode permissions: broker is inert; the agent config already denies bash, "
                     "edit and external_directory, so no ask can arrive")
            return
        log.info("opencode permissions: asks arrive %s; one left unanswered for %.0fs is refused",
                 source, self._window_s)
        await self._sweep.start(self.sweep_timeouts)

    async def stop(self) -> None:
        """Stop the sweep and wait for it, so shutdown leaves no pending task."""
        await self._sweep.stop()

    # -- internals ---------------------------------------------------------

    async def _store(
        self, app_id: str, ask: PendingPermission
    ) -> tuple[bool, PendingPermission | None]:
        """Put `ask` into this user's queue under the lock; report the ask and the refusal.
        The three outcomes are `core/pending_permission.py`'s: a replayed frame changes
        nothing, a newcomer waits behind the ask on screen, a full queue refuses the
        newcomer."""
        async with self._claim_for(app_id):
            stored = PermissionQueue.from_record(await self._memory.get_pending(app_id) or {})
            if stored is not None and stored.head is not None and stored.head.is_same(ask):
                return False, None
            if stored is not None and not stored.room:
                log.warning("opencode permissions: %s already has %d asks open and cannot hold "
                            "the %r ask of session %s; refusing IT, so every ask already stored "
                            "stays answerable",
                            app_id, len(stored.outstanding), preview(ask.title), ask.session_id)
                return False, ask
            await self._save(app_id, PermissionQueue(ask) if stored is None else stored.holding(ask))
            return stored is None, None

    async def _ask(self, ask: PendingPermission | None) -> None:
        """Put one ask to the user as the question. The only place a question is composed,
        and the only way a queued ask is ever asked."""
        if ask is not None:
            await self._tell(QUESTION.format(title=ask.title or UNTITLED))

    async def _save(self, app_id: str, queue: PermissionQueue | None) -> None:
        """Write the queue back under `app_id`, or clear the row when it is empty: one
        place knows that an empty queue is a DELETED row, not a record with no head."""
        if queue is None or queue.head is None:
            await self._memory.clear_pending(app_id)
        else:
            await self._memory.set_pending(app_id, queue.as_record())

    async def _post(self, ask: PendingPermission, answer: PermissionAnswer) -> bool:
        """The one place that answers a permission; whether the server took it.

        `answer` is the two-value domain above and all this call can send: a durable grant
        is not a value the parameter can hold. A failure is absorbed and reported as
        `False` rather than raised, because every caller must still close its row and tell
        the user -- and a `200 false` is as much a refusal as a 500.
        """
        try:
            return await self._client.respond_permission(ask.session_id, ask.permission_id, answer)
        except (httpx.HTTPError, OpencodeError) as exc:
            log.warning("opencode permissions: the server took no answer for the ask %s of "
                        "session %s: %s", ask.permission_id, ask.session_id, exc)
            return False

    async def _tell(self, text: str) -> None:
        """Send one Telegram message. A channel that is down must not stop the answer.

        The text is the user's and can be long and multi-line (a title is
        server-controlled), so it is never logged back -- only its size when Telegram
        refused it. The title is interpolated into every template and comes from the
        SERVER, so a command the user actually typed containing the routing token would
        otherwise reach their own chat as a raw protocol marker -- the leak D4 was opened
        for, arriving by a second door. `for_human` is the one outbound guard for the
        whole project, so this path uses it too, and `or text` keeps an emptied body from
        becoming an empty message.
        """
        text = for_human(text, sentinel=self._cfg.r2d2_needs_agent_sentinel) or text
        if not await send_message(self._cfg, text):
            log.warning("opencode permissions: Telegram took no message of %d chars", len(text))

    def _claim_for(self, app_id: str) -> asyncio.Lock:
        """The lock that makes an ask single-use: two answers arriving in the same instant
        must find the row claimed once, not answer one permission twice. Per application,
        so two users never wait on each other, and handed to the sweep so its refusals
        cannot race a user's own answer. No lock guards the dict: the lookup and the store
        have no await between them.
        """
        lock = self._claims.get(app_id)
        if lock is None:
            lock = self._claims[app_id] = asyncio.Lock()
        return lock
