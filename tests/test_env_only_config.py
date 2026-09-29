"""`app.config` merges no file: the environment is the whole configuration.

A bare import-time `load_dotenv()` used to merge whatever `.env` sat next to
the checkout into every process that imported `app.config`, no matter where
that process started. "Configure this entirely from the environment" was
therefore unachievable, and a deployment that believed it was isolated got a
silent merge -- during one live run the owner's real `TELEGRAM_BOT_TOKEN`, chat
id and `R2D2_OC_PASSWORD` were present in a process that overrode only the
token. The run was safe by luck (Telegram intercepted AND the token overridden),
which is a finding about the design, not a near-miss to forget.

The first test is the proof, in a real subprocess: a `.env` in the working
directory is not merged. The second pins that `Config.load` still reads the
environment it is given.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.config import Config

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = "R2D2_DOTENV_PROBE_VALUE"


def test_a_dotenv_in_the_working_directory_is_not_merged(tmp_path: Path) -> None:
    # Given a directory whose `.env` names a variable this process does not have
    (tmp_path / ".env").write_text(f"{PROBE}=from-a-file\n", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != PROBE}
    assert PROBE not in env
    # When a fresh interpreter imports `app.config` with that directory as CWD
    proc = subprocess.run(
        [sys.executable, "-c", f"import app.config, os; print(os.environ.get({PROBE!r}, ''))"],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={**env, "PYTHONPATH": str(REPO_ROOT)},
        timeout=60,
    )
    # Then the file was not merged: the variable is still absent
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "", proc.stdout


def test_config_load_reads_the_environment_it_is_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given an environment holding one override and missing one default
    monkeypatch.setenv("R2D2_FAST_DEADLINE", "1.5")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    # When/Then the override lands and the absence stays absent -- nothing else
    # is consulted
    cfg = Config.load()
    assert cfg.r2d2_fast_deadline == 1.5
    assert cfg.telegram_bot_token == ""
