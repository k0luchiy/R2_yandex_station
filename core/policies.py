"""How risky a command is, and what a human's reply to a question means.

Two unrelated decisions that share a file because both are about a human saying
something short and consequential: `risk_level` decides whether a command is
confirmed out loud before it runs, and `confirmation_verdict` decides whether a
reply is an answer to a question R2D2 already asked.

**The confirmation reader matches WHOLE WORDS, and the affirmative side is the
reason.** A «да» approves a command the user never agreed to run, so a word may
only be found as a word: `да` is a substring of `дай`, `задача`, `награда`,
`удар`, `сдать`, and `ок` is a substring of `рокировка`. Read as substrings, an
ordinary sentence approved a pending permission -- proved live in
`qa/live-run.md` §4d, where `дай сводку` was posted to opencode as `once`. The
message is cut into word tokens instead, so casing, punctuation, emoji and
whitespace are all outside the comparison and cannot create a match.

`DENY_WORDS` is still read first, so `да, не надо` is a refusal, and a message
with no confirmation word in it is `None` rather than a guess: `None` is what
leaves a pending ask pending for the real answer, and what the timeout sweep
later refuses.
"""

import re
from collections.abc import Iterable

SAFE_PREFIXES = (
    "ls", "pwd", "df", "ps", "cat", "echo", "date", "uptime", "whoami",
    "free", "du", "uname", "hostname", "git status", "git diff",
)

RISKY_KEYWORDS = (
    "rm ", "rm -", "rmdir", "mkfs", "shutdown", "sudo", "reboot", "kill",
    "pkill", "dd ", "chmod 777", "chmod +x", "chown", "mount", "fdisk",
    "mke2fs", "format", "wipe", "> /dev", "crontab",
)

CAUTION_KEYWORDS = (
    "systemctl", "service ", "apt ", "dnf ", "pacman", "pip install",
    "git pull", "npm install", "killall",
)

AFFIRM_WORDS = (
    "да", "подтверждаю", "подтверди", "выполняй", "валяй", "давай", "ок",
    "окей", "согласен", "конечно", "точно", "выполнить", "выполняем", "ага",
    "да да", "угу",
)

DENY_WORDS = (
    "нет", "не надо", "отмена", "отмени", "отменить", "стоп", "хватит",
    "не", "неа", "остановись",
)


def risk_level(command: str) -> str:
    low = command.strip().lower()
    low = re.sub(r"\s+", " ", low)
    if not low:
        return "safe"
    if any(k in low for k in RISKY_KEYWORDS):
        return "dangerous"
    if any(k in low for k in CAUTION_KEYWORDS):
        return "caution"
    return "safe"


#: A word is a maximal run of word characters, so a confirmation word can only be
#: matched whole: anything that is not a word character -- a space, a comma, a
#: full stop, a hyphen, an emoji -- ends the token instead of joining it.
_WORD = re.compile(r"\w+")


def _phrases(words: Iterable[str]) -> dict[int, frozenset[tuple[str, ...]]]:
    """A word set as token tuples, grouped by how many words each entry is.

    Grouping by length is what lets a multi-word entry (`да да`, `не надо`) be
    matched as a phrase without turning every single word into a prefix test
    again -- the two sets above stay the only vocabulary, and nothing here
    hard-codes a word of its own.
    """
    grouped: dict[int, set[tuple[str, ...]]] = {}
    for word in words:
        tokens = tuple(word.casefold().split())
        if tokens:
            grouped.setdefault(len(tokens), set()).add(tokens)
    return {width: frozenset(phrases) for width, phrases in grouped.items()}


_AFFIRM_PHRASES = _phrases(AFFIRM_WORDS)
_DENY_PHRASES = _phrases(DENY_WORDS)


def _mentions(tokens: list[str], phrases: dict[int, frozenset[tuple[str, ...]]]) -> bool:
    """Whether any run of adjacent tokens is one of the set's phrases."""
    return any(
        tuple(tokens[start : start + width]) in wanted
        for width, wanted in phrases.items()
        for start in range(len(tokens) - width + 1)
    )


def confirmation_verdict(text: str | None) -> str | None:
    """What a reply decides: `"yes"`, `"no"`, or `None` when it decides nothing.

    `None` is a real answer and not a shrug: `core/permissions.py` turns it into
    `unrelated`, which leaves the pending ask exactly where it was -- still
    answerable, and refused by the timeout sweep if nobody answers it. Guessing
    is what the substring reader did, and an approval guessed out of an ordinary
    sentence is arbitrary code execution on somebody's laptop.

    Denial is read before agreement, because `DENY_WORDS` holds the bare `не` and
    `да, не надо` is a refusal in Russian.
    """
    if not text:
        return None
    tokens = _WORD.findall(text.casefold())
    if _mentions(tokens, _DENY_PHRASES):
        return "no"
    if _mentions(tokens, _AFFIRM_PHRASES):
        return "yes"
    return None
