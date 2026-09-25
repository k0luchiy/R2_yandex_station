# R2D2's opencode config

`r2d2.opencode.json` is the source of truth. Install it with:

```sh
bash scripts/install_r2d2_opencode_config.sh            # -> ~/.r2d2/opencode
bash scripts/install_r2d2_opencode_config.sh --dest DIR # for tests and experiments
```

The opencode process is then started with `OPENCODE_CONFIG_DIR=~/.r2d2/opencode`
(see `scripts/opencode_serve.sh`).

**Why the file has no `//` comments.** It must stay strict JSON: it is read by
`json.loads` in `tests/test_r2d2_opencode_config.py`, and a comment that a
parser rejects would mean opencode silently loads a config with no agents — the
fast voice path would then have no agent to address. Everything a future editor
needs to know is here instead.

## C2 — the config dir merges, it does not isolate

Measured in `docs/11-opencode-contract.md` (U1): with `OPENCODE_CONFIG_DIR`
set, 11 foreign agents (`Sisyphus`, `oracle`, `prd-maker`, `task-clarifier`,
…) and 15 foreign providers from the owner's global `opencode.json` and the
`oh-my-openagent` plugin are **still loaded and visible**. Two consequences,
both of which this file is built around:

1. **Address agents by exact name.** `r2d2-voice` and `r2d2-agent` are the only
   names R2D2 may send in the `agent` field. Never "the last definition", never
   a prefix, never "the primary agent" — with 13 agents visible a positional or
   fuzzy pick is a coin flip that lands on `oracle`. The Python side already
   sends exact names from `app/config.py` (`r2d2_voice_agent`, `r2d2_task_agent`);
   this file must keep matching them, which
   `test_the_allowlisted_shim_matches_the_app_configuration` enforces.

2. **A missing rule is not a denial — it is an inheritance.** opencode applies
   "last matching rule wins", the owner's global rules sit *below* this file's,
   and they are not cancelled by a `"*"` here. So a `permission` block that
   omits a key leaves that key to the global config, where it may well be
   `allow`. Therefore:

   - `"*"` is the **first** key of every permission block, so the specific
     rules that follow it are the last match and win.
   - **Every** permission key opencode exposes is named explicitly in **both**
     agents. Nothing falls through, so no change to the owner's global
     `opencode.json` can widen what `r2d2-voice` or `r2d2-agent` may do.
   - The top-level block denies by default too, which also covers the 11
     foreign agents inside R2D2's own process.

`test_both_agents_decide_every_permission_key_so_nothing_is_inherited` is the
guard for all of this, and `permission_violations()` is the function to extend
if opencode grows a new permission key.

## C1 — one model, because it is the only one that works

`opencode/space-bunny-free` is the only model `opencode serve` accepts on this
machine: every other free Zen model returns HTTP 200 with an inner 403
`FreeTierError`, and paid models return an inner 402 because the Zen balance is
empty. So the voice path, the task path and summarisation all use this one id,
and the test asserts no other model id appears anywhere in the file. Funding
Zen later changes three strings in `config/backends.json` and nothing here.

## C4 — assert on `permission`, never on `tools`

In `GET /agent` the `tools` field reads back `null`, and a declared
`tools: {"bash": false}` is normalised into `permission` rules. `tools` cannot
be verified against a live server, so neither agent declares it — the plan's
draft `"tools": {"r2d2_do": true}` named a tool that does not exist, which reads
as "the shim is enabled" while enabling nothing.

## The permission matrix

| key | `r2d2-voice` | `r2d2-agent` |
|---|---|---|
| `*` | `deny` | `ask` |
| `read` | allow except `*.env` | allow except `*.env` |
| `glob`, `grep` | allow | allow |
| `bash` | deny, except the 12 allowlisted commands | ask, except the 12 allowlisted commands |
| `edit` | `deny` | `ask` |
| `task`, `skill`, `lsp`, `question` | `deny` | `ask` |
| `webfetch`, `websearch` | `deny` | allow |
| `external_directory` | `deny` | `ask` |
| `doom_loop` | `deny` | `ask` |

`bash`, `edit` and `external_directory` are never `allow` on either agent, and
`test_guard_rejects_a_config_that_widens_an_agent` fails the moment one is.

### The 12 allowlisted commands

The shim at `/home/koluchiy/.r2d2/r2d2_do.py` re-execs itself under the repo
venv, so a model may spell the same tool three ways and all three must resolve:

```
/home/koluchiy/.r2d2/r2d2_do.py *
python3 /home/koluchiy/.r2d2/r2d2_do.py *
/home/koluchiy/Documents/R2_yandex_station/.venv/bin/python /home/koluchiy/.r2d2/r2d2_do.py *
```

plus nine read-only status probes: `upower *`, `cat /sys/class/power_supply/*`,
`df *`, `free *`, `uname *`, `hostname *`, `ps *`, `uptime`, `date`.

**The trust boundary this creates.** `r2d2_do.py shell <command>` is inside
that allowlist, so it does not raise an opencode permission prompt. Two things
stand in front of it, and neither is opencode:

1. `r2d2-voice` cannot reach it — three `deny` rules for the `shell` subcommand
   come last in the voice `bash` block, its prompt forbids it outright, and
   such a request is escalated with the `[[NEEDS_AGENT]]` sentinel instead.
2. `r2d2-agent` can, and the shim's own `risk_level` gate is the gate: it exits
   2 without executing, the prompt makes the agent relay the question verbatim,
   and the command runs only after the user agrees. R2D2's permission broker
   (todo 14) never sends `always` for a bash rule (C5) — one `always` would
   grant that command mask permanently.

If the shim's subcommands ever change, update the allowlist and
`READ_ONLY_PROBES` in the test together.
