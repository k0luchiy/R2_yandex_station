"""The launcher, the unit and the startup gate -- the three things that stand
between "R2D2 has a brain" and "R2D2 exposed an unauthenticated server to
every process on this box".

The measured facts that shape every assertion here:

* **Auth is HTTP basic via `OPENCODE_SERVER_PASSWORD`; username defaults to
  `opencode`.** With that variable unset the server does not merely warn, it
  serves every route to every local process.  `OPENCODE_SERVER_PASSWORD` is not
  currently set anywhere on this machine, so "the launcher started" and "the
  launcher started *safely*" are two different outcomes and only the second is
  acceptable.  That is why the refusal to start is a test, not a comment.
* **`--port`'s default is ambiguous** -- the docs say 4096, `serve --help` on
  this box says `default: 0` -- so the port is asserted to be passed
  explicitly, in both the default and the overridden case.  A launcher that
  relied on the default would be listening on a random port one release and
  4096 the next.
* **A bare `opencode` on PATH is whatever sorts first**, and stale copies do
  exist on this box (1.18.21 in `~/.npm-global/bin`, 1.18.5 in `/usr/bin`), so a
  1.18.5 server with a different `--help` can end up owning port 4599.  The
  launcher must therefore resolve the binary to an absolute path, never through
  PATH, and never to a system copy.  *Which* absolute path is the installing
  machine's business, so nothing here spells one out: every path assertion in
  this file is either a property of the path (absolute, not a system location,
  consistent with the other two files that must agree) or an equality between
  two committed files.  That is what makes the file survive being installed at
  a different path, and it is also what lets the deployment's own files be
  rewritten to be path-portable without taking these tests down with them.
* **`OPENCODE_CONFIG_DIR` merges with the global config (C2); it does not
  isolate.** A missing directory is therefore not "no agents", it is "the
  owner's global `permission` rules apply" -- so the launcher points at the same
  `.r2d2/opencode` the installer writes and complains when it is absent.
* **R2D2 never manages the server.** No `subprocess`, no signal, no restart
  loop: a systemd user unit owns the process and enabling it is a deliberate
  manual act.  So `app/main.py` may not name those APIs at all.

Nothing here talks to a real server.  The launcher is pointed at a stub binary
that records the argv and environment it was exec'd with and exits, which
proves the environment is passed without a socket ever being opened; the
password-gate tests assert that stub was never reached, so a regression that
starts a server fails the suite instead of binding a port.

allow: SIZE_OK -- 550 pure LOC, 36 tests. Every test file in this repo is 257-991
pure LOC (test_opencode_client.py 991, test_sse.py 699) and a test module grows
with the number of behaviours it pins, not with the number of concepts it owns.
The 250 pure-LOC ceiling targets source modules; splitting this would scatter one
contract -- what the launcher must refuse -- across files that each need the
whole stub-binary harness to say anything.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = REPO_ROOT / "scripts" / "opencode_serve.sh"
UNIT = REPO_ROOT / "scripts" / "r2d2-opencode.service"
EXAMPLE = REPO_ROOT / ".env.oc.example"
INSTALLER = REPO_ROOT / "scripts" / "install_r2d2_opencode_config.sh"
MAIN = REPO_ROOT / "app" / "main.py"

#: Which absolute path the deployment's opencode binary lives at is the installing
#: machine's business, so none is written here. What must not happen is a PATH
#: lookup or a system copy: both are how a stale server ends up owning the port.
STALE_SHAPES = ("/usr/bin/opencode", "/usr/local/bin/opencode", ".npm-global/bin/opencode")
SYSTEM_PREFIXES = ("/usr/bin/", "/usr/local/bin/", "/bin/", "/sbin/", "/opt/")

#: A password nobody has, so a test can prove it travelled without leaking it.
SENTINEL_PASSWORD = "r2d2-test-only-4f2c9a7b1e"

#: The stub stands in for the opencode binary.  It records what it was exec'd
#: with, then exits 0 -- so a launcher that wrongly reaches the exec line still
#: cannot open a port, while a launcher that correctly refuses leaves no record
#: at all.  `>>` appends so several exec attempts in one test stay visible.
STUB = """#!/usr/bin/env bash
{
  printf 'ARGV=%s\\n' "$*"
  printf 'PWD=%s\\n' "$PWD"
  for name in OPENCODE_SERVER_PASSWORD OPENCODE_SERVER_USERNAME \\
              OPENCODE_CONFIG_DIR OPENCODE_LOG_LEVEL; do
    printf '%s=%s\\n' "$name" "${!name-<unset>}"
  done
} >> "$R2D2_TEST_ENV_DUMP"
exit 0
"""

#: A key whose value must never be printed: the launcher has to hand it to the
#: server, and has to be unable to leak it while doing so.
PASSWORD_VAR = "R2D2_OC_PASSWORD"
SECRET_ENV = "OPENCODE_SERVER_PASSWORD"
CONFIG_ENV = "OPENCODE_CONFIG_DIR"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def stub_binary(tmp_path: Path) -> Path:
    """An executable stand-in for the opencode binary that records its env."""
    path = tmp_path / "opencode-stub"
    path.write_text(STUB, encoding="utf-8")
    path.chmod(0o755)
    return path


def dump_path(tmp_path: Path) -> Path:
    return tmp_path / "exec-env.txt"


def run_launcher(
    tmp_path: Path,
    *,
    password: str | None,
    workspace: Path,
    port: str | None = None,
    config_dir: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the launcher with a curated environment -- never the owner's.

    The real process environment carries the owner's live opencode session
    variables, so a test that inherited it would test a different launcher than
    the unit runs.  Only `PATH` is inherited, and only because `env(1)` needs it.
    """
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path),
        "R2D2_OC_BIN": str(stub_binary(tmp_path)),
        "R2D2_OC_WORKSPACE": str(workspace),
        "R2D2_TEST_ENV_DUMP": str(dump_path(tmp_path)),
    }
    if password is not None:
        env[PASSWORD_VAR] = password
    if port is not None:
        env["R2D2_OC_PORT"] = port
    if config_dir is not None:
        env["R2D2_OC_CONFIG_DIR"] = str(config_dir)
    return subprocess.run(
        ["bash", str(LAUNCHER)],
        env=env,
        capture_output=True,
        text=True,
        # A launcher that blocks instead of failing is a defect, not a slow
        # test: the timeout turns a hang into a failure.
        timeout=20,
    )


def recorded(tmp_path: Path) -> dict[str, str]:
    """The stub's record of the environment it was exec'd with."""
    text = dump_path(tmp_path).read_text(encoding="utf-8")
    fields: dict[str, str] = {}
    for line in text.splitlines():
        key, _, value = line.partition("=")
        fields[key] = value
    return fields


def launcher_text() -> str:
    return LAUNCHER.read_text(encoding="utf-8")


def unit_text() -> str:
    return UNIT.read_text(encoding="utf-8")


def env_default(text: str, var: str) -> str | None:
    """The fallback a `"$VAR:-default"` expansion carries, however it is assigned.

    Returned verbatim: `${HOME}/x` stays `${HOME}/x` so a templated rewrite and a
    literal one can be told apart by `_same_directory` without either being
    spelled out here.
    """
    match = re.search(r"\$\{" + re.escape(var) + r":-((?:[^{}]|\$\{[^{}]*\})*)\}", text)
    return match.group(1) if match else None


def unit_directive(name: str) -> str | None:
    match = re.search(rf"^{re.escape(name)}(\S*)=(.*)$", unit_text(), re.M)
    return match.group(2).strip() if match else None


def _home_neutral(value: str) -> str:
    """Fold a home REFERENCE to a token, whichever way one is spelled.

    `$HOME`, `~`, systemd's own `%h` and the running user's real home all become
    `$HOME`, so a deployment that templates its paths and one that spells them out
    compare equal -- and so do they when the owner's home is a different string on
    a different machine. Anything else is left alone: a mismatch there is a real
    disagreement about which directory the server runs in.
    """
    folded = re.sub(r"^(?:\$\{?HOME\}?|~|%h)(?=/|$)", "$HOME", value.rstrip("/"))
    home = str(Path.home())
    return re.sub(rf"^{re.escape(home)}(?=/|$)", "$HOME", folded)


def same_directory(left: str, right: str) -> bool:
    """Whether two spellings name one directory.

    A home REFERENCE (`$HOME`, `~`) is folded to a token, so a deployment that
    spells the owner's home as a template is not a different directory from one
    that spells it out. Two absolute paths are still compared in full: a
    mismatch between them is a real disagreement about where the server runs,
    and it must be visible here rather than in a systemd journal at 3am.
    """
    return _home_neutral(left) == _home_neutral(right)


def binary_default_violations(text: str) -> list[str]:
    """Every way the launcher can end up running a binary PATH chose for it.

    Pure: takes the launcher's text, returns the reasons the binary is not pinned.
    The mutation tests below feed it the three broken spellings, which is the only
    honest way to show this guard is not vacuous.
    """
    default = env_default(text, "R2D2_OC_BIN")
    if default is None:
        return ["launcher: R2D2_OC_BIN has no :-default, so a bare $R2D2_OC_BIN leaves BIN empty"]
    bad: list[str] = []
    if not default.startswith(("/", "~", "$")):
        bad.append(f"launcher: the binary default {default!r} is a bare name resolved through PATH")
    for prefix in SYSTEM_PREFIXES:
        if default.startswith(prefix):
            bad.append(f"launcher: the binary default {default!r} is a system copy")
            break
    for shape in STALE_SHAPES:
        if shape in text:
            bad.append(f"launcher: names the stale copy {shape!r}")
    for line in _code_lines(text):
        if re.search(r"\bopencode\s+serve\b", line) and "$BIN" not in line:
            bad.append(f"launcher: exec line resolves opencode through PATH: {line.strip()!r}")
    return bad


# --------------------------------------------------------------------------
# 1. the launcher parses
# --------------------------------------------------------------------------


def test_launcher_is_valid_bash():
    # Given: the launcher on disk
    # When: bash parses it without running a single command
    result = subprocess.run(["bash", "-n", str(LAUNCHER)], capture_output=True, text=True)
    # Then: it is syntactically sound
    assert result.returncode == 0, result.stderr


def test_launcher_fails_closed_by_default():
    # Given: a launcher whose shell options are read as written
    # When: its `set` line is inspected
    options = launcher_text()
    # Then: an unset variable or a failed command stops it instead of letting
    # it continue with an empty password and an empty workspace
    assert re.search(r"^set -[a-z]*e", options, re.M), "missing errexit"
    assert re.search(r"^set -[a-z]*u", options, re.M), "missing nounset"
    assert re.search(r"^set -[a-z]*.*pipefail", options, re.M), "missing pipefail"


# --------------------------------------------------------------------------
# 2. explicit hostname and port
# --------------------------------------------------------------------------


def test_launcher_pins_the_hostname_and_an_explicit_port(tmp_path):
    # Given: a launcher with no R2D2_OC_PORT in the environment
    workspace = tmp_path / "ws"
    workspace.mkdir()
    # When: it runs
    result = run_launcher(tmp_path, password=SENTINEL_PASSWORD, workspace=workspace)
    # Then: the exec'd process was told to listen on loopback with a port
    # spelled out -- never on whatever the release's default happens to be
    assert result.returncode == 0, result.stderr
    argv = recorded(tmp_path)["ARGV"].split()
    assert "serve" in argv
    assert argv[argv.index("--hostname") + 1] == "127.0.0.1"
    assert "--port" in argv, f"no explicit --port in {argv}"
    assert argv[argv.index("--port") + 1].isdigit(), f"non-numeric port in {argv}"
    # and a host of 0.0.0.0 would expose the whole LAN
    assert "0.0.0.0" not in argv


def test_launcher_honours_a_port_override(tmp_path):
    # Given: an owner who moved the port
    workspace = tmp_path / "ws"
    workspace.mkdir()
    # When: the launcher runs with R2D2_OC_PORT set
    result = run_launcher(
        tmp_path, password=SENTINEL_PASSWORD, workspace=workspace, port="4711"
    )
    # Then: the exec'd process listens there, and the default is gone
    assert result.returncode == 0, result.stderr
    argv = recorded(tmp_path)["ARGV"].split()
    assert argv[argv.index("--port") + 1] == "4711"


def test_launcher_never_depends_on_which_opencode_is_first_on_path():
    # Given: the shipped launcher's own text
    # When / Then: the binary is pinned to an absolute path and named nowhere else
    assert binary_default_violations(launcher_text()) == []


@pytest.mark.parametrize(
    ("label", "replacement", "expected"),
    [
        ("a bare name resolved through PATH", "opencode", "through PATH"),
        ("a system copy", "/usr/bin/opencode", "system copy"),
        ("a stale npm copy", "${R2D2_OC_BIN:-/home/u/.npm-global/bin/opencode}", "stale copy"),
    ],
)
def test_the_binary_guard_bites_on_every_way_path_wins(label, replacement, expected):
    # Given: a launcher whose binary default is one of the three broken spellings,
    # and one whose exec line stops using "$BIN" altogether
    text = launcher_text().replace(
        env_default(launcher_text(), "R2D2_OC_BIN"), replacement
    )
    # When / Then: each is named, so the guard is specific rather than a blanket
    violations = binary_default_violations(text)
    assert any(expected in reason for reason in violations), violations

    through_path = launcher_text().replace('exec "$BIN" serve', "exec opencode serve")
    assert any("through PATH" in reason for reason in binary_default_violations(through_path))


# --------------------------------------------------------------------------
# 3. the secret and the config reach the exec'd environment
# --------------------------------------------------------------------------


def test_launcher_passes_the_password_into_the_server_environment(tmp_path):
    # Given: a launcher and an owner's password
    workspace = tmp_path / "ws"
    workspace.mkdir()
    config = tmp_path / "cfg"
    config.mkdir()
    # When: it runs
    result = run_launcher(
        tmp_path,
        password=SENTINEL_PASSWORD,
        workspace=workspace,
        config_dir=config,
    )
    # Then: the exec'd process finds the credential under the name the server
    # reads, and finds the config dir R2D2's own agents live in
    assert result.returncode == 0, result.stderr
    fields = recorded(tmp_path)
    assert fields[SECRET_ENV] == SENTINEL_PASSWORD
    assert fields[CONFIG_ENV] == str(config)


def test_launcher_defaults_the_config_dir_where_the_installer_writes_it():
    # Given: two files that must not drift -- the installer that COPIES the
    # config and the launcher that POINTS at it
    # When: each one's destination is read
    # Then: they are the same directory, or C2 turns a missing install into
    # the owner's global permission rules applying silently
    assert ".r2d2/opencode" in launcher_text()
    assert ".r2d2/opencode" in INSTALLER.read_text(encoding="utf-8")


def test_launcher_runs_from_the_r2d2_workspace(tmp_path):
    # Given: a workspace that exists
    workspace = tmp_path / "ws"
    workspace.mkdir()
    # When: the launcher runs
    result = run_launcher(tmp_path, password=SENTINEL_PASSWORD, workspace=workspace)
    # Then: the server's cwd is the workspace, because a session's directory is
    # the server's cwd (U5) and R2D2 must never see the owner's real files
    assert result.returncode == 0, result.stderr
    assert Path(recorded(tmp_path)["PWD"]) == workspace


def test_launcher_passes_the_log_level_through(tmp_path):
    # Given: a unit that silences opencode's own logging
    workspace = tmp_path / "ws"
    workspace.mkdir()
    env_file = tmp_path / ".env.oc"
    env_file.write_text("OPENCODE_LOG_LEVEL=warn\n", encoding="utf-8")
    # When: the launcher runs with that variable in its environment
    result = subprocess.run(
        ["bash", str(LAUNCHER)],
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(tmp_path),
            "R2D2_OC_BIN": str(stub_binary(tmp_path)),
            "R2D2_OC_WORKSPACE": str(workspace),
            "R2D2_TEST_ENV_DUMP": str(dump_path(tmp_path)),
            PASSWORD_VAR: SENTINEL_PASSWORD,
            "OPENCODE_LOG_LEVEL": "warn",
        },
        capture_output=True,
        text=True,
        timeout=20,
    )
    # Then: the server inherits it instead of the launcher inventing its own
    assert result.returncode == 0, result.stderr
    assert recorded(tmp_path)["OPENCODE_LOG_LEVEL"] == "warn"


# --------------------------------------------------------------------------
# 4. the password gate -- the behaviour that matters most here
# --------------------------------------------------------------------------


def test_launcher_refuses_to_start_without_a_password(tmp_path):
    # Given: no R2D2_OC_PASSWORD in the environment at all
    workspace = tmp_path / "ws"
    workspace.mkdir()
    # When: the launcher runs
    result = run_launcher(tmp_path, password=None, workspace=workspace)
    # Then: it exits 1 and says which variable is missing, in a form an
    # operator can act on at 3am from a systemd journal line.  The code is
    # pinned, not just "non-zero": a 127 here would mean the script is missing,
    # which is a different failure and must not pass as a safe refusal.
    assert result.returncode == 1, f"started anyway or failed oddly: {result.stdout}"
    assert SECRET_ENV in (result.stderr + result.stdout)
    # and the server was never exec'd: no record exists at all
    assert not dump_path(tmp_path).exists(), "the opencode binary was exec'd without a password"


def test_launcher_refuses_an_empty_password_the_same_way(tmp_path):
    # Given: the variable present but empty -- the shape a half-filled .env.oc
    # or an unset-and-exported shell produces
    workspace = tmp_path / "ws"
    workspace.mkdir()
    # When: the launcher runs
    result = run_launcher(tmp_path, password="", workspace=workspace)
    # Then: it refuses exactly as it does when the variable is absent, because
    # an empty HTTP-basic password is no password
    assert result.returncode == 1, result.stderr
    assert SECRET_ENV in (result.stderr + result.stdout)
    assert not dump_path(tmp_path).exists()


def test_launcher_refuses_a_whitespace_password(tmp_path):
    # Given: a password that is not empty but is not a password
    workspace = tmp_path / "ws"
    workspace.mkdir()
    # When: the launcher runs
    result = run_launcher(tmp_path, password="   ", workspace=workspace)
    # Then: it refuses, because a blank credential is still a blank credential
    assert result.returncode == 1, result.stderr
    assert SECRET_ENV in (result.stderr + result.stdout)
    assert not dump_path(tmp_path).exists()


# --------------------------------------------------------------------------
# secret hygiene
# --------------------------------------------------------------------------


def _code_lines(text: str) -> list[str]:
    """The lines of a shell script that actually run, comments and blanks out.

    Comment density is the point of a launcher: the refusal path has to explain
    itself, and a check that could be satisfied by a comment proves nothing.
    """
    return [
        line
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


#: The four ways a shell leaks a variable's value into its output.  Naming the
#: variable is how the refusal tells the operator what is missing, and that is
#: fine -- expanding it is not.
PRINTERS = ("echo", "printf", "tee", "logger")
#: `$R2D2_OC_PASSWORD` or `${OPENCODE_SERVER_PASSWORD}` in any spelling.
PASSWORD_EXPANSION = re.compile(r"\$\{?(R2D2_OC_PASSWORD|OPENCODE_SERVER_PASSWORD)\b")

#: The phrasings a startup log uses to say "the server is fine".  None of them
#: may appear in the output of an unreachable probe.
SUCCESS_READING = re.compile(r"\bok\b|reachable=true|healthy|status:\s*\"?ok")


def test_launcher_never_traces_or_prints_the_password():
    # Given: the launcher's own text
    text = launcher_text()
    code = _code_lines(text)
    # Then: nothing turns the shell into a password printer
    assert not re.search(r"^set -[a-z]*x", text, re.M), "set -x would log the exec line"
    for dump in ("printenv", "export -p", "declare -p"):
        assert dump not in text, f"{dump} dumps the environment into the journal"
    for line in code:
        first = line.split()[:1]
        if first and first[0] in PRINTERS:
            assert not PASSWORD_EXPANSION.search(line), f"prints the password: {line.strip()!r}"


def test_launcher_does_not_echo_the_password_it_was_given(tmp_path):
    # Given: a launcher that was handed a real secret
    workspace = tmp_path / "ws"
    workspace.mkdir()
    # When: it runs
    result = run_launcher(tmp_path, password=SENTINEL_PASSWORD, workspace=workspace)
    # Then: the secret reached the server ...
    assert result.returncode == 0, result.stderr
    assert recorded(tmp_path)[SECRET_ENV] == SENTINEL_PASSWORD
    # ... and appeared in nothing the launcher itself printed, because systemd
    # captures that output into a world-readable journal
    assert SENTINEL_PASSWORD not in result.stdout
    assert SENTINEL_PASSWORD not in result.stderr


# --------------------------------------------------------------------------
# adversarial: a missing workspace must fail fast, not hang or cd into nothing
# --------------------------------------------------------------------------


def test_launcher_fails_fast_when_the_workspace_is_missing(tmp_path):
    # Given: a workspace path that does not exist
    missing = tmp_path / "not-created"
    # When: the launcher runs with a valid password
    result = run_launcher(
        tmp_path, password=SENTINEL_PASSWORD, workspace=missing
    )
    # Then: it refuses, exits non-zero, and names the directory it wanted
    assert result.returncode == 1, result.stderr
    assert "not-created" in (result.stderr + result.stdout)
    # and the server was not started from a directory that is not there
    assert not dump_path(tmp_path).exists()


def test_launcher_reports_a_workspace_that_is_a_file(tmp_path):
    # Given: a workspace path occupied by something that is not a directory
    not_a_dir = tmp_path / "workspace-file"
    not_a_dir.write_text("", encoding="utf-8")
    # When: the launcher runs
    result = run_launcher(tmp_path, password=SENTINEL_PASSWORD, workspace=not_a_dir)
    # Then: it refuses instead of handing `cd` a file
    assert result.returncode == 1, result.stderr
    assert not dump_path(tmp_path).exists()


def test_launcher_defaults_the_workspace_to_the_configured_path():
    # Given: the launcher's default, the template that documents it, and the
    # `Config` default the app sends to the server as `?directory=`
    # When / Then: all three name ONE directory, so the server's cwd and the
    # sessions R2D2 creates cannot disagree. Compared home-neutrally, so the
    # assertion is about the directory and not about whose home it hangs under.
    default = env_default(launcher_text(), "R2D2_OC_WORKSPACE")
    assert default is not None, "the launcher must have a workspace default"
    documented = example_assignments()["R2D2_OC_WORKSPACE"]
    from app.config import Config

    configured = Config().r2d2_workspace
    assert same_directory(documented, default), f"{documented!r} != {default!r}"
    assert same_directory(configured, default), f"{configured!r} != {default!r}"


# --------------------------------------------------------------------------
# 5. the systemd user unit
# --------------------------------------------------------------------------


def test_unit_file_has_the_shape_of_a_user_unit():
    # Given: the unit on disk
    text = unit_text()
    # When: its sections are read
    # Then: it is a complete user unit, not a fragment
    for section in ("[Unit]", "[Service]", "[Install]"):
        assert section in text, f"missing {section}"
    assert "WantedBy=default.target" in text, "a user unit installs into default.target"


def test_unit_declares_the_directives_the_launcher_needs():
    # Given: the unit on disk
    text = unit_text()
    # When: each directive is looked up
    # Then: all of them are present -- WorkingDirectory pins the server cwd,
    # EnvironmentFile is how the password arrives, Restart keeps a crashed
    # brain-server from staying down
    for directive in ("WorkingDirectory=", "EnvironmentFile=", "ExecStart=", "Restart="):
        assert directive in text, f"missing {directive}"
    # Then: WorkingDirectory is the SAME directory the launcher's default names,
    # because systemd chdirs before ExecStart and the launcher's own `cd` is
    # then a no-op -- two different directories means the unit silently runs
    # somewhere R2D2's `?directory=` never points
    default = env_default(launcher_text(), "R2D2_OC_WORKSPACE")
    working = unit_directive("WorkingDirectory")
    assert working is not None and same_directory(working, default), (
        f"WorkingDirectory={working!r} is not the launcher's workspace {default!r}"
    )
    # Then: EnvironmentFile is this deployment's .env.oc, not an example and not
    # some other checkout's. Only the name is pinned: which home holds the
    # checkout is the installing machine's business.
    env_file = unit_directive("EnvironmentFile")
    assert env_file is not None and env_file.endswith("/.env.oc"), env_file
    assert "Restart=always" in text
    assert "RestartSec=3" in text
    assert "Environment=OPENCODE_LOG_LEVEL=warn" in text


def test_unit_exec_start_runs_the_launcher_script():
    # Given: the unit on disk
    text = unit_text()
    # When: ExecStart is read
    exec_start = [ln for ln in text.splitlines() if ln.startswith("ExecStart=")]
    # Then: there is exactly one, and it runs the script -- not `opencode serve`
    # spelled out a second time, which is how the password export gets lost
    assert len(exec_start) == 1, exec_start
    target = exec_start[0].split("=", 1)[1]
    # Then: the target is the launcher by name and by location. The path may be
    # any absolute one -- or a `$HOME` template, which systemd expands -- but it
    # has to be the repo's `scripts/`, or the unit runs a copy nobody edits.
    assert Path(target).name == LAUNCHER.name, target
    assert _home_neutral(target).endswith(f"/{LAUNCHER.parent.name}/{LAUNCHER.name}"), target
    assert not re.search(r"opencode serve\b", text), "the server is spelled out twice"


def test_unit_does_not_enable_itself_or_inline_a_secret():
    # Given: the unit on disk
    text = unit_text()
    # Then: no credential is written into a file systemd reads as root-owned
    # truth, and enabling the unit stays a manual, deliberate act
    for line in text.splitlines():
        if line.startswith("Environment=") or line.startswith("EnvironmentFile="):
            assert "PASSWORD" not in line, f"unit inlines a credential: {line!r}"
    assert "WantedBy" in text, "unit should be installable"


def test_unit_is_accepted_by_systemd_analyze(tmp_path):
    # Given: systemd's own parser, when this box has it
    if shutil.which("systemd-analyze") is None:
        pytest.skip("systemd-analyze not installed")
    # When: the installer renders the unit, and systemd is asked about THAT
    #
    # The generated unit, not the committed template, and not a re-implementation
    # of the render either. The committed template spells the checkout-relative
    # directives the way this repository was laid out, so verifying it verbatim asks
    # systemd about a directory that exists only on the author's machine -- which is
    # how this test came to fail on a clean clone at any other path. The installer
    # rewrites four directives by KEY and copies every other line verbatim, so
    # running it is the only way to check the unit an owner actually gets; a test
    # that reproduced the substitution would drift from the installer silently.
    dest = tmp_path / "prefix"
    unit_dir = tmp_path / "unitdir"
    workspace = tmp_path / "workspace"
    rendered = subprocess.run(
        ["bash", str(REPO_ROOT / "scripts" / "install_r2d2_opencode_config.sh"),
         "--dest", str(dest), "--unit", "--unit-dir", str(unit_dir),
         "--workspace", str(workspace)],
        capture_output=True, text=True, env={**os.environ, "HOME": str(tmp_path)},
    )
    assert rendered.returncode == 0, rendered.stdout + rendered.stderr
    unit = unit_dir / "r2d2-opencode.service"
    assert unit.is_file(), rendered.stdout + rendered.stderr
    text = unit.read_text()
    # the four path directives name the paths this run was given, which is the
    # whole claim: the same four lines are this machine's on every machine
    for expected in (f"ExecStart={REPO_ROOT}/scripts/opencode_serve.sh",
                     f"EnvironmentFile={REPO_ROOT}/.env.oc",
                     f"WorkingDirectory={workspace}",
                     f"Documentation=file://{REPO_ROOT}/docs/08-deployment.md"):
        assert expected in text, expected
    # and the committed template's own layout did not survive into the unit
    assert "%h/Documents" not in text
    result = subprocess.run(
        ["systemd-analyze", "verify", str(unit)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Error" not in result.stdout + result.stderr


# --------------------------------------------------------------------------
# 6. the environment file: ignored, and its example free of secrets
# --------------------------------------------------------------------------


def test_env_oc_is_git_ignored():
    # Given: the owner's real .env.oc, which will hold a password
    # When: git is asked whether it would be tracked
    result = subprocess.run(
        ["git", "check-ignore", "-v", ".env.oc"], cwd=REPO_ROOT, capture_output=True, text=True
    )
    # Then: it is ignored, so a password cannot be pushed by accident
    assert result.returncode == 0, ".env.oc is not in .gitignore"


#: Values a template may legitimately show: a documented default that is not a
#: credential, or an empty slot the owner fills in. Derived from the deployment's
#: OWN knobs rather than written out, so the safe set follows a port or a
#: workspace wherever the deployment puts it -- and so it cannot quietly widen to
#: cover whatever the example happens to say.
SAFE_EXAMPLE_VALUES = frozenset({
    env_default(launcher_text(), "R2D2_OC_PORT") or "",
    "opencode",
    env_default(launcher_text(), "R2D2_OC_WORKSPACE") or "",
})


def example_assignments() -> dict[str, str]:
    return {
        line.split("=", 1)[0]: line.split("=", 1)[1]
        for line in EXAMPLE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#") and "=" in line
    }


def test_env_oc_example_ships_the_deployment_port():
    # Given: the committed template
    # Then: the port it documents is the port the launcher actually passes, so a
    # template that ships a working-but-wrong port is caught
    assert example_assignments()["R2D2_OC_PORT"] == env_default(
        launcher_text(), "R2D2_OC_PORT"
    )


def test_env_oc_example_carries_no_secret():
    # Given: the committed template
    assignments = example_assignments()
    # Then: every value is empty, an obvious placeholder, or a documented
    # non-secret default -- a template that ships a working password is a
    # password in git history, and one that ships a wrong port is a bug
    for key, value in assignments.items():
        assert (
            value == "" or (value.startswith("<") and value.endswith(">")) or value in SAFE_EXAMPLE_VALUES
        ), f"{key} in .env.oc.example looks like a real value: {value!r}"
    # and the credential slot specifically is never filled
    assert assignments["R2D2_OC_PASSWORD"] == "", "the example ships a password"


def test_env_oc_example_documents_every_variable_the_unit_reads():
    # Given: the committed template
    text = EXAMPLE.read_text(encoding="utf-8")
    launcher = launcher_text()
    # When: each variable the launcher consumes is looked up
    # Then: all of them are documented, along with the rules that keep them safe
    for name in ("R2D2_OC_PORT", "R2D2_OC_USERNAME", "R2D2_OC_PASSWORD", "R2D2_OC_WORKSPACE"):
        assert name in launcher, f"the launcher does not use {name}"
        assert name in text, f".env.oc.example does not document {name}"
    # Then: the two rules an operator must not get wrong are written down
    assert "0600" in text
    assert ".env.oc" in text and "git" in text.lower()
    assert SECRET_ENV in text


# --------------------------------------------------------------------------
# 7-8. the startup health gate in app/main.py
# --------------------------------------------------------------------------


def test_main_never_manages_the_server_process():
    # Given: the ASGI entrypoint
    text = MAIN.read_text(encoding="utf-8")
    # When: it is scanned for process control
    # Then: there is none. A systemd user unit owns the opencode process; R2D2
    # spawning or killing one would make the owner's manual enable meaningless
    # and put a live server's lifetime inside the webhook's request lifetime.
    for token in ("subprocess", "create_subprocess", "os.system", "os.exec", "signal"):
        assert token not in text, f"app/main.py mentions {token!r}"


def test_main_names_the_launcher_as_the_cause():
    # Given: the ASGI entrypoint
    text = MAIN.read_text(encoding="utf-8")
    # When: it is scanned for the failure message
    # Then: an unreachable server produces a line that says what to run, not
    # just "unreachable" -- the operator's next action is a command
    assert "scripts/opencode_serve.sh" in text, "the error names no remedy"
    assert "logger.error" in text, "an unreachable server is not an error"


async def test_startup_reports_reachable_with_its_version(caplog, monkeypatch):
    # Given: an opencode server that answers /global/health
    from app import main as main_module

    monkeypatch.setattr(main_module, "load_backend_specs", _fake_specs, raising=False)
    monkeypatch.setattr(main_module, "OpencodeClient", _fake_client(), raising=False)
    # When: the gate runs
    with caplog.at_level(logging.INFO, logger="r2d2"):
        result = await main_module.probe_opencode_server(_cfg())
    # Then: the startup log carries reachability AND the version, and nothing
    # is logged at ERROR
    assert result is not None and result.reachable is True
    messages = [r.getMessage() for r in caplog.records]
    assert any("1.18.32" in m for m in messages), messages
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR], messages


async def test_startup_reports_unreachable_as_an_error_naming_the_launcher(caplog, monkeypatch):
    # Given: a server that answers but is NOT healthy -- the shape a 500 takes,
    # and the one that would make `reachable=True` a lie
    from app import main as main_module

    monkeypatch.setattr(main_module, "load_backend_specs", _fake_specs, raising=False)
    monkeypatch.setattr(main_module, "OpencodeClient", _fake_client(unhealthy=True), raising=False)
    # When: the gate runs
    with caplog.at_level(logging.INFO, logger="r2d2"):
        result = await main_module.probe_opencode_server(_cfg())
    # Then: the log is an ERROR naming the script to run ...
    assert result is not None and result.reachable is False
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors, [r.getMessage() for r in caplog.records]
    assert any("scripts/opencode_serve.sh" in r.getMessage() for r in errors)
    # ... and nothing in that output reads like success.  A 500 that answered
    # `health()` with a truthy flag, or a log line saying "ok" about a server
    # that is down, is the failure this assertion exists to catch.
    for record in caplog.records:
        message = record.getMessage().lower()
        assert not SUCCESS_READING.search(message), (
            f"unreachable reported as success: {record.getMessage()!r}"
        )


async def test_startup_gate_survives_a_broken_backend_config(caplog, monkeypatch):
    # Given: a config/backends.json that will not parse
    from app import main as main_module
    from core.backends.config_loader import BackendConfigError

    def broken(_cfg):
        raise BackendConfigError("config/backends.json: not json")

    monkeypatch.setattr(main_module, "load_backend_specs", broken, raising=False)
    # When: the gate runs
    with caplog.at_level(logging.INFO, logger="r2d2"):
        result = await main_module.probe_opencode_server(_cfg())
    # Then: it reports the cause and returns None instead of raising -- a brain
    # that cannot boot because a config file is malformed is worse than one
    # that boots on the fallback chain
    assert result is None
    assert [r for r in caplog.records if r.levelno >= logging.ERROR]


async def test_startup_gate_survives_a_registry_without_an_opencode_spec(caplog, monkeypatch):
    # Given: a registry that declares no opencode backend
    from app import main as main_module

    monkeypatch.setattr(main_module, "load_backend_specs", _no_opencode_specs, raising=False)
    # When: the gate runs
    with caplog.at_level(logging.INFO, logger="r2d2"):
        result = await main_module.probe_opencode_server(_cfg())
    # Then: it is a no-op, not an exception
    assert result is None
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_lifespan_runs_the_gate_before_reporting_started(caplog, monkeypatch, tmp_path):
    # Given: the real lifespan, with the gate stubbed so no socket is opened
    # and the database pointed at a scratch file so the owner's is untouched
    import asyncio

    from app import main as main_module
    from app.config import Config
    from core.opencode.client import OpencodeHealth

    probed: list[str] = []
    workspace = tmp_path / "the-workspace"
    workspace.mkdir()
    configured = Config(db_path=str(tmp_path / "t.db"), r2d2_workspace=str(workspace))

    async def fake_probe(cfg):
        probed.append(cfg.r2d2_workspace)
        return OpencodeHealth(reachable=True, version="1.18.32")

    monkeypatch.setattr(main_module, "probe_opencode_server", fake_probe)
    monkeypatch.setattr(
        main_module.Config, "load", classmethod(lambda cls: configured)
    )

    # When: the app starts and stops
    async def run() -> None:
        app = main_module.build_app()
        async with app.router.lifespan_context(app):
            return None

    with caplog.at_level(logging.INFO, logger="r2d2"):
        asyncio.run(run())

    # Then: the gate ran with the CONFIGURED workspace, not with a constant of
    # its own -- `?directory=` is how every session the server opens is scoped,
    # so a gate carrying its own directory would probe a server whose sessions
    # all point somewhere the owner never asked for
    assert probed == [str(workspace)]
    assert any("R2D2 started" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------
# test doubles
# --------------------------------------------------------------------------


def _cfg():
    from app.config import Config

    return Config(backends_path=str(REPO_ROOT / "config" / "backends.json"))


def _fake_specs(_cfg):
    from core.backends.config_loader import BackendChain, BackendSpec

    spec = BackendSpec(
        name="opencode",
        kind="opencode_session",
        base_url="http://127.0.0.1:4599",
        username="opencode",
        password=SENTINEL_PASSWORD,
        fast_model="opencode/space-bunny-free",
    )
    return BackendChain(order=("opencode",)), {"opencode": spec}


def _no_opencode_specs(_cfg):
    from core.backends.config_loader import BackendChain

    return BackendChain(order=()), {}


def _fake_client(*, unhealthy: bool = False):
    """A fresh client double, so one test's `unhealthy` never leaks into the next."""
    from core.opencode.client import OpencodeHealth

    class FakeClient:
        def __init__(self, spec, directory, *, client=None):
            self.spec = spec
            self.directory = directory

        async def health(self):
            if unhealthy:
                return OpencodeHealth(reachable=False, version=None)
            return OpencodeHealth(reachable=True, version="1.18.32")

        async def aclose(self):
            self.closed = True

    return FakeClient
