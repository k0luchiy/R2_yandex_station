"""A collector belongs to a turn: the anchor rule and the turn rule (`core/turn_lease.py`).

The live run parked turn N on a human answer, handed its work to the agent anyway,
and left its collector armed. When turn N+1 was asked, the orphan woke on THAT
turn's answer -- 7 836 characters of physics overview delivered as the answer to
"what day is it today" -- and the collector that owned it delivered the same text
1.3 s later. Two distinct causes, pinned here separately:

* **The anchor rule** (`assistant_text`): a missing anchor used to fall back to
  the whole session. That fallback is sound for truncation and wrong for
  deletion -- and this project deletes (`DELETE /session/:id/message/:id` sweeps
  the routing signal the collector was anchored at). The two are
  indistinguishable in the window, and a window starting at 0 has no upper bound
  either, so the safe direction is nothing: a stated loss, never a wrong answer
  (`docs/07-latency-strategy.md`).
* **The turn rule** (`newer_task`, `TurnLease`, `DeliveryLedger`): an armed
  collector answers only the turn it was armed for. A newer SUBMITTED task after
  its own task message is a turn it no longer owns -- a follow-up question alone
  is context, not a turn -- and a window whose every answer was already delivered
  is a second delivery. Both raise `TurnSuperseded`, which the worker records
  without sending anything.

Everything here is pure: `MessageRecord` values in, verdicts out. No server, no
clock, no pool.
"""

from __future__ import annotations

import pytest

from core import routing
from core.backends import opencode_session
from core.opencode.wire import MessageRecord
from core.turn_lease import (
    ASSISTANT_ROLE,
    USER_ROLE,
    DeliveryLedger,
    TurnLease,
    TurnSuperseded,
    assistant_text,
    newer_task,
    own_text,
)

TASK_N = "Пользователь попросил голосом: какой сегодня день"
TASK_N1 = "Пользователь попросил голосом: расскажи про квантовые точки"
PHYSICS = "Обзор: квантовые точки — это наночастицы."


def rec(message_id: str, role: str, text: str) -> MessageRecord:
    """One transcript entry; the collector only ever reads id, role and text."""
    return MessageRecord(id=message_id, role=role, text=text)


def test_the_two_role_spellings_agree() -> None:
    # Given the role re-declared in `core/turn_lease.py` rather than imported
    # When/Then a drift between the two copies is a red test, not a silent fork
    assert (USER_ROLE, ASSISTANT_ROLE) == ("user", "assistant")
    assert ASSISTANT_ROLE == routing.ASSISTANT_ROLE
    assert USER_ROLE == opencode_session.USER_ROLE


# ---------------------------------------------------------------------------
# 1. the anchor rule, in both directions
# ---------------------------------------------------------------------------


def test_a_positioned_anchor_reads_only_what_is_newer() -> None:
    # Given a window whose first message is the anchor itself
    records = [rec("m1", "assistant", "старый ответ"), rec("m2", "assistant", "новый ответ")]
    # When/Then only the newer text is collected -- never delivered twice
    assert assistant_text(records, "m1") == "новый ответ"


def test_an_empty_anchor_on_a_fresh_session_reads_everything() -> None:
    # Given the C8 case: a session that has held nothing, so `""` is the truth
    # rather than a default -- everything it ever holds IS newer
    records = [rec("m1", "assistant", "первый ответ")]
    # When/Then the collector ships it; `""` here is exact, not a fallback
    assert assistant_text(records, "") == "первый ответ"


def test_a_deleted_anchor_yields_nothing_not_the_window() -> None:
    # Given an anchor the window cannot position: the sweep deleted the routing
    # signal this collector was armed at, while the window itself is complete
    records = [rec("m1", "assistant", "первый"), rec("m2", "assistant", "второй")]
    # When/Then the collector delivers nothing. The whole window would also span
    # every LATER turn, which is how the orphan answered the wrong question.
    assert assistant_text(records, "msg_swept_away") == ""


def test_a_deleted_anchor_yields_nothing_even_with_a_later_turn_in_the_window() -> None:
    # Given the live shape exactly: the anchor is gone AND a newer turn's answer
    # is sitting in the window, waiting to be misdelivered
    records = [
        rec("task_n", "user", TASK_N),
        rec("task_n1", "user", TASK_N1),
        rec("ans_n1", "assistant", PHYSICS),
    ]
    # When/Then the fallback that would wake on that answer is gone
    assert assistant_text(records, "msg_swept_away") == ""


def test_only_the_assistant_speaks_through_any_anchor() -> None:
    # Given a window holding the user's own words after the anchor
    records = [rec("q", "user", "какой сегодня день"), rec("a", "assistant", "Понедельник.")]
    # When/Then the request is never collected as its own answer
    assert assistant_text(records, "") == "Понедельник."


# ---------------------------------------------------------------------------
# 2. the turn rule: a collector answers its own turn or gives up
# ---------------------------------------------------------------------------


def test_no_task_yet_is_no_newer_task() -> None:
    # Given a transcript that does not hold this turn's task at all -- not yet
    # submitted, or a hand-off whose submit failed
    records = [rec("q", "user", "какой сегодня день")]
    # When/Then the absence of a turn is not evidence of a newer one
    assert newer_task(records, TASK_N) is False
    assert newer_task(records, "") is False


def test_the_turns_own_task_alone_is_not_a_newer_task() -> None:
    # Given the session as this collector's hand-off left it: its own task message
    records = [rec("q", "user", "какой сегодня день"), rec("t", "user", TASK_N)]
    # When/Then it still owns the session
    assert newer_task(records, TASK_N) is False


def test_a_follow_up_question_is_context_not_a_newer_task() -> None:
    # Given the warm-up shape every returning session produces: the next question
    # asked while this collector slept, with no agent work submitted behind it.
    # A "newer user message" signal would fire here -- and end the lease of a
    # collector whose turn is still producing -- so the rule deliberately does
    # not: only a submitted task means another agent turn owns the session now.
    records = [
        rec("q", "user", "какой сегодня день"),
        rec("t", "user", TASK_N),
        rec("q2", "user", "расскажи про квантовые точки"),
        rec("a2", "assistant", "Квантовые точки — это наночастицы."),
    ]
    # When/Then the lease stands, and the window is unbounded above: the answer
    # arriving late is still this turn's to deliver
    assert newer_task(records, TASK_N) is False
    assert own_text(records, "", TASK_N) == "Квантовые точки — это наночастицы."


def test_a_newer_submitted_task_is_a_newer_task() -> None:
    # Given the live shape: another agent turn armed behind this one
    records = [
        rec("q", "user", "какой сегодня день"),
        rec("t", "user", TASK_N),
        rec("t2", "user", TASK_N1),
    ]
    # When/Then this collector no longer owns the session
    assert newer_task(records, TASK_N) is True


def test_asking_the_same_thing_twice_binds_the_newest_task() -> None:
    # Given two identical task messages -- text alone cannot tell whose is whose,
    # so the lease binds the newest rather than the first
    records = [rec("t1", "user", TASK_N), rec("t2", "user", TASK_N)]
    # When/Then it stands: a first match would bind the newer turn's collector to
    # the older task and make it give up on an answer nobody else collects, so
    # the failure direction is delivery, never loss
    assert newer_task(records, TASK_N) is False
    assert own_text(records, "", TASK_N) == ""


def test_the_own_window_ends_before_the_next_turn() -> None:
    # Given a turn that answered, then the session moved on and the next turn
    # answered too
    records = [
        rec("anchor", "assistant", "прежний ответ"),
        rec("task_n", "user", TASK_N),
        rec("ans_n", "assistant", "Понедельник."),
        rec("task_n1", "user", TASK_N1),
        rec("ans_n1", "assistant", PHYSICS),
    ]
    # When/Then the window holds this turn's output alone: the next turn's
    # answer is never in it to wake on, even though the anchor is behind both
    assert own_text(records, "anchor", TASK_N) == "Понедельник."
    # ... and the next turn, anchored where its own hand-off left it -- after
    # the earlier answer -- owns its own
    assert own_text(records, "ans_n", TASK_N1) == PHYSICS


def test_an_orphan_with_no_output_of_its_own_owns_nothing() -> None:
    # Given the live shape: the task, then the next turn's task and answer,
    # and nothing of this turn's in between
    records = [
        rec("anchor", "assistant", "прежний ответ"),
        rec("task_n", "user", TASK_N),
        rec("task_n1", "user", TASK_N1),
        rec("ans_n1", "assistant", PHYSICS),
    ]
    # When/Then there is no output of its own to deliver -- giving up is the
    # only honest outcome, and it is `TurnSuperseded`, not the next answer
    assert own_text(records, "anchor", TASK_N) == ""


def test_an_unpositionable_anchor_owns_nothing_either() -> None:
    # Given the sweep deleted the anchor this collector was armed at
    records = [rec("task_n", "user", TASK_N), rec("ans_n", "assistant", "Понедельник.")]
    # When/Then the cut cannot be positioned, so it is nothing -- the same rule
    # as `assistant_text`, and for the same reason
    assert own_text(records, "msg_swept_away", TASK_N) == ""


# ---------------------------------------------------------------------------
# 3. two turns, one session: the orphan gives up, the owner delivers once
# ---------------------------------------------------------------------------


def test_the_orphan_read_raises_and_the_owner_reads_and_claims() -> None:
    # Given the live transcript: turn N's task, turn N+1's task, and N+1's answer
    records = [
        rec("anchor", "assistant", "прежний ответ"),
        rec("task_n", "user", TASK_N),
        rec("task_n1", "user", TASK_N1),
        rec("ans_n1", "assistant", PHYSICS),
    ]
    delivered = DeliveryLedger()
    orphan = TurnLease("anchor", TASK_N, delivered)
    owner = TurnLease("task_n", TASK_N1, delivered)
    # When the orphan reads the window holding the next turn's answer
    with pytest.raises(TurnSuperseded):
        orphan.read("ses_1", records)
    # Then the owner reads exactly its turn's answer ...
    assert owner.read("ses_1", records) == PHYSICS
    delivered.claim("ses_1", records)
    # ... and a twin of the owner -- the double delivery the live run measured
    # 1.3 s apart -- finds every id already delivered and gives up too
    with pytest.raises(TurnSuperseded):
        TurnLease("task_n", TASK_N1, delivered).read("ses_1", records)


def test_a_collector_whose_turn_answered_still_delivers_it_after_the_session_moved_on() -> None:
    # Given a turn that answered BEFORE the next question was asked: its
    # collector slept through the newer turn, which is the warm-up shape every
    # returning session produces, not the orphan shape
    records = [
        rec("anchor", "assistant", "прежний ответ"),
        rec("task_n", "user", TASK_N),
        rec("ans_n", "assistant", "Понедельник."),
        rec("task_n1", "user", TASK_N1),
        rec("ans_n1", "assistant", PHYSICS),
    ]
    delivered = DeliveryLedger()
    # When/Then it delivers its own turn's output alone -- giving up here would
    # make every second turn of every session a loss, which is the fix the task
    # forbids: a collector that DOES produce output must still ship it
    assert TurnLease("anchor", TASK_N, delivered).read("ses_1", records) == "Понедельник."


def test_a_growing_window_is_not_an_already_delivered_one() -> None:
    # Given one collector whose answer is still arriving: the first poll claimed
    # what it saw, and the window has grown since
    delivered = DeliveryLedger()
    first = [rec("a1", "assistant", "Собираю")]
    delivered.claim("ses_1", first)
    # When/Then a later poll of the SAME collector carries strictly more, so it
    # is not refused -- the ledger blocks twins, not continuations
    grown = [*first, rec("a2", "assistant", "Готово")]
    assert delivered.already_delivered("ses_1", grown) is False
    assert delivered.already_delivered("ses_1", first) is True


def test_claiming_an_empty_window_locks_out_nothing() -> None:
    # Given a collector that has read nothing yet
    delivered = DeliveryLedger()
    delivered.claim("ses_1", [])
    # When/Then it cannot lock out the collector that actually has the answer
    assert delivered.already_delivered("ses_1", [rec("a1", "assistant", "Готово")]) is False
