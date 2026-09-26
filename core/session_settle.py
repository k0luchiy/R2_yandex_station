"""The one question the deadline branch cannot answer inside Alice's budget: is the
voice turn that outran it still writing?

`core/session_route.py` gives up on a voice turn at `r2d2_fast_deadline` and
acknowledges the user. The turn is not aborted -- it keeps running server-side -- and
the work still has to reach `r2d2-agent` (D5: on the live run six laptop-control
attempts all outran 3.2 s and the agent saw none of them, so the requests existed
nowhere at all). Submitting that work **immediately** is what put two turns in one
session at once, and the live run measured what that costs (`qa/live-run-v6.md` §D9):
the sweep could not see a message the voice turn had not written yet, so the residue
arrived behind it and the agent read it as its own history. The residue was not the
`[[NEEDS_AGENT]]` marker -- it was a **tool refusal**, whose `state.error` enumerates
the refused agent's whole permission matrix, and `r2d2-agent` then spent five
consecutive turns refusing `echo`, `ls`, `r2d2_do shell` and `read` -- four commands
its own matrix permits -- and never recovered.

Deleting the record does not undo that. What the refusing agent wrote in prose in its
place is still in the transcript, and prose is conversation: `transcript_sweep` is right
not to touch it. So the fix is the **order**, not the sweep, and this module owns it:

* **No agent turn begins while a voice turn for that session is in flight.** The wait
  happens after the acknowledgement, off Alice's clock, and the hand-off -- the one
  transcript read, the submit, the collector -- runs inside it. The deadline branch's
  ack therefore also stops paying `MARKER_TIMEOUT_S` for a read it no longer needs on
  the speaker's clock: the 0.5 s it spent is spent before the user is released, and
  now it is not.
* **"Settled" is measured, not guessed.** `TurnWatch.ended_after` is C5's
  conjunction, and it is the same predicate `core/answer_gate.py` already trusts to tell
  "the agent stopped writing" from "the agent finished": `session.idle` seen since the
  moment R2D2 gave up on the turn, **and** no ask of ours unanswered. The second half is
  what makes it safe on a session with two agents in it -- opencode sends `session.idle`
  even while a tool is blocked on a human answer (C5), so idle alone would release the
  wait on a turn that is parked rather than finished. `r2d2-voice` has an `ask` rule --
  its `bash` catch-all asks rather than denies, so a raw terminal attempt from the voice
  agent parks its turn on a human answer. The conjunction is therefore load-bearing
  rather than theoretical, and hitting the bound below is an ordinary outcome of that
  policy choice, not a pathological server.
* **The wait is bounded, and the bound is stated.** `SETTLE_TIMEOUT_S` is three times
  the worst measured voice turn (18.6 s for a cold first turn, `docs/11-opencode-contract.md`
  U6) and a twentieth of the collector's own 600 s ceiling, so a turn that outruns it is
  pathological rather than slow. The cost of hitting it is named below.
* **Without an event stream the guarantee is degraded, not pretended.** A deployment
  with no reader (`EVENT_MODE = "poll"`, or no opencode route) has no way to learn that
  the turn ended, so it waits `SETTLE_GRACE_S` and hands over anyway -- which is the
  ordering this defect had, and `SessionSweeper.sweep_residue` at the entry of the next
  turn is the backstop that already existed for it. The warning says so, because a
  guarantee that is silently not there is worse than one that is loudly absent.

**The cost of the bound, stated because there is one.** A voice turn that is still
running when `SETTLE_TIMEOUT_S` elapses gets its request handed over while it writes,
which is the old behaviour for that turn alone: the residue sweep at the next turn's
entry is still armed, the collector is still anchored on this hand-off's own read, and
the user is not left without an answer. The alternative -- refusing to hand over until
the turn is provably finished -- has no failure mode, and would trade a bounded
ordering defect for a lost request, which is the worse of the two.

**The safety net is the snapshot, not the wait.** If the wait is released a moment too
early -- a stale `session.idle` from the previous turn, a server that lies about being
idle -- the consequence is exactly what this project shipped before: the residue is
simply not in the snapshot the sweep takes. Nothing else about the hand-off changes, so
the wait is an improvement with a bounded worst case rather than a new invariant that
can fail open in a new way.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Final

from app.config import Config
from core.opencode.turn_watch import TurnWatch

__all__ = ["SETTLE_GRACE_S", "SETTLE_TIMEOUT_S", "TurnSettler"]

#: How long a deadline branch waits for the voice turn it gave up on to be SEEN to end
#: on the event stream. Spent by work the user has already been released from, so it is
#: generous; three times the worst measured voice turn, and a twentieth of the ceiling
#: the collector that follows is held to.
SETTLE_TIMEOUT_S: Final = 30.0
#: What is waited instead when there is no reader to learn the turn's end from. Long
#: enough that a voice turn which was going to answer usually has -- the fast path
#: measures 2.33-2.48 s and a deadline turn lands within a second or so of the deadline
#: -- and short enough that a deployment with no stream does not feel a stall it cannot
#: explain. The wait is a guess, and the warning below says which it was.
SETTLE_GRACE_S: Final = 5.0

#: The collector's half, as one callable: sweep the transcript, submit the task, arm the
#: collector. Passed in rather than reimplemented, so the one-snapshot rule and the
#: submit have exactly one home and there is no second copy to drift from it.
HandOver: Final = Callable[[], Awaitable[None]]
#: "This session's residue debt is settled." `SessionSweeper` owns the debt, so it owns
#: the forgetting too; this module only has to say which wait it ran.
ForgetDebt: Final = Callable[[str], None]


class TurnSettler:
    """One wait per deadline turn, and the tasks those waits are running in.

    The tasks are held in a set for the reason `core/session_sweeps.py` holds its anchor
    tasks: a task nobody keeps a reference to can be collected in the middle of a read,
    and a wait that vanishes silently is a hand-off that never happens -- which is the
    request being dropped on the floor, the exact failure the deadline branch exists to
    prevent. Nothing cancels them at shutdown, exactly as for those: each is bounded, and
    the process going away mid-wait costs a turn the user was already acknowledged for.
    """

    def __init__(self, cfg: Config, logger: logging.Logger, forget_debt: ForgetDebt) -> None:
        self._poll_s = cfg.r2d2_event_poll_interval
        self._log = logger
        self._forget_debt = forget_debt
        self._waiting: set[asyncio.Task[None]] = set()

    def arm(
        self, turns: TurnWatch | None, session_id: str, not_before: float, hand_over: HandOver
    ) -> None:
        """Start one wait, and return at once: the speaker is still waiting for words.

        `not_before` is the moment the route gave up on the voice turn, and it is passed
        in rather than taken here because it is a fact about the DEADLINE, not about this
        call: a turn that ended a millisecond before R2D2 stopped listening is settled,
        and a `session.idle` stamped after the deadline is the only evidence of that.
        `hand_over` is the collector's own half, so this module never learns what a
        hand-off is made of.
        """
        waiting = asyncio.create_task(
            self._settle_then(turns, session_id, not_before, hand_over),
            name=f"r2d2-settle:{session_id}",
        )
        self._waiting.add(waiting)
        waiting.add_done_callback(self._waiting.discard)

    async def _settle_then(
        self,
        turns: TurnWatch | None,
        session_id: str,
        not_before: float,
        hand_over: HandOver,
    ) -> None:
        """Wait for the turn to end, hand over once, and pay the debt only if it ended."""
        observed = await self._await_end(turns, session_id, not_before)
        try:
            await hand_over()
        except Exception:
            # A hand-over that raises would otherwise be an unretrieved task exception,
            # reported by nobody at a moment when the user is waiting for a message. The
            # collector's own halves log and continue; this is the one place where
            # something outside it can, so it is stated here rather than swallowed.
            self._log.exception(
                "opencode: the deadline hand-off of session %s did not run after the voice turn "
                "ended; the request was acknowledged and nothing will be collected for it",
                session_id,
            )
            return
        if observed:
            # The read the hand-over just took happened after the turn was seen to end, so
            # the residue this branch owed was in it and has just been deleted. A wait that
            # was only a grace does not get to say that, and leaves the next turn's sweep
            # armed -- which is the backstop for exactly this case.
            self._forget_debt(session_id)

    async def _await_end(
        self, turns: TurnWatch | None, session_id: str, not_before: float
    ) -> bool:
        """Whether the voice turn was SEEN to end inside the bound. `False` is a guess.

        `True` is the only answer that lets the residue debt be written off, because it is
        the only one backed by the server's own report rather than by a duration. Polling
        `state.changed` is a wake-up and not the test: the predicate is re-read on every
        pass, so a transition that landed between two waits cannot be missed.
        """
        if turns is None or not turns.watched(session_id):
            self._log.warning(
                "opencode: the turn in session %s is still running and this deployment has no "
                "event reader for it, so R2D2 waits %.1fs and then hands the request to the agent "
                "anyway. That agent may read what that turn is still writing -- the residue sweep "
                "at the entry of this user's next turn is what catches it, and the request is "
                "delivered either way.",
                session_id, SETTLE_GRACE_S,
            )
            await asyncio.sleep(SETTLE_GRACE_S)
            return False
        state = turns.state(session_id)
        deadline = time.monotonic() + SETTLE_TIMEOUT_S
        while True:
            if state.ended_after(not_before):
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._log.warning(
                    "opencode: the turn in session %s was still running %.0fs after the voice "
                    "deadline, so the request is handed to the agent while it may still be writing "
                    "(the sweep at the entry of this user's next turn catches the rest). The user "
                    "is acknowledged all the same and the collector is armed off Alice's clock.",
                    session_id, SETTLE_TIMEOUT_S,
                )
                return False
            with suppress(TimeoutError):
                await asyncio.wait_for(state.changed.wait(), min(self._poll_s, remaining))
