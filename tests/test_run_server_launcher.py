from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = REPO_ROOT / "scripts" / "run_server.sh"

# `app/config.py` does not call `load_dotenv()` at import time, on purpose: a
# module should not acquire credentials merely by being imported. The
# consequence is that whatever starts the gateway owns reading `.env`, and the
# thing that starts the gateway is this script. When it did not, the server came
# up perfectly healthy and refused every request with "Доступ запрещён." — no
# ALICE_SKILL_ID, no TELEGRAM_BOT_TOKEN, no LLM provider, and nothing in the
# health output to say why. These tests exist because the bug shipped once, with
# 1000+ other tests green: nothing covered this file at all.
#
# The script cds to `dirname $0/..` and then sources `./.venv/bin/activate` and
# reads `./.env`. Running it from the checkout would read the *owner's* live
# credentials, so every test runs a verbatim copy in a throwaway tree where the
# venv, the `.env` and the `uvicorn` on PATH are all stubs.


def stub_tree(tmp_path: Path, dotenv: str | None) -> tuple[Path, Path]:
    """A tree in which the copied launcher runs without touching the real repo.

    Returns the script and the path the stubbed `uvicorn` dumps its environment
    to.  The stub writes the environment it was exec'd with, so the assertion is
    about what the launcher actually *built*, not about what the source reads.
    """
    root = tmp_path / "tree"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy(LAUNCHER, scripts / "run_server.sh")

    venv = root / ".venv" / "bin"
    venv.mkdir(parents=True)
    (venv / "activate").write_text("# stub: the real one only edits PATH\n", encoding="utf-8")

    dump = root / "env-dump.txt"
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "uvicorn").write_text(
        "#!/usr/bin/env bash\n"
        f'env | sort > "{dump}"\n'
        'printf "%s\\n" "$@" > "' + str(root / "argv.txt") + '"\n',
        encoding="utf-8",
    )
    (bindir / "uvicorn").chmod(0o755)

    if dotenv is not None:
        (root / ".env").write_text(dotenv, encoding="utf-8")

    return scripts / "run_server.sh", dump


def run(
    tmp_path: Path,
    dotenv: str | None = None,
    *,
    env: dict[str, str] | None = None,
    make_venv: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run the copied launcher with a curated environment, never the owner's."""
    script, dump = stub_tree(tmp_path, dotenv)
    if not make_venv:
        (tmp_path / "tree" / ".venv" / "bin" / "activate").unlink()

    base = {
        "PATH": f"{tmp_path / 'bin'}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        "HOME": str(tmp_path),
    }
    if env:
        base.update(env)
    return subprocess.run(
        ["bash", str(script)],
        env=base,
        capture_output=True,
        text=True,
        # A launcher that blocks is a defect, not a slow test.
        timeout=20,
    )


def dumped(tmp_path: Path) -> dict[str, str]:
    """The stub's record of the environment it was exec'd with."""
    path = tmp_path / "tree" / "env-dump.txt"
    fields: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        fields[key] = value
    return fields


def assert_dotenv_loaded(tmp_path: Path, dotenv: str, **expect: str) -> None:
    """The shared check: after running, these names carry these values.

    Membership is asserted before equality so that a variable which never made
    it into the environment fails as an assertion -- the mutation tests below
    look for `AssertionError` specifically, and a bare `KeyError` would escape
    them and make them pass for the wrong reason.
    """
    result = run(tmp_path, dotenv)
    assert result.returncode == 0, result.stderr
    fields = dumped(tmp_path)
    for key, value in expect.items():
        assert key in fields, f"{key} was never exported; got {sorted(fields)}"
        assert fields[key] == value


def assert_exported_wins_over_dotenv(tmp_path: Path) -> None:
    """The shared precedence check, extracted so a mutation can call the very
    same assertion rather than a re-implementation of it."""
    result = run(
        tmp_path,
        "TELEGRAM_BOT_TOKEN=real-token\nALICE_USER_ID=from-file\n",
        env={"TELEGRAM_BOT_TOKEN": "fake-token"},
    )
    assert result.returncode == 0, result.stderr
    fields = dumped(tmp_path)
    assert fields["TELEGRAM_BOT_TOKEN"] == "fake-token"
    assert fields["ALICE_USER_ID"] == "from-file"


def test_the_launcher_is_valid_bash() -> None:
    result = subprocess.run(["bash", "-n", str(LAUNCHER)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_the_launcher_reads_dotenv_into_the_servers_environment(tmp_path: Path) -> None:
    assert_dotenv_loaded(tmp_path, "ALICE_SKILL_ID=abc123\nTELEGRAM_CHAT_ID=42\n",
                         ALICE_SKILL_ID="abc123", TELEGRAM_CHAT_ID="42")


def test_an_exported_variable_wins_over_dotenv(tmp_path: Path) -> None:
    """The ordering that keeps a test harness honest.

    A harness that exports a fake token must not have the real one from `.env`
    silently override it, and an operator who exports a value deliberately means
    it.  `.env` fills in what the environment does not already define.
    """
    assert_exported_wins_over_dotenv(tmp_path)


def test_dotenv_without_a_trailing_newline_still_loads(tmp_path: Path) -> None:
    result = run(tmp_path, "ALICE_SKILL_ID=last-line-no-newline")
    assert result.returncode == 0, result.stderr
    assert dumped(tmp_path)["ALICE_SKILL_ID"] == "last-line-no-newline"


def test_comments_and_blank_lines_are_skipped(tmp_path: Path) -> None:
    dotenv = "# a comment\n\nALICE_SKILL_ID=only-this\n\n# trailing comment\n"
    result = run(tmp_path, dotenv)
    assert result.returncode == 0, result.stderr
    assert dumped(tmp_path)["ALICE_SKILL_ID"] == "only-this"


def test_a_value_containing_spaces_survives_intact(tmp_path: Path) -> None:
    """`export "$line"` quotes the whole assignment, so a space cannot split it
    into a second command -- the failure mode of a naive `export $line`."""
    result = run(tmp_path, "R2D2_OC_WORKSPACE=/home/some one/with spaces\n")
    assert result.returncode == 0, result.stderr
    assert dumped(tmp_path)["R2D2_OC_WORKSPACE"] == "/home/some one/with spaces"


def test_a_line_that_is_not_an_assignment_is_skipped_not_fatal(tmp_path: Path) -> None:
    result = run(tmp_path, "this is not an assignment\nALICE_SKILL_ID=ok\n")
    assert result.returncode == 0, result.stderr
    assert dumped(tmp_path)["ALICE_SKILL_ID"] == "ok"


def test_a_missing_dotenv_still_starts_but_says_so_loudly(tmp_path: Path) -> None:
    """Not a crash: the gateway is runnable from a pure environment.  But the
    operator has to be told, because the failure it prevents is invisible."""
    result = run(tmp_path, None)
    assert result.returncode == 0, result.stderr
    assert "no .env" in result.stderr
    assert "ALICE_SKILL_ID" in result.stderr
    assert "refused" in result.stderr


def test_a_present_dotenv_is_announced(tmp_path: Path) -> None:
    result = run(tmp_path, "ALICE_SKILL_ID=x\n")
    assert result.returncode == 0, result.stderr
    assert "read from .env" in result.stderr


def test_it_refuses_to_run_without_the_checkout_venv(tmp_path: Path) -> None:
    """Silently using a system interpreter is how the venv-in-the-checkout rule
    gets broken without anybody noticing."""
    result = run(tmp_path, "ALICE_SKILL_ID=x\n", make_venv=False)
    assert result.returncode != 0
    assert ".venv" in result.stderr
    assert "system interpreter" in result.stderr


def test_it_binds_the_configured_host_and_port(tmp_path: Path) -> None:
    run(tmp_path, "SERVER_HOST=0.0.0.0\nSERVER_PORT=8099\n")
    argv = (tmp_path / "tree" / "argv.txt").read_text(encoding="utf-8").split()
    assert "app.main:app" in argv
    assert argv[argv.index("--host") + 1] == "0.0.0.0"
    assert argv[argv.index("--port") + 1] == "8099"


def test_it_defaults_to_loopback_and_8080(tmp_path: Path) -> None:
    """The code default is loopback.  `.env.example` ships `0.0.0.0` because a
    tunnel needs it, and that is the right trade only while the tunnel is the
    only thing pointed at the port -- so the fallback must be the safe one."""
    run(tmp_path, "")
    argv = (tmp_path / "tree" / "argv.txt").read_text(encoding="utf-8").split()
    assert argv[argv.index("--host") + 1] == "127.0.0.1"
    assert argv[argv.index("--port") + 1] == "8080"


# --- mutation tests: each proves the checker above can fail ---------------


def test_mutation_dropping_the_dotenv_loop_fails_the_reader_test(tmp_path: Path) -> None:
    """Remove the read of `.env` and the suite must go red.

    Without this, "26 variables loaded" could be satisfied by an unrelated change
    and the regression would be invisible -- which is what happened once.
    """
    text = LAUNCHER.read_text(encoding="utf-8")
    mutated = text.replace("if [ -f .env ]; then", "if false; then", 1)
    assert mutated != text, "the mutation must actually change the launcher"

    original = text
    try:
        LAUNCHER.write_text(mutated, encoding="utf-8")
        with pytest.raises(AssertionError):
            assert_dotenv_loaded(tmp_path, "ALICE_SKILL_ID=abc123\n", ALICE_SKILL_ID="abc123")
    finally:
        LAUNCHER.write_text(original, encoding="utf-8")


def test_mutation_making_dotenv_win_fails_the_precedence_test(tmp_path: Path) -> None:
    """Flip the precedence so `.env` overrides the environment: the exported
    value must stop winning, and the test must notice."""
    text = LAUNCHER.read_text(encoding="utf-8")
    mutated = text.replace(
        'if [ -z "${!name+x}" ]; then',
        'if [ -n "${!name+x}" ]; then',
        1,
    )
    assert mutated != text, "the mutation must actually change the launcher"

    original = text
    try:
        LAUNCHER.write_text(mutated, encoding="utf-8")
        with pytest.raises(AssertionError):
            assert_exported_wins_over_dotenv(tmp_path)
    finally:
        LAUNCHER.write_text(original, encoding="utf-8")
