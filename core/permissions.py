"""The permission broker: opencode asks, a human answers, nothing else (plan todo 14).

opencode raises `permission.asked` *before* it runs a risky command and blocks the
turn until somebody answers. R2D2 is on the other end of that block, so this is the
narrowest and most dangerous seam in the project: a wrong answer here is arbitrary
code execution on somebody's laptop. Everything below follows from that.

* **Two answers, and one of them is a trap.** The server also accepts a durable
  grant and offers the mask for it in `properties.always` (U4 measured
  `["echo *"]`); one such grant is not a per-turn answer but a RULE that outlives
  the session. So the answer domain is the two-value `Literal` below, the client's
  `respond_permission` is annotated with the same `Literal` and takes no third value
  either, and the masks are LOGGED -- an operator has to see what one grant would
  have bought -- and sent nowhere.
* **Every ambiguous path refuses.** No answer, an unreachable server, a `200 false`,
  a 404 for a session the server dropped, an ask whose stored clock cannot be read:
  all end in `reject`, and the pending row is cleared even then, because an ask that
  can never be answered is a confirmation the user cannot get out of.
* **The ask is a row, not a variable.** It lives in `pending_actions.action_json`
  with `kind: "opencode_permission"` (no schema change), which is what lets the
  sweep find it after a crash, what keeps it out of the shell confirmation
  `core/brain.py` writes into the same one-row-per-user table, and what makes the
  row -- not a session binding -- the population the sweep enumerates.
* **A replayed event does not extend the window.** A second `permission.asked` for
  an already-pending permission keeps the original `requested_at`; a second ask for a
  *different* permission refuses the one it evicts, which by then nobody can answer.

Alice cannot push, so the question goes to Telegram
(`core.tools.telegram_tool.send_message`) and the answer comes back as text through
`resolve_from_text`. Nothing here speaks and nothing here builds an Alice payload:
there is no import of `core.render` in this file, and `on_permission_requested`
returns `None`. Turn completion is `core.opencode.sse.turn_is_complete` and is
deliberately not reimplemented: this module answers a permission id, not a turn.

allow: SIZE_OK -- 326 pure LOC: 212 of code and 129 of docstring carrying the
measured C5 contract and the fail-safe reasoning a reviewer of a module this
dangerous is entitled to. The only separable piece is `PendingPermission`, and
splitting it out would give it exactly one caller while `core/permissions.py`
stays the import todos 15 and 18 pin -- the same trade `core/opencode/client.py`
records, and the same profile `core/opencode/sse.py` carries at 315.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal, TypeAlias

import httpx

from app.config import Config
from core.memory import Memory
from core.opencode.client import OpencodeClient, OpencodeError
from core.opencode.sse import EVENT_MODE
from core.policies import confirmation_verdict
from core.tools.telegram_tool import send_message

log = logging.getLogger(__name__)

__all__ = [
    "ANSWERS", "APPROVE_ONCE", "ASK_SOURCE", "DURABLE_GRANT", "PendingPermission",
    "PermissionAnswer", "PermissionBroker", "PermissionVerdict", "REFUSE", "SWEEP_INTERVAL_S",
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

#: The `kind` that marks a `pending_actions` row as this module's. The table holds
#: one row per user and `core/brain.py` writes a shell confirmation into it, so a row
#: without this key is somebody else's and is never answered here.
KIND: Final = "opencode_permission"
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

#: The question, the four answers, and the label for an ask opencode described with
#: nothing readable in it. Telegram is the only channel that can push, and these
#: sentences are the whole user-facing contract of this module.
QUESTION: Final = "Нужно подтверждение: {title}. Ответь в телеграм «да» или «нет»."
ACCEPTED: Final = "Принято, выполняю: {title}."
REFUSED: Final = "Отклонено: {title}."
UNANSWERED: Final = "Подтверждение не получено, действие отклонено."
UNANSWERABLE: Final = "Сервер не принял ответ, действие отклонено: {title}."
UNTITLED: Final = "действие opencode"
#: A logged ask is truncated as well as escaped: a title can be kilobytes long.
PREVIEW_CHARS: Final = 120


@dataclass(frozen=True, slots=True)
class PendingPermission:
    """One unanswered ask: which permission, what it wanted, and since when.

    Frozen and hashed because it is a value: the whole module compares asks rather
    than editing them, and the pending row is this object, not a loose dict.
    """

    session_id: str
    permission_id: str
    title: str
    masks: tuple[str, ...]
    requested_at: float

    def is_same(self, other: PendingPermission) -> bool:
        """Whether `other` is the same ask of the same permission, clock aside."""
        return (self.session_id, self.permission_id) == (other.session_id, other.permission_id)

    def as_record(self) -> dict[str, object]:
        """The `pending_actions.action_json` blob -- the only place it is written.

        The masks are stored for whoever reads the row, never to be sent: the wire
        form of an answer is a single key holding one of the two values above, and
        nothing else -- that is what keeps a durable grant off the wire.
        """
        return {
            "kind": KIND,
            "session_id": self.session_id,
            "permission_id": self.permission_id,
            "title": self.title,
            "always": list(self.masks),
            "requested_at": self.requested_at,
        }

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> PendingPermission | None:
        """The stored blob as a value, or `None` when it is not an ask we can answer.

        Every accessor is total: the blob is free-form JSON written by this module,
        by another feature and by older builds, so a row that cannot be read must
        read as absent rather than raise mid-sweep. A missing or non-numeric clock
        becomes `0.0`, which expires the ask: an age nobody can compute must not keep
        a tool blocked for the rest of the session.
        """
        session_id = _text(record.get("session_id"))
        permission_id = _text(record.get("permission_id"))
        if record.get("kind") != KIND or session_id is None or permission_id is None:
            return None
        requested_at = record.get("requested_at")
        return cls(
            session_id=session_id,
            permission_id=permission_id,
            title=_text(record.get("title")) or "",
            masks=_texts(record.get("always")),
            requested_at=float(requested_at) if isinstance(requested_at, (int, float)) else 0.0,
        )


class PermissionBroker:
    """Turns an opencode permission ask into a human decision -- and only that.

    The four collaborators are the same ones todo 8 assembled: the pending row in
    `Memory`, the opencode client, the operator's config, and Telegram. There is no
    second source of truth for "what is waiting", because a broker that kept the ask
    in a variable would forget it exactly when a restart matters most.
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
            app_id, _preview(ask.title), DURABLE_GRANT, ", ".join(ask.masks) or "nothing",
        )
        await self._tell(QUESTION.format(title=ask.title or UNTITLED))

    async def resolve_from_text(self, app_id: str, text: str) -> PermissionVerdict:
        """The user's answer to whatever is pending, read as a verdict.

        `approved` means the SERVER took a one-time approval, nothing weaker.
        Everything else -- a refusal, a timeout, a 500, a `200 false`, a 404 -- is
        `rejected`, because the caller asks "may this tool run?" and only one answer
        to that may ever be yes; `unrelated` means there was no ask of ours, so the
        caller carries on as before. The words are `core/policies.py`'s call, DENY
        before AFFIRM ("да, не надо" is a refusal), and the row is cleared even when
        the POST fails, so a server that forgot the session cannot re-ask for ever.
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
                app_id, _preview(ask.title), ask.session_id, self._window_s,
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
        """
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


def _text(value: object) -> str | None:
    """A `str`, or nothing: a wrong-typed value in a hand-writable blob must read as
    absent rather than reach a URL."""
    return value if isinstance(value, str) else None


def _texts(value: object) -> tuple[str, ...]:
    """The mask list as strings, dropping anything that is not one."""
    return tuple(mask for mask in value if isinstance(mask, str)) if isinstance(value, list) else ()


def _preview(text: str) -> str:
    """A bounded title for a log record. Only the size is handled here: one line is
    guaranteed by the `%r` the log calls render the title with, which escapes the
    control characters a server-controlled string could carry."""
    return text[:PREVIEW_CHARS]
