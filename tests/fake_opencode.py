"""`opencode serve` as a scriptable FastAPI app (plan todo 18).

The whole stack is proved against this file, so it has to be a SERVER and not a
transport mock: `tests/test_metrics_and_health.py` covers the two read-only routes
through `httpx.MockTransport`, and nothing so far has run the real
`OpencodeClient`, the real `OcSessionStore`, the real `PermissionBroker` and the
real `EventSource` over an HTTP surface that answers `GET /event` with a stream.
That is where the two shapes a mock cannot produce live: a response that never
ends, and a frame that arrives after the request did.

**Every route R2D2 uses, and no others.** The list is
`docs/11-opencode-contract.md`: `GET /global/health`, `GET /config/providers`,
`GET /agent`, `POST /session`, `GET /session`, `GET /session/:id/message`,
`POST /session/:id/message`, `POST /session/:id/prompt_async`,
`POST /session/:id/abort`, `POST /session/:id/summarize`,
`POST /session/:id/permissions/:permissionID`, `GET /session/status` and the
`GET /event` stream. An unknown route answers 404 with opencode's own
`NotFoundError` body, so a client that starts reaching for something else fails
instead of quietly working.

**The shapes are the measured ones, not convenient ones.** `{info, parts}` for a
message (C7 -- reading `role` off the top level yields `None`, which is how the
spike probe broke), `?directory=` as the session scope (C6), `204` from
`prompt_async`, `{healthy, version}` from `/global/health`, and a
`permission.asked` frame whose `properties` carry `sessionID`, `id`,
`metadata.command` and `always` (C5).

**Scriptable, in both modes.** The behaviour is attributes on a `FakeOpencode`
object a test mutates -- `reply`, `escalation`, `hang`, `error_status`,
`turn_error`, `delay_s` -- never module-level magic, so two fixtures in one
session cannot leak into each other. The STANDALONE mode cannot be mutated from
another process, so the same object also answers from the WORDS in the request
(`hang` hangs it, a question about a digest escalates, `permission` raises an
ask): `uvicorn tests.fake_opencode:app` is then drivable from a shell script,
which is what the todo-18 QA evidence does. The `/_fake/*` routes report what the
server recorded, so an out-of-process check reads the wire rather than the
application's memory.

`GET /event` is a GLOBAL stream (C5): one subscriber list for every session, and
the server never filters by `sessionID` -- the READER filters, and a fake that
filtered for it would hide exactly the defect the filter exists to prevent. A
subscriber gets `server.connected` first, then whatever is pushed; the stream ends
when the test says so (`close_streams`) or after `stream_idle_s`, so a forgotten
reader cannot hold a test open for ever. Every frame it pushes is shaped like the
live one: the name in the body's `"type"`, and no `event:` line (D11) -- and writing an
assistant message brings the `session.idle` that closes the turn, because a fake that
never sent it would hide the collector's own end condition.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Final

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from app.config import Config

__all__ = [
    "ANSWER", "ESCALATION_HINT", "FakeOpencode", "FakeTurn", "HANG_WORD",
    "PERMISSION_WORD", "REFUSED_TOOL_ERROR", "SENTINEL", "app", "application_id_of",
    "completed_tool_message", "failed_tool_message", "refused_tool_message",
]

#: What the model says to an ordinary question. Distinctive enough that a
#: paraphrase in the code could not pass for it.
ANSWER: Final = "Квантовая точка — это наночастица."
#: The text the model offers BEFORE the sentinel, i.e. `parse_voice_reply`'s hint.
ESCALATION_HINT: Final = "Собираю сводку, это надолго."
#: Read from the shipped config rather than hard-coded: a fixture holding a
#: literal would keep passing after the field it stands for is renamed, and would
#: then escalate nothing in production.
SENTINEL: Final = Config().r2d2_needs_agent_sentinel
#: Words the STANDALONE server reacts to, because a `curl` in a shell script must
#: be able to type them without an encoding argument.
HANG_WORD: Final = "hang"
ESCALATE_WORD: Final = "digest"
PERMISSION_WORD: Final = "permission"

VERSION: Final = "1.18.32"
#: The one role whose message ends a turn, and therefore the one that brings the idle
#: frame with it (C7: `info.role` is where the role is read from).
ASSISTANT_ROLE: Final = "assistant"
#: The catalogue `core.opencode.models` validates the configuration against (C1):
#: the ids listed here are the ids the deployment may send.
MODEL_ID: Final = "space-bunny-free"
PROVIDER_ID: Final = "opencode"
AGENTS: Final = [
    {"name": "r2d2-voice", "mode": "subagent", "description": "answers out loud"},
    {"name": "r2d2-agent", "mode": "primary", "description": "does the work"},
]
#: A turn takes this long by default. Not realism for its own sake: the measured
#: p50 is 1.667 s, and a sub-millisecond in-process turn would make `llm_ms` a
#: rounding accident, so the record `tests/test_e2e_stack.py` asserts on would
#: prove nothing.
TURN_DELAY_S: Final = 0.01
#: How long a subscriber waits for the next frame before the stream ends.
STREAM_IDLE_S: Final = 5.0
#: The prefix of the only session titles R2D2 creates; see `core.opencode.session_store`.
TITLE_PREFIX: Final = "r2d2:alice:"


# ---------------------------------------------------------------------------
# D9: the three stored tool-call messages a shared session really holds
# ---------------------------------------------------------------------------
#
# Captured BYTE FOR BYTE off a throwaway `opencode serve` v1.18.32 on
# 127.0.0.1:4610 (throwaway `OPENCODE_CONFIG_DIR`, isolated `XDG_*`, workspace
# /tmp, three fresh sessions; see `qa/live-run-v4.md`).  The one-session-per-human
# design means all three land in the SAME transcript, and they are the whole of
# the discrimination D9 needs:
#
# * `refused_tool_message` -- a permission refusal, `state.status == "error"`, no
#   `text` part, and `state.error` printing the REFUSED agent's rule matrix.  The
#   error string is byte-identical to the one contract U8 recorded on 4614, which
#   is the check that the reproduction is the same defect and not a lookalike.
# * `completed_tool_message` -- a tool call that WORKED, `state.status ==
#   "completed"`, and equally no `text` part.  Through the text channel these two
#   are the same message, which is why "empty" cannot mean "refused".
# * `failed_tool_message` -- a tool call that FAILED for an ordinary reason (the
#   host does not resolve), also `state.status == "error"`, and NOT opencode's
#   enforcement state.  This is the counterexample that makes the status alone
#   insufficient, and it is why the second half of the refusal test exists.
#
# `step-finish`'s cost/token counters and `info.time` are elided: they are
# billing noise and they carry none of the fact under test.  Everything else --
# every part, every key, the whole `state` object -- is as the server sent it.

#: `state.error` of the captured refusal. The sentence is opencode's own and the
#: rule list behind it is `r2d2-voice`'s effective matrix, `bash: deny` included.
REFUSED_TOOL_ERROR: Final = (
    'The user has specified a rule which prevents you from using this specific tool call. Here are so'
    'me of the relevant rules [{"permission":"*","action":"allow","pattern":"*"},{"permission":"*","a'
    'ction":"deny","pattern":"*"},{"permission":"*","action":"deny","pattern":"*"},{"permission":"bas'
    'h","pattern":"*","action":"deny"},{"permission":"bash","pattern":"/home/koluchiy/.r2d2/r2d2_do.p'
    'y *","action":"allow"},{"permission":"bash","pattern":"python3 /home/koluchiy/.r2d2/r2d2_do.py *'
    '","action":"allow"},{"permission":"bash","pattern":"/home/koluchiy/Documents/R2_yandex_station/.'
    'venv/bin/python /home/koluchiy/.r2d2/r2d2_do.py *","action":"allow"},{"permission":"bash","patte'
    'rn":"upower *","action":"allow"},{"permission":"bash","pattern":"cat /sys/class/power_supply/*",'
    '"action":"allow"},{"permission":"bash","pattern":"df *","action":"allow"},{"permission":"bash","'
    'pattern":"free *","action":"allow"},{"permission":"bash","pattern":"uname *","action":"allow"},{'
    '"permission":"bash","pattern":"hostname *","action":"allow"},{"permission":"bash","pattern":"ps '
    '*","action":"allow"},{"permission":"bash","pattern":"uptime","action":"allow"},{"permission":"ba'
    'sh","pattern":"date","action":"allow"},{"permission":"bash","pattern":"/home/koluchiy/.r2d2/r2d2'
    '_do.py shell *","action":"deny"},{"permission":"bash","pattern":"python3 /home/koluchiy/.r2d2/r2'
    'd2_do.py shell *","action":"deny"},{"permission":"bash","pattern":"/home/koluchiy/Documents/R2_y'
    'andex_station/.venv/bin/python /home/koluchiy/.r2d2/r2d2_do.py shell *","action":"deny"}]'
)


def _tool_message(
    message_id: str,
    part_id: str,
    tool: str,
    state: dict[str, Any],
    *,
    agent: str,
    reasoning: str = "",
) -> dict[str, Any]:
    """The captured envelope shape every one of the three messages shares."""
    parts: list[dict[str, Any]] = [{"id": f"{part_id}_s", "type": "step-start"}]
    if reasoning:
        parts.append({"id": f"{part_id}_r", "type": "reasoning", "text": reasoning})
    parts.append({"id": f"{part_id}_t", "type": "tool", "tool": tool, "state": state})
    parts.append({"id": f"{part_id}_f", "type": "step-finish", "reason": "tool-calls"})
    return {"info": {"id": message_id, "role": "assistant", "agent": agent}, "parts": parts}


def refused_tool_message(
    message_id: str = "msg_refused", *, agent: str = "r2d2-voice"
) -> dict[str, Any]:
    """The stored refusal: `bash`, `status: "error"`, the matrix printed, no text."""
    return _tool_message(
        message_id,
        f"prt_{message_id}",
        "bash",
        {
            "status": "error",
            "input": {"command": 'curl -s --max-time 10 https://example.com; echo "EXIT=$?"'},
            "error": REFUSED_TOOL_ERROR,
            "time": {"start": 1790410337180, "end": 1790410337483},
        },
        agent=agent,
        reasoning="The user explicitly asks to run a curl command in the terminal. Let me run it.",
    )


def completed_tool_message(
    message_id: str = "msg_completed", *, agent: str = "r2d2-agent"
) -> dict[str, Any]:
    """The stored SUCCESS: `webfetch`, `status: "completed"`, and no text part either."""
    return _tool_message(
        message_id,
        f"prt_{message_id}",
        "webfetch",
        {
            "status": "completed",
            "input": {"url": "https://example.com", "format": "markdown"},
            "output": (
                "Example Domain\n\n# Example Domain\n\nThis domain is for use in documentation "
                "examples without needing permission. Avoid use in operations.\n\n[Learn more]"
                "(https://iana.org/domains/example)"
            ),
            "title": "https://example.com (text/html)",
            "metadata": {"truncated": False},
            "time": {"start": 1790410295948, "end": 1790410296321},
        },
        agent=agent,
    )


def failed_tool_message(
    message_id: str = "msg_failed", *, agent: str = "r2d2-agent"
) -> dict[str, Any]:
    """A tool that failed for an ordinary reason -- and is also `status: "error"`."""
    return _tool_message(
        message_id,
        f"prt_{message_id}",
        "webfetch",
        {
            "status": "error",
            "input": {"url": "https://no-such-host.invalid/page", "format": "text", "timeout": 30},
            "error": "Transport error (GET https://no-such-host.invalid/page)",
            "time": {"start": 1790410301266, "end": 1790410301429},
        },
        agent=agent,
    )


@dataclass(frozen=True, slots=True)
class FakeTurn:
    """One message the server was asked to run, as it arrived on the wire."""

    session_id: str
    agent: str
    model: str
    text: str
    submitted: bool
    directory: str

    def document(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "agent": self.agent,
            "model": self.model,
            "text": self.text,
            "submitted": self.submitted,
            "directory": self.directory,
        }


@dataclass
class FakeOpencode:
    """The scriptable state of one fake server: mutate the attributes, ask questions.

    `app()` builds the ASGI application for THIS object, so two of these in one
    session are two independent servers. The defaults describe a healthy opencode
    that lists one usable model.
    """

    reply: str = ANSWER
    #: What a question about a digest gets back: the hint, then the sentinel, so
    #: `core.routing.parse_voice_reply` routes the turn to the agent.
    escalation: str = f"{ESCALATION_HINT} {SENTINEL}"
    #: A turn is accepted and never answered -- what a cold cache looks like from
    #: the client, and the only way to reach the deadline branch.
    hang: bool = False
    #: Every route answers this instead of its own. `503` is the in-process
    #: stand-in for "the server is not running"; the real refusal is proved by the
    #: loopback QA, which stops the process.
    error_status: int = 200
    #: `info.error` inside a 200 -- the C1 refusal shape the spike measured.
    turn_error: dict[str, Any] | None = None
    delay_s: float = TURN_DELAY_S
    version: str = VERSION
    stream_idle_s: float = STREAM_IDLE_S
    #: How long `GET /global/health` takes. A server that accepts the connection
    #: and never answers is the case the health route's own bound exists for, and
    #: no transport double can express it.
    health_hang_s: float = 0.0
    #: Sessions `/session/status` reports as `busy`, which is the only state the
    #: reaper may abort.
    busy_sessions: set[str] = field(default_factory=set)
    #: What an asynchronously submitted task answers with, once the session is
    #: polled again. The real server keeps working after `prompt_async` returns
    #: 204, so a fake that answered nothing would make every collector time out.
    task_reply: str = "Сводка готова."

    sessions: dict[str, dict[str, str]] = field(default_factory=dict)
    transcript: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    turns: list[FakeTurn] = field(default_factory=list)
    #: Every request as `"METHOD /path"`, recorded by the middleware BEFORE the
    #: route runs -- so "the abort path was never hit" means the server never saw
    #: an abort, and "no opencode traffic at all" means this list is empty.
    routes: list[str] = field(default_factory=list)
    permission_asks: list[tuple[str, str, str]] = field(default_factory=list)
    permission_answers: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)
    aborted: list[str] = field(default_factory=list)
    summarized: list[str] = field(default_factory=list)
    #: `(session_id, message_id)` of every accepted `DELETE .../message/...`, in
    #: order. Empty is the assertion that matters: a turn that escalated and never
    #: asked the server to remove the routing signal is the D6 defect.
    deleted: list[tuple[str, str]] = field(default_factory=list)

    _streams: list[asyncio.Queue] = field(default_factory=list, init=False, repr=False)
    _arrived: asyncio.Condition = field(
        default_factory=asyncio.Condition, init=False, repr=False
    )
    _sessions_made: int = field(default=0, init=False, repr=False)
    _polls: dict[str, int] = field(default_factory=dict, init=False, repr=False)
    _pending: dict[str, list[str]] = field(default_factory=dict, init=False, repr=False)

    # -- what a test reads and writes ----------------------------------------

    def say(self, text: str) -> "FakeOpencode":
        """What the model says from now on. Chainable: a test reads better."""
        self.reply = text
        return self

    def answers(self) -> list[str]:
        """The bodies posted to the permission route, in order."""
        return [str(body.get("response", "")) for _s, _p, body in self.permission_answers]

    def route_count(self, method: str, path: str) -> int:
        """How many times the server was asked exactly this. `GET /session` and
        `POST /session` are different requests, and the distinction is the whole
        of "one session per user"."""
        return len([route for route in self.routes if route == f"{method} {path}"])

    def ask_permission(self, session_id: str, *, title: str, permission_id: str = "perm_1") -> None:
        """Publish a `permission.asked` frame for `session_id` on every stream."""
        self._push(
            "permission.asked",
            {
                "sessionID": session_id,
                "id": permission_id,
                "metadata": {"command": title},
                "always": [f"{title} *"],
            },
        )
        self.permission_asks.append((session_id, permission_id, title))

    async def wait_for_stream(self, timeout_s: float = 5.0) -> None:
        """Block until a reader is attached to `GET /event`.

        Without this a frame is published into the void: the stream is
        request-scoped, so anything pushed before the reader connects is never
        delivered. Waiting on the attachment is what makes the SSE path
        deterministic without sleeping for a guessed interval.
        """
        async with self._arrived:
            await asyncio.wait_for(
                self._arrived.wait_for(lambda: bool(self._streams)), timeout_s
            )

    def close_streams(self) -> None:
        """End every open stream now, so the frames it already holds are delivered.

        Under `httpx.ASGITransport` a response body is collected before the client
        sees any of it, so a stream that stays open holds the frame it already
        holds. Closing is how a test says "that is all".
        """
        for queue in list(self._streams):
            queue.put_nowait(None)

    # -- the ASGI application ------------------------------------------------

    def app(self) -> FastAPI:
        """A FastAPI app serving this object's state, mounted and closed over."""
        server = self
        api = FastAPI(title="fake opencode", docs_url=None, redoc_url=None)

        @api.middleware("http")
        async def switch_off_when_down(request: Request, call_next: Any) -> Any:
            """Record the request, then answer `error_status` instead of running the route.

            One middleware rather than a branch in twelve handlers: a switch every
            route inherits cannot be half-applied. The `/_fake/*` routes stay
            readable, so a test can still read what the server recorded after the
            switch was thrown.
            """
            server.routes.append(f"{request.method} {request.url.path}")
            if server.error_status != 200 and not request.url.path.startswith("/_fake"):
                return _json(
                    {"name": "ServiceUnavailableError", "data": {"message": "opencode is down"}},
                    server.error_status,
                )
            return await call_next(request)

        @api.get("/global/health")
        async def health() -> Any:
            if server.health_hang_s:
                await asyncio.sleep(server.health_hang_s)
            return _json({"healthy": True, "version": server.version})

        @api.get("/config/providers")
        async def providers() -> Any:
            return _json(
                {
                    "providers": [
                        {
                            "id": PROVIDER_ID,
                            "name": "opencode",
                            "models": {MODEL_ID: {"name": "Space Bunny Free"}},
                        }
                    ],
                    "default": {"providerID": PROVIDER_ID, "modelID": MODEL_ID},
                }
            )

        @api.get("/agent")
        async def agents() -> Any:
            return _json([dict(agent) for agent in AGENTS])

        @api.post("/session")
        async def create_session(request: Request) -> Any:
            server._sessions_made += 1
            session_id = f"ses_{server._sessions_made}"
            server.sessions[session_id] = {
                "id": session_id,
                "title": str((await _body(request)).get("title", "")),
                "directory": request.query_params.get("directory", ""),
            }
            return _json(server.sessions[session_id])

        @api.get("/session")
        async def list_sessions() -> Any:
            return _json([dict(session) for session in server.sessions.values()])

        @api.post("/session/{session_id}/message")
        async def message(session_id: str, request: Request) -> Any:
            turn = await server._turn(session_id, request, submitted=False)
            answer = server._answer_for(turn.text)
            server._seed(session_id, turn.text, "user")
            if server.hang or HANG_WORD in turn.text.lower():
                # The turn is ACCEPTED and stays open: the user's message is in the
                # session, the answer is still being worked on, and it will land as
                # an assistant message later. That is what makes a deadline a
                # hand-off rather than a loss -- the collector is what waits for it.
                server._deliver_later(session_id, answer)
                return _forever()
            if server.turn_error is not None:
                return _json({"info": {"id": "msg_e", "error": server.turn_error}, "parts": []})
            if PERMISSION_WORD in turn.text.lower():
                server.ask_permission(session_id, title="echo привет")
            if server.delay_s:
                await asyncio.sleep(server.delay_s)
            server._seed(session_id, answer, "assistant")
            return _json(
                {
                    "info": {"id": _next_message_id(), "role": "assistant", "agent": turn.agent},
                    "parts": [{"type": "text", "text": answer}],
                }
            )

        @api.post("/session/{session_id}/prompt_async")
        async def prompt_async(session_id: str, request: Request) -> Any:
            turn = await server._turn(session_id, request, submitted=True)
            server._seed(session_id, turn.text, "user")
            server._deliver_later(session_id, server.task_reply)
            return Response(status_code=204)

        @api.get("/session/{session_id}/message")
        async def list_messages(session_id: str) -> Any:
            return _json(server._polled(session_id))

        @api.delete("/session/{session_id}/message/{message_id}")
        async def delete_message(session_id: str, message_id: str) -> Any:
            kept = [
                envelope
                for envelope in server.transcript.get(session_id, [])
                if envelope["info"].get("id") != message_id
            ]
            removed = len(kept) != len(server.transcript.get(session_id, []))
            if removed:
                server.transcript[session_id] = kept
            server.deleted.append((session_id, message_id))
            return _json(removed)

        @api.post("/session/{session_id}/abort")
        async def abort(session_id: str) -> Any:
            server.aborted.append(session_id)
            return _json(True)

        @api.post("/session/{session_id}/summarize")
        async def summarize(session_id: str) -> Any:
            server.summarized.append(session_id)
            return _json(True)

        @api.post("/session/{session_id}/permissions/{permission_id}")
        async def answer_permission(session_id: str, permission_id: str, request: Request) -> Any:
            server.permission_answers.append((session_id, permission_id, await _body(request)))
            return _json(True)

        @api.get("/session/status")
        async def status() -> Any:
            return _json(
                {
                    session_id: ("busy" if session_id in server.busy_sessions else "idle")
                    for session_id in server.sessions
                }
            )

        @api.get("/event")
        async def events() -> Any:
            return await server._stream()

        @api.get("/_fake/turns")
        async def recorded_turns() -> Any:
            return _json([turn.document() for turn in server.turns])

        @api.get("/_fake/answers")
        async def recorded_answers() -> Any:
            return _json(
                [
                    {"session_id": s, "permission_id": p, "response": b}
                    for s, p, b in server.permission_answers
                ]
            )

        @api.post("/_fake/ask")
        async def push_permission(request: Request) -> Any:
            body = await _body(request)
            server.ask_permission(
                str(body.get("session_id", "")), title=str(body.get("title", "echo"))
            )
            return _json({"pushed": True})

        @api.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
        async def unknown(path: str) -> Any:
            return _json({"name": "NotFoundError", "data": {"message": f"/{path}"}}, 404)

        return api

    # -- internals ----------------------------------------------------------

    async def _stream(self) -> StreamingResponse:
        """`GET /event`: the connected frame, then everything pushed, then the end."""
        queue: asyncio.Queue = asyncio.Queue()
        async with self._arrived:
            self._streams.append(queue)
            self._arrived.notify_all()

        async def frames() -> AsyncIterator[bytes]:
            yield _frame("server.connected", {})
            while True:
                try:
                    pushed = await asyncio.wait_for(queue.get(), self.stream_idle_s)
                except TimeoutError:
                    return
                if pushed is None:
                    return
                kind, properties = pushed
                yield _frame(kind, properties)

        return StreamingResponse(frames(), media_type="text/event-stream")

    def _push(self, kind: str, properties: dict[str, Any]) -> None:
        """Publish one frame to every attached stream, as the global stream does."""
        for queue in list(self._streams):
            queue.put_nowait((kind, properties))

    async def _turn(self, session_id: str, request: Request, *, submitted: bool) -> FakeTurn:
        body = await _body(request)
        model = body.get("model") if isinstance(body.get("model"), dict) else {}
        turn = FakeTurn(
            session_id=session_id,
            agent=str(body.get("agent", "")),
            model=str(model.get("modelID", "")),
            text=str((body.get("parts") or [{}])[0].get("text", "")),
            submitted=submitted,
            directory=request.query_params.get("directory", ""),
        )
        self.turns.append(turn)
        return turn

    def _deliver_later(self, session_id: str, answer: str) -> None:
        """Hold this answer back until the session is polled a second time.

        It is the shape of the real server: work accepted on `prompt_async` (or on
        a turn that is still running) lands in the session as an assistant message
        afterwards, and the collector is what waits for it. Delivering on the FIRST
        read instead would be a different server -- one that answers before the ask
        -- and it would make the marker
        `SessionCollector.hand_to_agent` takes equal to the answer it is looking for.

        A QUEUE, not one slot: a session that has accepted two turns owes two
        answers, and holding only the last one would lose a turn the server is
        still working on -- which is what happens when a voice turn outruns the
        deadline and the request is handed to the agent as well.
        """
        self._pending.setdefault(session_id, []).append(answer)

    def _answer_for(self, text: str) -> str:
        """What the model says about THIS question; `reply` unless it asks otherwise."""
        return self.escalation if ESCALATE_WORD in text.lower() else self.reply

    def _polled(self, session_id: str) -> list[dict[str, Any]]:
        """The transcript as of this read, releasing the held answers from the second one."""
        polls = self._polls[session_id] = self._polls.get(session_id, 0) + 1
        held = self._pending.get(session_id)
        if held and polls >= 2:
            del self._pending[session_id]
            for answer in held:
                self._seed(session_id, answer, "assistant")
        return self.transcript.get(session_id, [])

    def _seed(self, session_id: str, text: str, role: str) -> None:
        self.transcript.setdefault(session_id, []).append(
            {"info": {"id": _next_message_id(), "role": role}, "parts": [{"type": "text", "text": text}]}
        )
        if role == ASSISTANT_ROLE:
            # The live server closes a turn with `session.idle` (C5), and it carries
            # `properties.sessionID`. A fake that never sends it would leave every collector
            # that waits for the turn to end waiting for ever -- which is exactly what the
            # real server stops it from doing. Only an ASSISTANT message ends a turn: the
            # user's own message is the beginning of one.
            self._push("session.idle", {"sessionID": session_id})


def application_id_of(title: str) -> str:
    """`r2d2:alice:<id>` -> `<id>`, the only title shape R2D2 creates."""
    return title[len(TITLE_PREFIX) :] if title.startswith(TITLE_PREFIX) else title


async def _body(request: Request) -> dict[str, Any]:
    try:
        document = await request.json()
    except ValueError:
        return {}
    return document if isinstance(document, dict) else {}


def _json(document: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(document, status_code=status_code)


def _frame(kind: str, properties: dict[str, Any]) -> bytes:
    """One SSE frame, in the shape the live server actually sends: the event name is a
    top-level `"type"` of the JSON body and there is NO `event:` line.

    A 30-minute tap of `GET /event` on 1.18.32 recorded 1090 `data:` lines and 0
    `event:` lines (`qa/d11-wire-tap.py` for the shorter confirmation), so a fake that
    emitted an `event:` line would exercise a shape the server never sends -- which is
    exactly how D11 stayed invisible behind a green suite.
    """
    body = json.dumps({"id": f"evt_{_next_event_id()}", "type": kind, "properties": properties})
    return f"data: {body}\n\n".encode()


def _forever() -> StreamingResponse:
    """A 200 whose body never ends -- the cold cache the voice deadline exists for."""

    async def never() -> AsyncIterator[bytes]:
        while True:
            await asyncio.sleep(3600)
            yield b""  # pragma: no cover -- the sleep above never returns

    return StreamingResponse(never(), media_type="application/json")


def _next_message_id() -> str:
    global _sequence
    _sequence += 1
    return f"msg_{_sequence}"


_events = 0


def _next_event_id() -> str:
    global _events
    _events += 1
    return f"{_events}"


_sequence = 0

#: The standalone server: `uvicorn tests.fake_opencode:app --port 4599`. Nothing
#: below is module-level behaviour a test can trip over -- the attributes of THIS
#: instance are the script, and a test that wants another script builds its own
#: `FakeOpencode` and its own app.
app: Final = FakeOpencode().app()
