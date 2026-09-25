"""Outbound-socket blocker for CLI subprocesses (plan todo 11).

`tests/test_r2d2_do_cli.py` runs `r2d2_do` as a real subprocess, so the
in-process pytest plugins cannot police what those children do. This module is
placed on the *subprocess* `PYTHONPATH` instead: CPython imports `sitecustomize`
during startup, before any test code runs, so every child is offline from its
first instruction. Only the two test cases that genuinely need a network opt
out of it.

Why block `getaddrinfo` too, not just `connect`: an HTTP client that never
resolves a name cannot open a socket to one either, and a DNS lookup is the
easiest egress to leave unguarded.
"""

import socket

_BLOCKED = "outbound network access is disabled for the r2d2_do CLI tests"


def _refuse(*_args: object, **_kwargs: object) -> None:
    raise OSError(_BLOCKED)


def _refuse_resolve(*_args: object, **_kwargs: object) -> None:
    # `gaierror` is an OSError, so an HTTP client wraps this as a connection
    # error and the tool under test takes its documented failure path instead
    # of a traceback.
    raise socket.gaierror(_BLOCKED)


socket.socket.connect = _refuse  # type: ignore[method-assign]
socket.socket.connect_ex = _refuse  # type: ignore[method-assign]
socket.create_connection = _refuse  # type: ignore[assignment]
socket.getaddrinfo = _refuse_resolve  # type: ignore[assignment]
