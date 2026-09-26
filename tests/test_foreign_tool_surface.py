"""What the permission matrix actually withholds from the two R2D2 agents, measured
against opencode's own tool-visibility rule.

`docs/11-opencode-contract.md` (U1) measured how `OPENCODE_CONFIG_DIR` merges with
the owner's global config: 11 foreign agents, the owner's plugin and every MCP
server stay loaded, and R2D2's own rules are appended last so they win. That makes
the tool surface R2D2 must contain **unbounded** -- it is whatever the owner's
plugin and MCP servers contribute -- and a matrix that names fifteen built-in
keys is not a statement about all of it.

The live run (D7) reported plugin and MCP tools executing under `r2d2-voice`,
whose catch-all is `"*"`: "deny". Two independent probes say otherwise, and this
file is the offline form of the second one:

* `opencode debug agent r2d2-voice` prints `{tool: bool}` computed by opencode's
  own `Permission.disabled`, and reports every foreign tool as **false** --
  `call_omo_agent`, `interactive_bash`, `look_at`, `session_read`, and so on. The
  `*` key does cover plugin and MCP tool names; the report's stated mechanism
  ("`*` covers the built-in tool names, not plugin- or MCP-provided ones") is the
  part that does not hold.
* Under `r2d2-agent`, whose catch-all is `"*"`: "ask", the same probe reports
  every foreign tool as **true**, and opencode's `McpCatalog.convertTool` builds
  an `execute` that calls `client.callTool` and nothing else -- it never calls
  `ctx.ask`. A tool that never asks cannot be gated by `"ask"`, so for the whole
  foreign surface `ask` is not a promise opencode keeps. That is the real hole,
  and it is agent-scoped to the agent that is not deny-all.

So the rule this file pins is the one that is enforceable: **a tool is withheld
only by a `deny`**, because visibility (`Permission.disabled`) is the only gate
that applies to a tool which does not ask. `_disabled_tools` below is that
function, transcribed from the build that was measured, and every assertion is a
statement about the shipped JSON rather than about a re-implementation of it.

Only R2D2's own block is fed to the model. opencode's built-in defaults sit
below it (U1) and cannot re-open a bucket, because `Permission.disabled` only
withholds on a rule whose `pattern` is exactly `"*"` -- which is why the trailing
default `external_directory ~/.local/share/opencode/tool-output/* allow` that
the server appends last is harmless here.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Final

import pytest

REPO_ROOT: Final = Path(__file__).resolve().parent.parent
CONFIG: Final = REPO_ROOT / "config" / "opencode" / "r2d2.opencode.json"
VOICE: Final = "r2d2-voice"
AGENT: Final = "r2d2-agent"

#: opencode's own tool-name -> permission-bucket map, transcribed from
#: `Permission.disabled` in the 1.18.32 bundle. Three built-in MCP-resource tools
#: are bucketed as `read`, three editing tools as `edit`, and EVERYTHING else --
#: every plugin tool, every MCP tool -- as its own name. The last two groups are
#: why `"*": "deny"` really does hide the foreign surface, and the first is why
#: `list_mcp_resources` is not covered by it while `read` is allowed.
BUCKET_ALIASES: Final = {
    "edit": "edit",
    "write": "edit",
    "apply_patch": "edit",
    "list_mcp_resources": "read",
    "list_mcp_resource_templates": "read",
    "read_mcp_resource": "read",
}

#: The tools the owner's `oh-my-openagent` plugin contributed to the server on
#: this machine, read off `opencode debug agent r2d2-agent`. They are R2D2's
#: business only as a list of names it must withhold, and two of them are why the
#: matrix needs a catch-all at all: `interactive_bash` is a shell, and
#: `session_read` reads the owner's OTHER opencode sessions.
MEASURED_PLUGIN_TOOLS: Final = (
    "call_omo_agent",
    "interactive_bash",
    "look_at",
    "skill_mcp",
    "session_info",
    "session_list",
    "session_read",
    "session_search",
    "background_cancel",
    "background_output",
    "todowrite",
)

#: Every one of those names contains an underscore, which is what `"*_*"` matches
#: on -- so the convention covers the whole measured plugin surface and every MCP
#: tool, whose names opencode builds as `<server>_<tool>`. The assertion below
#: holds the matrix to that rather than trusting it.
CONVENTION_EXEMPT_TOOL: Final = "todowrite"

#: An MCP tool is named `<server>_<tool>` by opencode. The set cannot be
#: enumerated -- the owner's config decides which servers exist -- so it is
#: represented by its naming convention, which is the only thing a declarative
#: matrix can honestly match on.
MCP_TOOL_SHAPE: Final = "chrome-devtools_list_pages"
SYNTHETIC_MCP_TOOLS: Final = (
    MCP_TOOL_SHAPE,
    "context7_resolve-library-id",
    "exa_web_search_exa",
    "grep_app_searchGitHub",
    "websearch_web_search_exa",
)

#: What each agent is supposed to be able to reach. R2D2's own laptop control is
#: the `bash` shim; everything else it needs is a built-in whose permission key
#: the matrix decides by name. `apply_patch` is the same tool as `edit` under
#: another name, and `invalid` is opencode's no-op placeholder -- both are
#: reachable through the buckets the matrix already declares.
DECLARED_FOR_VOICE: Final = frozenset({"bash", "read", "glob", "grep"})
DECLARED_FOR_AGENT: Final = frozenset(
    {
        "bash", "read", "glob", "grep", "edit", "write", "apply_patch", "invalid",
        "task", "webfetch", "websearch", "skill", "lsp", "question",
    }
)
#: The full catalogue each assertion below is run against, so "nothing else is
#: offered" is a statement about a realistic surface and not about three names.
BUILTIN_CATALOGUE: Final = DECLARED_FOR_AGENT | {"todowrite"}

Rule = tuple[str, str, str]


def load(path: Path = CONFIG) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def rules_of(agent: str, config: dict | None = None) -> list[Rule]:
    """One agent's `permission` block as opencode's ordered `(permission, pattern, action)`.

    A string rule is a catch-all on that key; an object rule is one entry per
    pattern, in the order the JSON lists them -- and that order is load-bearing,
    because `Permission.evaluate` takes the LAST match.
    """
    block = (config or load())["agent"][agent]["permission"]
    out: list[Rule] = []
    for key, value in block.items():
        if isinstance(value, str):
            out.append((key, "*", value))
        else:
            out.extend((key, pattern, action) for pattern, action in value.items())
    return out


def _matches(value: str, pattern: str) -> bool:
    """`Wildcard.match`, transcribed: a general glob, anchored, dotall.

    Every regex metacharacter is escaped first, then `*` becomes `.*` and `?`
    becomes `.`, and a trailing `" *"` is made optional -- so `*.env` is a suffix
    and not "any character", and `*_*` means "contains an underscore" rather than
    the prefix/suffix pair a naive `partition` would give. Getting this wrong is
    not academic: it is the difference between a rule that covers every MCP tool
    and one that covers none.
    """
    body = re.escape(pattern).replace(r"\*", ".*").replace(r"\?", ".")
    if body.endswith(" .*"):
        body = body[:-3] + "( .*)?"
    return re.fullmatch(body, value, re.DOTALL) is not None


def _disabled_tools(tool_names: object, rules: list[Rule]) -> set[str]:
    """`Permission.disabled`, transcribed: the tools opencode keeps from the model.

    The bucket is the tool's own name except for the two alias groups above, and a
    tool is withheld only when the LAST rule matching its bucket is a `deny` whose
    pattern is exactly `"*"`. That last clause is the whole subtlety: a bucket
    whose final rule is narrow -- `read: {"*.env.example": "allow"}` ends on a
    path pattern -- is never withheld, whatever it decided before.
    """
    withheld: set[str] = set()
    for name in tool_names:  # type: ignore[union-attr]
        bucket = BUCKET_ALIASES.get(name, name)
        matched = [rule for rule in rules if _matches(bucket, rule[0])]
        if matched and matched[-1][1] == "*" and matched[-1][2] == "deny":
            withheld.add(name)
    return withheld


def reachable(agent: str, tools: tuple[str, ...], config: dict | None = None) -> set[str]:
    """The tools `agent` can still be offered, for the given tool catalogue."""
    return set(tools) - _disabled_tools(tools, rules_of(agent, config))


def foreign_tools() -> tuple[str, ...]:
    return MEASURED_PLUGIN_TOOLS + SYNTHETIC_MCP_TOOLS


# ---------------------------------------------------------------------------
# The unenumerable surface
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("agent", [VOICE, AGENT])
def test_no_foreign_tool_is_reachable_by_either_agent(agent: str) -> None:
    # Given: the owner's plugin tools and the shape of an MCP tool name, none of
    # which R2D2 declared and none of which it can enumerate
    tools = foreign_tools()
    # When: opencode's own visibility rule is applied to the shipped matrix
    still_reachable = reachable(agent, tools)
    # Then: nothing on that surface is offered to the model. `ask` cannot do this
    # -- a tool that never calls `ctx.ask` is not gated by `ask` -- so only a
    # `deny` counts, and every one of these names must resolve to one.
    assert still_reachable == set(), sorted(still_reachable)


@pytest.mark.parametrize("agent", [VOICE, AGENT])
def test_a_denied_foreign_tool_is_withheld_whatever_rule_wraps_it(agent: str) -> None:
    # Given: each foreign name in its own right, so a rule that happens to cover
    # one of them by accident cannot pass for a decision about all of them
    # When / Then: the bucket of every single name ends on a `*`/`deny` rule
    rules = rules_of(agent)
    for name in foreign_tools():
        assert name in _disabled_tools((name,), rules), name


def test_the_voice_agent_is_denied_by_its_catch_alone() -> None:
    # Given: `r2d2-voice` is the deny-all agent D7 was reported about
    rules = rules_of(VOICE)
    # When / Then: its catch-all already covers the whole foreign surface, which
    # is what the live `opencode debug agent r2d2-voice` reports
    assert rules[0] == ("*", "*", "deny")
    assert _disabled_tools(foreign_tools(), rules) == set(foreign_tools())


# ---------------------------------------------------------------------------
# The fix must not cost the agents anything they are supposed to have
# ---------------------------------------------------------------------------


def test_the_voice_agent_keeps_exactly_its_declared_tools() -> None:
    # Given: the whole built-in catalogue plus the foreign surface
    catalogue = tuple(sorted(BUILTIN_CATALOGUE | set(foreign_tools())))
    # When / Then: only the shim and the read-only lookups survive. Every other
    # built-in is denied by name in the matrix, and every foreign one by `*`.
    assert reachable(VOICE, catalogue) == DECLARED_FOR_VOICE


def test_the_agent_keeps_exactly_its_declared_tools() -> None:
    # Given: the same catalogue
    catalogue = tuple(sorted(BUILTIN_CATALOGUE | set(foreign_tools())))
    # When / Then: every tool the matrix decides by name is still offered -- the
    # foreign-surface denies must cost the agent nothing it is supposed to have --
    # and nothing from the foreign surface is
    assert reachable(AGENT, catalogue) == DECLARED_FOR_AGENT


def test_the_deny_rules_cannot_hijack_a_declared_bucket() -> None:
    # Given: the catch-alls that withhold the foreign surface are patterns too,
    # and `external_directory` contains an underscore -- so a `*_*` rule matches
    # that bucket as well. Placement is the only thing separating them.
    rules = rules_of(AGENT)
    # When / Then: every declared permission still resolves to its own last rule
    for key in ("external_directory", "doom_loop", "read", "bash", "webfetch", "task"):
        matched = [rule for rule in rules if _matches(key, rule[0])]
        assert matched, key
        assert matched[-1][0] == key, f"{key} is decided by {matched[-1][0]!r}, not by itself"
    # ... and the destructive ones are still `ask`, so the broker keeps working
    decided = {key: [r for r in rules if _matches(key, r[0])][-1][2] for key in ("external_directory", "edit", "task", "doom_loop")}
    assert decided == {
        "external_directory": "ask",
        "edit": "ask",
        "task": "ask",
        "doom_loop": "ask",
    }


# ---------------------------------------------------------------------------
# The guard bites
# ---------------------------------------------------------------------------


def _mutated(agent: str, drop: str) -> dict:
    config = load()
    config["agent"][agent]["permission"].pop(drop)
    return config


@pytest.mark.parametrize(
    ("dropped", "returns"),
    [
        ("*_*", "session_read"),
        ("*_*", MCP_TOOL_SHAPE),
        ("todowrite", CONVENTION_EXEMPT_TOOL),
    ],
)
def test_removing_a_deny_puts_a_named_foreign_tool_back_in_reach(
    dropped: str, returns: str
) -> None:
    # Given: the matrix with exactly one of its two foreign-surface rules gone
    config = _mutated(AGENT, dropped)
    # When: opencode's rule is applied again
    still_reachable = reachable(AGENT, foreign_tools(), config)
    # Then: the tool that rule alone was withholding is offered again, so the rule
    # is load-bearing rather than decorative
    assert returns in still_reachable, f"dropping {dropped!r} let nothing back in"


def test_only_one_foreign_tool_needs_a_rule_of_its_own() -> None:
    # Given: the measured plugin surface plus the MCP naming convention
    catalogue = foreign_tools()
    # When / Then: the convention covers all but one of them, which is why the
    # matrix carries `todowrite` beside `*_*` and not a list of eleven names
    without_convention = tuple(name for name in catalogue if not _matches(name, "*_*"))
    assert without_convention == (CONVENTION_EXEMPT_TOOL,)
    assert (CONVENTION_EXEMPT_TOOL, "*", "deny") in rules_of(AGENT)


def test_the_catch_all_is_not_enough_for_the_agent() -> None:
    # Given: the shipped agent block with its catch-all left as `ask` and the two
    # foreign-surface rules removed -- the state the live run ran in
    config = load()
    for key in ("*_*", "todowrite"):
        config["agent"][AGENT]["permission"].pop(key)
    # When / Then: the whole foreign surface is reachable and nothing withholds it,
    # which is the defect, stated as the arithmetic opencode performs
    assert reachable(AGENT, foreign_tools(), config) == set(foreign_tools())


# ---------------------------------------------------------------------------
# What the matrix cannot do, stated rather than implied
# ---------------------------------------------------------------------------


def test_the_mcp_resource_tools_share_the_read_bucket_and_cannot_be_denied_apart() -> None:
    # Given: opencode buckets `list_mcp_resources` as `read`, so its visibility is
    # decided by the read rules and by nothing else
    bucket = BUCKET_ALIASES["list_mcp_resources"]
    # When / Then: naming the tool in the matrix cannot move it -- a rule keyed by
    # the tool's own name matches no bucket the visibility filter ever asks about
    assert bucket == "read"
    for agent in (VOICE, AGENT):
        rules = rules_of(agent)
        assert "list_mcp_resources" not in {rule[0] for rule in rules}
        matched = [rule for rule in rules if _matches(bucket, rule[0])]
        assert matched[-1][1] != "*", "read would be denied outright, which is the other trade"
