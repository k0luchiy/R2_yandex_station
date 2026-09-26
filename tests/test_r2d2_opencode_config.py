"""R2D2's own opencode config is the safety boundary between a voice command
from a smart speaker and `bash` on the owner's real machine, so most of what is
asserted here is what the file must NOT say.

Three measured facts from the spike (`docs/11-opencode-contract.md`) shape every
assertion:

* **C2** -- `OPENCODE_CONFIG_DIR` **merges** with the owner's global
  `opencode.json`; 11 foreign agents and 15 foreign providers stay loaded and
  visible.  A missing or empty `permission` block therefore does not mean "no
  permissions", it means "the owner's global rules apply", and those can be
  `{"*": "allow"}`.  Both agents must decide **every** permission key
  explicitly, and `*` must come first so the specific rules are the last match
  and win.
* **C1** -- `opencode/space-bunny-free` is the only model `opencode serve`
  accepts: every other free Zen model returns an inner 403 `FreeTierError` and
  paid ones 402.  This file therefore names exactly one model, and no refused
  one.
* **C4** -- in `GET /agent` the `tools` field reads back `null`, and a declared
  `tools: {"bash": false}` is normalised into `permission` rules.  `permission`
  is the only thing worth asserting against; `tools` is only shape-checked.

Nothing here talks to a server, so the suite is deterministic and offline.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG = REPO_ROOT / "config" / "opencode" / "r2d2.opencode.json"
INSTALL = REPO_ROOT / "scripts" / "install_r2d2_opencode_config.sh"

MODEL = "opencode/space-bunny-free"
SCHEMA = "https://opencode.ai/config.json"
VOICE = "r2d2-voice"
AGENT = "r2d2-agent"
SENTINEL = "[[NEEDS_AGENT]]"

# The CLI shim lives at this path and re-execs itself under the repo venv, so an
# LLM may plausibly spell the same tool three different ways.  All three forms
# must be allowlisted or the agent loses the only tool it is supposed to have.
CLI = "/home/koluchiy/.r2d2/r2d2_do.py"
VENV_PY = REPO_ROOT / ".venv" / "bin" / "python"
R2D2_DO_FORMS = (
    f"{CLI} *",
    f"python3 {CLI} *",
    f"{VENV_PY} {CLI} *",
)
# Read-only probes that answer a spoken status question without any tool the
# model has to be trusted with.  Nothing here writes, moves or deletes.
READ_ONLY_PROBES = frozenset(
    {
        "upower *",
        "cat /sys/class/power_supply/*",
        "df *",
        "free *",
        "uname *",
        "hostname *",
        "ps *",
        "uptime",
        "date",
    }
)

# Every permission key this opencode build exposes (measured in todo 1, listed
# in the plan's "Critical context facts").  A key the config does not name
# falls through to the merged rules from the owner's global config, so the
# completeness test demands all of them.
PERMISSION_KEYS = frozenset(
    {
        "read",
        "edit",
        "glob",
        "grep",
        "bash",
        "task",
        "skill",
        "lsp",
        "question",
        "webfetch",
        "websearch",
        "external_directory",
        "doom_loop",
    }
)
# The three that can touch or destroy state on the owner's machine.
NEVER_ALLOWED = ("bash", "edit", "external_directory")
# Keys the top-level block may open up for a foreign agent inside R2D2's own
# process; everything else there must stay denied.
TOP_LEVEL_ALLOWED = frozenset({"read", "glob", "grep", "webfetch", "websearch"})

# A model id is `provider/model` with no spaces.  A dotted host has the same
# shape, so URLs are excluded -- but only URLs, so a real model hidden in a
# prompt is still caught.
MODEL_LIKE = re.compile(r"[a-z0-9][a-z0-9._-]*/[A-Za-z0-9._-]+")
URL_LIKE = re.compile(r"^[a-z]+://")
# Model ids the spike proved unusable (C1).  Presence of any of them anywhere
# in the file is a regression, whatever the top-level `model` says.
REFUSED_MODELS = ("muse-spark", "ling-3.0", "mimo-", "nemotron", "gpt-", "claude")

# The install script copies files.  It must never manage a process, so the
# script's own text may not even mention the three ways to do that.
PROCESS_CONTROL = ("opencode serve", "systemctl", "kill")

DELETE = object()


def load(path: Path = CONFIG) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def string_values(node: object) -> list[str]:
    """Every string in the document, keys excluded.

    A model id can hide in a prompt (a value); the absolute paths in the bash
    patterns are KEYS, and scanning those would match `sys/class`.
    """
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        return [text for value in node.values() for text in string_values(value)]
    if isinstance(node, list):
        return [text for value in node for text in string_values(value)]
    return []


def allows(rule: object) -> bool:
    return rule == "allow" or (isinstance(rule, dict) and rule.get("*") == "allow")


def write_path(config: dict, path: str, value: object) -> None:
    """Set (or delete) one dotted path inside a config dict."""
    *parents, leaf = path.split(".")
    node = config
    for key in parents:
        node = node[key]
    if value is DELETE:
        del node[leaf]
    else:
        node[leaf] = value


def permission_violations(agent_name: str, permission: object) -> list[str]:
    """Every way a `permission` block can quietly widen an agent's reach.

    Pure: takes the parsed block, returns the reasons it is unsafe.  The
    mutation tests below feed it deliberately broken blocks, which is the only
    honest way to show a guard is not vacuous.
    """
    if not isinstance(permission, dict) or not permission:
        return [
            f"{agent_name}: permission must be a non-empty object -- an absent "
            f"or empty one inherits the owner's global rules (C2)"
        ]
    bad: list[str] = []
    if permission.get("*") == "allow":
        bad.append(f'{agent_name}: permission["*"] == "allow"')
    if next(iter(permission), None) != "*":
        bad.append(
            f"{agent_name}: '*' must be the first key so the later specific "
            f"rules are the last match and win"
        )
    undecided = sorted(PERMISSION_KEYS - permission.keys())
    if undecided:
        bad.append(
            f"{agent_name}: keys {undecided} are undecided, so the merged "
            f"global rules decide them (C2)"
        )
    for key in NEVER_ALLOWED:
        rule = permission.get(key)
        decided = rule.get("*") if isinstance(rule, dict) else rule
        if decided == "allow":
            bad.append(f'{agent_name}: {key} is auto-allowed (its catch-all is "allow")')
    for key, rule in permission.items():
        if isinstance(rule, dict) and next(iter(rule), None) != "*":
            bad.append(
                f"{agent_name}: {key} has no leading catch-all, so which rule "
                f"wins for an unmatched input is not stated here"
            )
    return bad


def agent_permissions(config: dict) -> dict[str, object]:
    return {name: spec["permission"] for name, spec in config["agent"].items()}


# ---------------------------------------------------------------------------
# shape -- both agents exist and declare everything the schema shape needs
# ---------------------------------------------------------------------------


def test_config_parses_and_declares_both_agents_by_exact_name():
    # Given: the version-controlled source of truth
    config = load()
    # When: the two agents R2D2 addresses are looked up by exact name
    voice = config["agent"][VOICE]
    task = config["agent"][AGENT]
    # Then: C2 measured 11 foreign agents still loaded under this config dir,
    # so a name typo would silently address someone else's agent
    assert config["$schema"] == SCHEMA
    for name, agent in ((VOICE, voice), (AGENT, task)):
        assert agent["description"], f"{name} has no description"
        assert agent["mode"] == "primary"
        assert agent["permission"], f"{name} has no permission block"
        assert agent["prompt"], f"{name} has no prompt"


# ---------------------------------------------------------------------------
# C2 -- the permission matrix, and the guarantee that it cannot be inherited
# ---------------------------------------------------------------------------


def test_voice_denies_everything_except_its_allowlist():
    # Given: the voice agent answers inside Alice's 4.5s budget
    permission = load()["agent"][VOICE]["permission"]
    # When/Then: the catch-all is a hard deny, not a default
    assert permission["*"] == "deny"
    # and it can look things up and read machine status, but nothing else
    for key in ("read", "glob", "grep"):
        assert allows(permission[key]), f"voice lost {key}"
    for key in ("edit", "task", "webfetch", "websearch", "external_directory", "doom_loop"):
        assert permission[key] == "deny", f"voice may use {key}"


def test_agent_asks_before_everything_except_its_allowlist():
    # Given: the agent does real work on the owner's machine
    permission = load()["agent"][AGENT]["permission"]
    # When/Then: the catch-all asks, so nothing runs unreviewed
    assert permission["*"] == "ask"
    assert allows(permission["read"])
    for key in ("glob", "grep", "webfetch", "websearch"):
        assert permission[key] == "allow", f"agent lost {key}"
    # and the three destructive keys are asked, never allowed
    assert permission["edit"] == "ask"
    assert permission["external_directory"] == "ask"
    assert permission["doom_loop"] == "ask"
    assert isinstance(permission["bash"], dict)
    assert permission["bash"]["*"] == "ask"


def test_both_agents_decide_every_permission_key_so_nothing_is_inherited():
    # Given: OPENCODE_CONFIG_DIR merges with the owner's global opencode.json,
    # where an agent can be `{"*": "allow"}` (C2)
    config = load()
    # When: each agent's block is checked for gaps
    violations = [
        reason
        for name, permission in agent_permissions(config).items()
        for reason in permission_violations(name, permission)
    ]
    # Then: no gaps, no inherited decision, nothing auto-allowed
    assert violations == []


def test_top_level_permission_denies_by_default_and_opens_only_read_only_keys():
    # Given: the top-level block also governs the 11 foreign agents that stay
    # loaded inside R2D2's own process (C2)
    top = load()["permission"]
    # When/Then: deny first, then a short read-only allowlist
    assert next(iter(top), None) == "*"
    assert top["*"] == "deny"
    assert sorted(k for k, v in top.items() if k != "*" and v != "deny") == sorted(
        TOP_LEVEL_ALLOWED
    )


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("agent.r2d2-agent.permission.bash", "allow"),
        ("agent.r2d2-agent.permission.bash.*", "allow"),
        ("agent.r2d2-agent.permission.edit", "allow"),
        ("agent.r2d2-agent.permission.external_directory", "allow"),
        ("agent.r2d2-agent.permission.*", "allow"),
        ("agent.r2d2-agent.permission", {}),
        ("agent.r2d2-agent.permission.external_directory", DELETE),
        ("agent.r2d2-voice.permission.bash.*", "allow"),
        ("agent.r2d2-voice.permission.edit", "allow"),
        ("agent.r2d2-voice.permission", {}),
    ],
)
def test_guard_rejects_a_config_that_widens_an_agent(tmp_path, path, value):
    # Given: a mutated copy of the shipped config, written to disk so the guard
    # is fed a file the way the other tests feed it
    config = load()
    write_path(config, path, value)
    mutated = tmp_path / "mutated.opencode.json"
    mutated.write_text(json.dumps(config), encoding="utf-8")
    # When: the guard inspects it
    violations = [
        reason
        for name, permission in agent_permissions(load(mutated)).items()
        for reason in permission_violations(name, permission)
    ]
    # Then: the mutation is named, not quietly tolerated
    target = path.split(".")[1]
    assert any(target in reason for reason in violations), violations


def test_guard_rejects_a_permission_block_with_the_catch_all_not_first():
    # Given: a block whose specific rules come before "*"
    permission = load()["agent"][VOICE]["permission"]
    reversed_block = {key: permission[key] for key in reversed(list(permission))}
    # When/Then: "last match wins" would make the deny swallow the allowlist
    assert permission_violations(VOICE, reversed_block)


def test_guard_does_not_flag_the_allowlist_the_shipped_config_actually_needs():
    # Given: the real config, whose websearch allow is deliberate for the agent
    permission = load()["agent"][AGENT]["permission"]
    # When/Then: the guard is specific -- it rejects widening, not capability
    assert permission_violations(AGENT, permission) == []


# ---------------------------------------------------------------------------
# the only tool the agents are supposed to have
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("agent", [VOICE, AGENT])
def test_all_three_r2d2_do_forms_are_allowlisted(agent):
    # Given: the shim re-execs itself, so an LLM may spell it three ways
    bash = load()["agent"][agent]["permission"]["bash"]
    # When/Then: every form the model may plausibly emit resolves to allow
    missing = [form for form in R2D2_DO_FORMS if bash.get(form) != "allow"]
    assert missing == [], f"{agent} would refuse: {missing}"


def test_the_allowlisted_shim_matches_the_app_configuration():
    # Given: the rest of R2D2 addresses the same shim through app/config.py
    from app.config import Config

    # When/Then: the allowlist and the app agree on the path, and the third
    # form names this repo's own venv -- the only interpreter that can import
    # core.tools
    cfg = Config()
    assert cfg.r2d2_cli_path == CLI
    assert cfg.r2d2_voice_agent == VOICE and cfg.r2d2_task_agent == AGENT
    assert cfg.r2d2_needs_agent_sentinel == SENTINEL
    assert R2D2_DO_FORMS[2] == f"{REPO_ROOT / '.venv' / 'bin' / 'python'} {CLI} *"


def test_voice_cannot_reach_the_shell_subcommand():
    # Given: `r2d2_do.py *` is allowlisted, which includes its `shell`
    # subcommand -- the one call that can destroy something
    bash = load()["agent"][VOICE]["permission"]["bash"]
    # When: the subcommand is looked for behind each spelling
    denies = [rule for rule, action in bash.items() if "shell" in rule]
    # Then: defence in depth -- the prompt forbids it and the CLI's own risk
    # gate refuses it, and here opencode refuses it before either is reached
    assert denies, "voice has no shell deny rule"
    assert all(bash[rule] == "deny" for rule in denies)
    assert {rule.split("r2d2_do.py")[0] for rule in denies} == {
        form.split("r2d2_do.py")[0] for form in R2D2_DO_FORMS
    }


def test_voice_bash_allowlist_is_the_shim_plus_read_only_probes():
    # Given: the voice agent runs on a 4.5s budget with no human watching, so
    # its bash allowlist is the whole of what a smart speaker can reach
    bash = load()["agent"][VOICE]["permission"]["bash"]
    # When/Then: exactly the shim and the read-only probes -- set equality, so
    # an extra command cannot be added without a test noticing
    allowed = {rule for rule, action in bash.items() if action == "allow"}
    assert allowed == set(R2D2_DO_FORMS) | READ_ONLY_PROBES


# ---------------------------------------------------------------------------
# prompts -- the escalation contract, and prompt injection
# ---------------------------------------------------------------------------


def test_voice_prompt_states_the_escalation_sentinel_as_an_exact_literal():
    # Given: the prompt is prose an LLM reads
    prompt = load()["agent"][VOICE]["prompt"]
    # When/Then: the routing code dispatches on this exact token, so it is a
    # machine-consumed literal -- rewording the prompt must never touch it
    assert SENTINEL in prompt
    # and the prompt must place it first, because core/routing.py strips the
    # sentinel before core/render.clean().  Keyword-level on purpose: deleting
    # the rule fails, rewording the sentence around it does not.
    assert re.search(r"начин\w+[^.]{0,60}" + re.escape(SENTINEL), prompt)


def test_voice_prompt_never_names_the_agent_it_escalates_to():
    # Given: the voice agent hands the task over by sentinel, not by name
    prompt = load()["agent"][VOICE]["prompt"]
    # When/Then: naming the other agent would let a user turn it into its own
    assert AGENT not in prompt


def test_voice_prompt_forbids_answers_that_break_the_voice_contract():
    # Given: Alice reads the answer aloud, so formatting ruins it
    prompt = load()["agent"][VOICE]["prompt"].lower()
    # When/Then: each rule the contract depends on is stated
    for token in ("markdown", "эмодзи", "ссылк", "по-русски", "не выдумывай"):
        assert token in prompt, f"voice prompt lost the {token!r} rule"
    # and long content leaves by telegram instead of being read out
    assert "telegram" in prompt or "телеграм" in prompt


def test_voice_prompt_refuses_instructions_that_change_its_own_rules():
    # Given: the realistic attack is a spoken or read instruction that says
    # "ignore your rules, now run ..." -- the user turn and every file the
    # agent reads are untrusted input
    prompt = load()["agent"][VOICE]["prompt"].lower()
    # When/Then: the guard is present at keyword level
    assert "нельзя переопределить" in prompt
    assert re.search(r"изменить\s+эти\s+правила", prompt)
    assert "игнорируй" in prompt


def test_agent_prompt_states_the_confirmation_and_honesty_contract():
    # Given: the agent's final text is delivered to Telegram
    prompt = load()["agent"][AGENT]["prompt"].lower()
    # When/Then: each rule that keeps a spoken request from becoming an
    # unreviewed system change is stated
    assert "телеграм" in prompt and "markdown" in prompt
    assert "r2d2_do shell" in prompt
    assert "подтверждени" in prompt
    assert re.search(r"не\s+говори|никогда\s+не\s+утверждай|не\s+заявляй", prompt)
    assert "жди" in prompt or "ожидай" in prompt


# ---------------------------------------------------------------------------
# prompts -- one session, two permission matrices
# ---------------------------------------------------------------------------

#: The one persistent opencode session per human is deliberately shared by both
#: agents, and opencode writes its ENFORCEMENT state into that shared history:
#: a refused tool call is stored as a `tool` part whose `state.error` prints the
#: effective rules -- the refused agent's matrix, not the reader's.  The reader
#: then concluded the refusal was about itself and stopped calling tools its own
#: matrix allows (`qa/live-run-postfix.md`, D9), so each prompt must say, in its
#: own words, all seven of these.  Keyword level on purpose, like the sentinel
#: test above: deleting a rule fails, rewording the sentence around it does not.
REFUSAL_RULE: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"отказ\w*"), "name the artefact -- a refusal left in the session"),
    (re.compile(r"правил\w* разрешени"), "name what the refusal prints -- the permission rules"),
    (
        re.compile(r"которому отказали|чужому вызову"),
        "attribute those rules to the agent that was REFUSED",
    ),
    (re.compile(r"не\s+твои|не\s+свои"), "say they are not the reader's own matrix"),
    (
        re.compile(r"проверяет\s+каждый\s+твой\s+вызов\s+по\s+твоим\s+правилам"),
        "say the reader's own calls are checked against the reader's own rules",
    ),
    (
        re.compile(r"не\s+означает|не\s+является\s+причиной"),
        "forbid reading a stored refusal as a ban on the reader's tools",
    ),
    (
        re.compile(r"инструмент\w*,?\s+которы\w+\s+тебе\s+разрешен"),
        "name the tools the reader may still use",
    ),
)


def refusal_violations(agent_name: str, prompt: object) -> list[str]:
    """Every way a prompt can leave a shared-session refusal readable as a self-ban.

    Pure: takes one prompt, returns the reasons D9 is still open for that agent.
    The mutation test below feeds it a prompt with the rule deleted, which is the
    only honest way to show the guard is not vacuous.
    """
    if not isinstance(prompt, str):
        return [f"{agent_name}: prompt must be a string, got {type(prompt).__name__}"]
    lowered = prompt.lower()
    return [
        f"{agent_name}: the prompt does not {what}"
        for pattern, what in REFUSAL_RULE
        if not pattern.search(lowered)
    ]


def test_both_prompts_say_a_foreign_refusal_is_not_their_own_rule():
    # Given: ONE session, shared by two agents with different permission
    # matrices, into which opencode stores a refused tool call together with the
    # refused agent's whole effective matrix.  The live run measured what that
    # does: the other agent read the dump as a statement about itself and refused
    # every tool, including the ones its own matrix allows.
    config = load()
    # When/Then: BOTH prompts carry the rule, in the words that fix it -- the
    # failure is mirrored (the voice agent refused its own allowlisted shim after
    # one denied call), so a rule in only one of them leaves the other poisoned
    for name, agent in config["agent"].items():
        assert refusal_violations(name, agent["prompt"]) == []


def test_a_refusal_of_my_own_is_still_mine_to_obey():
    # Given: the rule above could be read as "refusals are ignorable"
    prompt = load()["agent"][AGENT]["prompt"].lower()
    # When/Then: a refusal the reader receives ITSELF stays binding, so the fix
    # does not become an instruction to work around opencode
    assert re.search(r"отказ,\s+который\s+получишь\s+ты\s+сам", prompt)
    assert re.search(r"это\s+уже\s+твоё\s+правило", prompt)
    # and the pre-existing "do not bypass an ask" rule is untouched
    assert "не обходи разрешения" in prompt


def test_guard_rejects_a_prompt_whose_refusal_rule_was_deleted():
    # Given: a prompt whose rule about a shared-session refusal is gone -- what a
    # bad merge or a careless rewrite leaves behind, and the exact state the
    # shipped file was in before this defect was fixed
    prompt = load()["agent"][AGENT]["prompt"]
    stripped = "\n".join(line for line in prompt.split("\n") if "чужому вызову" not in line)
    assert stripped != prompt, "the shipped prompt has no rule to delete"
    # Then: every fact the deleted rule carried is reported, so the guard can
    # fail -- an assertion that cannot fail is a hole, not a test
    violations = refusal_violations(AGENT, stripped)
    assert len(violations) == len(REFUSAL_RULE)
    assert all(AGENT in violation for violation in violations)



# ---------------------------------------------------------------------------
# C1 -- exactly one model, and it is the only one that works
# ---------------------------------------------------------------------------


def test_the_only_model_in_the_file_is_the_usable_free_one():
    # Given: the raw file text
    raw = CONFIG.read_text(encoding="utf-8")
    config = load()
    # When: every `provider/model` shaped token inside a string value is
    # collected -- a prompt counts, the absolute paths in the bash patterns
    # are keys and do not
    found = [
        model
        for value in string_values(config)
        if not URL_LIKE.match(value)
        for model in MODEL_LIKE.findall(value)
    ]
    # Then: C1 -- space-bunny-free is the only model opencode serve accepts
    assert found == [MODEL]
    assert config["model"] == MODEL
    # and none of the refused ids is hiding anywhere in the file
    for refused in REFUSED_MODELS:
        assert refused not in raw.lower(), f"{refused} is unusable through opencode serve"


# ---------------------------------------------------------------------------
# C4 -- `tools` is not a thing worth asserting on
# ---------------------------------------------------------------------------


def test_tools_maps_are_record_string_boolean():
    # Given: U3 -- `tools` is Record<string, boolean>, not a list
    config = load()
    # When/Then: any declared map has that shape
    for name, agent in config["agent"].items():
        tools = agent.get("tools", {})
        assert isinstance(tools, dict), f"{name} tools is not a map"
        for key, value in tools.items():
            assert isinstance(key, str) and isinstance(value, bool), f"{name}: {key!r}"


def test_no_agent_declares_a_tools_map_at_all():
    # Given: C4 -- in `GET /agent` `tools` reads back `null` and a declared
    # `tools: {"bash": false}` is normalised into `permission` rules, so
    # `tools` can never be verified against a live server
    config = load()
    # When/Then: `permission` is the only source of truth.  The plan's draft
    # also had `"tools": {"r2d2_do": true}`, naming a tool that does not exist:
    # it reads as "the shim is enabled" while enabling nothing.
    for name, agent in config["agent"].items():
        assert "tools" not in agent, f"{name} declares tools={agent['tools']!r}"


# ---------------------------------------------------------------------------
# install -- the shipped copy and the live copy must never drift
# ---------------------------------------------------------------------------


@pytest.fixture
def sandbox(tmp_path: Path) -> tuple[Path, Path]:
    """A destination and a stand-in HOME, both inside one throwaway root.

    The script also installs the shim, whose default path is derived from
    ``$HOME``; without the stand-in the test suite would overwrite the owner's
    real ``~/.r2d2/r2d2_do.py`` on every run.
    """
    home = tmp_path / "home"
    home.mkdir()
    return tmp_path / "oc-cfg", home


def install(dest: Path, home: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(INSTALL), "--dest", str(dest)],
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(home)},
    )


def test_install_into_a_temp_dir_is_byte_identical(sandbox):
    # Given: a destination that does not exist yet
    dest, home = sandbox
    # When: the install script is pointed at it
    result = install(dest, home)
    # Then: a stale installed config would keep the OLD permissions alive, so
    # the installed bytes must equal the repo bytes exactly
    assert result.returncode == 0, result.stderr
    assert (dest / "opencode.json").read_bytes() == CONFIG.read_bytes()


def test_install_tightens_a_loose_destination(sandbox):
    # Given: a destination somebody already created world-readable
    dest, home = sandbox
    dest.mkdir(mode=0o755)
    # When: the install script runs against it
    result = install(dest, home)
    # Then: the directory and the config are private to the owner
    assert result.returncode == 0, result.stderr
    assert stat.S_IMODE(dest.stat().st_mode) == 0o700
    assert stat.S_IMODE((dest / "opencode.json").stat().st_mode) == 0o600


def test_install_writes_nothing_outside_its_destination(sandbox):
    # Given: a stand-in HOME, so the shim install has somewhere harmless to go
    dest, home = sandbox
    root = dest.parent
    # When: the script runs
    result = install(dest, home)
    # Then: every path it reports writing is inside the sandbox, never in the
    # owner's real ~/.r2d2
    written = [
        Path(line.split(" ", 1)[1])
        for line in result.stdout.splitlines()
        if line.startswith("installed ")
    ]
    assert written, result.stdout
    assert all(path == root or root in path.parents for path in written), written
    # and the hint it prints names the destination it actually used
    assert f"OPENCODE_CONFIG_DIR={dest}" in result.stdout


def test_install_script_is_valid_bash():
    # When: the shell parses the script without running it
    result = subprocess.run(["bash", "-n", str(INSTALL)], capture_output=True, text=True)
    # Then: it is syntactically sound
    assert result.returncode == 0, result.stderr


def test_install_script_never_manages_a_process():
    # Given: the script's own text
    text = INSTALL.read_text(encoding="utf-8")
    # When/Then: R2D2 never starts or stops the opencode process -- that is a
    # systemd user unit (todo 12) -- so the script may not even name the verbs
    for token in PROCESS_CONTROL:
        assert token not in text, f"install script mentions {token!r}"


def test_gitignore_covers_the_installed_config_directory():
    # Given: a config directory landing inside the repo by accident
    # When: git is asked whether it would be tracked
    result = subprocess.run(
        ["git", "check-ignore", "-q", ".r2d2/opencode/opencode.json"],
        cwd=REPO_ROOT,
        capture_output=True,
    )
    # Then: it is ignored, so a live copy with its permissions is never pushed
    assert result.returncode == 0, ".r2d2/ is not in .gitignore"
