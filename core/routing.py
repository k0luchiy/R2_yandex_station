"""The one decision that keeps the voice path inside Alice's 4.5 s budget -- and
the two guards that keep a machine token from ever being spoken (plan todo 13).

`r2d2-voice` gets one round, `r2d2_fast_deadline` seconds, to either answer or
say it cannot. It signals the second case by emitting the sentinel
(`r2d2_needs_agent_sentinel`, `[[NEEDS_AGENT]]` by default) into its reply, and
this module turns that string into a `RouteDecision`. Two independent guards
stand between the sentinel and Alice's `text`/`tts` field, and the third is the
brain's own assertion (todo 15):

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

Two decisions this module makes, both tested:

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
