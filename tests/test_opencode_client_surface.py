"""The opencode client's public surface, pinned across the client/transport split.

`core/opencode/client.py` used to hold the connection and the catalogue of routes
in one class. It is two modules now, and what rots silently is not the behaviour
-- `tests/test_opencode_client.py` covers that against a real `httpx.MockTransport`
for every route, the deadline, the C1 envelope and the credential scrub -- but the
SURFACE around the seam. None of the following fails loudly; all of them are
somebody's three-line diff six weeks from now.

So these are structural checks, deliberately not behavioural ones:

* every name the module exposed before the split still resolves there, so
  `app/main.py`, `app/opencode_route.py`, `app/diagnostics.py`, `core/brain.py`'s
  two split-out siblings and the existing suite need no edit;
* the fourteen ROUTES are still DEFINED in `core/opencode/client.py`, because that
  file's source is what `tests/test_docs.py::wire_routes()` reads to build the
  authoritative route list it checks against the endpoint table in
  `docs/11-opencode-backend.md` in both directions. A route that moved out of that
  file would stop being a documented route and the guard would pass over it blind;
* the CONNECTION is defined once, in `core/opencode/transport.py`, and the client
  INHERITS it rather than holding a collaborator of its own -- two owners of
  `self._client` would be two connection pools and two ideas of which server this
  is talking to;
* a moved name is an ALIAS of the new module's object, not a copy of it, and the
  error hierarchy is still the very one `core.backends.base` hangs off, because
  the brain branches on that single root for every backend;
* the new module imports nothing from `core.opencode.client`, so it cannot close an
  import cycle through the client it is the base of.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Final

import httpx

from core.backends.base import BackendError
from core.backends.config_loader import BackendSpec
from core.opencode import client, transport, wire

#: Every non-private name `core/opencode/client.py` exposed before the split: the
#: declared `__all__` plus the two timeout constants the routes read. A name that
#: moved to the base class is still on this list, because the old path has to keep
#: serving whoever imported it from there.
SURFACE: Final[frozenset[str]] = frozenset({
    "DEADLINE_GRACE_S",
    "HEALTH_TIMEOUT_S",
    "MessageRecord",
    "OpencodeClient",
    "OpencodeDeadlineExceeded",
    "OpencodeError",
    "OpencodeErrorEnvelope",
    "OpencodeHealth",
    "OpencodeProtocolError",
    "OpencodeReply",
    "OpencodeStatusError",
    "SessionInfo",
})

#: The methods `core/opencode/client.py` DEFINES, and therefore the `_request(...)`
#: calls `tests/test_docs.py` reads out of its source as the route list.
ROUTES: Final[frozenset[str]] = frozenset({
    "health", "find_session", "create_session", "resolve_session", "send_message",
    "send_message_async", "abort", "delete_message", "summarize", "respond_permission",
    "list_sessions", "list_messages", "session_status", "providers", "agents",
})

#: The one owner of the socket, the base URL, the credentials and the request line.
CONNECTION: Final[frozenset[str]] = frozenset({
    "__init__", "__repr__", "aclose", "_http", "_request", "_scoped_params", "_turn_body",
})

WORKSPACE: Final = str(Path(__file__).resolve().parent.parent)


def _defined(path: Path) -> set[str]:
    """Every class and function DEFINED in a module, by its own source."""
    return {
        node.name
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _spec(**overrides: object) -> BackendSpec:
    return BackendSpec(
        name="opencode",
        kind="opencode_session",
        base_url="http://127.0.0.1:4599",
        api_key="",
        model="",
        auth_style="bearer",
        username="opencode",
        password="R2D2_OC_PASSWORD_VALUE",
        **overrides,  # type: ignore[arg-type]
    )


def test_every_name_the_old_module_exposed_still_resolves_there():
    # Given: the surface `core/opencode/client.py` exposed before the split
    # When: each name is looked up on the module the old callers import from
    missing = sorted(name for name in SURFACE if not hasattr(client, name))
    # Then: not one of them costs a call site an edit
    assert missing == [], f"core.opencode.client no longer exposes {missing}"


def test_the_declared_surface_is_exactly_what_it_was():
    # Given: the `__all__` the module shipped before the split
    # When / Then: it is unchanged, so a `from ... import *` caller sees the same names
    assert set(client.__all__) == {
        "MessageRecord", "OpencodeClient", "OpencodeDeadlineExceeded", "OpencodeError",
        "OpencodeErrorEnvelope", "OpencodeHealth", "OpencodeProtocolError", "OpencodeReply",
        "OpencodeStatusError", "SessionInfo",
    }
    assert set(client.__all__) <= SURFACE


def test_a_moved_name_is_an_alias_and_not_a_copy():
    # Given: the base class that now owns the connection
    # When / Then: the old path hands out the very same object the subclass inherits,
    # so `isinstance` and `issubclass` hold for anything built through either path
    assert client.OpencodeTransport is transport.OpencodeTransport
    assert issubclass(client.OpencodeClient, transport.OpencodeTransport)


def test_the_error_hierarchy_is_still_the_one_the_brain_branches_on():
    # Given: the two error types other modules import from this path
    # When / Then: they are the objects `core.backends.base` defines, so the ONE root
    # every backend failure hangs off is unchanged by the split
    assert client.OpencodeError is wire.OpencodeError
    assert client.OpencodeDeadlineExceeded is wire.OpencodeDeadlineExceeded
    assert issubclass(client.OpencodeError, BackendError)
    assert issubclass(client.OpencodeErrorEnvelope, client.OpencodeError)
    assert issubclass(client.OpencodeStatusError, client.OpencodeError)


def test_the_routes_are_still_defined_where_the_source_guards_read_them():
    # Given: `tests/test_docs.py`, which extracts every `_request("VERB", "path"...)`
    # from this file's source as the authoritative route list, both directions
    defined = _defined(Path(client.__file__))
    # When / Then: every route is DEFINED here, so those guards keep reading the code
    # they were written to police; a re-export would leave them reading a file that no
    # longer holds the answer seam, which is the shape of a guard that checks nothing
    assert ROUTES <= defined, f"moved out of client.py: {sorted(ROUTES - defined)}"
    assert not (ROUTES & CONNECTION)


def test_the_connection_has_exactly_one_owner():
    # Given: the two modules the client is built from
    defined = _defined(Path(client.__file__)) | _defined(Path(transport.__file__))
    # When / Then: nothing in the connection half is defined twice, so there is one
    # `httpx.AsyncClient`, one base URL and one set of credentials
    assert CONNECTION <= _defined(Path(transport.__file__))
    assert not (CONNECTION & _defined(Path(client.__file__)))


def test_the_client_inherits_its_connection_instead_of_holding_a_second_one():
    # Given: the class the client is defined as
    # When / Then: the constructor, the pool builder and the request line are the base
    # class's OWN functions rather than the subclass's, so one client is one pool. That
    # is the property a collaborator-shaped seam would quietly break, which is why the
    # seam is inheritance and not `self._wire`.
    assert client.OpencodeClient.__init__ is transport.OpencodeTransport.__init__
    assert client.OpencodeClient._http is transport.OpencodeTransport._http
    assert client.OpencodeClient.aclose is transport.OpencodeTransport.aclose
    assert client.OpencodeClient._request is transport.OpencodeTransport._request
    assert client.OpencodeClient._scoped_params is transport.OpencodeTransport._scoped_params
    assert client.OpencodeClient._turn_body is transport.OpencodeTransport._turn_body
    # and the state the connection owns exists in `__dict__` of one object, not two
    subject = client.OpencodeClient(_spec(), WORKSPACE, client=httpx.AsyncClient())
    assert not any(
        name in vars(client.OpencodeClient) for name in ("_client", "_auth", "_base_url")
    )


def test_the_new_module_cannot_close_a_cycle_through_the_client():
    # Given: the module the client is a subclass of
    # When: its import statements are read
    project_imports = {
        node.module
        for node in ast.walk(ast.parse(Path(transport.__file__).read_text(encoding="utf-8")))
        if isinstance(node, ast.ImportFrom) and node.module
        and node.module.split(".")[0] in {"app", "core"}
    }
    # Then: nothing of the client's is reachable from it, so the connection sits BELOW
    # the routes in the graph instead of beside them
    assert project_imports == {"core.backends.config_loader", "core.opencode.wire"}


def test_the_client_is_still_constructed_the_way_every_caller_constructs_it():
    # Given: the `OpencodeClient(spec, directory, client=...)` seam the suite drives
    injected = httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={})))
    # When
    subject = client.OpencodeClient(_spec(), WORKSPACE, client=injected)
    # Then: the injection seam is the base class's, and it is still the ONE pool
    assert subject._client is injected
    assert subject._owns_client is False
    assert repr(subject) == f"OpencodeClient(base_url='http://127.0.0.1:4599', directory={WORKSPACE!r})"
    assert "R2D2_OC_PASSWORD_VALUE" not in repr(subject)
