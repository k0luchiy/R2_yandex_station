"""Every path that walks the fallback chain walks the SAME chain (D8).

`config/backends.json` declares `["opencode", "zen", "yandexgpt", "openrouter"]`
and the first entry is not a chain member: `opencode` is the ROUTE, and it is the
one `kind` `build_chain` cannot construct without an `OpencodeWiring`. Hand it an
unfiltered order and `build_backends` raises `BackendConfigError`, which takes the
whole chain down rather than skipping one entry.

`core/brain.py:_call_llm` has always removed it, and `app/diagnostics.py` removes
it too. `core/tools/arxiv_tool.py` did not: it passed `chain` straight through, so
**every arxiv digest raised** the moment `R2D2_OC_PASSWORD` was set -- which is to
say in the deployment this project ships, on the arxiv digest, which is one of its
headline scenarios. The live run in `qa/live-run.md` never reached the line (the
turn was escalated and dropped by an earlier defect), so the suite was green and
the feature was dead.

What is asserted here, and why each of the three is a defect that shipped:

* **the arxiv summariser answers with the session backend present AND
  credentialed** -- the deployment state, with the precondition itself asserted,
  because a test that only proves "no raise" would also pass if the backend were
  missing and nothing was being reached anyway;
* **`build_chain` still refuses the unfiltered order** in the same test, so the
  next change cannot quietly make the crash unreachable instead of fixing it;
* **the brain, the diagnostics route and the arxiv summariser pass one order**,
  compared through the three real entry points rather than through the helper. The
  shared function is `core.backends.registry.fallback_chain`; `core/brain.py`
  still carries its own inline filter (it is the reference implementation and
  another change owns the file), so "single source of truth" here is enforced by
  this comparison, not asserted by a comment.

Nothing opens a socket: the `openai_compatible` members point at a closed
loopback port, and `build_chain` constructs a backend without opening one.

allow: SIZE_OK -- four tests, each one a defect that reached main.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from app import diagnostics
from app.config import Config
from core.backends.config_loader import BackendChain, BackendConfigError, load_backend_specs
from core.backends.registry import SESSION_KIND, build_chain, fallback_chain
from core.brain import SESSION_KIND as BRAIN_SESSION_KIND
from core.brain import Brain
from core.memory import Memory
from core.tools import arxiv_tool
from tests.test_backend_registry import spec

#: A credential that is not one: nothing here is read from the operator's `.env`,
#: and `test_no_secrets_tracked.py` scans tracked files for real key shapes.
SESSION_PASSWORD = "hunter2-not-a-credential"
#: A loopback port nothing listens on, so the summariser's one call is refused by
#: the kernel instead of leaving the machine. The same trick, for the same reason,
#: is `tests/test_r2d2_do_cli.py::_offline_backends`.
DEAD_URL = "http://127.0.0.1:9/v1"

SESSION_ENTRY = {
    "name": "opencode",
    "kind": "opencode_session",
    "base_url": "http://127.0.0.1:4599",
    "username": "${R2D2_OC_USERNAME}",
    "password": "${R2D2_OC_PASSWORD}",
    "fast_model": "opencode/space-bunny-free",
}


def http_entry(name: str, model: str) -> dict[str, str]:
    return {
        "name": name,
        "kind": "openai_compatible",
        "base_url": DEAD_URL,
        "api_key": "${OPENROUTER_API_KEY}",
        "model": model,
        "auth_style": "bearer",
    }


YANDEX_ENTRY = {
    "name": "yandexgpt",
    "kind": "openai_compatible",
    "base_url": "https://llm.api.cloud.yandex.net/foundationModels/v1",
    "api_key": "${YANDEX_API_KEY}",
    "model": "gpt://${YANDEX_FOLDER_ID}/yandexgpt-lite-5",
    "auth_style": "yandex",
    "auth_mode": "api_key",
}

ENTRIES = {
    "R2D2_OC_PASSWORD": SESSION_PASSWORD,
    "R2D2_OC_USERNAME": "r2d2",
    "R2D2_ZEN_KEY": "zen-key-for-the-loader",
    "YANDEX_API_KEY": "yandex-key-for-the-loader",
    "YANDEX_FOLDER_ID": "b1gfakefolder",
    "OPENROUTER_API_KEY": "key-for-the-loader",
}

ARTICLES = [
    {
        "title": "Adaptive routing for retrieval",
        "summary": "A router between dense and sparse indices.",
        "link": "https://arxiv.test/abs/2601.00001",
        "published": "2026-01-02T00:00:00Z",
    }
]


def write_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entries: list[dict], chain: list[str]
) -> Config:
    """A registry file plus a fully credentialed environment.

    Every credential is set on purpose: `app.config` calls `load_dotenv()` at
    import, so the ambient environment is whatever the operator's `.env` holds and
    a test that relied on it would measure their machine, not this fixture.
    """
    for variable, value in ENTRIES.items():
        monkeypatch.setenv(variable, value)
    path = tmp_path / "backends.json"
    path.write_text(json.dumps({"chain": chain, "backends": entries}), encoding="utf-8")
    return Config(backends_path=str(path))


# ---------------------------------------------------------------------------
# 1. the arxiv path, with the session backend in the chain and credentialed
# ---------------------------------------------------------------------------


async def test_the_arxiv_summariser_answers_with_the_session_backend_in_the_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given the deployment's registry: `opencode` is first in the chain AND has a
    # password, which is the state `build_chain`'s own filter cannot drop it from
    cfg = write_registry(
        tmp_path,
        monkeypatch,
        [SESSION_ENTRY, http_entry("openrouter", "fixture/model")],
        ["opencode", "openrouter"],
    )
    _, specs = load_backend_specs(cfg)
    assert specs["opencode"].kind == SESSION_KIND
    assert specs["opencode"].password == SESSION_PASSWORD, "the crash needs a credential"
    # and the unfiltered order is still unbuildable -- the defect this fixes
    with pytest.raises(BackendConfigError, match="opencode_session"):
        build_chain(specs, BackendChain(order=("opencode", "openrouter")))
    # When the summariser walks the chain it was given
    summary = await arxiv_tool.summarize_entries(cfg, "RAG", ARTICLES)
    # Then it answers, with the link list the summariser owes when no backend can
    # speak -- rather than raising `BackendConfigError` out of the arxiv job
    assert summary.splitlines() == [
        "• Adaptive routing for retrieval — https://arxiv.test/abs/2601.00001"
    ]


# ---------------------------------------------------------------------------
# 2. one filter, three call sites: the orders must be identical
# ---------------------------------------------------------------------------


async def test_every_chain_caller_removes_the_session_backend_the_same_way(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a registry holding all four declared backends, the session one
    # included and credentialed
    cfg = write_registry(
        tmp_path,
        monkeypatch,
        [
            SESSION_ENTRY,
            http_entry("zen", "fixture/zen"),
            YANDEX_ENTRY,
            http_entry("openrouter", "fixture/openrouter"),
        ],
        ["opencode", "zen", "yandexgpt", "openrouter"],
    )
    seen: dict[str, tuple[str, ...]] = {}

    def recorder(caller: str):
        """A `build_chain` that records the order it was handed and builds nothing."""

        def build_chain_stub(specs, chain, **kwargs) -> list:
            seen[caller] = chain.order
            return []

        return build_chain_stub

    monkeypatch.setattr("core.brain.build_chain", recorder("brain"))
    monkeypatch.setattr("app.diagnostics.build_chain", recorder("diagnostics"))
    monkeypatch.setattr("core.tools.arxiv_tool.build_chain", recorder("arxiv"))

    # When each of the three real entry points runs
    brain = Brain(cfg, Memory.__new__(Memory), None, logging.getLogger("test.brain"))
    with pytest.raises(RuntimeError, match="all LLM providers failed"):
        await brain._call_llm([{"role": "user", "content": "hi"}])
    assert await diagnostics.fallback_backends(cfg, load_backend_specs) == []
    await arxiv_tool.summarize_entries(cfg, "RAG", ARTICLES)
    # Then all three walked one order: the declared one minus the session backend,
    # which is neither dropped twice nor left in by one caller and removed by two
    assert seen == {
        "brain": ("zen", "yandexgpt", "openrouter"),
        "diagnostics": ("zen", "yandexgpt", "openrouter"),
        "arxiv": ("zen", "yandexgpt", "openrouter"),
    }
    # and the two names for that kind are one name
    assert BRAIN_SESSION_KIND == SESSION_KIND


# ---------------------------------------------------------------------------
# 3./4. the shared filter itself
# ---------------------------------------------------------------------------


def test_fallback_chain_keeps_the_order_and_the_unused_bookkeeping() -> None:
    # Given a chain whose `unused` bookkeeping must survive the filter
    chain = BackendChain(order=("zen", "opencode", "openrouter"), unused=("ghost",))
    specs = {
        "zen": spec("zen", kind="openai_compatible"),
        "opencode": spec("opencode", kind=SESSION_KIND),
        "openrouter": spec("openrouter", kind="openai_compatible"),
    }
    # When
    filtered = fallback_chain(specs, chain)
    # Then only the session kind is gone; the order and the unused names are not
    # re-derived, so a caller cannot lose a warning by asking for the filter
    assert filtered.order == ("zen", "openrouter")
    assert filtered.unused == ("ghost",)
    assert chain.order == ("zen", "opencode", "openrouter"), "the argument was mutated"


def test_fallback_chain_leaves_a_chain_of_only_http_backends_untouched() -> None:
    # Given a chain with no session backend in it at all
    chain = BackendChain(order=("zen", "openrouter"))
    specs = {
        "zen": spec("zen", kind="openai_compatible"),
        "openrouter": spec("openrouter", kind="openai_compatible"),
    }
    # When / Then
    assert fallback_chain(specs, chain) == chain
