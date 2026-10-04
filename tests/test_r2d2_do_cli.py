"""Behavioural tests for the `r2d2_do` CLI shim (plan todo 11).

This CLI is the **only** sanctioned way the opencode agent touches this
machine. Everything it can do, it does by calling an existing
`core/tools/*` handler, and the one that matters is `shell` -- its arguments
come from a language model that may have misheard a voice sentence, from a web
page, or from text pasted into Telegram. So the load-bearing assertions here are
not "does `echo hi` work" but:

* **the risk gate cannot be routed around.** `rm -rf` must exit 2 with a
  `pending` block and the victim directory still on disk, whether the command
  arrives clean, arrives with an injected `; rm -rf`, or arrives with
  `R2D2_APPROVED_TOKEN` set in the environment but no `--approved` flag.
* **`--approved` is a capability, not a flag.** It is refused unless the
  operator put `R2D2_APPROVED_TOKEN` in the process environment, so the agent
  cannot grant itself approval; a bare `--approved` with no token exits non-zero
  having run nothing at all. The same test proves the flag is not a no-op by
  running the *dangerous* command through it in a temp directory and watching
  the victim disappear.
* **no argument is ever interpolated into a command line.** `open-app` resolves
  a name through `laptop_tool.APPS` to a fixed command or refuses; it never
  splices the caller's string into a shell.
* **output is honest.** A failure is `ok: false` with an `error`, never
  `ok: true` with an empty `text`; an exception becomes one JSON line, never a
  traceback on stdout.
* **stdout is a machine contract.** Exactly one line of JSON, always, whatever
  the exit code -- including usage errors.

allow: SIZE_OK -- 542 pure LOC, 36 tests. Every test file in this repo is
257-991 pure LOC and a test module grows with the number of behaviours it pins;
splitting the risk gate from the invocation contract would give each half a file
that cannot say what the other half assumes about the harness.

The CLI is exercised **only** as a subprocess: it is a script, its stdout is a
contract for an LLM to parse, and an in-process call would test pytest's import
machinery instead of the file the agent runs. Every test gets a temp SQLite
(`DB_PATH`) and runs with `cwd` outside the repo, so no test can read the
operator's `.env`, write the real database, or depend on the working directory.

Two hermeticity mechanisms, both real rather than asserted:

* `tests/_hermetic/sitecustomize.py` is on every subprocess `PYTHONPATH` and
  blocks outbound sockets, so a test that accidentally reaches the network fails
  instead of passing slowly. `test_hermetic_socket_blocker_is_real` proves the
  blocker is not a no-op.
* the arXiv test is the single case that legitimately needs the network. It
  skips when `R2D2_CLI_TEST_OFFLINE` is set (the hermeticity harness sets it)
  or when arxiv.org is not reachable, and it points `BACKENDS_PATH` at a fixture
  whose single backend points at a closed port, so the summarisation step fails
  fast and locally instead of spending a real LLM call.
"""

from __future__ import annotations

import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CLI = REPO / "opencode" / "r2d2_cli" / "r2d2_do.py"
VENV_PY = REPO / ".venv" / "bin" / "python"
HERMETIC = REPO / "tests" / "_hermetic"
LAUNCH_LOG_ENV = "R2D2_FAKE_LAUNCH_LOG"

#: The stdout contract, as one object. Any extra or missing key is a break.
CONTRACT_KEYS = {"ok", "text", "needs_confirm", "pending", "error"}
EXIT_OK, EXIT_FAIL, EXIT_CONFIRM = 0, 1, 2

#: Environment the tests own. Everything the CLI or `Config.load()` can read is
#: cleared first: a test must never inherit the operator's credentials from the
#: ambient environment.
_STRIPPED_PREFIXES = ("R2D2_", "TELEGRAM_", "OPENROUTER_", "YANDEX_", "SHELL_", "BACKENDS_PATH", "DB_PATH")
_STRIPPED_EXACT = ("PYTHONPATH", "R2D2_DO_REEXEC", "R2D2_FAKE_LAUNCH_LOG")

SENTINEL_TOKEN = "1234567890:AAFakeTokenValueForRedactionChecks"
SENTINEL_APPROVED = "approved-token-9f8e7d6c5b4a"


# --------------------------------------------------------------------------- #
# harness
# --------------------------------------------------------------------------- #
def _env(tmp_path: Path, **overrides: str) -> dict[str, str]:
    env = {
        name: value
        for name, value in os.environ.items()
        if name not in _STRIPPED_EXACT
        and not any(name.startswith(prefix) for prefix in _STRIPPED_PREFIXES)
    }
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    env["PYTHONPATH"] = os.pathsep.join((str(HERMETIC), str(REPO)))
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    env["DB_PATH"] = str(tmp_path / "sessions.db")
    env["R2D2_APPLICATION_ID"] = "r2d2-qa"
    env[LAUNCH_LOG_ENV] = str(tmp_path / "launched.log")
    env.update(overrides)
    return env


def _fake_launcher(tmp_path: Path) -> Path:
    """A fake `xdg-open` so `open-app` cannot open a real browser window.

    It records the argv it was handed, which is what proves the allowlisted
    command is a fixed string and not the caller's name spliced into a shell.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    shim = bindir / "xdg-open"
    shim.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$' + LAUNCH_LOG_ENV + '"\n', encoding="utf-8")
    shim.chmod(0o755)
    return tmp_path / "launched.log"


def _run(
    tmp_path: Path,
    *args: str,
    env_overrides: dict[str, str] | None = None,
    hermetic: bool = True,
    timeout: float = 90.0,
) -> subprocess.CompletedProcess[str]:
    env = _env(tmp_path, **(env_overrides or {}))
    if not hermetic:
        # The one test that talks to arxiv.org opts out of the socket blocker.
        env["PYTHONPATH"] = str(REPO)
    return subprocess.run(
        [str(VENV_PY), str(CLI), *args],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _contract(stdout: str) -> dict:
    """Parse stdout, insisting on the whole contract: one line, five keys, typed."""
    lines = stdout.strip().splitlines()
    assert len(lines) == 1, f"stdout must be exactly one line, got {len(lines)}: {stdout!r}"
    payload = json.loads(lines[0])
    assert set(payload) == CONTRACT_KEYS, f"contract keys drifted: {sorted(payload)}"
    assert isinstance(payload["ok"], bool)
    assert isinstance(payload["text"], str)
    assert isinstance(payload["needs_confirm"], bool)
    assert payload["pending"] is None or isinstance(payload["pending"], dict)
    assert payload["error"] is None or isinstance(payload["error"], str)
    return payload


def _wait_for_lines(path: Path, count: int, timeout: float = 5.0) -> list[str]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line]
            if len(lines) >= count:
                return lines
        time.sleep(0.05)
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line] if path.exists() else []


def _recorded_sends(tmp_path: Path) -> list[dict]:
    log = tmp_path / "tg.log"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line]


def _stub_tg(**overrides: str) -> dict[str, str]:
    return {"R2D2_TEST_NO_TG": "1", "R2D2_TEST_TG_LOG": "tg.log", **overrides}


def _offline_backends(tmp_path: Path) -> str:
    """A backend registry whose only backend points at a closed local port.

    `arxiv_tool.summarize_entries` walks the chain and swallows transport
    errors, so this keeps the arXiv test deterministic and offline on the
    summarisation step instead of making a paid LLM call.
    """
    path = tmp_path / "backends.json"
    path.write_text(
        json.dumps(
            {
                "chain": ["offline"],
                "backends": [
                    {
                        "name": "offline",
                        "kind": "openai_compatible",
                        "base_url": "http://127.0.0.1:9/v1",
                        "api_key": "fixture-key-not-a-credential",
                        "model": "offline-model",
                        "auth_style": "bearer",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return str(path)


@pytest.fixture(scope="session")
def bare_python(tmp_path_factory: pytest.TempPathFactory) -> str:
    """A real interpreter WITHOUT the project's dependencies.

    The agent may invoke the installed copy as `python3 <path>`, and the system
    python is not the venv. This stands in for exactly that situation -- no
    mocking of `sys.executable` required.
    """
    root = tmp_path_factory.mktemp("bare-interpreter")
    made = subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(root / "bare")],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if made.returncode != 0:
        pytest.skip(f"cannot build a dependency-free interpreter: {made.stderr.strip()}")
    return str(root / "bare" / "bin" / "python")


# --------------------------------------------------------------------------- #
# the contract itself
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("args", "env_overrides"),
    [
        (("status",), None),
        (("open-app", "browser"), None),
        (("tg", "привет"), _stub_tg()),
        (("shell", "echo hi"), None),
        (("shell", "rm -rf /tmp/r2d2-qa-contract"), None),
        (("не-существует",), None),
    ],
)
def test_stdout_is_one_line_of_contract_json(
    tmp_path: Path, args: tuple[str, ...], env_overrides: dict[str, str] | None
) -> None:
    # Given any subcommand, including a usage error
    proc = _run(tmp_path, *args, env_overrides=env_overrides)
    # When it runs
    # Then stdout is exactly one line of JSON with exactly the contract keys
    _contract(proc.stdout)


def test_status_reports_ok_with_non_empty_text(tmp_path: Path) -> None:
    proc = _run(tmp_path, "status")
    assert proc.returncode == EXIT_OK, proc.stderr
    payload = _contract(proc.stdout)
    assert payload["ok"] is True
    assert payload["text"].strip()
    assert payload["error"] is None
    assert payload["needs_confirm"] is False


def test_open_app_browser_launches_only_the_allowlisted_command(tmp_path: Path) -> None:
    log = _fake_launcher(tmp_path)
    proc = _run(tmp_path, "open-app", "browser")
    assert proc.returncode == EXIT_OK, proc.stderr
    payload = _contract(proc.stdout)
    assert payload["ok"] is True
    assert payload["text"].strip()
    # the launch went to the fake, and it is the fixed APPS entry -- not the
    # caller's string
    assert _wait_for_lines(log, 1) == ["https://ya.ru"]


def test_tg_records_exactly_one_send_and_prints_non_empty_text(tmp_path: Path) -> None:
    proc = _run(tmp_path, "tg", "привет", env_overrides=_stub_tg())
    assert proc.returncode == EXIT_OK, proc.stderr
    payload = _contract(proc.stdout)
    assert payload["ok"] is True
    assert payload["text"].strip()
    sends = _recorded_sends(tmp_path)
    assert len(sends) == 1, sends
    assert sends[0]["text"] == "привет"


def test_tg_without_credentials_fails_instead_of_claiming_success(tmp_path: Path) -> None:
    # An honest failure: no token, no chat id -> `send_message` returns False,
    # and the CLI must say so instead of reporting a delivery that never happened.
    proc = _run(tmp_path, "tg", "привет", env_overrides={"R2D2_TEST_NO_TG": "", "TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""})
    assert proc.returncode == EXIT_FAIL, proc.stdout
    payload = _contract(proc.stdout)
    assert payload["ok"] is False
    assert payload["error"]
    assert _recorded_sends(tmp_path) == []


# --------------------------------------------------------------------------- #
# the risk gate -- the reason this file exists
# --------------------------------------------------------------------------- #
def _victim(tmp_path: Path) -> Path:
    victim = tmp_path / "victim"
    victim.mkdir(exist_ok=True)
    (victim / "keep.txt").write_text("intact\n", encoding="utf-8")
    return victim


def test_dangerous_shell_command_asks_confirmation_and_victim_survives(tmp_path: Path) -> None:
    victim = _victim(tmp_path)
    proc = _run(tmp_path, "shell", f"rm -rf {victim}")
    assert proc.returncode == EXIT_CONFIRM, (proc.returncode, proc.stdout, proc.stderr)
    payload = _contract(proc.stdout)
    assert payload["needs_confirm"] is True
    assert payload["pending"], "the agent needs the pending block to relay and retry"
    assert str(victim) in json.dumps(payload["pending"], ensure_ascii=False)
    assert "Подтверди" in payload["text"]
    assert victim.is_dir(), "the dangerous command must not have run"
    assert (victim / "keep.txt").read_text(encoding="utf-8") == "intact\n"


def test_injected_second_command_still_hits_the_risk_gate(tmp_path: Path) -> None:
    # `echo hi; rm -rf <victim>` -- the LLM-supplied string contains a second
    # command. The gate classifies the WHOLE string, so the tail is caught.
    victim = _victim(tmp_path)
    proc = _run(tmp_path, "shell", f"echo hi; rm -rf {victim}")
    assert proc.returncode == EXIT_CONFIRM, (proc.returncode, proc.stdout)
    payload = _contract(proc.stdout)
    assert payload["needs_confirm"] is True
    assert victim.is_dir()


def test_approved_token_in_the_environment_alone_does_not_bypass_the_gate(tmp_path: Path) -> None:
    victim = _victim(tmp_path)
    proc = _run(
        tmp_path,
        "shell",
        f"rm -rf {victim}",
        env_overrides={"R2D2_APPROVED_TOKEN": SENTINEL_APPROVED},
    )
    assert proc.returncode == EXIT_CONFIRM, (proc.returncode, proc.stdout)
    assert _contract(proc.stdout)["needs_confirm"] is True
    assert victim.is_dir()


def test_shell_echo_succeeds(tmp_path: Path) -> None:
    proc = _run(tmp_path, "shell", "echo hi")
    assert proc.returncode == EXIT_OK, proc.stderr
    payload = _contract(proc.stdout)
    assert payload["ok"] is True
    assert "hi" in payload["text"]
    assert payload["needs_confirm"] is False


def test_approved_flag_without_the_operator_token_runs_nothing(tmp_path: Path) -> None:
    marker = tmp_path / "approved-ran"
    proc = _run(tmp_path, "shell", "--approved", f"touch {marker}")
    assert proc.returncode != EXIT_OK, (proc.returncode, proc.stdout)
    payload = _contract(proc.stdout)
    assert payload["ok"] is False
    assert "R2D2_APPROVED_TOKEN" in payload["error"]
    assert not marker.exists(), "--approved without the token must not execute anything"


def test_approved_flag_with_the_token_runs_the_confirmed_command(tmp_path: Path) -> None:
    # The mirror of the refusal above, so the guard cannot pass by refusing
    # everything: with the operator's token present, the confirmed retry runs
    # the dangerous command.
    victim = _victim(tmp_path)
    proc = _run(
        tmp_path,
        "shell",
        "--approved",
        f"rm -rf {victim}",
        env_overrides={"R2D2_APPROVED_TOKEN": SENTINEL_APPROVED},
    )
    assert proc.returncode == EXIT_OK, (proc.returncode, proc.stdout, proc.stderr)
    assert _contract(proc.stdout)["ok"] is True
    assert not victim.exists(), "an approved command must actually run"


def test_approved_flag_help_says_it_is_for_a_confirmed_retry(tmp_path: Path) -> None:
    proc = _run(tmp_path, "shell", "--help")
    assert proc.returncode == EXIT_OK, proc.stderr
    assert "R2D2_APPROVED_TOKEN" in proc.stdout
    assert "confirm" in proc.stdout.lower() or "подтверж" in proc.stdout.lower()


# --------------------------------------------------------------------------- #
# untrusted arguments
# --------------------------------------------------------------------------- #
def test_open_app_refuses_a_path_shaped_name_and_launches_nothing(tmp_path: Path) -> None:
    log = _fake_launcher(tmp_path)
    proc = _run(tmp_path, "open-app", "../../etc/passwd")
    assert proc.returncode == EXIT_FAIL, (proc.returncode, proc.stdout)
    payload = _contract(proc.stdout)
    assert payload["ok"] is False
    assert payload["text"].strip()
    assert not log.exists(), "an unknown app name must not launch anything"


def test_open_app_never_interpolates_the_name_into_the_command(tmp_path: Path) -> None:
    log = _fake_launcher(tmp_path)
    marker = tmp_path / "pwned"
    # This name used to fuzzy-match `browser`, so the launch happened and only the
    # recorded argv stood between the caller and a shell. `resolve_app` is an exact
    # lookup now, so the name matches nothing and there is nothing to interpolate.
    proc = _run(tmp_path, "open-app", f"browser; touch {marker}")
    assert proc.returncode == EXIT_FAIL, (proc.returncode, proc.stdout, proc.stderr)
    assert _contract(proc.stdout)["ok"] is False
    assert not log.exists(), "an injection-shaped name must launch nothing at all"
    assert not marker.exists(), "the name must never reach a shell"


def test_tg_treats_markup_as_plain_text(tmp_path: Path) -> None:
    payload_text = "<script>alert(1)</script>"
    proc = _run(tmp_path, "tg", payload_text, env_overrides=_stub_tg())
    assert proc.returncode == EXIT_OK, proc.stderr
    sends = _recorded_sends(tmp_path)
    assert [send["text"] for send in sends] == [payload_text]


# --------------------------------------------------------------------------- #
# honest failure
# --------------------------------------------------------------------------- #
def test_unknown_subcommand_exits_nonzero_without_a_traceback_on_stdout(tmp_path: Path) -> None:
    proc = _run(tmp_path, "rm-slash-rf")
    assert proc.returncode == EXIT_FAIL, proc.returncode
    assert "Traceback" not in proc.stdout
    payload = _contract(proc.stdout)
    assert payload["ok"] is False
    assert payload["error"]


def test_handler_exception_becomes_one_error_line_not_a_traceback(tmp_path: Path) -> None:
    # `R2D2_TEST_TG_RAISE` makes the stubbed send raise an exception whose text
    # embeds the bot token -- a stand-in for any handler that dies with a secret
    # (an httpx URL, a stack frame) in its message.
    proc = _run(
        tmp_path,
        "tg",
        "привет",
        env_overrides=_stub_tg(
            R2D2_TEST_TG_RAISE="1",
            TELEGRAM_BOT_TOKEN=SENTINEL_TOKEN,
            TELEGRAM_CHAT_ID="1",
        ),
    )
    assert proc.returncode == EXIT_FAIL, (proc.returncode, proc.stdout)
    assert "Traceback" not in proc.stdout
    payload = _contract(proc.stdout)
    assert payload["ok"] is False
    assert payload["error"], "an exception must be reported as an error string"
    assert SENTINEL_TOKEN not in json.dumps(payload, ensure_ascii=False)
    assert "***" in payload["error"]


def test_shell_timeout_from_config_bounds_a_slow_command(tmp_path: Path) -> None:
    started = time.monotonic()
    proc = _run(
        tmp_path,
        "shell",
        "sleep 60",
        env_overrides={"SHELL_TIMEOUT": "1"},
        timeout=45.0,
    )
    elapsed = time.monotonic() - started
    assert proc.returncode == EXIT_FAIL, (proc.returncode, proc.stdout)
    payload = _contract(proc.stdout)
    assert payload["ok"] is False
    assert "timeout" in payload["error"].lower()
    assert elapsed < 30.0, f"cfg.shell_timeout was not passed through (took {elapsed:.1f}s)"


def test_long_shell_output_goes_to_telegram_instead_of_being_dropped(tmp_path: Path) -> None:
    # `shell_tool` returns a short ack plus `tg_send` for long output. The JSON
    # contract has no `tg_send` key, so the payload must be delivered rather than
    # silently discarded.
    proc = _run(tmp_path, "shell", "seq 1 1000", env_overrides=_stub_tg())
    assert proc.returncode == EXIT_OK, proc.stderr
    payload = _contract(proc.stdout)
    assert payload["ok"] is True
    assert payload["text"].strip()
    sends = _recorded_sends(tmp_path)
    assert len(sends) == 1, sends
    assert "seq 1 1000" in sends[0]["text"]


# --------------------------------------------------------------------------- #
# interruptions
# --------------------------------------------------------------------------- #
def _signal_mid_flight(
    tmp_path: Path, command: str, sig: signal.Signals, *, delay: float = 1.5
) -> tuple[int, str, float]:
    env = _env(tmp_path)
    started = time.monotonic()
    proc = subprocess.Popen(
        [str(VENV_PY), str(CLI), "shell", command],
        env=env,
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    time.sleep(delay)  # let the child shell start
    proc.send_signal(sig)
    out, _err = proc.communicate(timeout=60)
    return proc.returncode, out, time.monotonic() - started


def test_sigint_during_shell_exits_nonzero_with_a_json_error_line(tmp_path: Path) -> None:
    # `asyncio.run` installs its own SIGINT handler that cancels the main task
    # and swallows the first interrupt, which would let the run continue to
    # `cfg.shell_timeout` (10s by default) and report a misleading "timeout".
    # So the error must name the interruption AND the call must end long before
    # the timeout -- both halves are load-bearing.
    returncode, out, elapsed = _signal_mid_flight(tmp_path, "sleep 30", signal.SIGINT)
    assert returncode != EXIT_OK, returncode
    payload = _contract(out)
    assert payload["ok"] is False
    assert "прервано" in payload["error"], payload["error"]
    assert elapsed < 8.0, f"SIGINT was not honoured promptly (took {elapsed:.1f}s)"


def test_sigterm_during_shell_exits_nonzero_with_a_json_error_line(tmp_path: Path) -> None:
    returncode, out, elapsed = _signal_mid_flight(tmp_path, "sleep 30", signal.SIGTERM)
    assert returncode != EXIT_OK, returncode
    payload = _contract(out)
    assert payload["ok"] is False
    assert "прервано" in payload["error"], payload["error"]
    assert elapsed < 8.0, f"SIGTERM was not honoured promptly (took {elapsed:.1f}s)"


# --------------------------------------------------------------------------- #
# secrets
# --------------------------------------------------------------------------- #
def test_bot_token_never_appears_in_output(tmp_path: Path) -> None:
    # The seam is OFF, so `send_message` really builds
    # https://api.telegram.org/bot<TOKEN>/sendMessage and fails offline. The
    # token must not survive into stdout or stderr on any path.
    proc = _run(
        tmp_path,
        "tg",
        "привет",
        env_overrides={"TELEGRAM_BOT_TOKEN": SENTINEL_TOKEN, "TELEGRAM_CHAT_ID": "1"},
    )
    combined = proc.stdout + proc.stderr
    assert SENTINEL_TOKEN not in combined, combined
    assert proc.returncode == EXIT_FAIL, proc.stdout


def _ambient_bot_token() -> str:
    """The operator's real token, if this process has one.

    `app.config` merges no file, so only a token the environment really holds
    counts as "ambient" -- which is exactly the configuration where a leak
    would hurt.
    """
    import app.config  # noqa: F401 -- the import is the point: it must not populate os.environ

    return os.environ.get("TELEGRAM_BOT_TOKEN", "")


def test_ambient_bot_token_never_appears_in_output(tmp_path: Path) -> None:
    ambient = _ambient_bot_token()
    if len(ambient) < 8:
        pytest.skip("no ambient TELEGRAM_BOT_TOKEN to leak")
    proc = _run(tmp_path, "tg", "привет", env_overrides=_stub_tg())
    assert ambient not in proc.stdout + proc.stderr


def test_approved_token_never_appears_in_output(tmp_path: Path) -> None:
    proc = _run(
        tmp_path,
        "shell",
        "--approved",
        "echo confirmed",
        env_overrides={"R2D2_APPROVED_TOKEN": SENTINEL_APPROVED},
    )
    assert proc.returncode == EXIT_OK, (proc.returncode, proc.stdout, proc.stderr)
    assert SENTINEL_APPROVED not in proc.stdout + proc.stderr
    assert "confirmed" in _contract(proc.stdout)["text"]


# --------------------------------------------------------------------------- #
# state and invocation
# --------------------------------------------------------------------------- #
def test_repeated_runs_share_one_sqlite_db_without_corruption(tmp_path: Path) -> None:
    for _ in range(3):
        proc = _run(tmp_path, "status")
        assert proc.returncode == EXIT_OK, proc.stderr
    db = tmp_path / "sessions.db"
    assert db.exists(), "the CLI must create its own database when absent"
    connection = sqlite3.connect(db)
    try:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {"sessions", "pending_actions", "jobs", "oc_sessions"} <= tables, tables
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        connection.close()


def test_shebang_and_venv_constant_name_the_project_interpreter() -> None:
    source = CLI.read_text(encoding="utf-8")
    constant = re.search(r'^VENV_PY = "(.+)"$', source, re.M)
    assert constant, "the shim must name the interpreter it re-execs into"
    venv_py = constant.group(1)
    # Then: the shebang and the constant are the SAME interpreter. A shebang
    # pointing somewhere else means the two invocations the allowlist grants --
    # the bare path and `python3 <path>` -- run different pythons, and only the
    # one the constant names can import `core`.
    assert source.splitlines()[0] == f"#!{venv_py}", "the shebang must be the venv interpreter"
    # Then: that interpreter is a PROJECT venv, not a system or stale copy.
    # Only the tail is pinned: which checkout and which home hold it is the
    # installing machine's business, so no absolute path is written here.
    parts = Path(venv_py).parts
    assert parts[-3:] == (".venv", "bin", "python"), venv_py
    # and it is the venv THIS checkout runs its tests from, by name
    assert VENV_PY.parts[-3:] == parts[-3:], (VENV_PY, venv_py)
    assert os.access(CLI, os.X_OK), "the bare-path invocation form needs the executable bit"


def test_module_constants_are_the_documented_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    # Loading the script (never running main) pins the exit-code table and the
    # derived repo root. `R2D2_DO_REEXEC` is pinned so that importing it under a
    # non-venv interpreter cannot `execv` and replace the pytest process.
    import importlib.util

    monkeypatch.setenv("R2D2_DO_REEXEC", "1")
    spec = importlib.util.spec_from_file_location("r2d2_do_under_test", CLI)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert (module.EXIT_OK, module.EXIT_FAIL, module.EXIT_CONFIRM) == (0, 1, 2)
    assert module.DEFAULT_APPLICATION_ID == "r2d2-cli"
    # Then: REPO_ROOT is DERIVED from the venv, two levels up. That derivation
    # is the whole mechanism -- `sys.path` is seeded from it and every
    # `core.tools` import hangs off it -- so a shim that hard-coded a different
    # root, or a venv installed outside the checkout, would import someone
    # else's `core`. Compared structurally, not against this machine's path.
    assert Path(module.REPO_ROOT) == Path(module.VENV_PY).parent.parent.parent
    assert Path(module.VENV_PY).name == "python"


def test_re_execs_into_the_project_venv_from_a_bare_interpreter(
    tmp_path: Path, bare_python: str
) -> None:
    # Exactly the invocation the opencode config allowlists: `python3 <path>`
    # with an interpreter that has none of the project's dependencies.
    proc = subprocess.run(
        [bare_python, str(CLI), "status"],
        env=_env(tmp_path),
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert proc.returncode == EXIT_OK, (proc.stdout, proc.stderr)
    assert _contract(proc.stdout)["ok"] is True


def test_bare_interpreter_without_dependencies_answers_with_the_contract(
    tmp_path: Path, bare_python: str
) -> None:
    # The re-exec is what normally saves that invocation. When it is suppressed,
    # the imports still fail -- and the failure must arrive as the contract, not
    # as a traceback the agent has to guess at.
    proc = subprocess.run(
        [bare_python, str(CLI), "status"],
        env=_env(tmp_path, R2D2_DO_REEXEC="1"),
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert proc.returncode == EXIT_FAIL, (proc.returncode, proc.stdout)
    assert "Traceback" not in proc.stdout
    payload = _contract(proc.stdout)
    assert payload["ok"] is False
    assert payload["error"]


def test_hermetic_socket_blocker_is_real(tmp_path: Path) -> None:
    # Without this, "the tests are offline" would be a claim rather than a fact.
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import socket; socket.create_connection(('1.1.1.1', 443), 1)",
        ],
        env=_env(tmp_path),
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode != 0, "the hermetic PYTHONPATH must refuse outbound sockets"
    assert "Traceback" in proc.stderr


# --------------------------------------------------------------------------- #
# arXiv -- the one case that needs the network
# --------------------------------------------------------------------------- #
def _arxiv_reachable() -> bool:
    import socket

    try:
        with socket.create_connection(("export.arxiv.org", 443), timeout=5.0):
            return True
    except OSError:
        return False


def test_arxiv_prints_a_digest_or_an_explicit_nothing_found(tmp_path: Path) -> None:
    if os.environ.get("R2D2_CLI_TEST_OFFLINE") == "1" or not _arxiv_reachable():
        pytest.skip("arxiv needs the network; hermetic or offline run")
    proc = _run(
        tmp_path,
        "arxiv",
        "llm",
        "--max",
        "1",
        "--days",
        "3650",
        env_overrides={"BACKENDS_PATH": _offline_backends(tmp_path)},
        hermetic=False,
        timeout=120.0,
    )
    assert proc.returncode == EXIT_OK, (proc.returncode, proc.stdout, proc.stderr)
    payload = _contract(proc.stdout)
    assert payload["ok"] is True
    assert payload["text"].strip()
    assert "не нашёл" in payload["text"] or "Ссылки" in payload["text"], payload["text"]
