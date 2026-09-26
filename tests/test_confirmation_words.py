"""The confirmation reader, read as a whole-word matcher (live-run defect D3b).

`core.policies.confirmation_verdict` decides whether a human's free text is an
answer to a question R2D2 asked. An approval on that answer lets the agent run a
command the user never agreed to, so the affirmative side is the dangerous one --
and it used to be a **substring** test. `да` is a substring of `дай`, `да`,
`задача`, `награда`, `удар`, `сдать`; `ок` is a substring of `рокировка`,
`абок`, `сок`; `ага` of `агата`. One of those arrived while a permission was
pending and the broker posted a real one-time approval for a sentence that was
not an answer. The live run is quoted in `qa/live-run.md` §4d.

So the rule this file pins is narrow and mechanical:

* **A confirmation word is a whole word.** The message is cut into word tokens
  (`\\w+` over Unicode) and a set entry matches only when it equals one token --
  or, for a multi-word entry such as `да да`, a run of adjacent tokens. Casing,
  punctuation, emoji and surrounding whitespace cannot create or destroy a match,
  because none of them is inside a token.
* **Denial is read first, as before.** `да, не надо` contains both an
  affirmative and `не`, and the refusal wins.
* **No confirmation word at all is neither.** It is `None`, and `None` is what
  leaves a pending ask exactly where it is: the broker answers `unrelated`, the
  message is handled as an ordinary question, and the ask is refused by the sweep
  at `r2d2_permission_timeout`. Nothing is approved and nothing is closed.
* **`AFFIRM_WORDS` and `DENY_WORDS` are the only vocabulary.** Every entry in
  both tuples is exercised below in its own bare form, so a word deleted from a
  set -- or a second list hard-coded anywhere else -- fails here.

A bare word also has to keep working, because that is what the question R2D2
actually asks: it tells the user to reply «да» or «нет».

allow: SIZE_OK -- the file is one parametrized table plus the cases a table
cannot state. Splitting it would put half the vocabulary in one file and half in
another, and the property under test -- that a set and its reader agree -- is
exactly what a split would hide.
"""

from __future__ import annotations

from typing import Final

import pytest

from core.policies import AFFIRM_WORDS, DENY_WORDS, confirmation_verdict

#: Every entry of both sets, and the verdict its own bare form must read as. The
#: single source of truth asserted from the other side: if a word leaves a set,
#: the row that carries it fails.
BARE_FORMS: Final[tuple[tuple[str, str], ...]] = tuple(
    (word, "yes") for word in AFFIRM_WORDS
) + tuple((word, "no") for word in DENY_WORDS)


#: (message, verdict). `None` is the third verdict and is not a shrug: it is what
#: keeps a pending ask pending.
CASES: Final[tuple[tuple[str, str | None], ...]] = (
    # -- the affirmative, in every shape a Telegram message really arrives in ---
    ("да", "yes"),
    ("Да", "yes"),
    ("ДА", "yes"),
    ("дА", "yes"),
    ("да!", "yes"),
    ("да!!!", "yes"),
    ("да .", "yes"),
    ("да.", "yes"),
    ("да,", "yes"),
    ("  да  ", "yes"),
    ("да 😃", "yes"),
    ("ок, да", "yes"),
    ("да да", "yes"),
    ("да-да", "yes"),
    ("да, да!", "yes"),
    ("давай", "yes"),
    ("ага", "yes"),
    ("угу", "yes"),
    ("да, запускай", "yes"),
    # -- the refusal, including the one that carries an affirmative with it ---
    ("нет", "no"),
    ("Нет", "no"),
    ("НЕТ", "no"),
    ("нет.", "no"),
    ("нет-нет", "no"),
    ("не надо", "no"),
    ("не", "no"),
    ("отмена", "no"),
    ("стоп", "no"),
    ("да, не надо", "no"),
    ("давай, не надо", "no"),
    ("да, не", "no"),
    # -- a sentence that merely CONTAINS a confirmation word -------------------
    # The defect. Each of these was an approval before the reader matched whole
    # words, and each is an ordinary thing to type while a question is open.
    ("дай", None),
    ("дай сводку", None),
    ("давай сводку", "yes"),  # `давай` is its own word in the set
    ("давайте", None),
    ("задача", None),
    ("награда", None),
    ("удар", None),
    ("сдать", None),
    ("издатель", None),
    ("рокировка", None),
    ("сок", None),
    ("агата", None),
    ("даю", None),
    # -- no confirmation word at all -------------------------------------------
    ("", None),
    ("   ", None),
    ("а это вообще что", None),
    ("привет", None),
    ("да?", "yes"),  # a question mark is punctuation, not doubt
    # -- the negation keeps refusing an ordinary sentence -----------------------
    # `не` used to deny any word CONTAINING it, so "неопределённое" refused. It
    # is one word now, so it is not a denial at all -- and an ask that is not
    # answered is refused by the sweep, which is the safe direction.
    ("что-то неопределённое", None),
    ("некоторый", None),
    ("понедельник", None),
    ("недавно", None),
)


@pytest.mark.parametrize(("text", "expected"), CASES, ids=[c[0] or "empty" for c in CASES])
def test_a_confirmation_is_read_as_a_whole_word(text: str, expected: str | None) -> None:
    # Given / When / Then: the message the user typed, and the one verdict it may read as
    assert confirmation_verdict(text) == expected


@pytest.mark.parametrize(("word", "expected"), BARE_FORMS, ids=[w for w, _ in BARE_FORMS])
def test_every_word_in_the_sets_is_still_recognised(word: str, expected: str) -> None:
    """The bare form of every entry must read as itself.

    This is what keeps `AFFIRM_WORDS`/`DENY_WORDS` the single source of truth: the
    reader is exercised against the vocabulary rather than against a copy of it, so
    removing a word, or a second list appearing anywhere, fails here.
    """
    assert confirmation_verdict(word) == expected


def test_a_denial_beats_an_approval_in_the_same_message() -> None:
    """`DENY_WORDS` holds the bare `не`, so a message carrying both is a refusal.

    Checked for every affirmative that could plausibly be followed by `не`, rather
    than for the one sentence the live run happened to produce.
    """
    for word in AFFIRM_WORDS:
        assert confirmation_verdict(f"{word}, не надо") == "no", word


def test_nothing_is_decided_without_text() -> None:
    """`None` and an empty string are "no answer", which is the third verdict.

    `None` must never be confused with a refusal either: the broker treats it as
    `unrelated` and leaves the ask alone, so a caller cannot turn a missing answer
    into a rejection of somebody else's row.
    """
    assert confirmation_verdict(None) is None
    assert confirmation_verdict("") is None


def test_only_two_verdicts_exist() -> None:
    """`yes` and `no` are the whole domain, and `None` is not a third answer.

    A new verdict would have to be a new `str` here, which is the point: the
    broker's `match verdict` has two arms and an `unrelated` default, and nothing
    downstream can act on a value that cannot be constructed.
    """
    produced = {confirmation_verdict(text) for text, _ in CASES}
    assert produced <= {"yes", "no", None}
    assert "yes" in produced and "no" in produced and None in produced


def test_a_confirmation_word_is_never_a_substring_of_the_message() -> None:
    """The defect itself, stated as a property over the vocabulary.

    For every affirmative there must be at least one ordinary word that contains
    it and does not read as an answer -- otherwise the whole-word rule is
    untested for that word, and a future reader could go back to `in` unnoticed.
    """
    traps = {
        "да": "задача",
        "давай": "давайте",
        "ок": "рокировка",
        "ага": "агата",
        "угу": "угущать",
    }
    for word in AFFIRM_WORDS:
        trap = traps.get(word)
        if trap is None:
            continue
        assert word in trap, (word, trap)
        assert confirmation_verdict(trap) is None, (word, trap)


@pytest.mark.parametrize(
    "text",
    (
        "да.запускай",
        "да/запускай",
        "да-запускай",
        "да\tзапускай",
        "да\nзапускай",
        "да\u00a0запускай",
    ),
)
def test_a_token_is_delimited_by_anything_that_is_not_a_word_character(text: str) -> None:
    """Not by whitespace: by the tokenisation itself.

    A reader that split on spaces would pass every sentence in `CASES` above and
    then approve `да.запускай` in production, because a dot is not a space. The
    table pins the rule; this pins the alphabet it is allowed to delimit with.
    """
    assert confirmation_verdict(text) == "yes"
