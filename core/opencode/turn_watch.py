"""What the event stream said about a turn: is the agent parked on a human right now?

`collect_reply` decides "the agent has finished" from silence, and silence cannot tell
*finished* from *blocked on a permission question R2D2 itself raised* -- the one situation
this project creates. The live run measured the difference: an agent that narrates its plan
and then reaches for a tool produces text, goes quiet while it waits for a human, and a 2 s
silence heuristic read that as the end of the turn. The collector shipped `"Сделаю. Сначала
посмотрю, что доступно в окружении."` and the digest the agent went on to write reached
nobody. Silence plus a "no news" rule is not a state machine; this module is the part that
has the states.

**The signal is the ask/reply pair, and deliberately NOT `session.idle`.** opencode sends
`permission.asked` when it blocks on a human and `permission.replied` when that human
answered. `session.idle` is the only event that closes a turn, but the contract records that
the server sends it *even while a tool is blocked on a permission answer*, which is why
`core/opencode/sse_frames.turn_is_complete` needs a conjunction before it will call a turn
over. An idle frame therefore cannot answer the question a collector actually has -- "is
this text the plan or the result?" -- while an unanswered ask answers it exactly: the agent
is waiting for a person, so what it wrote before it stopped is what it said on the way there.
Confirmed on this build: `session.idle` does carry `properties.sessionID` and does reach the
per-session reader, so the alternative was available and was not the right one.

It is fed from `app/opencode_route.SessionReaders`, which is already one reader per session
and already filters every frame by `properties.sessionID` (C5) -- so nothing here can be told
about a stranger's session, and the reader ladder, the reconnect and the credential handling
stay in `core/opencode/sse.py` where they belong.

**What it deliberately does NOT do.** It does not read the transcript, it does not decide
whether a message is an answer, and it never cancels anything. It is a memory of two event
names, and a collector with no reader attached sees an empty session -- which degrades to
the silence heuristic, the pre-agreed direction for `EVENT_MODE = "poll"` and for a build
with no stream.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Final

from core.opencode.sse import (
    PERMISSION_ASKED,
    PERMISSION_REPLIED,
    TURN_COMPLETE,
    OpencodeEvent,
)

__all__ = ["MAX_UNANSWERED", "TurnState", "TurnWatch", "ask_message_id"]

log = logging.getLogger(__name__)

#: How many `permission.asked` ids one session's memory keeps. The measured maximum
#: outstanding in one session is two; the bound exists so a session whose replies never
#: arrive cannot grow this dict for the life of the process. Overflow is DROPPED, not
#: refused: the fail-safe for an ask nobody answers is the broker's 300 s sweep, and this
#: memory must not be what turns a missing answer into a blocked turn.
MAX_UNANSWERED: Final = 8


def ask_message_id(event: OpencodeEvent) -> str | None:
    """`properties.tool.messageID` of a `permission.asked`, or `None`.

    Read here rather than on the event because it is a property of the ASK, and the
    collector is the only consumer that can use it: everything the agent writes after the
    blocked tool call is its result, and everything in front of it is the plan it narrated
    on the way there. Measured present on this build's live frames, and `None` is a real
    answer rather than an error -- a server that named no tool message gives the collector
    nothing to move to, and it then keeps waiting instead.
    """
    tool = event.properties.get("tool")
    message_id = tool.get("messageID") if isinstance(tool, dict) else None
    return message_id if isinstance(message_id, str) and message_id else None


@dataclass(slots=True)
class TurnState:
    """One session's memory of the asks it has open and when the turn last ended.

    `changed` is the wake-up for anything that would rather be told than poll: it is set and
    cleared on every transition, so a subscriber cannot miss one and cannot wake on a state
    that has not moved. Nothing here is cleared by a new turn -- an ask outlives the turn
    that raised it until somebody answers it or the broker refuses it, and that clock belongs
    to the broker, not to this memory. The idle stamp is monotonic and therefore only
    comparable inside this process, which is the only place it is read.
    """

    waiting: dict[str, str | None] = field(default_factory=dict)
    idle_at: float = 0.0
    #: Every frame of this session, whatever its name. NOT the same as "we know something":
    #: it is the evidence that a reader is attached at all, which is what a collector needs
    #: before it may wait for an event instead of for silence.
    frames: int = 0
    changed: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    @property
    def blocked(self) -> bool:
        """Whether an ask of ours is unanswered, so the turn cannot be over."""
        return bool(self.waiting)

    def ended_after(self, moment: float) -> bool:
        """Whether the turn was over at `moment`: idle since, and nothing outstanding.

        Both halves of C5 rule 2, and the time bound is what makes a stale latch harmless:
        an `idle` from the PREVIOUS turn does not end this one, which is the failure a plain
        boolean would have on the second collector of a session.
        """
        return self.idle_at >= moment and not self.waiting

    @property
    def blocked_at(self) -> str | None:
        """The first unanswered ask's message id, or `None` when there is nothing to move to.

        `None` is two things at once and a caller must not confuse them: no ask is
        outstanding, or an ask is outstanding and the server named no tool message. In both
        cases there is no id this watch can anchor a collector at, because a message id it
        never saw cannot position one.
        """
        return next((message_id for message_id in self.waiting.values() if message_id), None)


class TurnWatch:
    """The per-session memory, and the one place an event is recorded.

    A dict rather than a task per session: the readers call `note` synchronously from their
    own handler, and a collector reads `state(session_id)` without awaiting anything. An
    entry is one session this process has served, which is one per human, and nothing here
    holds a socket or a task.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, TurnState] = {}

    def state(self, session_id: str) -> TurnState:
        """This session's state, created on first sight so no caller has to branch."""
        return self._sessions.setdefault(session_id, TurnState())

    def note(self, event: OpencodeEvent) -> None:
        """Record one ask, one reply or one idle of `event.session_id`. Ignores the rest.

        The session filter is `EventSource`'s, not this method's: a frame with no
        `sessionID` (`server.connected`, `server.heartbeat`) describes the connection rather
        than a turn, and attributing it to a session would be a guess. A reader must survive
        every frame the server invents, so nothing here may raise.
        """
        session_id = event.session_id
        if session_id is None:
            return
        state = self.state(session_id)
        state.frames += 1
        if event.type == PERMISSION_ASKED:
            asked = event.permission_id
            if asked is not None and len(state.waiting) < MAX_UNANSWERED:
                state.waiting[asked] = ask_message_id(event)
        elif event.type == PERMISSION_REPLIED:
            answered = event.request_id
            if answered is not None:
                state.waiting.pop(answered, None)
        elif event.type == TURN_COMPLETE:
            state.idle_at = time.monotonic()
        else:
            return
        state.changed.set()
        state.changed.clear()

    def watched(self, session_id: str) -> bool:
        """Whether a reader is attached to this session at all.

        The question a collector asks before it may wait for an EVENT rather than for
        silence: with no frames there is no `session.idle` coming, and a collector that
        waited for one would hold a worker slot for its whole ceiling. A reader attaches on
        every turn (`SessionWatchingStore.resolve`), long before the collector starts, so in
        a deployment with the stream this is true from the first millisecond.
        """
        state = self._sessions.get(session_id)
        return state is not None and state.frames > 0

    def describe(self, session_id: str) -> str:
        """One line an operator can read: what the watch believes about a session."""
        state = self._sessions.get(session_id)
        if state is None:
            return "no events seen"
        return (
            f"{len(state.waiting)} unanswered ask(s), blocked_at={state.blocked_at or 'unknown'}, "
            f"idle {state.idle_at and 'seen' or 'not seen'}"
        )
