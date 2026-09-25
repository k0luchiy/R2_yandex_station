"""One opencode session per R2D2 user, behind the ordinary `Backend` protocol.

The brain has two jobs -- answer a voice question inside 3.2 s, and get real work
done -- and opencode offers both through the same persistent session. This module
is the seam that lets `core/brain.py` keep calling ONE interface: `complete()` is
the fast voice turn, `submit_task()` is the agent turn, and the protocol signature
stays untouched while the session, the agent name and the model all come from
`config/backends.json`.

Everything hard about it was measured in todo 1
(`docs/11-opencode-contract.md`) rather than guessed, and three of those findings
shape the code here:

* **C1 -- a model this server does not know is NOT an error.** opencode
  substitutes a different model, answers **200** and the reply looks fine, so the
  brain would answer from a brain nobody chose. `validate_models()` is the startup
  gate for the cheap half of this and `core.opencode.models` says exactly how much
  it proves; a refused model and a silently substituted one are caught here, at the
  turn, as `OpencodeErrorEnvelope` and `OpencodeEmptyReply` -- never as an answer.
* **C3/U2 -- the session already holds the conversation.** Only the new utterance
  goes on the wire, and the system prompt lives in the agent definition (todo
  10), not in a per-message field. For the same reason `tools`, `max_tokens` and
  `temperature` are accepted and ignored: the agent's own `tools`/`permission`
  (C4, U3) is the only place a tool decision can live, and `Choice.tool_calls`
  is always empty because this protocol carries text, not tool calls.
* **A deadline is not an abort.** The first message in a fresh session costs
  15.5-18.6 s (C8), so a turn that runs past the voice budget keeps running
  server-side and the collector below fetches it. `OpencodeDeadlineExceeded`
  therefore propagates unchanged and nothing here ever calls `abort`.

The session is found through a module-level `ContextVar` rather than a parameter,
because the `Backend` protocol has no `application_id` to pass and adding one
would change every caller of every backend for the sake of one. That only works
if the variable is task-local, which `ContextVar` is: two Alice users are two
tasks and each reaches its own session. Todo 15 sets it before calling; an unset
value is a programming error and raises rather than guessing a session.

`collect_reply` is the F1 polling fallback that `EVENT_MODE` (currently `"sse"`,
todo 7) does not need. It stays because the degradation is pre-agreed, typed and
now tested: the idleness rule is "no new assistant text for one
`r2d2_event_poll_interval`", not `session.idle`, because a session can sit `busy`
for a whole agent turn and cutting on that would truncate live work.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Final

from app.config import Config
from core.backends.base import ChatMessage, Choice, ToolSchema
from core.backends.config_loader import BackendSpec
from core.opencode.client import OpencodeClient
from core.opencode.models import verify_models
from core.opencode.session_store import OcSessionStore
from core.opencode.sse import EventSource
from core.opencode.wire import MessageRecord, OpencodeError

__all__ = [
    "NoCurrentApplicationId",
    "OpencodeEmptyReply",
    "OpencodeSessionBackend",
    "OpencodeWiring",
    "current_application_id",
]

log = logging.getLogger(__name__)

#: The user whose session the next `complete()` serves. Set by the brain
#: (todo 15) immediately before the call; `None` and "" are the two forms of
#: "unset", and neither is a user.
current_application_id: Final[ContextVar[str | None]] = ContextVar(
    "r2d2_current_application_id", default=None
)
#: The two roles opencode's `info.role` distinguishes on this route (C7).
USER_ROLE: Final = "user"
ASSISTANT_ROLE: Final = "assistant"


# ---------------------------------------------------------------------------
# Failures
# ---------------------------------------------------------------------------


class NoCurrentApplicationId(OpencodeError):
    """`complete()` was called with no `current_application_id` set.

    Guessing is the one move here that cannot be recovered from: two users share
    this process, and a wrong guess answers one from the other's context.
    """


class OpencodeEmptyReply(OpencodeError):
    """A 200 whose `parts` carried no text -- C1's silent model substitution.

    `Choice(content="")` would be a misleading success: the speaker has nothing to
    say and the operator sees no reason why.
    """


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OpencodeWiring:
    """What the composition root (todo 18) hands the adapter: server, store, config.

    Grouped into one value because the adapter takes no more than the spec and
    this: a fifth positional argument is how a constructor stops being readable.
    `events` is the `GET /event` reader for the live `EVENT_MODE == "sse"` path --
    carried, and closed, but not consulted here, because with SSE there is nothing
    to poll. Todo 14's permission broker is its other reader.
    """

    client: OpencodeClient
    store: OcSessionStore
    cfg: Config
    events: EventSource | None = None


def _assistant_text(records: Sequence[MessageRecord], since_message_id: str) -> str:
    """The assistant text in `records` that is newer than `since_message_id`.

    `GET /session/:id/message` is chronological (C7), so "newer than the marker"
    means "after the marker's position in the list". A marker the server no longer
    returns means the window was truncated at the front, and then every message
    still listed is newer than it: losing a whole reply because an anchor
    scrolled out of the window is the worse failure. Only the assistant speaks --
    the user's own text is the request, not the answer.
    """
    ids = [record.id for record in records]
    start = ids.index(since_message_id) + 1 if since_message_id in ids else 0
    return "\n".join(
        record.text for record in records[start:] if record.role == ASSISTANT_ROLE and record.text
    )


# ---------------------------------------------------------------------------
# The adapter
# ---------------------------------------------------------------------------


class OpencodeSessionBackend:
    """The opencode session, as a `Backend` plus the two agent-path methods.

    Satisfies `core.backends.base.Backend` structurally; `name` is the spec name,
    which is what lands in `Choice.provider` and in the brain's log line. The
    client, the session store and the config arrive as one `OpencodeWiring`, and
    nothing is opened at construction.
    """

    def __init__(self, spec: BackendSpec, wiring: OpencodeWiring) -> None:
        self._spec = spec
        self._wiring = wiring
        self.name: str = spec.name
        self._poll_interval_s = wiring.cfg.r2d2_event_poll_interval
        log.info(
            "opencode session backend: name=%s fast_model=%s task_agent=%s poll_interval=%.1fs",
            self.name, spec.fast_model, spec.task_agent, self._poll_interval_s,
        )

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSchema] | None = None,
        *,
        max_tokens: int = 400,
        temperature: float = 0.7,
        timeout: float = 3.0,
        model: str | None = None,
    ) -> Choice:
        """The voice turn for the current application, in the user's own session.

        `timeout` IS the deadline the caller wants -- todo 15 passes
        `cfg.r2d2_fast_deadline`, measured at 3.2 s inside Alice's 4.5 s budget --
        so this does not second-guess it with a private default. `tools`,
        `max_tokens` and `temperature` are accepted and ignored (see the module
        docstring); `model` overrides the configured `fast_model` for one call.

        Raises `NoCurrentApplicationId`, `OpencodeError` -- including
        `OpencodeDeadlineExceeded`, which the caller branches on to acknowledge
        the turn instead of failing it -- or `httpx.HTTPError`.
        """
        application_id = self._application_id()
        text = self._turn_text(messages)
        session_id = await self._wiring.store.resolve(application_id)
        used_model = model or self._spec.fast_model
        reply = await self._wiring.client.send_message(
            session_id, text, agent=self._spec.voice_agent, model=used_model, deadline_s=timeout
        )
        if not reply.text.strip():
            raise OpencodeEmptyReply(
                f"opencode {self.name!r}: the turn came back with no text for model {used_model!r}; "
                "an unknown modelID is answered 200 by a DIFFERENT model (C1), so there is nothing "
                "here to speak"
            )
        return Choice(content=reply.text, tool_calls=[], provider=self.name, model=used_model)

    async def submit_task(self, session_id: str, text: str) -> None:
        """`POST /session/:id/prompt_async` with the AGENT agent: submitted, not awaited.

        Fire and forget by contract -- the 204 means the server owns the turn now,
        and the reply is collected later by `collect_reply`. The client's own
        per-request timeout bounds this call, so a stalled server cannot hold the
        acknowledgement the speaker is waiting for.
        """
        await self._wiring.client.send_message_async(
            session_id, text, agent=self._spec.task_agent, model=self._spec.task_model
        )

    async def collect_reply(self, session_id: str, since_message_id: str, timeout_s: float) -> str:
        """The agent's assistant text produced after `since_message_id`, or `""`.

        The F1 fallback collector. It ends on whichever comes first: no new
        assistant text for one `r2d2_event_poll_interval`, or `timeout_s` elapsed.
        Both bounds are absolute -- each poll is itself given only the time left
        before the deadline, so a server that accepts a request and never answers
        cannot outlive `timeout_s` either.

        Everything runs inline, with no task of its own, so a caller that cancels
        the collector (a worker shutting down) cancels the request with it and
        leaves nothing running. A session that disappears mid-poll raises the
        client's typed status error instead of spinning to the ceiling.
        """
        deadline = time.monotonic() + timeout_s
        changed_at = deadline
        text = ""
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return text
            try:
                records = await asyncio.wait_for(
                    self._wiring.client.list_messages(session_id), remaining
                )
            except TimeoutError:
                return text
            candidate = _assistant_text(records, since_message_id)
            if candidate != text:
                text, changed_at = candidate, time.monotonic()
            if time.monotonic() - changed_at >= self._poll_interval_s:
                return text
            await asyncio.sleep(min(self._poll_interval_s, deadline - time.monotonic()))

    async def validate_models(self) -> tuple[str, ...]:
        """Check every model this spec configures against the server; return them.

        The startup half of C1, called from the lifespan (todo 18) before the first
        Alice request, because an id the server does not list is a silent brain swap
        and only a pre-flight check can name it. The gate itself, and exactly what
        it can and cannot prove, is `core.opencode.models`.
        """
        return await verify_models(self._spec, self._wiring.client)

    async def aclose(self) -> None:
        """Close the client and the event source; never the store, which is not ours.

        Both closeables own what they opened and nothing more, so this is safe to
        call twice -- a lifespan and a supervisor may both reach shutdown.
        """
        await self._wiring.client.aclose()
        events = self._wiring.events
        if events is not None:
            await events.aclose()

    # -- internals ---------------------------------------------------------

    def _application_id(self) -> str:
        """The current application, or a typed refusal to guess one."""
        application_id = current_application_id.get()
        if not application_id:
            raise NoCurrentApplicationId(
                f"opencode {self.name!r}: no current_application_id is set, so there is no session to "
                "answer from; set it before complete() rather than letting the adapter pick a user"
            )
        return application_id

    def _turn_text(self, messages: Sequence[ChatMessage]) -> str:
        """The new utterance: the LAST user message, because the session holds the rest.

        Scanning backwards rather than taking `messages[-1]`, because a caller may
        append a system note after the question. Anything that is not a non-empty
        user turn is a caller bug, refused here before a request exists: a turn
        with no text is a wasted round trip on the voice budget.
        """
        for message in reversed(messages):
            content = message.get("content")
            if message.get("role") == USER_ROLE and isinstance(content, str) and content.strip():
                return content
        raise OpencodeError(
            f"opencode {self.name!r}: the turn carries no user message with text, so there is nothing "
            "to send to the session"
        )
