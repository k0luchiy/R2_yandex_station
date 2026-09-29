"""The mechanical guard: no credential may ever be committed (plan todo 20).

This repository holds three live secrets -- the OpenRouter key and the Telegram
bot token in `.env`, and the `opencode serve` password in `.env.oc`. A single
`git add -A` under a broken `.gitignore` publishes all three, and nothing else in
the suite would notice. Four layers, because each alone can be defeated by a
coincidence:

1. **Shape.** Every tracked file is scanned for the shapes a real key has: an
   OpenRouter/vendor `sk-` key, a Telegram bot token (URL and raw form), a bearer
   header carrying a token, a JSON `apiKey` field, a PEM private key. This layer
   needs no knowledge of *which* secrets exist, so it also catches a credential
   nobody remembered to list.
2. **The live values.** The real contents of `.env` and `.env.oc` are read AT
   TEST TIME -- never at import, never cached, never written into this file or
   into a fixture -- and searched for in every tracked file.
3. **Documentation.** `.env.example` must name every field `Config` declares,
   reflected over the dataclass, so a new field without documentation fails here
   instead of in production.
4. **Ignorability.** `git check-ignore -v` -- not a read of `.gitignore` -- must
   match the secret files and must NOT match the two committed example files.

**Canary versus credential.** The secret-hygiene tests legitimately carry fake
keys, and one of them asserts its own canary never reaches a log line. A hit is
suppressed only when BOTH hold: the file is in `CANARY_FILES`, an explicit list a
human reviewed and committed, AND the matched text carries a marker from
`CANARY_MARKERS`. A real key pasted into an allowlisted file carries no marker
and is still reported (`test_a_realistic_credential_is_reported_in_any_file`),
and `test_the_canary_allowlist_has_no_stale_entries` stops an exemption from
outliving the canary that justified it.

**No silent success.** `test_the_scan_actually_examined_something` fails if the
tracked-file count or the pattern count is zero, and
`test_live_secrets_appear_in_no_tracked_file` fails if it read no secret at all.
**This file scans itself**: a guard that leaks is worse than no guard, so every
probe string below is assembled at runtime from a public constant.

allow: SIZE_OK -- 386 pure LOC, 15 tests, against 514-991 for every other test
file here. The prose is the contract: what counts as a canary, why an allowlist
entry alone must not be enough, and what a credential slot is when there is no
live `.env` to compare against, are the decisions this file exists to record.
Splitting it would leave a half-guard that checks shapes without checking the
live values, which is the more dangerous of the two.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from dataclasses import fields as dataclass_fields
from pathlib import Path
from typing import Final

import pytest

from app.config import Config

REPO_ROOT: Final = Path(__file__).resolve().parents[1]
ENV_EXAMPLE: Final = REPO_ROOT / ".env.example"
ENV_OC_EXAMPLE: Final = REPO_ROOT / ".env.oc.example"
LIVE_ENV_FILES: Final = (REPO_ROOT / ".env", REPO_ROOT / ".env.oc")

#: Credential shapes as (name, pattern). The two JSON/header patterns spell their
#: quotes `\x22` rather than `"` so they cannot match their own source line: a
#: guard whose definition trips its own scan is a guard nobody trusts.
_SHAPES: Final = (
    ("openrouter-key", r"sk-or-[A-Za-z0-9_-]{20,}"),
    ("vendor-sk-key", r"\bsk-[A-Za-z0-9_-]{20,}"),
    ("telegram-token-in-url", r"bot[0-9]{8,10}:[A-Za-z0-9_-]{35,}"),
    ("telegram-token-raw", r"(?<![0-9A-Za-z_])[0-9]{8,10}:[A-Za-z0-9_-]{35,}"),
    ("bearer-header", r"Authorization: Bearer [A-Za-z0-9_.=-]{30,}"),
    ("bearer-header-json", r"Authorization\x22\s*:\s*\x22\s*Bearer [A-Za-z0-9_.=-]{30,}"),
    ("json-api-key", r"apiKey\x22?:\s*\x22[A-Za-z0-9_-]{20,}"),
    ("pem-private-key", r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----"),
)
SHAPES: Final[tuple[tuple[str, re.Pattern[str]], ...]] = tuple(
    (name, re.compile(pattern)) for name, pattern in _SHAPES
)

#: Files allowed to contain a canary: one entry per test module that fabricates a
#: credential to assert it is redacted. Every entry must be tracked and must still
#: contribute a suppressed hit, so an exemption cannot outlive its justification.
CANARY_FILES: Final[frozenset[str]] = frozenset(
    {
        "tests/test_backend_config.py",
        "tests/test_backend_registry.py",
        "tests/test_brain_hybrid.py",
        "tests/test_e2e_stack.py",
        "tests/test_metrics_and_health.py",
        "tests/test_openai_compatible.py",
    }
)
#: A matched value carrying one of these is a fabricated test double. Every marker
#: is a word no real key contains: English, hyphenated or underscore-joined, where
#: a credential is random base64url noise.
CANARY_MARKERS: Final[tuple[str, ...]] = (
    "sentinel",
    "do-not-",
    "key_value",
    "-test",
    "fake",
    "dummy",
    "not-a-real",
    "redacted",
    "placeholder",
    "canary",
)

#: Variables `.env.example` documents that `Config` does not declare:
#: `config_loader` expands them from the environment into `config/backends.json`.
NON_CONFIG_VARS: Final[frozenset[str]] = frozenset({"R2D2_ZEN_KEY"})

#: A variable whose name ends in one of these is meant to hold a secret. This is
#: what keeps `OPENROUTER_BASE_URL` and `YANDEX_MODEL` -- long, distinctive, and
#: not secrets -- out of the comparison in test 2.
SECRET_NAME: Final = re.compile(r"(?:^|_)(?:API_KEY|KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIALS?)$")
MIN_SECRET_LEN: Final = 20

#: Set to `1` to run the opt-in teeth check that stages a real tracked file holding
#: a realistic key. Off in CI, where the mutation is absent.
TEETH_ENV: Final = "R2D2_LEAK_TEETH"
TEETH_PATH: Final = "r2d2_leak_teeth_probe.txt"


def _probe(prefix: str) -> str:
    """A credential-shaped string that is not a credential: derived from a public
    constant, so it is reproducible and unauthenticatable, and carries no canary
    marker, so no exemption can hide it."""
    return prefix + hashlib.sha256(b"r2d2-no-secrets-tracked-teeth").hexdigest()[:48]


@dataclass(frozen=True, slots=True)
class Finding:
    path: str
    line: int
    shape: str
    canary: bool

    def __str__(self) -> str:
        # Never the matched text: on a real leak, that text IS the secret.
        return f"{self.path}:{self.line} looks like a credential ({self.shape})"


@dataclass(frozen=True, slots=True)
class Scan:
    files: int
    pairs: int
    findings: tuple[Finding, ...]

    @property
    def leaks(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if not f.canary)


def _git(*args: str) -> str:
    done = subprocess.run(
        ("git", *args), cwd=REPO_ROOT, capture_output=True, text=True, check=True
    )
    return done.stdout


def _git_ignored(paths: Sequence[str]) -> dict[str, str]:
    """Map each path git matched to the line that matched it; unmatched are absent."""
    done = subprocess.run(
        ("git", "check-ignore", "-v", *paths),
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    matched: dict[str, str] = {}
    for line in done.stdout.splitlines():
        _origin, target = line.rsplit("\t", 1)
        matched[target] = line
    return matched


def _tracked_files() -> tuple[str, ...]:
    return tuple(p for p in _git("ls-files", "-z").split("\0") if p)


def _contents() -> dict[str, str]:
    """Every readable tracked file, read once so the layers agree on the set."""
    return {p: t for p in _tracked_files() if (t := _text_of(p)) is not None}


def _text_of(path: str) -> str | None:
    try:
        return (REPO_ROOT / path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _scan_text(path: str, text: str) -> list[Finding]:
    findings: list[Finding] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for shape, pattern in SHAPES:
            match = pattern.search(line)
            if match is None:
                continue
            value = match.group(0).lower()
            canary = path in CANARY_FILES and any(m in value for m in CANARY_MARKERS)
            findings.append(Finding(path, lineno, shape, canary))
    return findings


def _scan(paths: Sequence[str]) -> Scan:
    findings: list[Finding] = []
    readable = 0
    for path in paths:
        text = _text_of(path)
        if text is None:
            continue
        readable += 1
        findings.extend(_scan_text(path, text))
    return Scan(files=readable, pairs=readable * len(SHAPES), findings=tuple(findings))


def _live_secrets() -> tuple[dict[str, str], list[str]]:
    """Secret-named values from the live env files, read now, plus why each skip."""
    secrets: dict[str, str] = {}
    skipped: list[str] = []
    for path in LIVE_ENV_FILES:
        if not path.exists():
            skipped.append(f"{path.name}: not present")
            continue
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            name, value = name.strip(), value.strip().strip("\"'")
            if not SECRET_NAME.search(name):
                continue
            if len(value) < MIN_SECRET_LEN:
                skipped.append(f"{name}: under {MIN_SECRET_LEN} chars")
                continue
            secrets[f"{path.name}:{name}"] = value
    return secrets, skipped


# --- 1. shape scan over every tracked file ---------------------------------


def test_the_scan_actually_examined_something() -> None:
    """Given a repository with tracked files: the scan must not be vacuous."""
    tracked = _tracked_files()
    scan = _scan(tracked)
    assert len(tracked) >= 40, f"only {len(tracked)} tracked files -- is git ls-files working?"
    assert len(SHAPES) >= 7, f"only {len(SHAPES)} credential shapes are defined"
    assert scan.files == len(tracked), f"{len(tracked) - scan.files} tracked files unreadable"
    assert scan.pairs == scan.files * len(SHAPES) and scan.pairs > 0
    assert any(f.canary for f in scan.findings), "no canary seen; suppression is untested"


def test_no_tracked_file_contains_a_credential() -> None:
    """Given every tracked file: none may hold a credential-shaped value."""
    leaks = _scan(_tracked_files()).leaks
    assert not leaks, "\n".join(str(f) for f in leaks)


def test_a_realistic_credential_is_reported_in_any_file() -> None:
    """Given a credential pasted into a file -- allowlisted or not: it is reported."""
    key, digest, bot_id = _probe("sk-or-v1-"), _probe(""), "1234567890"
    probes = (
        ("tests/test_brain_hybrid.py", f'KEY = "{key}"'),
        ("core/brain.py", f"Authorization: Bearer {key}"),
        ("app/main.py", f'url = "https://api.telegram.org/{"bot"}{bot_id}:{digest[:40]}"'),
        ("config/backends.json", f'"apiKey": "{digest}"'),
        # Assembled from two literals: `test_this_file_contains_no_credential`
        # rejects this file for writing a PEM header out whole.
        ("scripts/x.sh", "echo " + "-----BEGIN OPENSSH " + "PRIVATE KEY-----"),
    )
    for path, line in probes:
        found = _scan_text(path, line)
        assert found, f"{path}: probe not detected: {line[:40]!r}"
        assert all(not f.canary for f in found), f"{path}: probe suppressed as a canary"


def test_the_canary_allowlist_has_no_stale_entries() -> None:
    """Given the canary allowlist: every entry is tracked and still earns its place."""
    tracked = _tracked_files()
    scan = _scan(tracked)
    for path in sorted(CANARY_FILES):
        assert path in tracked, f"allowlist names untracked {path}"
        assert any(f.canary and f.path == path for f in scan.findings), (
            f"{path} no longer holds a canary; drop it from CANARY_FILES"
        )


def test_this_file_contains_no_credential() -> None:
    """Given this guard's own source: it must not itself be a leak."""
    source = Path(__file__).read_text("utf-8")
    found = _scan_text("tests/test_no_secrets_tracked.py", source)
    assert not [f for f in found if not f.canary], "\n".join(str(f) for f in found)


# --- 2. the live secrets are absent from every tracked file -----------------


def _secret_slots() -> dict[str, str]:
    """Every secret-named variable the two committed examples declare.

    Read from the examples rather than listed, so a new credential gets this
    check for free: the reflection that guards `.env.example` completeness is
    the same one that decides what counts as a credential slot here.
    """
    slots: dict[str, str] = {}
    for path in (ENV_EXAMPLE, ENV_OC_EXAMPLE):
        for raw in path.read_text("utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            if SECRET_NAME.search(name.strip()):
                slots[name.strip()] = value.strip()
    return slots


def _is_placeholder(value: str) -> bool:
    """A value a human wrote to mark an empty slot, rather than a credential.

    Four shapes and no allowlist, because an allowlist here would be exactly
    the "exemption that outlives its justification" this file warns about
    elsewhere: nothing, a run of dots, a value TRUNCATED with an ellipsis, a
    bracketed note, or a comma-separated list of UPPER_SNAKE variable names (how
    the docs show "the env file holds these"). Each list element must carry an
    underscore, which is what keeps an all-caps token from reading as a list of
    names. The ellipsis is safe because no credential alphabet this file knows
    contains a dot, so no real value can end in one.
    """
    bare = value.strip().strip("\"'").strip()
    if not bare or set(bare) == {"."}:
        return True
    if bare.endswith(("...", "…")):
        return True
    if bare.startswith("<") and bare.endswith(">"):
        return True
    parts = [p.strip().strip("<>").strip() for p in bare.split(",")]
    parts = [p for p in parts if p]
    return bool(parts) and all("_" in p and p.replace("_", "").isupper() for p in parts)


def test_no_tracked_file_fills_a_credential_slot() -> None:
    """Given the deployment's credential slots: no tracked file assigns a value.

    This half needs no `.env`, so it runs on a fresh clone -- the one place the
    live-value check below has nothing to compare against, and therefore the
    one place a leak would go unnoticed. It is not a substitute for that check:
    it catches a credential too short or too plain to look like one (a six-word
    password pasted into a notes file), which no shape pattern recognises.
    """
    slots = _secret_slots()
    assert slots, "no secret-named variable is declared by the examples"
    found = [
        f"{path}:{lineno} assigns {name}={value[:24]!r}"
        for path, text in ((p, t) for p, t in _contents().items())
        for lineno, line in enumerate(text.splitlines(), start=1)
        for name, _, value in [line.strip().partition("=")]
        if name in slots and value and not _is_placeholder(value)
    ]
    assert not found, "\n".join(found)


def test_the_credential_slot_guard_bites_on_a_value_that_looks_like_nothing() -> None:
    """Given a slot filled with a plain word: the guard must report it.

    Without this, `test_no_tracked_file_fills_a_credential_slot` could be
    passing because nothing ever matches -- the failure mode the skip used to
    hide.
    """
    slots = _secret_slots()
    name = next(iter(slots))
    for planted in ("hunter2", "correct horse battery staple", "sk-or-v1-abc123def456"):
        assert not _is_placeholder(planted), f"{planted!r} read as a placeholder"
        assert name in slots
    for legitimate in ("", "...", "<из .env.oc>", "SENTINEL_TOKEN,", "sk-or-..."):
        assert _is_placeholder(legitimate), legitimate


def test_live_secrets_appear_in_no_tracked_file() -> None:
    """Given the live `.env`/`.env.oc`: no tracked file may contain their values.

    The strongest of the four layers, and the one that needs a configured
    machine. It used to skip on a fresh clone, which left the whole file's
    sharpest check inert exactly where a leak would first happen; the slot
    check above now runs unconditionally, so nothing is skipped -- this layer
    only ever ADDS to it.
    """
    if not any(path.exists() for path in LIVE_ENV_FILES):
        return
    secrets, skipped = _live_secrets()
    assert secrets, f"read no live secret to search for; skipped: {skipped}"
    contents = _contents()
    found = [
        f"{path} contains the value of {label}"
        for path, text in contents.items()
        for label, value in secrets.items()
        if value in text
    ]
    assert not found, "\n".join(found)


# --- 3. .env.example documents every Config field ---------------------------


def test_env_example_documents_every_config_field() -> None:
    """Given the `Config` dataclass: `.env.example` must name each of its fields."""
    documented = set(re.findall(r"^([A-Z][A-Z0-9_]*)=", ENV_EXAMPLE.read_text("utf-8"), re.M))
    declared = {f.name.upper() for f in dataclass_fields(Config)}
    assert declared, "Config declares no fields -- the reflection is broken"
    assert not declared - documented, f"undocumented: {', '.join(sorted(declared - documented))}"
    assert not (documented - declared - NON_CONFIG_VARS), (
        f"documented but never read by Config.load(): "
        f"{', '.join(sorted(documented - declared - NON_CONFIG_VARS))}"
    )


def test_env_example_defines_no_variable_twice() -> None:
    """Given `.env.example`: a duplicated name means the second line silently wins."""
    names = re.findall(r"^([A-Z][A-Z0-9_]*)=", ENV_EXAMPLE.read_text("utf-8"), re.M)
    dupes = sorted({n for n in names if names.count(n) > 1})
    assert not dupes, f"duplicated: {dupes}"


def test_env_example_points_at_the_opencode_env_file() -> None:
    """Given `.env.example`: the server-side credentials are cross-referenced, not copied."""
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    assert ".env.oc.example" in text
    assert "R2D2_OC_PASSWORD" in text
    assert "R2D2_ZEN_KEY" in text
    assert len(ENV_OC_EXAMPLE.read_text("utf-8").splitlines()) >= 20


# --- 4. ignore rules, proven by git and not by reading .gitignore ----------


def test_secret_files_and_caches_are_git_ignored() -> None:
    """Given the paths that hold secrets or state: git must match an ignore rule.

    Each directory is probed by a file INSIDE it, not by the bare name: git
    matches a `dir/` pattern against `dir/file` whether or not the directory
    exists on disk, whereas `check-ignore db` only matches once someone has
    actually created `db/`. Probing the bare name made this test pass or fail
    on a fresh clone depending on what had been run before it -- a machine
    artefact, not a property of `.gitignore`.
    """
    paths = (".env", ".env.oc", ".venv/pyvenv.cfg", "db/sessions.db", ".omo/plan.md", ".r2d2/opencode/opencode.json")
    matched = _git_ignored(paths)
    for path in paths:
        assert path in matched, f"{path} is not git-ignored"
        assert matched[path].startswith(".gitignore:"), matched[path]


def test_the_committed_examples_are_not_git_ignored() -> None:
    """Given the two example files: an over-broad rule must not hide them."""
    matched = _git_ignored((".env.example", ".env.oc.example"))
    assert not matched, f"example file is ignored by {matched}"


# --- 5. the example files hold placeholders only ---------------------------


def test_example_files_contain_placeholders_only() -> None:
    """Given the two committed example files: no value may look like a credential."""
    for path in (ENV_EXAMPLE, ENV_OC_EXAMPLE):
        text = path.read_text(encoding="utf-8")
        assert len(text.splitlines()) >= 20, f"{path.name} is too short to be a real example"
        found = _scan_text(path.name, text)
        assert not found, "\n".join(str(f) for f in found)


# --- teeth: the guard must catch a real tracked file -----------------------


@pytest.fixture
def tracked_teeth_probe() -> Iterator[Path | None]:
    """Stage a tracked file holding a realistic key, then leave the tree clean.

    Opt-in via `R2D2_LEAK_TEETH=1`: in CI the mutation is absent, and creating a
    tracked file as a side effect of a test is not a thing to do by surprise.
    """
    if os.environ.get(TEETH_ENV) != "1":
        yield None
        return
    target = REPO_ROOT / TEETH_PATH
    assert not target.exists(), f"{TEETH_PATH} exists; refusing to clobber it"
    target.write_text(f'# harmless looking\nkey = "{_probe("sk-or-v1-")}"\n', encoding="utf-8")
    _git("add", "--force", TEETH_PATH)
    try:
        yield target
    finally:
        _git("rm", "--cached", "--force", "--quiet", TEETH_PATH)
        target.unlink()


def test_a_tracked_probe_file_is_reported(tracked_teeth_probe: Path | None) -> None:
    """Given a tracked file that really holds a realistic key: the guard must fail."""
    if tracked_teeth_probe is None:
        pytest.skip(f"set {TEETH_ENV}=1 to stage the tracked-file teeth check")
    assert TEETH_PATH in {f.path for f in _scan(_tracked_files()).leaks}
