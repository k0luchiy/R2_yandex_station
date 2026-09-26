"""One unanswered opencode ask as a value -- the record the broker persists.

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
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

#: The `kind` that marks a `pending_actions` row as the broker's. The table holds
#: one row per user and `core/brain.py` writes a shell confirmation into it, so a
#: row without this key is somebody else's and is never answered here.
KIND: Final = "opencode_permission"


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


def _text(value: object) -> str | None:
    """A `str`, or nothing: a wrong-typed value in a hand-writable blob must read as
    absent rather than reach a URL."""
    return value if isinstance(value, str) else None


def _texts(value: object) -> tuple[str, ...]:
    """The mask list as strings, dropping anything that is not one."""
    return tuple(mask for mask in value if isinstance(mask, str)) if isinstance(value, list) else ()
