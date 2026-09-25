"""The opencode HTTP client (plan todo 6) -- which route, with which body, under
which timeout.

Every endpoint was measured against a live `opencode serve` v1.18.32 in todo 1;
`docs/11-opencode-contract.md` is the evidence and the ADOPTED lines in it are
the contract this file implements. Three of its findings are honoured here
because each one already broke something, and the vocabulary that implements
them lives in `core.opencode.wire`:

* **C1** -- a 200 is not a reply: `info.error` carries the failure (403 free-tier,
  402 no funds) inside an HTTP 200, so `reply_of` raises instead of returning an
  empty answer. What this file adds is the model split that feeds the request.
* **C6** -- the directory is the query parameter `?directory=<abs path>`; the same
  key in the body is accepted with a 200 and silently ignored, and the server does
  not validate the path at all, so `_scoped_params` refuses locally.
The deadline belongs to the caller and is enforced here, but the turn is NOT
aborted when it expires: a cold first message in a fresh session measured
15.5-18.6 s, far outside any voice budget, and a background collector fetches that
result later, so aborting would destroy work already paid for. A refused model is
the opposite case and must be loud: an unknown `modelID` is not an error at all
(opencode substitutes another model and answers 200), so ids are validated
against `GET /config/providers` at startup in plan todo 9.

The configured password never leaves this module: scrubbed from every error
excerpt, absent from every log line, absent from `repr`, and the `Authorization`
header is never logged.

The value types and the error hierarchy live in `core.opencode.wire` and are
re-exported here, so `core.opencode.client` stays the single import that todos 8,
9 and 14 need.

allow: SIZE_OK -- 307 pure LOC. The vocabulary half is already split out into
`core.opencode/wire.py`; what is left is the 15 routes of the opencode wire
contract in `docs/11-opencode-contract.md` (each 3-8 lines) plus 5 HTTP
internals. The route set is fixed by that contract and by the API plan todos 8,
9 and 14 call, and every route needs the same `_request`/`_scoped_params`, so
there is no second class to split them into without duplicating those internals.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Mapping
from types import MappingProxyType
from typing import Final, Literal
from urllib.parse import quote

import httpx

from core.backends.config_loader import BackendSpec
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
    excerpt,
    records,
    reply_of,
    required_text,
    split_model,
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


class OpencodeClient:
    """Sessions, turns and permissions on one `opencode serve` instance.

    The `httpx.AsyncClient` is injectable so tests can drive every route through
    `MockTransport`; without one, this client builds its own on first use (so
    construction opens no socket) and `aclose()` closes exactly what it built.
    """

    def __init__(
        self, spec: BackendSpec, directory: str, *, client: httpx.AsyncClient | None = None
    ) -> None:
        self._name = spec.name
        self._base_url = spec.base_url.rstrip("/")
        self._directory = directory
        self._timeout = spec.timeout
        self._password = spec.password
        self._auth: httpx.Auth | None = (
            httpx.BasicAuth(spec.username, spec.password)
            if spec.username and spec.password
            else None
        )
        self._client = client
        self._owns_client = client is None
        log.info(
            "opencode client: base_url=%s workspace=%s auth=%s",
            self._base_url, directory, "basic" if self._auth is not None else "none",
        )

    def __repr__(self) -> str:
        return f"OpencodeClient(base_url={self._base_url!r}, directory={self._directory!r})"

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

    async def aclose(self) -> None:
        """Close the client only if this object created it.

        An injected client stays open AND stays attached: dropping the reference
        would make the next call build a real one and hit the network.
        """
        client = self._client
        if client is not None and self._owns_client:
            self._client = None
            await client.aclose()

    # -- internals ---------------------------------------------------------

    def _turn_body(self, text: str, *, agent: str, model: str) -> dict[str, object]:
        """The body both message routes send, per U2's ADOPTED line (C1, C6)."""
        provider, model_id = split_model(model)
        body: dict[str, object] = {
            "model": {"providerID": provider, "modelID": model_id},
            "parts": [{"type": "text", "text": text}],
        }
        if agent:
            # An unknown agent is a 500 whose body explains nothing (the real
            # reason is only in the server log under a `ref`), so an empty name
            # is dropped rather than sent.
            body["agent"] = agent
        return body

    def _scoped_params(self) -> Mapping[str, str]:
        """The `?directory=` every session-scoped route needs -- checked locally (C6).

        The server accepts a directory that does not exist and answers 200, so a
        typo would hand the agent's file tools a root that is not there. The check
        lives here, inseparable from the parameter it guards.
        """
        if not os.path.isdir(self._directory):
            raise OpencodeError(
                f"opencode {self._name}: workspace {self._directory!r} does not exist and the server "
                "does not validate ?directory=, so no session-scoped request was sent"
            )
        return {"directory": self._directory}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        timeout: float | None = None,
        params: Mapping[str, str] | None = None,
        json_body: object = None,
    ) -> httpx.Response:
        """One request: absolute URL, basic auth, explicit timeout, non-2xx raised."""
        response = await self._http().request(
            method,
            f"{self._base_url}{path}",
            params=params,
            json=json_body,
            auth=self._auth,
            timeout=self._timeout if timeout is None else timeout,
        )
        if not response.is_success:
            raise OpencodeStatusError(
                f"opencode {self._name}: {method} {path} -> HTTP {response.status_code} ({excerpt(response, self._password)})",
                status_code=response.status_code,
            )
        return response

    def _http(self) -> httpx.AsyncClient:
        """The shared client, built on first use so construction opens no socket."""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client
