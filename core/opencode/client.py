"""The opencode HTTP client (plan todo 6) -- which route, with which body, under
which timeout.

Every endpoint was measured against a live `opencode serve` v1.18.32 in todo 1;
`docs/11-opencode-contract.md` is the evidence and the ADOPTED lines in it are the
contract this file implements. This module is the CATALOGUE half of it: the routes
the deployment sends, each with the body it sends and the reading of the answer.
The other half -- the base URL, the credentials, the one `httpx.AsyncClient`, the
`?directory=` scope (C6), the shared turn body, the non-2xx rule and the
credential scrub -- is `core.opencode.transport.OpencodeTransport`, and this class
is that class plus the routes. Keeping the routes HERE is not a free choice:
`tests/test_docs.py` reads this file's source and takes the authoritative list of
routes from every `_request("VERB", "path"...)` in it, in both directions against
the endpoint table of `docs/11-opencode-backend.md`.

**C1** is honoured here because it already broke something: a 200 is not a reply,
`info.error` carries the failure (403 free-tier, 402 no funds) inside an HTTP 200,
so `reply_of` raises instead of returning an empty answer, and the model split
that feeds the request is validated against `GET /config/providers` at startup in
plan todo 9.

The deadline belongs to the caller and is enforced here, but the turn is NOT
aborted when it expires: a cold first message in a fresh session measured
15.5-18.6 s, far outside any voice budget, and a background collector fetches that
result later, so aborting would destroy work already paid for. A refused model is
the opposite case and must be loud.

The value types and the error hierarchy live in `core.opencode.wire` and are
re-exported here, so `core.opencode.client` stays the single import that todos 8,
9 and 14 need.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from types import MappingProxyType
from typing import Final, Literal
from urllib.parse import quote

import httpx

# The base class, not a compatibility re-export: this file has no connection of
# its own, and every route below reaches the socket, the credentials and the
# `?directory=` scope through it.
from core.opencode.transport import OpencodeTransport
from core.opencode.wire import (
    MessageRecord,
    OpencodeDeadlineExceeded,
    OpencodeError,
    OpencodeErrorEnvelope,
    OpencodeHealth,
    OpencodeProtocolError,
    OpencodeReply,
    OpencodeStatusError,
    SessionInfo,
    decode,
    records,
    reply_of,
    required_text,
    text_of_parts,
)

#: `core.opencode.wire` is the definition site for everything but the client;
#: re-exporting it here keeps todos 8, 9 and 14 on a single import.
__all__ = [
    "MessageRecord", "OpencodeClient", "OpencodeDeadlineExceeded", "OpencodeError",
    "OpencodeErrorEnvelope", "OpencodeHealth", "OpencodeProtocolError", "OpencodeReply",
    "OpencodeStatusError", "SessionInfo",
]

log = logging.getLogger(__name__)

#: A liveness probe must not eat the voice budget, so it gets its own bound.
HEALTH_TIMEOUT_S: Final = 1.5
#: The request timeout for a deadline-bounded call is the deadline plus this, so
#: `asyncio.wait_for` is always what fires first and the caller always sees
#: `OpencodeDeadlineExceeded` rather than a bare `httpx.TimeoutException`.
DEADLINE_GRACE_S: Final = 0.5


class OpencodeClient(OpencodeTransport):
    """Sessions, turns and permissions on one `opencode serve` instance.

    One method per route of the wire contract in `docs/11-opencode-contract.md`,
    each saying which verb and path it sends, what body, and how the answer is
    read. The connection every one of them shares is `OpencodeTransport`'s.
    """

    async def health(self) -> OpencodeHealth:
        """`GET /global/health` -> `{healthy, version}`. Never raises.

        The one route the server declares no query parameters for, which is why
        a missing workspace cannot make the server look unreachable here.
        """
        try:
            response = await self._request("GET", "/global/health", timeout=HEALTH_TIMEOUT_S)
            document = decode(response, dict)
        except (httpx.HTTPError, OpencodeError) as exc:
            log.warning("opencode: %s is not usable: %s", self._base_url, exc)
            return OpencodeHealth(reachable=False)
        version = document.get("version")
        return OpencodeHealth(
            reachable=document.get("healthy") is not False,
            version=version if isinstance(version, str) else None,
        )

    async def find_session(self, title: str) -> str | None:
        """`GET /session` -> the id of the session with exactly this title."""
        for session in await self.list_sessions():
            if session.title == title:
                return session.id
        return None

    async def create_session(self, title: str) -> str:
        """`POST /session {"title": ...}` -> the new session id."""
        response = await self._request(
            "POST", "/session", params=self._scoped_params(), json_body={"title": title}
        )
        return required_text(decode(response, dict), "id", "a created session")

    async def resolve_session(self, title: str) -> str:
        """The id of the session with this title, created once if absent.

        The ONLY place a session is created: find-then-create, so a restart
        re-attaches to the session the user already has.
        """
        found = await self.find_session(title)
        return found if found is not None else await self.create_session(title)

    async def send_message(
        self, session_id: str, text: str, *, agent: str, model: str, deadline_s: float
    ) -> OpencodeReply:
        """`POST /session/:id/message`, blocking, bounded by `deadline_s` (C1).

        Raises `OpencodeDeadlineExceeded` on expiry without aborting, or
        `OpencodeErrorEnvelope` when a 200 carries `info.error`.
        """
        try:
            response = await asyncio.wait_for(
                self._request(
                    "POST",
                    f"/session/{session_id}/message",
                    params=self._scoped_params(),
                    json_body=self._turn_body(text, agent=agent, model=model),
                    timeout=deadline_s + DEADLINE_GRACE_S,
                ),
                deadline_s,
            )
        except TimeoutError as exc:
            raise OpencodeDeadlineExceeded(
                f"opencode {self._name}: POST /session/{session_id}/message exceeded "
                f"{deadline_s}s; the turn is still running server-side and was not aborted"
            ) from exc
        return reply_of(decode(response, dict))

    async def send_message_async(
        self, session_id: str, text: str, *, agent: str, model: str
    ) -> None:
        """`POST /session/:id/prompt_async` -- submitted, not awaited (agent path)."""
        response = await self._request(
            "POST",
            f"/session/{session_id}/prompt_async",
            params=self._scoped_params(),
            json_body=self._turn_body(text, agent=agent, model=model),
        )
        if response.status_code != 204:
            raise OpencodeStatusError(
                f"opencode {self._name}: POST /session/{session_id}/prompt_async answered "
                f"HTTP {response.status_code}, expected 204",
                status_code=response.status_code,
            )

    async def abort(self, session_id: str) -> bool:
        """`POST /session/:id/abort` -> whether the server stopped the turn."""
        response = await self._request("POST", f"/session/{session_id}/abort", params=self._scoped_params())
        return decode(response, bool)

    async def delete_message(self, session_id: str, message_id: str) -> bool:
        """`DELETE /session/:id/message/:messageID` -> whether it was removed.

        The route exists for one caller and one reason: the escalation sentinel
        is a control signal, and a control signal stored in the session is read
        back by the next agent as part of its own conversation (see
        `core.routing.transcript_sweep`). Summarising the session away is not a
        substitute -- the server answers `true` and leaves the transcript
        untouched -- so the message is deleted, by id, on the turn that observed
        it. The id is quoted because a message id is server-controlled text.
        """
        removed = await self._request(
            "DELETE",
            f"/session/{session_id}/message/{quote(message_id, safe='')}",
            params=self._scoped_params(),
        )
        return decode(removed, bool)

    async def summarize(self, session_id: str, *, provider: str, model: str) -> bool:
        """`POST /session/:id/summarize {"providerID", "modelID"}` -> the server's bool."""
        response = await self._request(
            "POST",
            f"/session/{session_id}/summarize",
            params=self._scoped_params(),
            json_body={"providerID": provider, "modelID": model},
        )
        return decode(response, bool)

    async def respond_permission(
        self, session_id: str, permission_id: str, response: Literal["once", "reject"]
    ) -> bool:
        """`POST /session/:id/permissions/:permissionID {"response": ...}` -> bool.

        `"always"` is deliberately NOT in the type. The server offers it, and one
        `always` permanently grants the whole command mask the event advertised in
        `properties.always` (e.g. `["echo *"]`), so it has to be unrepresentable at
        this signature rather than merely unused by today's caller. A "once" answer
        is forgotten with the turn, which is the only durable cost of answering.
        """
        answered = await self._request(
            "POST",
            f"/session/{session_id}/permissions/{quote(permission_id, safe='')}",
            params=self._scoped_params(),
            json_body={"response": response},
        )
        return decode(answered, bool)

    async def list_sessions(self) -> list[SessionInfo]:
        """`GET /session` -> the sessions of this workspace (`?directory=` filters)."""
        response = await self._request("GET", "/session", params=self._scoped_params())
        return [
            SessionInfo(
                id=required_text(entry, "id", "a session"),
                title=str(entry.get("title", "")),
                directory=str(entry.get("directory", "")),
            )
            for entry in records(response, "the session list")
        ]

    async def list_messages(self, session_id: str) -> list[MessageRecord]:
        """`GET /session/:id/message` -> `[{info, parts}]` read as records (C7)."""
        response = await self._request("GET", f"/session/{session_id}/message", params=self._scoped_params())
        out: list[MessageRecord] = []
        for entry in records(response, "the message list"):
            info = entry.get("info")
            parts = entry.get("parts")
            if not isinstance(info, Mapping) or not isinstance(parts, list):
                raise OpencodeProtocolError(
                    f"opencode {self._name}: a message envelope must hold an 'info' object and a "
                    f"'parts' list, got {type(info).__name__} and {type(parts).__name__}"
                )
            out.append(
                MessageRecord(
                    id=required_text(info, "id", "a message"),
                    role=str(info.get("role", "")),
                    text=text_of_parts(parts),
                )
            )
        return out

    async def session_status(self) -> Mapping[str, str]:
        """`GET /session/status` -> `{sessionID: "idle" | "busy" | ...}` for the reaper.

        The server sends one status OBJECT per session (`{"type": "idle"}`); this
        projects its `type` -- the only field the reaper branches on -- and raises on
        a shape it cannot read instead of stringifying a guess.
        """
        response = await self._request("GET", "/session/status", params=self._scoped_params())
        return MappingProxyType(
            {
                session_id: required_text(status, "type", f"the status of {session_id!r}")
                for session_id, status in decode(response, dict).items()
            }
        )

    async def providers(self) -> Mapping[str, object]:
        """`GET /config/providers` -> `{providers: [...], default: {...}}`."""
        response = await self._request("GET", "/config/providers", params=self._scoped_params())
        return decode(response, dict)

    async def agents(self) -> list[Mapping[str, object]]:
        """`GET /agent` -> the agent definitions, for validating names at startup."""
        response = await self._request("GET", "/agent", params=self._scoped_params())
        return records(response, "the agent list")
