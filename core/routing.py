"""The one decision that keeps the voice path inside Alice's 4.5 s budget -- and
the three guards that keep a machine token from ever reaching a human (plan todo 13).

`r2d2-voice` gets one round, `r2d2_fast_deadline` seconds, to either answer or
say it cannot. It signals the second case by emitting the sentinel
(`r2d2_needs_agent_sentinel`, `[[NEEDS_AGENT]]` by default) into its reply, and
this module turns that string into a `RouteDecision`. Two independent guards
stand between the sentinel and Alice's `text`/`tts` field, the third is the
brain's own assertion (todo 15), and a fourth guards the other direction a turn
can leave by -- Telegram, which Alice cannot send to:

* `parse_voice_reply` is the **structural** guard: it splits at the first
  sentinel and *discards the tail*, so the escalation payload is never a value
  anyone can pass on. It cannot be forgotten downstream, because there is
  nothing to forget -- and the discarded `spoken` is `""`, not the hint, because
  the ack (`r2d2_task_ack`) replaces it. Speaking the hint would mean the user
  hears half a request instead of an answer.
* `sanitize_for_speech` is the **total** guard: `""` for *any* input containing
  the sentinel, including `None`. It exists for the caller who reaches a
  `text`/`tts` field without going through `parse_voice_reply` -- a future tool
  result, a Telegram-only answer, a backend's error string. A guard that is
  total rather than case-by-case is the whole point: enumerating the shapes is
  how a guard leaks.
* `for_human` is the guard for the **collector**, which reads an assistant
  message straight out of the session and has no idea whether it is an answer or
  a routing signal. It lives here rather than in the worker because the sentinel
  is this module's subject: the worker is handed the token, exactly as
  `parse_voice_reply` is. Unlike the two above it does NOT drop the whole
  message -- a 60-paper digest that quotes the token in one sentence must still
  arrive -- so it removes the token and keeps the rest.

Three decisions this module makes, all tested:

* **An empty sentinel raises `ValueError`.** `"" in raw` is true for every
  string, so treating an empty sentinel as "absent" would silently disable
  escalation (the model could emit the real sentinel and have it read aloud),
  while treating it as a match would mute the voice path entirely. Neither is
  acceptable, and both are misconfiguration, so it is loud instead.
* **A model id is never provider-less.** `split_model` is re-exported from
  `core.opencode.wire`, the one implementation the client and this module
  share, so `"/x"` gets the same `("opencode", "x")` the wire body needs rather
  than a `("", "x")` that the server would reject.

The module is pure: no I/O, no config, no logging, no globals beyond the type
constants. It is handed the sentinel, so changing
`r2d2_needs_agent_sentinel` changes the routing without touching this file.
It also does **not** clean or truncate: that is `core/render.py:clean`, and it
must run *after* the sentinel is gone, so a sentinel past the 1024-char cut is
still found here (`test_truncation_cannot_hide_the_sentinel`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from core.opencode.wire import split_model

__all__ = [
    "RouteDecision",
    "for_human",
    "parse_voice_reply",
    "sanitize_for_speech",
    "split_model",
]


@dataclass(frozen=True, slots=True)
class RouteDecision:
    """What the voice turn decided, and the two things the caller may use.

    `spoken` is what may reach Alice's `text`/`tts`; it is `""` on an
    `escalate` decision, so the ack is spoken in its place. `task_hint` is the
    text the model wrote *before* the sentinel -- the optional refinement it
    offers about the task -- and is `""` when it offered none or only
    whitespace. Frozen and slotted because a decision is a fact the caller
    forwards, not a draft it may edit on the way to the speaker.
    """

    kind: Literal["speak", "escalate"]
    spoken: str
    task_hint: str


def _require_sentinel(sentinel: str) -> None:
    if not sentinel:
        raise ValueError(
            "routing: the escalation sentinel must be a non-empty literal; an "
            "empty one matches every reply and would mute the voice path"
        )


def parse_voice_reply(raw: str, *, sentinel: str) -> RouteDecision:
    """Route one voice reply, discarding everything from the sentinel onward.

    The sentinel is matched **literally** with `str.find` -- it is
    `[[NEEDS_AGENT]]`, a regex character class to anyone careless, so no regex
    engine ever sees it. The **first** occurrence splits, so a doubled sentinel
    cannot smuggle a second payload past the guard, and the text before it is
    the whole hint. Input without a sentinel is returned untouched: routing
    neither cleans nor truncates, because `render.clean` runs later and must
    never be the thing that decides whether a sentinel survived.
    """
    _require_sentinel(sentinel)
    index = raw.find(sentinel)
    if index < 0:
        return RouteDecision(kind="speak", spoken=raw, task_hint="")
    return RouteDecision(kind="escalate", spoken="", task_hint=raw[:index].strip())


def sanitize_for_speech(raw: str | None, *, sentinel: str) -> str:
    """The last thing a string passes through on its way to a speaker.

    Total by construction: `""` for `None` and for *any* input containing the
    sentinel, the input itself otherwise -- unchanged, because a sanitiser that
    also reformats is a second, differently-buggy `render.clean`. Dropping the
    whole string rather than the sentinel alone is deliberate: a reply that
    asked to be escalated has nothing speakable in it, and a partial strip
    would leave the model reading its own instruction aloud.
    """
    _require_sentinel(sentinel)
    if raw is None or sentinel in raw:
        return ""
    return raw


def for_human(raw: str | None, *, sentinel: str) -> str:
    """The part of `raw` a human may read: the token is out, the answer is in.

    The collector reads assistant text straight out of a session, and a session
    holds BOTH kinds of assistant message: the agent's answer, and the voice
    agent's `[[NEEDS_AGENT]]` routing signal. Telling them apart is this
    module's job, so this is where the Telegram boundary is guarded -- one
    function every outbound body passes through, rather than a strip in each
    producer that can be forgotten by the next one.

    Unlike `sanitize_for_speech` it does **not** drop everything it is given.
    On a speaker there is nothing else in a text carrying the token, but a
    Telegram body is a 60-paper digest that may *quote* the token in a sentence,
    and suppressing the whole message to remove one word would throw the answer
    away with it. So the token is removed and the rest survives:

    * a line that was nothing but the token disappears, because a line of pure
      protocol is not a message;
    * a line that merely mentions it keeps every other character, with the double
      space the removal leaves closed, so an answer is never mangled past
      recognition;
    * a line without the token is passed through byte for byte -- the output is a
      subsequence of the input's lines, and the only thing ever edited is a line
      the protocol had already touched.

    `""` means nothing was left to show, which is the same fact an empty
    collection reports and is the caller's to state.
    """
    _require_sentinel(sentinel)
    if not raw:
        return ""
    kept: list[str] = []
    for line in raw.split("\n"):
        if sentinel not in line:
            kept.append(line)
            continue
        remainder = line.replace(sentinel, "").replace("  ", " ").strip()
        if remainder:
            kept.append(remainder)
    return "\n".join(kept)
