"""What one turn cost, recorded once, whatever the turn did (plan todo 17).

Alice allows the webhook **4.5 s** and `docs/07-latency-strategy.md` budgets
about **2.5 s** of that for R2D2. Nothing proves we fit that budget except a
record per turn: the speech recognition is Yandex's, so the only cost we can
shave is ours, and a turn that quietly took 4 s is indistinguishable from one that
took 400 ms once you only read the answers.

Three properties are load-bearing:

* **One record per turn, whatever happened.** `turn()` opens the record and its
  `finally` writes it, so a turn that raised is measured like any other. A
  failure without a record is the turn whose duration nobody can ever explain.
* **The clock is monotonic and a duration is never negative.** `time.time()` steps
  backwards across an NTP correction, a `date -s` or a laptop suspend, and a
  duration computed across that jump reads as "no time passed" -- which then
  poisons every average built from the record. `time.monotonic` cannot go
  backwards, and the clock is a parameter so a test can prove the clamp without
  sleeping through one.
* **No credential can reach the record.** The fields are a route, a path, a model
  id, an agent name, three counters and a list of tool names: nothing that came
  out of a `${...}` expansion in `config/backends.json`.

**The `path` vocabulary** -- what the turn did with the request:

* `voice` -- answered inside this request; the user heard the text.
* `escalate` -- acknowledged, and the work moved to the agent, the background
  worker or Telegram.
* `error` -- the turn failed and the graceful text was spoken instead.
* `deadline` -- the voice turn outran `r2d2_fast_deadline` and was handed to the
  collector, and `route=opencode` for the branch that answers from a session.
* `parked` -- read, understood and deliberately NOT submitted, because the
  session is standing on an ask of ours that the user has not answered.
* `permission` -- the turn WAS the answer to such an ask. No model was asked
  anything and no model answered, and the text acknowledges a decision rather
  than answering a question.

Both of those belong to `core/session_route.py:SessionRoute.turn`, which reports
them itself since todo 18: it is the only branch that knows which agent spoke and
whether the voice turn ran out of budget, so a recorder that could not be told
would be a recorder nobody fills in. `docs/11-opencode-contract.md`'s measured
p50 of 1.667 s against the 2.5 s budget is the number the record exists to keep
watching, and it lands on that branch. `permission` belongs to
`SessionRoute.answer_permission`, whose `да` used to leave every field at its
default: a Telegram button answer was recorded as `path=voice`, and `voice` is
the vocabulary for "the user heard this spoken aloud" -- so the one record the
ledger could not be read for was the one it wrote most confidently.

**What `permission_asked` claims, and when it can honestly be known.** It says an
opencode permission ask is *involved in this turn*: the turn is standing on one,
it is the answer to one, or one was raised while the turn was waiting on the
model. It is deliberately NOT "an ask was raised somewhere inside this turn's
window", and it is NOT set when `turn()` opens -- because it cannot be. The frame
arrives on the opencode event stream, in ANOTHER task, at a moment no caller can
predict: `permission.asked` for a still-running voice turn usually reaches the
user's chat *after* the request has already been acknowledged. A flag set on entry
is a guess, and a flag the ledger cannot make true is worse than a missing field,
because it looks like a measurement. So each terminal path of
`core/session_route.py` decides for itself and says why in the comment there, the
paths that provably cannot see an ask leave it `False`, and the ONE place an ask
can appear while a turn is in flight -- the still-running voice turn of the
`deadline` branch -- re-reads the broker's own row for it.

**The ask's own turn usually is not this one.** The agent turn that raises an ask
is submitted *after* the acknowledgement, so the turn that submitted it records
`False` and the NEXT turn records `path=parked permission_asked=True`. One ask,
one `True`, and no record ever claiming a question nobody was asked. What the
field cannot do is date-stamp the ask to the request that caused it: that would
mean writing the record after the collector's ceiling, and a turn whose cost is
known minutes late is the turn the whole of this module exists to prevent.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Final, TypeVar

log = logging.getLogger("r2d2.metrics")

#: Which brain answered the turn. `opencode` is the persistent-session route,
#: `fallback` is the provider chain in `config/backends.json`.
ROUTE_OPENCODE: Final = "opencode"
ROUTE_FALLBACK: Final = "fallback"

#: What the turn did with the request; see the module docstring for the
#: vocabulary. `voice` is the default because most turns are answered in place.
PATH_VOICE: Final = "voice"
PATH_ESCALATE: Final = "escalate"
PATH_ERROR: Final = "error"
PATH_DEADLINE: Final = "deadline"
#: The turn was refused before it reached a model, because the session was parked on
#: an ask the user had not answered. It is its own path and not `error` because nothing
#: failed: the request was read, understood, and deliberately not submitted, because
#: opencode does not serve a turn submitted into a session it still calls busy
#: (`qa/live-run-v9.md` F1). Without it this turn recorded `path=voice` with a model
#: that answered nothing, which is the one shape the ledger cannot be read for.
PATH_PARKED: Final = "parked"
#: The turn WAS the answer to a brokered permission ask: `да` or `нет`, posted to the
#: server as `{"response": "once"}` or `{"response": "reject"}`. It is its own path
#: and not `voice` because `voice` means the user HEARD this spoken aloud, and a
#: permission answer arrives as a Telegram message that nobody speaks -- so the live
#: run recorded every `да` as `path=voice` and the field could not be read for it.
#: And not `escalate`: nothing moved to the agent, because the work was already
#: running and this turn is what released it.
PATH_PERMISSION: Final = "permission"

#: The one record per turn. The first seven fields are the format the plan pins;
#: `msgs` and `tools` are what the line it replaced carried, kept because they are
#: the only record of how much history a turn actually sent and what it may call.
#: Its `content_len` is deliberately not kept: the length of the answer is a
#: property of the model, not of what the turn cost.
TURN_FORMAT: Final = (
    "turn route=%s path=%s model=%s agent=%s llm_ms=%d total_ms=%d escalated=%s "
    "permission_asked=%s msgs=%d tools=%s"
)

Clock = Callable[[], float]
_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class TurnMetrics:
    """One measured turn, as it was logged. Frozen: the log is the record."""

    route: str
    path: str
    model: str
    agent: str
    llm_ms: int
    total_ms: int
    escalated: bool
    permission_asked: bool
    msgs: int = 0
    tools: tuple[str, ...] = ()

    def arguments(self) -> tuple[object, ...]:
        """The fields in `TURN_FORMAT` order, so the two cannot drift apart."""
        return (
            self.route,
            self.path,
            self.model,
            self.agent,
            self.llm_ms,
            self.total_ms,
            self.escalated,
            self.permission_asked,
            self.msgs,
            self.tools,
        )


class Turn:
    """The mutable half of one turn: what the code doing the turn reports as it goes.

    A recorder rather than the frozen record itself, because the facts arrive
    after the turn has started -- the model answers, and only then is there a
    model name, a history size and a duration. `record()` freezes them.

    The recorder is task-local (`ContextVar`) rather than a parameter, because the
    code that knows a turn's facts is several frames below the code that opens the
    turn, and threading a parameter down to it is the kind of change that makes
    instrumentation optional.

    `permission_asked` is the one field with no default reading. It is not set here
    and cannot be: the ask arrives on another task, at an unpredictable moment. The
    terminal path that CAN see it sets it, and the module docstring says what it
    claims and which path that is.
    """

    __slots__ = (
        "route",
        "path",
        "model",
        "agent",
        "llm_ms",
        "escalated",
        "permission_asked",
        "msgs",
        "tools",
        "_clock",
        "_started",
    )

    def __init__(self, clock: Clock | None = None) -> None:
        self.route = ""
        self.path = PATH_VOICE
        self.model = ""
        self.agent = ""
        self.llm_ms = 0
        self.escalated = False
        self.permission_asked = False
        self.msgs = 0
        self.tools: tuple[str, ...] = ()
        self._clock = time.monotonic if clock is None else clock
        self._started = self._clock()

    async def measure(self, awaitable: Awaitable[_T]) -> _T:
        """Time one awaited call as this turn's `llm_ms`.

        The timing is closed in a `finally` for the same reason the record is: a
        model call that failed still took the time it took.
        """
        started = self._clock()
        try:
            return await awaitable
        finally:
            self.llm_ms = self._ms_since(started)

    def answered(self, *, route: str, model: str, msgs: int, tools: Sequence[str]) -> None:
        """The route's model call returned: who answered, and on how much."""
        self.route = route
        self.model = model
        self.msgs = msgs
        self.tools = tuple(tools)

    def record(self) -> TurnMetrics:
        """The frozen measurement of this turn."""
        return TurnMetrics(
            route=self.route,
            path=PATH_ESCALATE if self.escalated else self.path,
            model=self.model,
            agent=self.agent,
            llm_ms=self.llm_ms,
            # The turn contains the model call, so a clock that stepped backwards
            # in between cannot make the turn look shorter than the call in it.
            total_ms=max(self._ms_since(self._started), self.llm_ms),
            escalated=self.escalated,
            permission_asked=self.permission_asked,
            msgs=self.msgs,
            tools=self.tools,
        )

    def _ms_since(self, mark: float) -> int:
        # The clamp is the point: a clock that stepped backwards yields 0 ms, which
        # is a lie of omission, where a negative duration is a lie that also
        # survives into every average computed from the record.
        return max(0, int((self._clock() - mark) * 1000))


_ACTIVE: Final[ContextVar[Turn | None]] = ContextVar("r2d2_turn", default=None)


def current() -> Turn:
    """The turn in flight on this task, or a fresh detached one.

    A detached recorder measures exactly like the real one and its record goes
    nowhere: a missing metric must not be able to fail a voice answer, and the
    cost of that choice -- a turn recorded outside `turn()` is silently dropped --
    is paid only by the code that forgot to open the turn, which is a test's
    problem and not a user's.
    """
    return _ACTIVE.get() or Turn()


@contextmanager
def turn(logger: logging.Logger | None = None, *, clock: Clock | None = None) -> Iterator[Turn]:
    """Open one turn's record and log it exactly once, whatever the body does.

    The body re-raises: the record is written in a `finally`, so measuring a turn
    never changes what that turn returns or raises.
    """
    recorder = Turn(clock)
    token = _ACTIVE.set(recorder)
    try:
        yield recorder
    finally:
        _ACTIVE.reset(token)
        (logger or log).info(TURN_FORMAT, *recorder.record().arguments())
