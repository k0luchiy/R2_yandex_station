"""How one request reaches one `opencode serve`: the address, the identity, the
scope, the status rule.

`core/opencode/client.py` owns the CATALOGUE -- which routes this deployment
sends, with which body, and what each answer means. This module owns the half all
of them share and none of them owns: the base URL, the basic-auth credentials, the
one `httpx.AsyncClient` and the pool that goes with it, the `?directory=` that
scopes a request to a workspace, the body the two message routes share, and the
rule that a non-2xx is an exception. `OpencodeClient` is this class plus the
routes, so every route body still calls `self._request(...)`.

The cut follows the connection rather than the middle of the class, and one test
is what holds that line: `tests/test_docs.py` reads `core/opencode/client.py`'s
SOURCE and extracts every `_request("VERB", "path"...)` out of it as the
authoritative list of routes the client sends, then checks it against the endpoint
table of `docs/11-opencode-backend.md` in BOTH directions. A route that walked out
of that file would stop being a documented route and the guard would pass over it
blind, so the routes are what stays.

Two measured facts live here rather than on a route, because both are properties
of how the server is ADDRESSED (`docs/11-opencode-contract.md`):

* **C6** -- the directory is the query parameter `?directory=<abs path>`; the same
  key in the body is accepted with a 200 and silently ignored, and the server does
  not validate the path at all, so `_scoped_params` refuses locally.
* **The configured password never leaves this module**: scrubbed from every error
  excerpt, absent from every log line, absent from `repr`, and the `Authorization`
  header is never logged.

The value types and the error hierarchy are `core.opencode.wire`'s, and
`core.opencode.client` re-exports them, so `core.opencode.client` remains the
single import every caller needs.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping

import httpx

from core.backends.config_loader import BackendSpec
from core.opencode.wire import OpencodeError, OpencodeStatusError, excerpt, split_model

__all__ = ["OpencodeTransport"]

log = logging.getLogger(__name__)


class OpencodeTransport:
    """One connection to one `opencode serve`, addressed the way it requires.

    The base class of `core.opencode.client.OpencodeClient`, and the only place
    the socket, the base URL and the credentials exist: every route reaches them
    through here, so a client cannot end up with a second pool or a second idea
    of which server it is talking to. The `httpx.AsyncClient` is injectable so
    tests can drive every route through `MockTransport`; without one, this client
    builds its own on first use (so construction opens no socket) and `aclose()`
    closes exactly what it built.
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
