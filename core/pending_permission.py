"""One unanswered opencode ask as a value -- and the ordered queue of them.

`core/permissions.py` keeps nothing in a variable, so this is the whole of what
R2D2 knows about an ask between the moment opencode raises it and the moment
somebody answers it. It is a frozen, slotted dataclass rather than a dict because
the broker *compares* asks (`is_same`) instead of editing them, and because the row
in `pending_actions.action_json` is this object rather than a loose mapping.

* **The ask is a row, not a variable.** It lives in `pending_actions` with
  `kind: "opencode_permission"` (no schema change), which is what lets the sweep
  find it after a crash, what keeps it out of the shell confirmation
  `core/brain.py` writes into the same one-row-per-user table, and what makes the
  row -- not a session binding -- the population that sweep enumerates. An ask held
  in a Python variable would be forgotten exactly when a restart matters most.
* **The stored shape is the record, and the record is not an answer.** `as_record`
  writes the ids, the title, the masks the server offered and the clock, and
  nothing else. The masks are stored for whoever reads the row and are never sent:
  what reaches the server is a single key holding one of the two values in
  `core/permissions.py`, and a durable grant is not one of them.
* **Every accessor is total.** The blob is free-form JSON written by this project,
  by another feature and by older builds, so a row that cannot be read must read as
  absent rather than raise in the middle of a sweep. A missing or non-numeric clock
  becomes `0.0`, which expires the ask: an age nobody can compute must not keep a
  tool blocked for the rest of the session.

**And the queue those rows hold, because the row is a queue (D14).** `pending_actions`
holds one row per `application_id`, and until the third live run that row held exactly one
ask, so a new ask evicted the old one and the evicted one was refused. Measured: two asks
0.55 s apart, the first refused before the user could read its Telegram question, and a
turn that needed two confirmations could not complete (`qa/live-run-v3.md` §D14 -- the
arxiv digest is the smallest example). The row was never the constraint; treating it as a
single slot was. `PermissionQueue` is that row, and the invariants it exists to hold:

* **One question at a time, each answered once.** The head is the only ask the user is
  asked about; answering it promotes the next one, which is asked in its turn. So the
  chat never carries two questions, and every ask in the row is reachable by exactly
  one `да` or `нет`.
* **Nothing in the row is unanswerable.** An ask leaves the row in one of three ways: the
  user's answer, the 300 s refusal, or the queue being full -- and the last one refuses
  the ask that did NOT fit, never the ones that did. That is the direction the old
  eviction had wrong.
* **A replayed ask changes nothing.** opencode published the same `permission.asked`
  twice on this build (measured), so `is_same` on the head is what keeps a reconnect from
  re-asking a question the user is already looking at.
* **Each ask keeps its own clock.** `requested_at` is written once and never refreshed,
  so a queue of three does not become a queue that can never expire.

The stored shape is the head's own record plus a `queued` list, so a row holding one ask
reads exactly as it always did and an older build's row reads exactly as it always was.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Final

__all__ = ["KIND", "MAX_QUEUED", "QUEUED", "PendingPermission", "PermissionQueue"]

#: The `kind` that marks a `pending_actions` row as the broker's. The table holds
#: one row per user and `core/brain.py` writes a shell confirmation into it, so a
#: row without this key is somebody else's and is never answered here.
KIND: Final = "opencode_permission"
#: The key under which the tail of the queue is stored, beside the head's own fields.
QUEUED: Final = "queued"
#: How many asks one human may have OPEN at once, head included. The measured maximum
#: in one turn is two; the bound is here so a turn that raises asks in a loop cannot grow
#: the row without end, and the ask that does not fit is refused rather than queued behind
#: a question the user may be answering right now.
MAX_QUEUED: Final = 4


@dataclass(frozen=True, slots=True)
class PendingPermission:
    """One unanswered ask: which permission, what it wanted, and since when.

    Frozen and hashed because it is a value: the whole broker compares asks rather
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
        form of an answer is a single key holding one of the two values in
        `core/permissions.py`, and nothing else -- that is what keeps a durable
        grant off the wire.
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


@dataclass(frozen=True, slots=True)
class PermissionQueue:
    """The asks one `application_id` has open: the head, then the rest in order.

    Frozen because the broker COMPARES and rewrites this value rather than editing it,
    and because the head may be `None` for exactly one state: the queue is empty, which
    is the broker's cue to clear the row rather than to write an empty one.
    """

    head: PendingPermission | None
    rest: tuple[PendingPermission, ...] = ()

    @property
    def outstanding(self) -> tuple[PendingPermission, ...]:
        """Every ask still unanswered, in the order they must be answered."""
        return () if self.head is None else (self.head, *self.rest)

    @property
    def room(self) -> bool:
        """Whether another ask fits. A full queue refuses the newcomer, not the head."""
        return len(self.outstanding) < MAX_QUEUED

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> PermissionQueue | None:
        """The stored blob as a queue, or `None` when it is not an ask of ours.

        Total, like every accessor of `PendingPermission`: the blob is free-form JSON
        written by this project, by another feature and by older builds, so a row that
        cannot be read must read as absent rather than raise in the middle of a sweep.
        A corrupt entry in the tail is dropped rather than promoted -- a head the broker
        cannot answer is already a refusal, and stacking more on top of it helps nobody.
        """
        head = PendingPermission.from_record(record)
        if head is None:
            return None
        return cls(head, _asks(record.get(QUEUED)))

    def as_record(self) -> dict[str, object]:
        """The `pending_actions.action_json` blob: the head's record plus the tail.

        The head's own `as_record` is spread rather than nested, so a row holding one ask
        is byte-for-byte the shape older builds wrote, and `from_record` reads both.
        """
        if self.head is None:
            raise ValueError("permission queue: an empty queue has no record; clear the row instead")
        return {**self.head.as_record(), QUEUED: [ask.as_record() for ask in self.rest]}

    def holding(self, ask: PendingPermission) -> PermissionQueue:
        """The queue with `ask` added behind the head, or itself when it is already there.

        The replay case: opencode published the same ask twice, and a second copy in
        the queue would be a second question about the same command.
        """
        if self.head is not None and self.head.is_same(ask):
            return self
        return PermissionQueue(self.head, (*self.rest, ask))

    def answered(self) -> PermissionQueue | None:
        """The queue after the head was answered, or `None` when nothing is left.

        The promotion is what makes the queue a queue: the ask behind the head becomes
        the question, so it is asked in its turn instead of expiring untouched. `None`
        rather than a head-less queue, so "the row must go" is one value.
        """
        if self.head is None or not self.rest:
            return None
        return PermissionQueue(self.rest[0], self.rest[1:])

    def without(self, asks: Iterable[PendingPermission]) -> PermissionQueue | None:
        """The queue with `asks` removed, whatever their position; `None` when empty.

        Used by the timeout sweep, which must refuse every expired ask of a queue and
        keep the fresh ones -- including a fresh one that was waiting behind an expired
        head, which becomes the question from then on.
        """
        gone = {id(ask) for ask in asks}
        kept = [ask for ask in self.outstanding if id(ask) not in gone]
        if not kept:
            return None
        return PermissionQueue(kept[0], tuple(kept[1:]))


def _asks(value: object) -> tuple[PendingPermission, ...]:
    """The tail of the queue as values, dropping anything unreadable."""
    if not isinstance(value, list):
        return ()
    asks = (PendingPermission.from_record(entry) for entry in value if isinstance(entry, Mapping))
    return tuple(ask for ask in asks if ask is not None)


def _text(value: object) -> str | None:
    """A `str`, or nothing: a wrong-typed value in a hand-writable blob must read as
    absent rather than reach a URL."""
    return value if isinstance(value, str) else None


def _texts(value: object) -> tuple[str, ...]:
    """The mask list as strings, dropping anything that is not one."""
    return tuple(mask for mask in value if isinstance(mask, str)) if isinstance(value, list) else ()
