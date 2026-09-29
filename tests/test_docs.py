"""The docs must describe the code that exists, not the code that was deleted.

The repository was rewritten from "an Alice skill that calls an LLM API" into
"a voice gateway over the owner's own opencode agent".  Ten planning documents
were written against the OLD architecture and kept their details, so they now
mislead in ways that are invisible until an operator acts on them: a `providers/`
box that no longer exists, a model recommendation that returns 429, a roadmap
whose stage 4 promises an implementation the repo took a different way.

So every assertion here is a CROSS-CHECK between a document and the artifact it
describes.  Three properties make the check worth more than the doc:

* **The claim is parsed, not pattern-matched.**  An endpoint table row, a
  permission cell and a fence header are each read into a structure, and the
  structure is compared against the real config, the real client or the real
  file.  A doc that merely mentions `deny` somewhere passes nothing.
* **The direction is both ways.**  Every endpoint the doc names must exist in
  `core/opencode/client.py`, AND every route that client sends must be in the
  table.  A new route with no documentation is as stale as a documented route
  that was deleted, and only the second direction catches it.
* **Every guard has a mutation test that fails.**  Each pure checker below is
  fed a deliberately broken document or a deliberately broken config in
  `test_guard_rejects_*`, so a checker that cannot fail is a failing test in
  this file rather than a silent hole in the suite.

Nothing here reads a credential into a document: the secret-hygiene test reads
the operator's `.env`/`.env.oc` at RUN time, asserts their values are absent
from every doc, and reports only the VARIABLE NAME when it fails.

allow: SIZE_OK -- 30 tests over 8 documents and 6 artifacts.  The checkers are
pure functions precisely so the mutation tests can call them directly, which is
the only honest way to show a doc guard is not vacuous; splitting them across
files would give each one half of a consistency rule and no way to see the whole
of it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS = REPO_ROOT / "docs"
README = REPO_ROOT / "README.md"

BACKEND_DOC = DOCS / "11-opencode-backend.md"
CONTRACT_DOC = DOCS / "11-opencode-contract.md"
SECURITY_DOC = DOCS / "09-security.md"
ROADMAP_DOC = DOCS / "10-roadmap.md"
ARCHITECTURE_DOC = DOCS / "03-architecture.md"
PROVIDER_DOC = DOCS / "05-llm-provider.md"
TOOLS_DOC = DOCS / "06-tools.md"
LATENCY_DOC = DOCS / "07-latency-strategy.md"
DEPLOYMENT_DOC = DOCS / "08-deployment.md"

#: Every document this test is allowed to hold responsible.
DOC_FILES = (README, *sorted(DOCS.glob("*.md")))

BACKENDS_JSON = REPO_ROOT / "config" / "backends.json"
OC_CONFIG = REPO_ROOT / "config" / "opencode" / "r2d2.opencode.json"
CLIENT_PY = REPO_ROOT / "core" / "opencode" / "client.py"
SSE_PY = REPO_ROOT / "core" / "opencode" / "sse.py"

#: `provider/model`, with no URL in front of it -- a dotted host has the same
#: shape, so `https://...` is excluded explicitly, and so is anything following
#: a brace, which makes `${PASSWORD//...}` a shell expansion rather than a model.
#: A token that names a file in this repo is a PATH, not a model id:
#: `core/opencode/client.py` and `config/opencode/r2d2.opencode.json` have the
#: same shape as `opencode/space-bunny-free` and would otherwise be reported as
#: unmeasured models.
MODEL_TOKEN = re.compile(r"(?<![:/.\w{}])[A-Za-z][A-Za-z0-9._-]*/[A-Za-z0-9._:/-]+")
#: A token beginning with `/` inside backticks, i.e. a path a reader would type.
BACKTICK_PATH = re.compile(r"`(/[^`\s]+)`")
#: Absolute paths on this machine.  They start with `/` too, and they are not
#: HTTP routes, so they are separated by PREFIX rather than by "does it exist":
#: a doc must be able to name a workspace that has not been created yet.
FS_PREFIXES = (
    "/home/", "/tmp/", "/etc/", "/usr/", "/opt/", "/var/", "/sys/",
    "/proc/", "/dev/", "/root/", "/bin/", "/srv/", "/run/", "//",
)
#: Every `_request(...)` call in the client: the verb and the path template it
#: sends.  This is the authoritative route list -- not a hand-kept copy.
REQUEST_CALL = re.compile(r'_request\(\s*"(?P<verb>GET|POST|DELETE)",\s*f?"(?P<path>[^"]+)"')
#: Fences, with their info string, so a `verbatim` block can name its origin.
FENCE = re.compile(r"^```(?P<lang>[^\n`]*)\n(?P<body>.*?)^```", re.M | re.S)
#: Fence languages that are legitimately NOT committed source: an operator's
#: command line, a diagram, a shell session.  Anything else -- `python`,
#: `json`, an empty info string -- is a paraphrase wearing a code fence, and
#: that is exactly what the verbatim rule exists to prevent.
NON_SOURCE_LANGS = frozenset({"text", "dotenv", "console", "bash"})
#: Model ids the spike proved unusable through `opencode serve` (C1).  Anchored
#: to the contract document below, so a change of evidence fails the test.
REFUSED_MODELS = (
    "muse-spark-1.3-contributor-free",
    "ling-3.0-flash-fin-free",
    "mimo-v2.6-flash-free",
    "nemotron-3-ultra-free",
    "nemotron-3.5-lightning-free",
)
#: A real-looking OpenRouter key.  `sk-or-...` as an ellipsis is allowed; 4+ real
#: characters after the prefix is a credential in a document.
LIVE_KEY = re.compile(r"sk-or-[A-Za-z0-9]{4,}")
#: The minimum a value must have before "it appears in the docs" means anything.
MIN_SECRET_CHARS = 8
#: What makes an env variable a CREDENTIAL.  By name, not by a list of this
#: project's variables: a doc is allowed to name `yandexgpt-lite-5` and
#: `https://openrouter.ai/api/v1` -- those are configuration, and hiding them
#: behind placeholders would make the docs less useful, not safer.  What must
#: never appear is the value of a key, a password or a token.
CREDENTIAL_NAME = re.compile(r"(?:KEY|PASSWORD|TOKEN|SECRET)$")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def backends(path: Path = BACKENDS_JSON) -> dict:
    return json.loads(read(path))


def opencode_config(path: Path = OC_CONFIG) -> dict:
    return json.loads(read(path))


def placeholder_pattern(documented: str) -> re.Pattern[str]:
    """A documented path as a matcher for whatever the source calls the ids.

    `/session/{session_id}/message` must match the source's
    `f"/session/{session_id}/message"` -- and also a source that binds a
    different local name to the same position.  A route that does not exist at
    all still matches nothing, which is the point.
    """
    escaped = re.escape(documented).replace(r"\{", "{").replace(r"\}", "}")
    return re.compile(re.sub(r"\{[^{}]*\}", r"\{[^}]*\}", escaped))


def wire_routes() -> list[tuple[str, str]]:
    """Every route the opencode client sends, plus the one only SSE reads."""
    routes = [
        (m.group("verb"), m.group("path")) for m in REQUEST_CALL.finditer(read(CLIENT_PY))
    ]
    return [*routes, ("GET", "/event")]


def documented_endpoints(text: str) -> list[tuple[str, str]]:
    """(verb, path) claimed by every table row of the document."""
    claims: list[tuple[str, str]] = []
    for row in table_rows(text):
        verb = next((c for c in row if re.fullmatch(r"`(?:GET|POST|DELETE)`", c)), None)
        if verb is None:
            continue
        for cell in row:
            for token in BACKTICK_PATH.findall(cell):
                if not token.startswith(FS_PREFIXES):
                    claims.append((verb.strip("`"), token))
    return claims


def table_rows(text: str) -> list[list[str]]:
    """Every markdown table row, minus the `|---|` separators."""
    rows = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if all(cell and set(cell) <= set("-: ") for cell in cells):
            continue
        rows.append(cells)
    return rows


def code_blocks(text: str) -> list[tuple[str, str]]:
    return [(m.group("lang").strip(), m.group("body")) for m in FENCE.finditer(text)]


def rstripped(text: str) -> str:
    """Trailing whitespace made invisible; nothing else is forgiven."""
    return "\n".join(line.rstrip() for line in text.splitlines())


def core_modules() -> set[str]:
    """Every module under `core/`, as the docs name them."""
    return {
        path.relative_to(REPO_ROOT).as_posix()
        for path in (REPO_ROOT / "core").rglob("*.py")
        if path.name != "__init__.py"
    }


SIZE_MARKER: Final = re.compile(r"allow: SIZE_OK -- (\d+) pure LOC")
def size_marker_violations(root: Path | None = None) -> list[str]:
    """Every `allow: SIZE_OK -- N pure LOC` whose N is not the file's own size.

    The marker is a claim about a number, and a hand-written number drifts: four of
    the eight in this repository were wrong when this checker was added, by -24 to
    +19 lines, in both directions. A ceiling that is granted on the strength of a
    number nobody recomputes is not a ceiling, it is a comment -- so the number is
    derived here from the file itself, under the same definition the modules quote
    (non-blank lines, comment-only lines removed, docstrings counted, which is what
    reproduces `app/main.py = 167`).

    A marker that states a size some other way -- `test_e2e_stack.py` says "pure
    LOC is over the 250 ceiling" without a figure, and `test_docs.py` counts tests
    rather than lines -- is not matched here and not counted against anyone. `root`
    is a parameter so a test can point the checker at a tree it built.
    """
    base = root or REPO_ROOT
    violations: list[str] = []
    for path in sorted(base.rglob("*.py")):
        if any(part in {".venv", "__pycache__", "db"} for part in path.parts):
            continue
        text = path.read_text(encoding="utf-8")
        claimed = SIZE_MARKER.search(text)
        if claimed is None:
            continue
        actual = len([
            line for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ])
        if int(claimed.group(1)) != actual:
            violations.append(
                f"{path.relative_to(base)}: marker says {claimed.group(1)}, "
                f"the file is {actual}"
            )
    return violations


def test_every_size_marker_states_the_size_its_file_actually_is():
    # Given/When: every module that grants itself an exception to the LOC ceiling
    # Then: the number it excuses itself with is the number it is
    assert size_marker_violations() == []


def test_the_size_checker_names_a_marker_that_drifted(tmp_path):
    # The marker is assembled rather than written out, so this test's own source
    # does not contain a claim the checker would then hold this file to.
    claim = "allow: SIZE_OK -- {n}" + " pure LOC, over the ceiling because.\n"
    body = '"""\n{claim}stays.\n"""\nx = 1\ny = 2\n'

    def counted(text: str) -> int:
        return len([
            line for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ])

    # Counted with a real claim in place, not an empty one: the claim occupies a
    # line of its own, and a blank placeholder would not be counted while the
    # filled-in line is, which is a difference of exactly one.
    real = counted(body.format(claim=claim.format(n=0)))
    (tmp_path / "honest.py").write_text(body.format(claim=claim.format(n=real)), encoding="utf-8")
    (tmp_path / "stale.py").write_text(
        body.format(claim=claim.format(n=real + 1)), encoding="utf-8"
    )
    # When: the same checker that guards this repository is pointed at it
    violations = size_marker_violations(tmp_path)
    # Then: only the drifted one is named, with both numbers in the message
    assert len(violations) == 1, violations
    assert violations[0].startswith(f"stale.py: marker says {real + 1}, the file is {real}")




def configured_models(config: dict | None = None) -> set[str]:
    """The model strings `config/backends.json` actually sends."""
    document = backends() if config is None else config
    slots = ("model", "fast_model", "task_model", "summarize_model")
    return {
        entry[slot]
        for entry in document["backends"]
        for slot in slots
        if isinstance(entry.get(slot), str) and entry[slot]
    }


# ---------------------------------------------------------------------------
# checkers -- pure, so a mutation test can call them on a broken document
# ---------------------------------------------------------------------------


def endpoint_violations(text: str, routes: list[tuple[str, str]]) -> list[str]:
    """Documented routes that the client does not send, with the wrong verb."""
    bad = []
    for verb, path in documented_endpoints(text):
        matches = [r for r in routes if placeholder_pattern(path).fullmatch(r[1])]
        if not matches:
            bad.append(f"{verb} {path} is documented but no request sends it")
        elif verb not in {r[0] for r in matches}:
            bad.append(f"{path} is documented as {verb}, the client sends {sorted({r[0] for r in matches})}")
    return bad


def uncovered_routes(text: str, routes: list[tuple[str, str]]) -> list[str]:
    """Routes the client sends that the document never mentions."""
    claimed = {path for _verb, path in documented_endpoints(text)}
    return [f"{verb} {path}" for verb, path in routes if not any(
        placeholder_pattern(claim).fullmatch(path) for claim in claimed
    )]


def model_violations(text: str, configured: set[str], refused: set[str]) -> list[str]:
    """`provider/model` tokens the configuration does not explain.

    A documented token is compared on its model id, not on the whole
    `provider/id` string, because the registry names models two ways: the
    contract document lists bare ids, the config lists them prefixed.  A token
    whose TAIL is unmeasured is still unmeasured, so this widens the vocabulary,
    not the permission.
    """
    known = {m.rsplit("/", 1)[-1] for m in configured | refused}
    return [
        f"model {model!r} is neither configured in config/backends.json nor a measured refusal"
        for model in sorted(set(models_in(text)))
        if model.rsplit("/", 1)[-1] not in known
    ]


def models_in(text: str) -> list[str]:
    """Model-shaped tokens, minus the ones that name a real file in this repo."""
    return [
        token
        for token in MODEL_TOKEN.findall(text)
        if not (REPO_ROOT / token).exists()
    ]


def fence_violations(text: str, name: str) -> list[str]:
    """Blocks that are not a verbatim excerpt of a committed file."""
    bad = []
    for lang, body in code_blocks(text):
        if not lang.startswith("verbatim"):
            if lang not in NON_SOURCE_LANGS:
                bad.append(
                    f"{name}: a `{lang or 'unlabelled'}` block is not committed source; tag a "
                    f"verbatim excerpt as ```verbatim <path> or use one of {sorted(NON_SOURCE_LANGS)}"
                )
            continue
        origin = lang.split(maxsplit=1)[1] if len(lang.split()) > 1 else ""
        source = REPO_ROOT / origin
        if not origin or not source.is_file():
            bad.append(f"{name}: a verbatim block names {origin!r}, which is not a file in this repo")
        elif rstripped(body).strip("\n") not in rstripped(read(source)):
            bad.append(f"{name}: the block tagged `verbatim {origin}` is a paraphrase, not an excerpt")
    return bad


AGENTS = ("r2d2-voice", "r2d2-agent")


def permission_claims(text: str) -> list[tuple[str, str, str]]:
    """(agent, permission key, value) as `docs/09-security.md` states them.

    The table is found by its header -- the first row carrying a `` `*` `` cell --
    and every later row of the same width whose first cell names one of the two
    agents is read against it.  A cell states its value inside a paragraph of
    prose, so the first `allow`/`ask`/`deny` in backticks is the claim, and a
    cell with none claims nothing.
    """
    rows = table_rows(text)
    header = next((row for row in rows if any(c.strip("`") == "*" for c in row)), None)
    if header is None:
        return []
    keys = [cell.strip("`") for cell in header[1:]]
    claims: list[tuple[str, str, str]] = []
    for row in rows:
        if row[0].strip("`") not in AGENTS or len(row) != len(header):
            continue
        for key, cell in zip(keys, row[1:], strict=True):
            value = re.search(r"`(allow|ask|deny)`", cell)
            if value is not None:
                claims.append((row[0].strip("`"), key, value.group(1)))
    return claims


def permission_violations(text: str, config: dict) -> list[str]:
    """Permission values the document states that the opencode config denies."""
    bad = []
    for agent, key, claimed in permission_claims(text):
        block = config.get("agent", {}).get(agent, {}).get("permission", {})
        actual = block.get(key)
        if isinstance(actual, dict):
            actual = actual.get("*")
        if actual is None:
            bad.append(f"docs/09: {agent} has no permission key {key!r} at all")
        elif actual != claimed:
            bad.append(f"docs/09: {agent}.{key} is documented as {claimed!r}, the config says {actual!r}")
    return bad


def roadmap_violations(text: str) -> list[str]:
    """Superseded stages still presented as pending, or a missing stage 11."""
    bad = []
    sections = re.split(r"^## ", text, flags=re.M)
    stages: dict[int, str] = {}
    for section in sections[1:]:
        title, _, body = section.partition("\n")
        match = re.search(r"Этап (\d+)", title)
        if match:
            stages[int(match.group(1))] = title + "\n" + body
    for number in (4, 5):
        if number not in stages:
            bad.append(f"docs/10: stage {number} was removed instead of being marked superseded")
        elif "superseded" not in stages[number].splitlines()[0].lower():
            bad.append(f"docs/10: stage {number} heading does not say it is superseded")
        elif re.search(r"^- \[ \]", stages[number], flags=re.M):
            bad.append(f"docs/10: stage {number} is superseded yet still lists pending checkboxes")
    if 11 not in stages:
        bad.append("docs/10: there is no stage 11 for the opencode rewrite")
    return bad


def secret_violations(text: str, secrets: dict[str, str], name: str) -> list[str]:
    """Operator credentials found in a document. Only the NAME is reported."""
    return [
        f"{name} contains the value of ${variable}" for variable, value in secrets.items()
        if value in text
    ]


def env_secrets() -> dict[str, str]:
    """`KEY=VALUE` pairs from the operator's real env files, values only.

    Read at run time and never written anywhere: the test exists to prove the
    docs carry no credential, which is impossible to prove without the values.
    """
    secrets: dict[str, str] = {}
    for name in (".env", ".env.oc"):
        path = REPO_ROOT / name
        if not path.is_file():
            continue
        for line in read(path).splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            variable, _, value = line.partition("=")
            value = value.strip().strip('"').strip("'")
            if CREDENTIAL_NAME.search(variable.strip()) and len(value) >= MIN_SECRET_CHARS:
                secrets[variable.strip()] = value
    return secrets


# ---------------------------------------------------------------------------
# 1. the endpoint table is the client's route list, both ways
# ---------------------------------------------------------------------------


def test_every_documented_endpoint_is_one_the_client_sends():
    # Given: the new backend document and the routes the client actually sends
    text = read(BACKEND_DOC)
    routes = wire_routes()
    # When: each table row claiming a route is checked against the client
    # Then: no documented route is invented, and no route is called with the
    # wrong verb -- a table that drifted from the code is worse than no table
    assert endpoint_violations(text, routes) == []
    assert len(documented_endpoints(text)) >= 12, "the endpoint table lost rows"


def test_every_route_the_client_sends_is_documented():
    # Given: a route added to `OpencodeClient` after the doc was written
    routes = wire_routes()
    # When: the document's table is checked for coverage
    # Then: the client sends nothing the operator cannot read about here
    assert uncovered_routes(read(BACKEND_DOC), routes) == []


@pytest.mark.parametrize("bogus", ["/session/{session_id}/messag", "/session/{id}/frobnicate"])
def test_guard_rejects_a_documented_route_the_client_does_not_send(bogus):
    # Given: a document that invents a route
    text = read(BACKEND_DOC).replace("`/session/{session_id}/message`", f"`{bogus}`", 1)
    # When/Then: the invented route is named, not quietly accepted
    violations = endpoint_violations(text, wire_routes())
    assert violations, f"{bogus} passed as a real route"
    assert any(bogus in reason for reason in violations)


def test_guard_rejects_a_route_the_client_sends_with_another_verb():
    # Given: a document that calls the health probe a POST. The route is
    # deliberately one the client sends with a SINGLE verb -- `/session` is
    # sent both ways, so flipping that row would be a correct document
    text = read(BACKEND_DOC)
    row = "| `GET` | `/global/health` |"
    assert row in text, "the health row is not canonically spaced, so the mutation is a no-op"
    # When: the verb is rewritten
    violations = endpoint_violations(text.replace(row, "| `POST` | `/global/health` |"), wire_routes())
    # Then: the verb mismatch is caught
    assert any("documented as POST" in reason for reason in violations), violations


def test_guard_rejects_a_document_that_stops_documenting_a_route():
    # Given: a table with one row removed
    routes = wire_routes()
    line = next(
        ln for ln in read(BACKEND_DOC).splitlines() if "`/session/{session_id}/summarize`" in ln
    )
    # When/Then: coverage is enforced in the other direction too
    assert uncovered_routes(read(BACKEND_DOC).replace(line, ""), routes)


# ---------------------------------------------------------------------------
# 2. every model id the document names is explained by the configuration
# ---------------------------------------------------------------------------


def test_every_model_id_in_the_backend_doc_is_configured_or_measured_refused():
    # Given: the backend registry and the refusal sweep
    configured = configured_models()
    refused = set(REFUSED_MODELS)
    contract = read(CONTRACT_DOC)
    for model in REFUSED_MODELS:
        assert model in contract, f"{model} is not recorded in docs/11-opencode-contract.md"
        assert model not in configured, f"{model} is refused by the server AND configured"
    # When: every `provider/model` token in the document is classified
    # Then: none is a model nobody measured and nobody configured
    assert model_violations(read(BACKEND_DOC), configured, refused) == []


def test_the_backend_doc_states_the_configured_models_verbatim():
    # Given: the three model slots the opencode backend sends
    config = backends()
    entry = next(b for b in config["backends"] if b["name"] == "opencode")
    text = read(BACKEND_DOC)
    # When/Then: each configured id is quoted exactly, so changing the config
    # without updating the doc is a red test rather than a stale sentence
    for slot in ("fast_model", "task_model", "summarize_model"):
        assert entry[slot] in text, f"{slot}={entry[slot]!r} is not in the document"


def test_the_refused_models_are_documented_as_refused():
    # Given: a document that lists a refused model without saying it fails
    text = read(BACKEND_DOC)
    # When/Then: each refusal is stated with its measured inner status, which
    # is what makes the list a reason rather than a scare
    for model in REFUSED_MODELS:
        row = next((r for r in table_rows(text) if model in " ".join(r)), None)
        assert row is not None, f"{model} is not in a table row"
        assert re.search(r"40[23]", " ".join(row)), f"{model} row states no refusal: {row}"


def test_guard_rejects_a_model_id_nobody_configured_or_measured():
    # Given: a document recommending a model that was never tested
    text = read(BACKEND_DOC) + "\n`opencode/some-model-nobody-measured`\n"
    # When/Then: an unmeasured model is refused as a recommendation
    violations = model_violations(text, configured_models(), set(REFUSED_MODELS))
    assert any("some-model-nobody-measured" in reason for reason in violations)


# ---------------------------------------------------------------------------
# 3. every code block in the new document is a verbatim excerpt
# ---------------------------------------------------------------------------


def test_every_code_block_is_a_verbatim_excerpt_or_a_declared_non_source_block():
    # Given: the new backend document
    text = read(BACKEND_DOC)
    # When/Then: no block is a paraphrase wearing a code fence
    assert fence_violations(text, "docs/11-opencode-backend.md") == []
    # and the document actually quotes the source rather than describing it
    verbatim = [lang for lang, _body in code_blocks(text) if lang.startswith("verbatim")]
    assert len(verbatim) >= 8, f"only {len(verbatim)} verbatim excerpts; the doc is prose"


def test_guard_rejects_a_paraphrased_excerpt():
    # Given: one character changed inside a block tagged as an excerpt
    text = read(BACKEND_DOC)
    lang, body = next(
        (lang, body) for lang, body in code_blocks(text) if lang.startswith("verbatim")
    )
    broken = text.replace(body, body.replace(":", ";", 1))
    # When/Then: a block that is no longer in the file is refused
    violations = fence_violations(broken, "docs/11-opencode-backend.md")
    assert any(lang in reason and "paraphrase" in reason for reason in violations)


def test_guard_rejects_an_untagged_source_block():
    # Given: an excerpt pasted without naming where it came from
    lang, body = next(
        (lang, body)
        for lang, body in code_blocks(read(BACKEND_DOC))
        if lang.startswith("verbatim")
    )
    text = read(BACKEND_DOC).replace(f"```{lang}\n{body}", f"```python\n{body}")
    # When/Then: a `python` fence is a claim of source with nothing behind it
    assert fence_violations(text, "docs/11-opencode-backend.md")


def test_guard_rejects_a_verbatim_block_naming_a_file_that_does_not_exist():
    # Given: a block claiming to quote a module that is not in this repo
    text = read(BACKEND_DOC) + "\n```verbatim core/opencode/imaginary.py\nx = 1\n```\n"
    # When/Then: an invented origin is named, not skipped
    violations = fence_violations(text, "docs/11-opencode-backend.md")
    assert any("imaginary.py" in reason for reason in violations)


# ---------------------------------------------------------------------------
# 4. the two agents and their permission matrices, quoted from the config
# ---------------------------------------------------------------------------


def test_security_doc_permission_matrix_matches_the_opencode_config():
    # Given: `config/opencode/r2d2.opencode.json`, the source of truth
    config = opencode_config()
    # When/Then: every permission value docs/09 states is the one the file has
    assert permission_violations(read(SECURITY_DOC), config) == []
    claims = permission_claims(read(SECURITY_DOC))
    assert len(claims) >= 8, f"only {len(claims)} permission values are stated; the matrix is thin"
    for agent in ("r2d2-voice", "r2d2-agent"):
        assert {a for a, _k, _v in claims} >= {agent}, f"{agent} is not in the matrix"


@pytest.mark.parametrize(
    ("agent", "key", "claimed"),
    [
        ("r2d2-voice", "*", "ask"),
        ("r2d2-voice", "edit", "allow"),
        ("r2d2-agent", "*", "deny"),
        ("r2d2-agent", "webfetch", "ask"),
        ("r2d2-agent", "bash", "allow"),
    ],
)
def test_guard_rejects_a_permission_value_the_config_does_not_have(agent, key, claimed):
    # Given: a document that states a value the config does not have
    config = opencode_config()
    text = read(SECURITY_DOC)
    assert permission_violations(text, config) == [], "the shipped doc is already wrong"
    header = next(row for row in table_rows(text) if any(c.strip("`") == "*" for c in row))
    column = header.index(f"`{key}`")
    row = next(r for r in table_rows(text) if r[0].strip("`") == agent and len(r) == len(header))
    # When: the cell is rewritten to the wrong value
    marker = "| " + " | ".join(row) + " |"
    assert marker in text, f"the row is not canonically spaced, so the mutation is a no-op: {row}"
    wrong = [f"`{claimed}`" if index == column else cell for index, cell in enumerate(row)]
    mutated = text.replace(marker, "| " + " | ".join(wrong) + " |", 1)
    # Then: the wrong value is named, with the agent, the key and both values
    violations = permission_violations(mutated, config)
    assert any(f"{agent}.{key}" in reason for reason in violations), violations


def test_security_doc_states_the_permission_broker_invariants():
    # Given: the five facts an operator needs before trusting the broker
    from app.config import Config

    text = read(SECURITY_DOC)
    # When/Then: loopback binding and the server password are named
    assert "OPENCODE_SERVER_PASSWORD" in text
    assert "127.0.0.1" in text
    # the durable grant is named as the thing that is never sent
    assert '"always"' in text or "`always`" in text
    # and the reject-on-timeout window is the configured one, not a remembered one
    assert str(int(Config().r2d2_permission_timeout)) in text
    # the granular allowlist and the untouched global config are both named
    assert "r2d2_do" in text
    assert "opencode.json" in text


# ---------------------------------------------------------------------------
# 5. the sentinel protocol and the three EVENT_MODE values
# ---------------------------------------------------------------------------


def test_backend_doc_states_the_sentinel_and_the_three_guards():
    # Given: the routing module and the configured sentinel
    from app.config import Config

    text = read(BACKEND_DOC)
    routing_source = read(REPO_ROOT / "core" / "routing.py")
    # When/Then: the exact literal the router dispatches on is documented
    assert Config().r2d2_needs_agent_sentinel in text
    # and the two guards that keep it out of a speaker are named, and exist
    for guard in ("parse_voice_reply", "sanitize_for_speech"):
        assert guard in text, f"{guard} is not documented"
        assert f"def {guard}(" in routing_source, f"{guard} is not in core/routing.py"
    # and the third guard is described by what makes it different: it runs
    # before the cleaner that would have dismantled the sentinel
    assert "render.clean" in text or "_speakable" in text


def test_backend_doc_documents_every_event_mode_value():
    # Given: the typed mode vocabulary, straight from the module
    from typing import get_args

    from core.opencode.sse import EVENT_MODE, EventMode

    modes = get_args(EventMode)
    assert modes == ("sse", "poll", "deny")
    text = read(BACKEND_DOC)
    # When/Then: each value is documented, and the live one is named as live
    for mode in modes:
        assert f"`{mode}`" in text, f"EVENT_MODE {mode!r} is not documented"
    assert f"`{EVENT_MODE}`" in text
    assert "event_mode" in text.lower()


def test_backend_doc_states_the_measured_latencies():
    # Given: the spike evidence, parsed rather than retyped
    contract = read(CONTRACT_DOC)
    measured = {
        "p50": re.search(r"p50\D{0,8}(\d+\.\d+)", contract),
        "p95": re.search(r"p95\D{0,8}(\d+\.\d+)", contract),
    }
    text = read(BACKEND_DOC)
    # When/Then: the same figures appear in the document, and the cold-session
    # cost is stated, because it is the reason a first turn goes async
    for label, match in measured.items():
        assert match is not None, f"{label} is not in the contract document"
        assert match.group(1) in text, f"{label} {match.group(1)} is not in the backend document"
    for cold in ("15.5", "18.6"):
        assert cold in contract and cold in text, f"the cold first message ({cold} s) is missing"


# ---------------------------------------------------------------------------
# 6. the fallback chain and the two one-line recipes
# ---------------------------------------------------------------------------


def test_backend_doc_describes_the_configured_fallback_chain_in_order():
    # Given: the registry's own order
    config = backends()
    order = config["chain"]
    rows = table_rows(read(BACKEND_DOC))
    # When/Then: the document lists exactly the declared backends, in order
    documented = [row[0].strip("`") for row in rows if row[0].strip("`") in order]
    assert documented == list(order), f"documented {documented}, configured {list(order)}"
    # and the session backend is named as the ROUTE, not a chain member: the
    # brain removes it before walking the chain
    assert "opencode_session" in read(BACKEND_DOC)


def brace_blocks(text: str) -> list[str]:
    """Every top-level `{...}` region, with brace nesting counted.

    A regex cannot do this: the provider recipe and the shipped registry entries
    both carry `${VAR}` inside an object, so `[^{}]*` cannot reach across one, and
    a multi-line entry needs a real depth counter anyway.
    """
    blocks: list[str] = []
    depth, start = 0, 0
    for index, char in enumerate(text):
        if char == "{":
            depth += 1
            start = index if depth == 1 else start
        elif char == "}" and depth:
            depth -= 1
            if depth == 0:
                blocks.append(text[start : index + 1])
    return blocks


def provider_recipes(text: str) -> list[dict]:
    """Every brace region that parses as a backend spec: a name and a kind."""
    recipes = []
    for block in brace_blocks(text):
        try:
            parsed = json.loads(block)
        except ValueError:
            continue
        if isinstance(parsed, dict) and "kind" in parsed and "name" in parsed:
            recipes.append(parsed)
    return recipes


def test_backend_doc_recipes_use_only_real_kinds_and_real_slots():
    # Given: the kinds the registry can build and the slots the model gate reads
    from core.backends.config_loader import VALID_KINDS
    from core.opencode.models import MODEL_ROLES

    text = read(BACKEND_DOC)
    recipes = provider_recipes(text)
    # When/Then: the "add a provider" recipe names a kind that really exists
    assert recipes, "no provider recipe to check"
    assert [r["kind"] for r in recipes if r["kind"] in VALID_KINDS], recipes
    for recipe in recipes:
        if not recipe["name"].startswith("${"):
            assert recipe["kind"] in VALID_KINDS, f"{recipe['kind']!r} is not a registered kind"
    # and the "change the model" recipe names slots the gate actually checks
    for role in MODEL_ROLES:
        assert role in text, f"the model recipe does not mention {role}"


def test_adding_a_provider_is_six_lines_of_json():
    # Given: the claim in the document that a provider costs six lines
    six = {"name", "kind", "base_url", "api_key", "model", "auth_style"}
    complete = [r for r in provider_recipes(read(BACKEND_DOC)) if set(r) == six]
    # When/Then: a recipe with exactly the six fields exists, and every one of
    # them is a real `BackendSpec` field -- otherwise "six lines" is a promise
    # the loader would reject
    from dataclasses import fields as dataclass_fields

    from core.backends.config_loader import BackendSpec

    known = {f.name for f in dataclass_fields(BackendSpec)}
    assert complete, f"no six-field recipe; found {[sorted(r) for r in provider_recipes(read(BACKEND_DOC))]}"
    assert six <= known, f"the recipe invents fields: {sorted(six - known)}"


def test_changing_a_model_is_one_line_per_slot():
    # Given: the three model slots the opencode backend sends
    from core.opencode.models import MODEL_ROLES

    entry = next(b for b in backends()["backends"] if b["name"] == "opencode")
    rows = table_rows(read(BACKEND_DOC))
    # When/Then: for each slot there is a TABLE ROW naming the slot, the value in
    # effect and the file to edit -- which is what "change the model in one
    # line" has to mean for a reader who has never opened the config
    for slot in MODEL_ROLES:
        recipe = [
            row for row in rows
            if slot in row[0] and entry[slot] in " ".join(row) and "config/backends.json" in " ".join(row)
        ]
        assert len(recipe) == 1, f"{slot} has {len(recipe)} one-line recipes, expected 1: {recipe}"


# ---------------------------------------------------------------------------
# 7. no document may present the deleted package as live
# ---------------------------------------------------------------------------


#: The deleted package, assembled rather than written out: `tests/
#: test_backend_registry.py::test_no_python_file_outside_this_test_references_
#: core_providers` forbids this exact string in every `.py` file except its own,
#: so a literal here fails a test that has nothing to do with the documents.
DEAD_PACKAGE = "core" + "." + "providers"
DEAD_PATHS = (
    DEAD_PACKAGE,
    DEAD_PACKAGE.replace(".", "/"),
    "providers/openrouter",
    "providers/yandexgpt",
    "providers/factory",
    "providers/base.py",
)


@pytest.mark.parametrize("path", DOC_FILES, ids=lambda p: p.name)
def test_no_document_presents_the_deleted_providers_package_as_live(path):
    # Given: a document describing the architecture
    text = read(path)
    # When/Then: the package that was deleted is not named as a live import,
    # not even in a "modules" listing a reader would go looking in
    for dead in DEAD_PATHS:
        assert dead not in text, f"{path.name} still names {dead}"


def test_architecture_doc_lists_the_packages_that_exist():
    # Given: the three packages the repo actually has under `core/`
    packages = {
        path.name for path in (REPO_ROOT / "core").iterdir() if (path / "__init__.py").is_file()
    }
    assert packages == {"backends", "opencode", "tools"}
    text = read(ARCHITECTURE_DOC)
    # When/Then: the architecture diagram names each of them
    for package in sorted(packages):
        assert f"core/{package}" in text, f"core/{package} is missing from the architecture diagram"
    # and the modules the diagram promises exist
    assert "core/brain.py" in text and "core/routing.py" in text


def test_backend_doc_table_lists_every_module_under_core():
    # Given: the modules on disk
    modules = core_modules()
    # When: the module table is read out of the document
    documented = {module for module in modules if f"`{module}`" in read(BACKEND_DOC)}
    # Then: a new core module cannot be added without a line of documentation
    assert documented == modules, f"undocumented: {sorted(modules - documented)}"


def test_readme_doc_table_points_at_every_document():
    # Given: the documents that exist
    docs = {f"docs/{path.name}" for path in sorted(DOCS.glob("*.md"))}
    text = read(README)
    # When/Then: the README table links each one, so a new doc is discoverable
    missing = sorted(doc for doc in docs if f"({doc})" not in text)
    assert missing == [], f"README does not link {missing}"


# ---------------------------------------------------------------------------
# 8. the roadmap does not present superseded work as pending
# ---------------------------------------------------------------------------


def test_roadmap_marks_stages_four_and_five_superseded_and_has_stage_eleven():
    # Given: the roadmap after the rewrite
    text = read(ROADMAP_DOC)
    # When/Then: the superseded stages say so in their own heading, list no
    # pending work, and the rewrite has a stage of its own
    assert roadmap_violations(text) == []


def test_guard_rejects_a_superseded_stage_still_listing_pending_work():
    # Given: a roadmap whose superseded stage 4 has a fresh unchecked box
    text = read(ROADMAP_DOC)
    heading = next(s for s in text.split("## ") if "Этап 4" in s)
    # When: a pending item is appended to it
    mutated = text.replace(heading, heading + "\n- [ ] свежая задача\n", 1)
    # Then: a pending checkbox under a superseded stage is refused
    violations = roadmap_violations(mutated)
    assert any("pending" in reason for reason in violations), violations
    # and the shipped roadmap is clean, so the mutation is what failed
    assert roadmap_violations(text) == []


def test_guard_rejects_a_roadmap_without_stage_eleven():
    # Given: the pre-rewrite roadmap
    text = read(ROADMAP_DOC).split("## Этап 11", 1)[0]
    # When/Then: the missing stage is named
    assert any("stage 11" in reason for reason in roadmap_violations(text))


def test_every_roadmap_stage_is_answered_by_the_other_documents():
    # Given: a roadmap whose stage 11 claims something the docs do not support
    text = read(ROADMAP_DOC)
    backend = read(BACKEND_DOC)
    # When/Then: the stage that describes this rewrite names the two agents and
    # the broker, so a reader who stops at the roadmap still learns the shape
    stage = text.split("## Этап 11", 1)[1]
    for token in ("r2d2-voice", "r2d2-agent"):
        assert token in stage, f"stage 11 never mentions {token}"
        assert token in backend, f"{token} is not in docs/11-opencode-backend.md"
    assert "permission" in stage.lower()


# ---------------------------------------------------------------------------
# 9. secret hygiene -- the docs carry no credential
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", DOC_FILES, ids=lambda p: p.name)
def test_no_document_contains_a_real_credential(path):
    # Given: the operator's own environment files, read at run time
    secrets = env_secrets()
    text = read(path)
    # When/Then: no value from either file is in the document.  The failure
    # names the VARIABLE, never the value.
    assert secret_violations(text, secrets, path.name) == []
    # and nothing wearing the shape of an OpenRouter key is either
    assert LIVE_KEY.search(text) is None, f"{path.name} contains a real-looking API key"


def test_the_docs_use_placeholders_rather_than_credentials():
    # Given: the deployment document, which is where a key would be shown
    text = read(DEPLOYMENT_DOC)
    # When/Then: the opencode and fallback credentials are shown as references
    # the operator fills in, which is also what the config loader expands
    for placeholder in ("${R2D2_OC_PASSWORD}", "${R2D2_ZEN_KEY}"):
        assert placeholder in text, f"{placeholder} is not documented as a placeholder"


def test_guard_rejects_a_document_carrying_the_operators_own_secret():
    # Given: a document into which somebody pasted a real key
    secrets = env_secrets() or {"R2D2_OC_PASSWORD": "r2d2-doc-test-4f2c9a7b"}
    victim, value = next(iter(secrets.items()))
    text = read(README) + f"\nключ: {value}\n"
    # When/Then: the leak is reported by variable name
    violations = secret_violations(text, secrets, "README.md")
    assert violations and victim in violations[0]
    assert value not in violations[0], "the failure message itself leaked the value"
