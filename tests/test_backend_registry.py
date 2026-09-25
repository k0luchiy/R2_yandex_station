"""Tests for the config-driven backend registry (plan todo 5).

`core/providers/factory.py` dispatched on a hardcoded `if key in ("yandex",
...)` chain; `core/backends/registry.py` dispatches on the `kind` field of a
`BackendSpec` and drops a backend it cannot possibly use. These tests pin:

* the chain the registry returns is the chain `config/backends.json` declares,
  in that order;
* a backend that cannot work -- no credential, no base URL, no model -- is
  DROPPED with a WARNING naming it, never returned as something that fails on
  first use (the "misleading success" class: a non-empty chain containing a
  certain-failure entry is a trap, not a fallback);
* an empty chain is an error, not a silent `[]`;
* a `kind` with no constructor raises instead of silently producing nothing;
* no WARNING ever embeds a credential VALUE;
* `core.providers` is gone -- unimportable, and absent from disk;
* `core/brain.py:_call_llm` walks the chain: built once per call, advancing to
  the next backend on `BackendError` or a timeout, ending in the same
  recognisable `RuntimeError` the old provider factory produced.

Nothing here opens a socket: the registry constructs backends, and an
`OpenAICompatibleBackend` builds its `httpx.AsyncClient` only on first use, so
construction is inert. The brain cases inject their own backends.
"""

from __future__ import annotations

import importlib
import json
import logging
from collections.abc import Mapping, Sequence
from pathlib import Path

import httpx
import pytest

from app.config import Config
from core.backends.base import Backend, Choice
from core.backends.config_loader import (
    BackendChain,
    BackendConfigError,
    BackendSpec,
    load_backend_specs,
)
from core.backends.openai_compatible import BackendError
from core.backends.registry import build_backends, build_chain
from core.brain import Brain
from core.memory import Memory

REPO_ROOT = Path(__file__).resolve().parent.parent
SHIPPED = str(REPO_ROOT / "config" / "backends.json")
SHIPPED_DOC = json.loads(Path(SHIPPED).read_text(encoding="utf-8"))

# `app.config` calls load_dotenv() at import, so the ambient environment is not
# clean and every test must state its own credential state.
TOUCHED = (
    "R2D2_OC_USERNAME",
    "R2D2_OC_PASSWORD",
    "R2D2_ZEN_KEY",
    "YANDEX_API_KEY",
    "YANDEX_FOLDER_ID",
    "OPENROUTER_API_KEY",
)
SENTINEL = "sk-or-v1-REGISTRY-SECRET-do-not-log-0123456789"
NOT_YET = "opencode_session backend not yet available"


@pytest.fixture(autouse=True)
def isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in TOUCHED:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def loadable_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make `load_backend_specs` succeed, so the brain actually reaches the registry.

    `isolate_env` leaves every credential unset, and the shipped config embeds
    `${YANDEX_FOLDER_ID}` in a NON-credential field, so an unset folder id fails
    the loader before any registry or brain code runs. An empty folder id is a
    value, not an unexpanded reference.
    """
    monkeypatch.setenv("YANDEX_FOLDER_ID", "")
    monkeypatch.setenv("OPENROUTER_API_KEY", "live-key-for-the-loader")


def spec(
    name: str = "zen",
    *,
    kind: str = "openai_compatible",
    base_url: str = "https://example.test/v1",
    api_key: str = "some-key",
    username: str = "",
    password: str = "",
    model: str = "some-model",
    fast_model: str = "",
    auth_style: str = "bearer",
    auth_mode: str = "",
) -> BackendSpec:
    return BackendSpec(
        name=name,
        kind=kind,
        base_url=base_url,
        api_key=api_key,
        username=username,
        password=password,
        model=model,
        fast_model=fast_model,
        auth_style=auth_style,
        auth_mode=auth_mode,
    )


def specs(*backends: BackendSpec) -> dict[str, BackendSpec]:
    return {backend.name: backend for backend in backends}


def cfg_for(tmp_path: Path, doc: object) -> Config:
    path = tmp_path / "backends.json"
    path.write_text(doc if isinstance(doc, str) else json.dumps(doc), encoding="utf-8")
    return Config(backends_path=str(path))


def names(backends: Sequence[Backend]) -> list[str]:
    return [backend.name for backend in backends]


# ---------------------------------------------------------------------------
# 1. chain order is config order
# ---------------------------------------------------------------------------


def test_shipped_config_returns_exactly_its_declared_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given the shipped config with only OpenRouter credentialed -- the state of
    # this machine (OPENROUTER_API_KEY set, every other credential unset)
    monkeypatch.setenv("YANDEX_FOLDER_ID", "")
    monkeypatch.setenv("OPENROUTER_API_KEY", SENTINEL)
    # When
    chain, loaded = load_backend_specs(Config(backends_path=SHIPPED))
    built = build_chain(loaded, chain)
    # Then the loader read the declared order, and the registry returned the
    # credentialed subset of it, in that same order
    assert chain.order == tuple(SHIPPED_DOC["chain"])
    assert chain.unused == ()
    assert names(built) == ["openrouter"]
    assert set(names(built)) <= set(SHIPPED_DOC["chain"])


def test_chain_order_is_the_declared_order_not_the_dict_order(tmp_path: Path) -> None:
    # Given a config whose backends are declared in a different order than the
    # chain asks for -- the failure mode of an implementation that iterates the
    # specs mapping instead of the chain
    cfg = cfg_for(
        tmp_path,
        {
            "chain": ["zulu", "alpha", "mike"],
            "backends": [
                {
                    "name": name,
                    "kind": "openai_compatible",
                    "base_url": f"https://{name}.test/v1",
                    "api_key": f"{name}-key",
                    "model": f"{name}-model",
                }
                for name in ("alpha", "mike", "zulu")
            ],
        },
    )
    # When
    chain, loaded = load_backend_specs(cfg)
    # Then
    assert names(build_chain(loaded, chain)) == ["zulu", "alpha", "mike"]


# ---------------------------------------------------------------------------
# 2./3. uncredentialed backends are dropped, an empty chain is an error
# ---------------------------------------------------------------------------


def test_uncredentialed_backend_is_dropped_with_a_warning_naming_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Given a chain where one backend has no api key
    loaded = specs(spec("zen", api_key=""), spec("openrouter", api_key="live-key"))
    # When
    with caplog.at_level(logging.WARNING):
        built = build_chain(loaded, BackendChain(order=("zen", "openrouter")))
    # Then the rest of the chain survives ...
    assert names(built) == ["openrouter"]
    # ... and the drop is announced, naming the backend and the field
    assert [record.levelno for record in caplog.records] == [logging.WARNING]
    message = caplog.records[0].getMessage()
    assert "zen" in message
    assert "api_key" in message


def test_all_uncredentialed_chain_raises_backend_config_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Given every backend without a credential
    loaded = specs(spec("zen", api_key=""), spec("openrouter", api_key=""))
    # When / Then
    with caplog.at_level(logging.WARNING):
        with pytest.raises(BackendConfigError) as excinfo:
            build_chain(loaded, BackendChain(order=("zen", "openrouter")))
    assert "zen" in str(excinfo.value)
    assert "openrouter" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 4. opencode_session is explicit, not a KeyError
# ---------------------------------------------------------------------------


def test_build_backends_rejects_opencode_session_with_the_plans_message() -> None:
    # Given a credentialed opencode_session spec
    loaded = specs(
        spec(
            "opencode",
            kind="opencode_session",
            base_url="http://127.0.0.1:4599",
            api_key="",
            username="opencode",
            password="hunter2",
            fast_model="opencode/space-bunny-free",
        )
    )
    # When / Then
    with pytest.raises(BackendConfigError, match=NOT_YET) as excinfo:
        build_backends(loaded)
    assert "opencode" in str(excinfo.value)


def test_uncredentialed_opencode_is_dropped_before_kind_dispatch(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Given the shipped opencode entry with no password -- todo 9's constructor
    # does not exist yet, so the order of filter and dispatch decides whether the
    # whole chain dies or this one entry is skipped
    loaded = specs(
        spec(
            "opencode",
            kind="opencode_session",
            base_url="http://127.0.0.1:4599",
            api_key="",
            password="",
            fast_model="opencode/space-bunny-free",
        ),
        spec("openrouter", api_key="live-key"),
    )
    # When
    with caplog.at_level(logging.WARNING):
        built = build_chain(loaded, BackendChain(order=("opencode", "openrouter")))
    # Then the missing credential wins: opencode is skipped, the rest lives
    assert names(built) == ["openrouter"]
    assert "opencode" in caplog.records[0].getMessage()


def test_credentialed_opencode_in_a_chain_still_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Given an operator who HAS configured the opencode credential
    monkeypatch.setenv("R2D2_OC_PASSWORD", "hunter2")
    cfg = cfg_for(
        tmp_path,
        {
            "chain": ["opencode"],
            "backends": [
                {
                    "name": "opencode",
                    "kind": "opencode_session",
                    "base_url": "http://127.0.0.1:4599",
                    "username": "opencode",
                    "password": "${R2D2_OC_PASSWORD}",
                    "fast_model": "opencode/space-bunny-free",
                }
            ],
        },
    )
    chain, loaded = load_backend_specs(cfg)
    # When / Then -- the credential is present, so the missing constructor is
    # what stops the chain, loudly
    with pytest.raises(BackendConfigError, match=NOT_YET):
        build_chain(loaded, chain)


# ---------------------------------------------------------------------------
# 5. core.providers is gone
# ---------------------------------------------------------------------------


def test_core_providers_is_not_importable() -> None:
    # When / Then
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("core.providers")


def test_core_providers_directory_is_gone_from_disk() -> None:
    # When / Then
    assert not (REPO_ROOT / "core" / "providers").exists()


@pytest.mark.parametrize(
    "name",
    ("core.providers.factory", "core.providers.openrouter", "core.providers.yandexgpt"),
)
def test_no_module_under_core_providers_survives(name: str) -> None:
    # Given the package is gone -- asserted first, so a ModuleNotFoundError
    # from a half-deleted package cannot masquerade as a pass
    assert not (REPO_ROOT / "core" / "providers").exists()
    importlib.invalidate_caches()
    # When / Then
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(name)


# ---------------------------------------------------------------------------
# 6./7. protocol conformance and determinism
# ---------------------------------------------------------------------------


def test_every_built_backend_satisfies_the_backend_protocol() -> None:
    # Given
    loaded = specs(spec("zen"), spec("openrouter"))
    # When
    built = build_backends(loaded)
    # Then
    assert set(built) == {"zen", "openrouter"}
    assert all(isinstance(backend, Backend) for backend in built.values())


def test_build_chain_is_deterministic() -> None:
    # Given
    loaded = specs(
        spec("zen"),
        spec("openrouter"),
        spec("yandexgpt", auth_style="yandex", auth_mode="api_key"),
    )
    order = ("yandexgpt", "openrouter", "zen")
    # When
    first = build_chain(loaded, BackendChain(order=order))
    second = build_chain(loaded, BackendChain(order=order))
    # Then
    assert names(first) == names(second) == ["yandexgpt", "openrouter", "zen"]


# ---------------------------------------------------------------------------
# 8. an unknown kind raises rather than silently producing nothing
# ---------------------------------------------------------------------------


def test_unknown_kind_raises_and_names_the_wired_kinds() -> None:
    # Given a spec that bypassed the loader (the loader already rejects this)
    # When / Then
    with pytest.raises(BackendConfigError) as excinfo:
        build_backends(specs(spec("telepathy", kind="telepathy")))
    message = str(excinfo.value)
    assert "telepathy" in message
    assert "openai_compatible" in message
    assert "opencode_session" in message


def test_unknown_kind_reached_through_build_chain_raises() -> None:
    # Given
    loaded = specs(spec("telepathy", kind="telepathy"), spec("zen"))
    # When / Then -- never an empty list dressed up as a fallback
    with pytest.raises(BackendConfigError, match="telepathy"):
        build_chain(loaded, BackendChain(order=("telepathy", "zen")))


# ---------------------------------------------------------------------------
# 9. declared-but-unused backends stay out of the chain
# ---------------------------------------------------------------------------


def test_unused_backend_is_reported_and_never_built(tmp_path: Path) -> None:
    # Given a backend that is declared but absent from "chain"
    cfg = cfg_for(
        tmp_path,
        {
            "chain": ["zen"],
            "backends": [
                {
                    "name": "zen",
                    "kind": "openai_compatible",
                    "base_url": "https://zen.test/v1",
                    "api_key": "zen-key",
                    "model": "zen-model",
                },
                {
                    "name": "ghost",
                    "kind": "openai_compatible",
                    "base_url": "https://ghost.test/v1",
                    "api_key": "ghost-key",
                    "model": "ghost-model",
                },
            ],
        },
    )
    # When
    chain, loaded = load_backend_specs(cfg)
    built = build_chain(loaded, chain)
    # Then the ghost is reported as unused and stays out of the chain
    assert chain.unused == ("ghost",)
    assert names(built) == ["zen"]
    assert "ghost" in loaded


# ---------------------------------------------------------------------------
# 10. a certain-failure backend must never look like a fallback
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "field"),
    (({"base_url": ""}, "base_url"), ({"model": ""}, "model")),
)
def test_structurally_unusable_backend_is_dropped_not_returned(
    caplog: pytest.LogCaptureFixture, overrides: dict[str, str], field: str
) -> None:
    # Given a backend that could only ever fail when called
    loaded = specs(spec("zen", **overrides), spec("openrouter", api_key="live-key"))
    # When
    with caplog.at_level(logging.WARNING):
        built = build_chain(loaded, BackendChain(order=("zen", "openrouter")))
    # Then it is skipped, with the offending field named
    assert names(built) == ["openrouter"]
    assert field in caplog.records[0].getMessage()


def test_drop_warning_never_embeds_a_credential_value(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Given a live credential on a backend that is dropped for another reason
    loaded = specs(spec("zen", api_key=SENTINEL, model=""), spec("openrouter"))
    # When
    with caplog.at_level(logging.WARNING):
        build_chain(loaded, BackendChain(order=("zen", "openrouter")))
    # Then
    assert SENTINEL not in caplog.text


# ---------------------------------------------------------------------------
# core/brain.py consumes the registry (the rewiring todo 5 owns)
# ---------------------------------------------------------------------------


class RecordingBackend:
    """A `Backend` that records how it was called and fails on demand."""

    def __init__(self, name: str, failure: BaseException | None = None) -> None:
        self.name = name
        self.failure = failure
        self.calls: list[dict[str, object]] = []
        self.closed = False

    async def complete(
        self,
        messages: Sequence[Mapping[str, object]],
        tools: Sequence[Mapping[str, object]] | None = None,
        *,
        max_tokens: int = 400,
        temperature: float = 0.7,
        timeout: float = 3.0,
        model: str | None = None,
    ) -> Choice:
        self.calls.append(
            {
                "messages": list(messages),
                "tools": list(tools or []),
                "max_tokens": max_tokens,
                "timeout": timeout,
            }
        )
        if self.failure is not None:
            raise self.failure
        return Choice(content=f"reply from {self.name}", provider=self.name, model="m")

    async def aclose(self) -> None:
        self.closed = True


def make_brain(
    monkeypatch: pytest.MonkeyPatch, backends: list[RecordingBackend]
) -> tuple[Brain, list[int]]:
    """A `Brain` whose chain is `backends`, plus a counter of chain builds.

    Memory and the worker are unused by `_call_llm`; `Memory.__new__` gives a
    real instance without opening a database.
    """
    builds: list[int] = []

    def fake_build_chain(
        specs: Mapping[str, BackendSpec], chain: BackendChain
    ) -> list[RecordingBackend]:
        builds.append(1)
        return backends

    monkeypatch.setattr("core.brain.build_chain", fake_build_chain)
    cfg = Config(llm_max_tokens=321, llm_timeout=2.5)
    brain = Brain(cfg, Memory.__new__(Memory), None, logging.getLogger("test.brain"))
    return brain, builds


async def test_brain_walks_the_chain_in_order_and_uses_config_budgets(
    monkeypatch: pytest.MonkeyPatch, loadable_registry: None
) -> None:
    # Given a chain whose first entry answers
    first = RecordingBackend("zen")
    second = RecordingBackend("openrouter")
    brain, builds = make_brain(monkeypatch, [first, second])
    # When
    choice = await brain._call_llm([{"role": "user", "content": "hi"}])
    # Then the first answer is returned and the walk stopped there
    assert choice.content == "reply from zen"
    assert len(first.calls) == 1
    assert second.calls == []
    # ... the per-call budget comes from config ...
    assert first.calls[0]["max_tokens"] == 321
    assert first.calls[0]["timeout"] == 2.5
    # ... the tool registry is offered ...
    assert first.calls[0]["tools"]
    # ... the chain is built ONCE per call, not once per attempt ...
    assert builds == [1]
    # ... and the clients this call created are released
    assert first.closed and second.closed


@pytest.mark.parametrize(
    "failure",
    (
        pytest.param(BackendError("first backend refused"), id="backend-error"),
        pytest.param(httpx.TimeoutException("too slow"), id="timeout"),
    ),
)
async def test_brain_advances_to_the_next_backend(
    monkeypatch: pytest.MonkeyPatch, loadable_registry: None, failure: BaseException
) -> None:
    # Given a chain whose first entry fails
    first = RecordingBackend("zen", failure)
    second = RecordingBackend("openrouter")
    brain, builds = make_brain(monkeypatch, [first, second])
    # When
    choice = await brain._call_llm([{"role": "user", "content": "hi"}])
    # Then the failure advances the walk instead of retrying the same backend
    assert choice.content == "reply from openrouter"
    assert len(first.calls) == 1
    assert len(second.calls) == 1
    assert builds == [1]


async def test_brain_raises_the_recognisable_runtime_error_when_all_fail(
    monkeypatch: pytest.MonkeyPatch, loadable_registry: None
) -> None:
    # Given a chain where every entry fails
    first = RecordingBackend("zen", BackendError("first"))
    second = RecordingBackend("openrouter", httpx.TimeoutException("second"))
    brain, _ = make_brain(monkeypatch, [first, second])
    # When / Then -- the message shape the operator's logs already recognise
    with pytest.raises(RuntimeError) as excinfo:
        await brain._call_llm([{"role": "user", "content": "hi"}])
    assert str(excinfo.value).startswith("all LLM providers failed: ")
    assert "second" in str(excinfo.value)
    assert first.closed and second.closed


async def test_brain_surfaces_a_broken_registry_as_a_config_error(
    monkeypatch: pytest.MonkeyPatch, loadable_registry: None
) -> None:
    # Given a registry that cannot produce a chain at all
    def explode(
        specs: Mapping[str, BackendSpec], chain: BackendChain
    ) -> list[RecordingBackend]:
        raise BackendConfigError("backend chain is empty: zen, openrouter")

    monkeypatch.setattr("core.brain.build_chain", explode)
    brain = Brain(Config(), Memory.__new__(Memory), None, logging.getLogger("test.brain"))
    # When / Then -- a config error is not an LLM failure and must not be
    # dressed up as one
    with pytest.raises(BackendConfigError, match="backend chain is empty") as excinfo:
        await brain._call_llm([{"role": "user", "content": "hi"}])
    assert "openrouter" in str(excinfo.value)


# ---------------------------------------------------------------------------
# stale state: the deleted package must leave no importer behind
# ---------------------------------------------------------------------------


def test_brain_no_longer_imports_the_provider_factory() -> None:
    # When
    source = (REPO_ROOT / "core" / "brain.py").read_text(encoding="utf-8")
    # Then
    assert "core.providers" not in source
    assert "get_provider" not in source


def test_no_python_file_outside_this_test_references_core_providers() -> None:
    # When
    offenders = [
        str(path.relative_to(REPO_ROOT))
        for path in REPO_ROOT.rglob("*.py")
        if ".venv" not in path.parts
        and "__pycache__" not in path.parts
        and path.name != Path(__file__).name
        and "core.providers" in path.read_text(encoding="utf-8")
    ]
    # Then
    assert offenders == []
