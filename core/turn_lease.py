"""The three questions about a collector's window that decide what a human is told.

A collector is armed for ONE turn and then reads the session over and over until the
turn is finished. Everything it puts in front of a person is a function of three
questions about that window, and this module is where all three are answered, because
the live run measured what happens when they are answered in different places or not
at all:

* **Where does the window start?** An anchor is a MESSAGE ID, so "newer than the
  anchor" only means anything if the server still lists that message. When it does
  not, the honest answer used to be `0` -- read the whole session -- on the argument
  that a marker which scrolled out must not cost a whole reply. **That argument is
  about truncation and the code cannot tell truncation from deletion** (see
  `assistant_text`), so the fallback is gone: an anchor the window cannot position
  yields nothing, and the caller states the loss. A bounded loss of an answer is this
  project's own rule (`docs/07-latency-strategy.md`); a silently WRONG one is not, and
  a whole-session window can only ever be the wrong one the moment a later turn lands
  in it.
* **Is this still my turn?** The anchor alone answers nothing about that. A collector
  armed at 20:10:36 for a turn parked on a human answer stayed armed for its whole
  ceiling, and when the NEXT question was asked it woke on that turn's answer,
  delivered 7 836 characters of a physics overview as the answer to "what day is it
  today", and was followed 1.3 s later by the collector that owned it. `newer_task` is
  the missing fact: the task text this collector submitted is a `user` message in the
  session, so a newer SUBMITTED task after it is a turn this one no longer owns. A
  newer question alone is not -- in a shared session a follow-up question is routine
  context (every warm-up asks one), while a newer task means another agent turn was
  armed and will deliver. Giving up is only half the rule, though: a collector whose
  own turn DID produce output still delivers it. `own_text` is the other half -- the
  assistant text after the anchor and before the newer task -- so an orphan collects
  its turn's output or nothing, and the next turn's answer is never in its window to
  wake on.
* **Has this window already been delivered?** Belt and braces for the same failure:
  two collectors for one session must not both put the same assistant messages in
  front of a person. `DeliveryLedger` is the in-process record, and it is on the same
  wire as the answer rather than beside it.

**Why the turn is identified by its own task text and not by the anchor's position.**
The hand-off reads the transcript, deletes the routing signals and only THEN submits
the task, because the anchor must survive its own sweep; so at the moment the collector
is armed the task message does not exist yet and there is no id to name it by. The
TEXT is known at arm time and is written into the session verbatim, which is enough:
the turn this collector owns is the newest `user` message carrying that text. Newest,
not first: two turns asking the same question submit byte-identical tasks, and the
collector armed for the newer one must bind the newer message -- a first match would
bind it to the older turn and make it give up on an answer nobody else collects.
What ends the lease is not the next question -- questions are context -- but the next
message carrying a task after the binding.

allow: SIZE_OK -- 295 pure LOC. The three window rules, the measured failure behind
each, and the direction the project chose are the documentation the next change needs;
splitting the module would separate a rule from the failure that justifies it.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from core.opencode.wire import MessageRecord

__all__ = [
    "SUPERSEDED",
    "TASK_PREFIX",
    "DeliveryLedger",
    "TurnLease",
    "TurnSuperseded",
    "assistant_text",
    "newer_task",
    "own_text",
    "window_after",
]

#: The one role that asks (`GET /session/:id/message`, finding C7). A turn begins with
#: a user message and an assistant message can only ever be an answer to one.
USER_ROLE: Final = "user"
#: The one role that speaks, re-declared from the same finding rather than imported:
#: the suite asserts the spellings agree, so a drift is a red test, not a silent fork.
ASSISTANT_ROLE: Final = "assistant"
#: The prefix `SessionCollector` puts on the agent task it submits, owned here so the
#: lease and the hand-off cannot disagree about what a task message looks like: the
#: turn this collector owns is the first `user` message carrying its task text, and a
#: newer turn is the next `user` message carrying ANY task. A follow-up question the
#: user asked out loud carries no prefix and is context, never a turn.
TASK_PREFIX: Final = "Пользователь попросил голосом: "
#: What the `jobs` row records when a collector discovered it no longer owns the turn.
#: The user is sent nothing -- see `core/answer_gate.py` -- so the one honest place to
#: say what happened is the record the operator reads.
SUPERSEDED: Final = (
    "не доставлено: в этой сессии начался более новый ход, а этот сборщик принадлежит прежнему"
)


class TurnSuperseded(Exception):
    """A newer turn owns this session, or this window was already delivered.

    Not a failure and deliberately not a `str`: there is no text to speak. The live run
    measured both halves of why -- a superseded collector that said "Агент не ответил"
    would be false (the agent did answer, 1.3 s later, to the user's real question), and
    a superseded collector that said nothing while the right answer was on its way is
    the correct behaviour. `core/answer_gate.py` turns this into a delivered NOTHING
    plus a WARNING, so the loss is stated in the log and in the `jobs` row.
    """


def _is_task(record: MessageRecord) -> bool:
    """Whether this stored message is a submitted agent task rather than a question."""
    return record.role == USER_ROLE and record.text.startswith(TASK_PREFIX)


def _own_index(records: Sequence[MessageRecord], turn_text: str) -> int | None:
    """The position of this turn's task message: the LAST exact match.

    Last, not first: two turns asking the same question submit byte-identical task
    texts, and a collector armed for the newer one must bind the newer message. A
    first match would bind it to the older turn, see its own task as a "newer" one
    and give up -- losing an answer nobody else collects. Binding the newest fails
    safe the other way: the older turn's collector binds the same message, finds no
    newer task behind it and delivers the lumped window, which is the pre-existing
    shape, not a loss. `None` when the task is absent altogether -- never submitted,
    or not stored yet -- and the absence of a turn is not evidence of a newer one.
    """
    if not turn_text.strip():
        return None
    at: int | None = None
    for index, record in enumerate(records):
        if record.role == USER_ROLE and record.text.strip() == turn_text:
            at = index
    return at


def newer_task(records: Sequence[MessageRecord], turn_text: str) -> bool:
    """Whether a newer SUBMITTED task after this turn's own task is in `records`.

    Only a task counts -- a follow-up question the user asked out loud carries no
    prefix and is context every warm-up produces, not a turn; treating it as one
    would make every second turn of every session a supersession, which the suite
    (and the live run's warm-up shape) forbids. Asking the same question twice is
    still two turns: the binding is the newest match, so the second identical task
    message is a task after the first turn's binding, and a collector that answered
    the first is not the collector for the second.

    A turn that has not been submitted yet, or a hand-off whose submit failed, simply
    has no binding and is not superseded: the absence of a turn is not evidence of a
    newer one, and saying so is the safe direction, because the ceiling and the gate
    still end it. An empty `turn_text` is that absence spelled out -- a caller that
    never submitted a task -- so it never matches, not even an empty user message.
    """
    own = _own_index(records, turn_text)
    if own is None:
        return False
    return any(_is_task(record) for record in records[own + 1 :])


def assistant_text(records: Sequence[MessageRecord], since_message_id: str) -> str:
    """The assistant text in `records` newer than `since_message_id`, or `""`.

    Three cases, and the third is the one this rule is about:

    * **`""` means "everything this session ever holds is newer"**, which is the truth
      for a session that has held nothing -- the C8 case, pinned by
      `test_a_cold_first_turn_enqueues_the_collector_that_will_ship_the_answer`.
    * **The anchor is in the window**: collect strictly after it, which is the ordinary
      case and the reason the one-snapshot rule in `core/session_collector.py` exists.
    * **The anchor is a non-empty id the window does not contain: nothing.** Not the
      whole window, and not a guess.

    **The removed fallback, and what it conflated.** The third case used to read `0`
      and take everything, on the reasoning that a marker the server no longer returns
      means the window truncated at the front. Truncation is one way the anchor can be
      missing. Deletion is the other, and this project deletes: `core/routing.py`'s
      sweep removes the `[[NEEDS_AGENT]]` marker and the stored tool refusal by
      `DELETE /session/:id/message/:id`. The two are indistinguishable here, and the
      fallback was wrong for a third reason that the argument never reached: **a window
      that starts at 0 has no upper bound either**, so it also spans every turn that
      comes after this one. That is the failure the live run recorded -- the orphaned
      collector's whole-session window woke on the NEXT turn's answer and delivered it
      as this turn's, while the collector that owned that answer delivered the same
      text 1.3 s later.

    The cost is stated rather than hidden. If the server truncates a long session while
    a collector is waiting, that collector delivers nothing and the user is told
    "Агент не ответил." instead of receiving a reply that may be missing its first
    paragraph and may contain a previous turn's. That is the direction
    `docs/07-latency-strategy.md` already chose for every other bounded loss here: an
    answer that never arrives and says so, rather than one that arrives and is wrong.
    """
    return "\n".join(
        record.text
        for record in window_after(records, since_message_id)
        if record.role == ASSISTANT_ROLE and record.text
    )


def window_after(
    records: Sequence[MessageRecord], since_message_id: str
) -> list[MessageRecord]:
    """The records newer than `since_message_id`, under `assistant_text`'s three cases.

    One definition of "the window", shared by the reader that produces the answer and
    the ledger that decides whether it was already sent. They were once the same
    boundary computed twice, and the copy the ledger used covered the **whole
    session**: a lease whose window had merely not grown yet saw every assistant id
    belonging to earlier turns, found them all already claimed, and abandoned the
    turn without sending anything. Measured: the collector is armed before the
    submit, so that race is the normal case under load, not an exotic one
    (`qa/live-run-v11.md`). `test_a_stale_window_is_not_an_already_delivered_one` pins it.
    """
    if not since_message_id:
        return list(records)
    ids = [record.id for record in records]
    if since_message_id not in ids:
        return []
    return list(records[ids.index(since_message_id) + 1 :])


def own_text(
    records: Sequence[MessageRecord], since_message_id: str, turn_text: str
) -> str:
    """This turn's own assistant text: after the anchor, before the newer task.

    The window `assistant_text` reads has no upper bound, so once a newer turn
    lands in the session it also holds that turn's answer -- which is what the
    orphaned collector woke on. Cutting the window at the first newer SUBMITTED
    task after this turn's own task message leaves exactly what this turn produced:
    an orphan whose turn never answered owns the empty string, and a collector
    whose turn DID produce output still delivers it even though the session has
    moved on. A turn that cannot be found in the window owns nothing either --
    the task was never submitted, or has not been stored yet -- and an anchor
    the window cannot position is nothing, the same rule as `assistant_text`.
    """
    if not turn_text.strip():
        return ""
    ids = [record.id for record in records]
    if since_message_id and since_message_id not in ids:
        return ""
    start = ids.index(since_message_id) + 1 if since_message_id else 0
    own = _own_index(records, turn_text)
    if own is None:
        return ""
    end = next(
        (index for index in range(own + 1, len(records)) if _is_task(records[index])),
        len(records),
    )
    return "\n".join(
        record.text
        for record in records[start:end]
        if record.role == ASSISTANT_ROLE and record.text
    )


class DeliveryLedger:
    """The assistant messages each session has already put in front of a human.

    A second guard on the same failure, and deliberately in-process: two collectors for
    one session are two objects in one worker pool, and a process that restarts has
    already lost the previous delivery's Telegram message too. What it refuses is the
    case the live run recorded -- two collectors, one window, one answer sent twice --
    and it refuses it by MESSAGE ID, which is the only identity two collectors can be
    certain they share.

    A subset test rather than a comparison of the newest id, because the window grows
    as the agent writes: the first delivery claims what it sent, and a later collection
    by the SAME turn carries strictly more, so "everything I have was already delivered"
    is false for it and true only for a collector that has nothing of its own. Nothing
    is claimed for an empty window, so a collector that has read nothing yet cannot lock
    out the one that has.

    The claim is made by the delivery, not by the collection: the gate re-collects a
    parked turn round after round while it waits, and none of those rounds is a
    delivery. `OpencodeSessionBackend.note_delivered` is the call, made after the job's
    text is decided and before it is sent, so a twin reading after the claim refuses.
    """

    def __init__(self) -> None:
        self._by_session: dict[str, frozenset[str]] = {}

    def already_delivered(self, session_id: str, records: Sequence[MessageRecord]) -> bool:
        """Whether every answer in this window has been delivered from this session."""
        claimed = self._by_session.get(session_id)
        if not claimed:
            return False
        ids = self._answer_ids(records)
        return bool(ids) and ids <= claimed

    def claim(self, session_id: str, records: Sequence[MessageRecord]) -> None:
        """Record the answer this delivery is about to make, replacing the last claim."""
        ids = self._answer_ids(records)
        if ids:
            self._by_session[session_id] = ids

    @staticmethod
    def _answer_ids(records: Sequence[MessageRecord]) -> frozenset[str]:
        return frozenset(
            record.id for record in records if record.role == ASSISTANT_ROLE and record.text
        )


class TurnLease:
    """One `collect_reply` call's claim on one session while it reads it.

    Built per call and thrown away with it, because a collector's turn is per call:
    `OpencodeSessionBackend` is a single adapter serving every user, so nothing about
    one turn may live on it. The one thing that deliberately outlives the call is the
    `DeliveryLedger`, which is per PROCESS -- that is what lets two collectors for one
    session refuse to both deliver.

    `read` is the only way to get text out of a window and it can refuse, so a caller
    cannot forget the check: the position, the anchor rule and the turn rule are one
    call, and none of them is available separately.
    """

    def __init__(self, since_message_id: str, turn_text: str, delivered: DeliveryLedger) -> None:
        self._anchor = since_message_id
        self._turn_text = turn_text
        self._delivered = delivered
        self._window: list[MessageRecord] = []

    def read(self, session_id: str, records: list[MessageRecord]) -> str:
        """The answer in this window that belongs to this turn, or raise `TurnSuperseded`.

        A newer task in the window does not end the collection by itself: the
        collector still delivers what its own turn produced, cut off before the
        newer task, so a turn that answered before the session moved on is a
        delivery and not a loss. What ends it is an orphan -- a newer task, and
        nothing of its own -- or a window whose every answer was already
        delivered from this process.
        """
        if self._delivered.already_delivered(session_id, window_after(records, self._anchor)):
            raise TurnSuperseded(
                f"opencode: every assistant message in the window of session {session_id!r} has "
                f"already been delivered from this process, so a second collector would send the "
                f"same answer to the user twice"
            )
        if newer_task(records, self._turn_text):
            own = own_text(records, self._anchor, self._turn_text)
            if not own:
                raise TurnSuperseded(
                    f"opencode: a task newer than the one this collector of session "
                    f"{session_id!r} was armed for is in the window, and this turn produced "
                    f"nothing of its own, so the session has moved on to another turn and this "
                    f"one has nothing left to say about it"
                )
            self._window = records
            return own
        self._window = records
        return assistant_text(records, self._anchor)

    @property
    def window(self) -> list[MessageRecord]:
        """The records the last successful `read` saw; `[]` if it never succeeded.

        Kept so the delivery can claim exactly what was collected: the backend
        stashes it per session, and `note_delivered` claims it once the job's text
        is decided. A lease whose `read` raised holds nothing, so an orphan whose
        collection failed can neither deliver nor lock out the turn that can.
        """
        return list(self._window)
