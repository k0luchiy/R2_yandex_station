"""Tests for the config-driven backend registry loader (plan todo 2)."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from app.config import Config
from core.backends.config_loader import (
    DEFAULT_EVENT_READ_TIMEOUT,
    DEFAULT_TIMEOUT,
    VALID_KINDS,
    BackendChain,
    BackendConfigError,
    BackendSpec,
    load_backend_specs,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SHIPPED = str(REPO_ROOT / "config" / "backends.json")

# Every variable the shipped config or these tests reference. `app.config`
# calls load_dotenv() at import, so the ambient environment is not clean.
TOUCHED = (
    "R2D2_OC_USERNAME",
    "R2D2_OC_PASSWORD",
    "R2D2_ZEN_KEY",
    "YANDEX_API_KEY",
    "YANDEX_FOLDER_ID",
    "OPENROUTER_API_KEY",
    "SENTINEL_SECRET",
)

SENTINEL = "sk-or-SENTINEL-0123456789-do-not-leak"

SPEC_FIELDS = (
    "name",
    "kind",
    "base_url",
    "api_key",
    "username",
    "password",
    "model",
    "fast_model",
    "task_agent",
    "voice_agent",
    "task_model",
    "summarize_model",
    "timeout",
    "event_read_timeout",
    "auth_style",
    "auth_mode",
    "extra",
)

CONFIG_FIELDS = (
    ("backends_path", "config/backends.json"),
    ("r2d2_fast_deadline", 3.2),
    ("r2d2_task_ack", "Проверяю, пришлю в телеграм."),
    ("r2d2_needs_agent_sentinel", "[[NEEDS_AGENT]]"),
    ("r2d2_voice_agent", "r2d2-voice"),
    ("r2d2_task_agent", "r2d2-agent"),
    ("r2d2_workspace", "/home/koluchiy/r2d2-workspace"),
    ("r2d2_permission_timeout", 300.0),
    ("r2d2_session_soft_limit", 40),
    ("r2d2_stale_session_seconds", 900.0),
    ("r2d2_cli_path", "/home/koluchiy/.r2d2/r2d2_do.py"),
    ("r2d2_event_poll_interval", 2.0),
)


@pytest.fixture(autouse=True)
def isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in TOUCHED:
        monkeypatch.delenv(var, raising=False)


def backend(**overrides: object) -> dict[str, object]:
    spec: dict[str, object] = {
        "name": "zen",
        "kind": "openai_compatible",
        "base_url": "https://example.test/v1",
        "api_key": "plain-key",
        "model": "some-model",
    }
    spec.update(overrides)
    return spec


def payload(*backends: dict[str, object], chain: list[str] | None = None) -> dict[str, object]:
    return {
        "chain": chain if chain is not None else [b["name"] for b in backends],
        "backends": list(backends),
    }


def cfg_for(tmp_path: Path, doc: object) -> Config:
    path = tmp_path / "backends.json"
    path.write_text(doc if isinstance(doc, str) else json.dumps(doc), encoding="utf-8")
    return Config(backends_path=str(path))


# --------------------------------------------------------------------------
# happy path
# --------------------------------------------------------------------------


def test_shipped_config_parses_opencode_session(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given the repo's own config/backends.json and a resolvable folder id
    monkeypatch.setenv("YANDEX_FOLDER_ID", "b1gfakefolder")
    # When
    chain, specs = load_backend_specs(Config(backends_path=SHIPPED))
    # Then
    assert chain.order == ("opencode", "zen", "yandexgpt", "openrouter")
    assert chain.unused == ()
    assert specs["opencode"].kind == "opencode_session"
    assert set(VALID_KINDS) == {"opencode_session", "openai_compatible"}


@pytest.mark.parametrize("slot", ("fast_model", "task_model", "summarize_model"))
def test_shipped_opencode_uses_the_only_working_zen_model(
    monkeypatch: pytest.MonkeyPatch, slot: str
) -> None:
    # Spike correction C1: only space-bunny-free survives `opencode serve`.
    monkeypatch.setenv("YANDEX_FOLDER_ID", "b1gfakefolder")
    # Given / When
    _, specs = load_backend_specs(Config(backends_path=SHIPPED))
    # Then
    assert getattr(specs["opencode"], slot) == "opencode/space-bunny-free"
    assert specs["opencode"].base_url == "http://127.0.0.1:4599"
    assert specs["opencode"].voice_agent == "r2d2-voice"
    assert specs["opencode"].task_agent == "r2d2-agent"
    assert specs["opencode"].timeout == 3.2


def test_placeholder_expands_from_environ(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Given a credential placeholder and the variable set
    monkeypatch.setenv("R2D2_OC_PASSWORD", "hunter2-from-env")
    cfg = cfg_for(tmp_path, payload(backend(password="${R2D2_OC_PASSWORD}")))
    # When
    _, specs = load_backend_specs(cfg)
    # Then
    assert specs["zen"].password == "hunter2-from-env"


def test_placeholder_expands_inside_a_larger_string(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Given a folder id embedded in a URI
    monkeypatch.setenv("YANDEX_FOLDER_ID", "b1gREALfolder")
    cfg = cfg_for(
        tmp_path, payload(backend(model="gpt://${YANDEX_FOLDER_ID}/yandexgpt-lite-5"))
    )
    # When
    _, specs = load_backend_specs(cfg)
    # Then the gpt:// prefix and the model tail survive verbatim
    assert specs["zen"].model == "gpt://b1gREALfolder/yandexgpt-lite-5"


def test_shipped_yandexgpt_keeps_gpt_uri_verbatim(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Given the shipped yandexgpt entry shape
    monkeypatch.setenv("YANDEX_FOLDER_ID", "b1gabc123")
    monkeypatch.setenv("YANDEX_API_KEY", "yandex-key")
    cfg = cfg_for(
        tmp_path,
        payload(
            backend(
                name="yandexgpt",
                base_url="https://llm.api.cloud.yandex.net/foundationModels/v1",
                api_key="${YANDEX_API_KEY}",
                model="gpt://${YANDEX_FOLDER_ID}/yandexgpt-lite-5",
                auth_style="yandex",
                auth_mode="api_key",
            )
        ),
    )
    # When
    _, specs = load_backend_specs(cfg)
    # Then
    assert specs["yandexgpt"].model == "gpt://b1gabc123/yandexgpt-lite-5"
    assert specs["yandexgpt"].api_key == "yandex-key"
    assert specs["yandexgpt"].auth_style == "yandex"
    assert specs["yandexgpt"].auth_mode == "api_key"


@pytest.mark.parametrize("field", ("api_key", "password"))
def test_empty_string_credential_is_not_an_expansion_failure(
    tmp_path: Path, field: str
) -> None:
    # Given an explicitly empty credential ("" is a value, not a placeholder)
    cfg = cfg_for(tmp_path, payload(backend(**{field: ""})))
    # When / Then - empty != unexpanded
    _, specs = load_backend_specs(cfg)
    assert getattr(specs["zen"], field) == ""


def test_unknown_keys_land_in_extra(tmp_path: Path) -> None:
    # Given a key the dataclass does not declare
    cfg = cfg_for(tmp_path, payload(backend(region="eu-central1")))
    # When
    _, specs = load_backend_specs(cfg)
    # Then
    assert specs["zen"].extra == {"region": "eu-central1"}


def test_spec_is_frozen_and_extra_is_read_only(tmp_path: Path) -> None:
    cfg = cfg_for(tmp_path, payload(backend(region="eu-central1")))
    _, specs = load_backend_specs(cfg)
    with pytest.raises(Exception):
        specs["zen"].name = "other"  # type: ignore[misc]
    with pytest.raises(TypeError):
        specs["zen"].extra["region"] = "us"  # type: ignore[index]


def test_backend_not_in_chain_is_reported_as_unused(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Given a spec the chain never references
    cfg = cfg_for(tmp_path, payload(backend(), backend(name="spare"), chain=["zen"]))
    # When
    with caplog.at_level(logging.WARNING):
        chain, specs = load_backend_specs(cfg)
    # Then
    assert chain.order == ("zen",)
    assert chain.unused == ("spare",)
    assert "spare" in caplog.text
    assert set(specs) == {"zen", "spare"}


def test_backend_chain_and_spec_shapes() -> None:
    # The plan pins BackendSpec's field list exactly.
    assert tuple(BackendSpec.__dataclass_fields__) == SPEC_FIELDS
    spec = BackendSpec(name="x", kind="opencode_session", base_url="u")
    assert spec.timeout == 3.0
    assert spec.extra == {}
    assert BackendChain(order=("a",)).unused == ()


def test_the_stream_read_bound_has_its_own_default_and_is_read_as_a_number(
    tmp_path: Path,
) -> None:
    # Given a backend that declares the bound, and one that does not
    cfg = cfg_for(
        tmp_path,
        payload(backend(event_read_timeout=12.5), backend(name="spare")),
    )
    # When
    _, specs = load_backend_specs(cfg)
    # Then a declared value is read, and an absent one is the measured default
    assert specs["zen"].event_read_timeout == 12.5
    assert specs["spare"].event_read_timeout == DEFAULT_EVENT_READ_TIMEOUT
    # and it is a bound for the long-lived stream, so it is not the request deadline
    assert DEFAULT_EVENT_READ_TIMEOUT > DEFAULT_TIMEOUT
    assert specs["spare"].timeout == DEFAULT_TIMEOUT


# --------------------------------------------------------------------------
# Config surface (plan todo 2 field list)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(("name", "default"), CONFIG_FIELDS)
def test_config_declares_new_field_with_documented_default(name: str, default: object) -> None:
    # Given / When
    cfg = Config()
    # Then
    assert name in Config.__dataclass_fields__
    assert getattr(cfg, name) == default


def test_config_still_maps_uppercase_env_to_new_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given the pre-existing load() mechanism
    monkeypatch.setenv("R2D2_FAST_DEADLINE", "1.5")
    monkeypatch.setenv("R2D2_SESSION_SOFT_LIMIT", "7")
    monkeypatch.setenv("R2D2_VOICE_AGENT", "voice-from-env")
    # When
    cfg = Config.load()
    # Then
    assert cfg.r2d2_fast_deadline == 1.5
    assert cfg.r2d2_session_soft_limit == 7
    assert cfg.r2d2_voice_agent == "voice-from-env"


# --------------------------------------------------------------------------
# unexpandable placeholders
# --------------------------------------------------------------------------


def test_unexpandable_var_in_non_credential_field_raises(tmp_path: Path) -> None:
    cfg = cfg_for(tmp_path, payload(backend(base_url="http://${DOES_NOT_EXIST}")))
    with pytest.raises(BackendConfigError) as excinfo:
        load_backend_specs(cfg)
    message = str(excinfo.value)
    assert "DOES_NOT_EXIST" in message
    assert "zen" in message
    assert "base_url" in message


def test_unexpandable_var_in_credential_is_lenient_and_warned(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # A missing credential makes the backend uncredentialed, not the file invalid:
    # dropping it is build_chain's job (todo 5).
    cfg = cfg_for(tmp_path, payload(backend(api_key="${R2D2_ZEN_KEY}")))
    with caplog.at_level(logging.WARNING):
        _, specs = load_backend_specs(cfg)
    assert specs["zen"].api_key == ""
    assert "R2D2_ZEN_KEY" in caplog.text


def test_strict_credentials_flag_promotes_missing_credential_to_an_error(tmp_path: Path) -> None:
    cfg = cfg_for(tmp_path, payload(backend(api_key="${R2D2_ZEN_KEY}")))
    with pytest.raises(BackendConfigError) as excinfo:
        load_backend_specs(cfg, strict_credentials=True)
    message = str(excinfo.value)
    assert "R2D2_ZEN_KEY" in message
    assert "zen" in message


@pytest.mark.parametrize("value", ("sk-or-${BAD-NAME}", "${}", "prefix${UNCLOSED"))
def test_placeholder_surviving_expansion_is_a_failure_not_a_parse(
    tmp_path: Path, value: str
) -> None:
    # A spec that loads "fine" while still holding ${...} in a credential is a lie.
    cfg = cfg_for(tmp_path, payload(backend(api_key=value)))
    with pytest.raises(BackendConfigError) as excinfo:
        load_backend_specs(cfg)
    assert "api_key" in str(excinfo.value)
    assert "zen" in str(excinfo.value)


# --------------------------------------------------------------------------
# malformed input -> typed error, never a leaking KeyError/TypeError
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("doc", "needle"),
    [
        ("{not json at all", "JSON"),
        ("", "JSON"),
        ("[]", "object"),
        ('"a string"', "object"),
        ('{"chain": ["a"]}', "backends"),
        ('{"backends": []}', "chain"),
        ('{"chain": "a", "backends": []}', "chain"),
        ('{"chain": [1], "backends": []}', "chain"),
        ('{"chain": [], "backends": {}}', "backends"),
        ('{"chain": [], "backends": "zen"}', "backends"),
    ],
)
def test_malformed_document_raises_typed_error(
    tmp_path: Path, doc: str, needle: str
) -> None:
    cfg = cfg_for(tmp_path, doc)
    with pytest.raises(BackendConfigError) as excinfo:
        load_backend_specs(cfg)
    assert needle in str(excinfo.value)
    assert isinstance(excinfo.value, BackendConfigError)


def test_missing_file_raises_typed_error(tmp_path: Path) -> None:
    cfg = Config(backends_path=str(tmp_path / "nope.json"))
    with pytest.raises(BackendConfigError) as excinfo:
        load_backend_specs(cfg)
    assert "nope.json" in str(excinfo.value)


def test_entry_that_is_not_an_object_raises(tmp_path: Path) -> None:
    cfg = cfg_for(tmp_path, {"chain": ["zen"], "backends": [backend(), "oops"]})
    with pytest.raises(BackendConfigError) as excinfo:
        load_backend_specs(cfg)
    assert "object" in str(excinfo.value)


@pytest.mark.parametrize("missing", ("name", "kind", "base_url"))
def test_required_field_missing_raises(tmp_path: Path, missing: str) -> None:
    doc = {"chain": ["zen"], "backends": [{k: v for k, v in backend().items() if k != missing}]}
    cfg = cfg_for(tmp_path, doc)
    with pytest.raises(BackendConfigError) as excinfo:
        load_backend_specs(cfg)
    assert missing in str(excinfo.value)


def test_duplicate_name_raises(tmp_path: Path) -> None:
    cfg = cfg_for(tmp_path, payload(backend(), backend(model="other")))
    with pytest.raises(BackendConfigError) as excinfo:
        load_backend_specs(cfg)
    assert "zen" in str(excinfo.value)
    assert "duplicate" in str(excinfo.value).lower()


def test_unknown_kind_lists_both_valid_kinds(tmp_path: Path) -> None:
    cfg = cfg_for(tmp_path, payload(backend(kind="telepathy")))
    with pytest.raises(BackendConfigError) as excinfo:
        load_backend_specs(cfg)
    message = str(excinfo.value)
    assert "opencode_session" in message
    assert "openai_compatible" in message
    assert "telepathy" in message


def test_chain_entry_without_a_matching_backend_raises(tmp_path: Path) -> None:
    cfg = cfg_for(tmp_path, payload(backend(), chain=["zen", "ghost"]))
    with pytest.raises(BackendConfigError) as excinfo:
        load_backend_specs(cfg)
    assert "ghost" in str(excinfo.value)


@pytest.mark.parametrize(
    ("overrides", "needle", "named"),
    [
        ({"name": 7}, "name", None),
        ({"kind": 7}, "kind", "zen"),
        ({"base_url": ["x"]}, "base_url", "zen"),
        ({"model": 3}, "model", "zen"),
        ({"timeout": "3.2"}, "timeout", "zen"),
        ({"timeout": True}, "timeout", "zen"),
        ({"timeout": None}, "timeout", "zen"),
        ({"event_read_timeout": "30"}, "event_read_timeout", "zen"),
        ({"event_read_timeout": False}, "event_read_timeout", "zen"),
        # 0 and a negative are not "no bound": httpx maps them onto an
        # already-expired deadline, so every connect raises at once and the
        # reader is dead rather than patient.
        ({"event_read_timeout": 0}, "event_read_timeout", "zen"),
        ({"event_read_timeout": -1.0}, "event_read_timeout", "zen"),
        ({"retry_policy": {"n": 1}}, "retry_policy", "zen"),
    ],
)
def test_wrongly_typed_field_raises(
    tmp_path: Path, overrides: dict[str, object], needle: str, named: str | None
) -> None:
    # chain is pinned because `payload` derives it from a name that may be invalid
    cfg = cfg_for(tmp_path, payload(backend(**overrides), chain=["zen"]))
    with pytest.raises(BackendConfigError) as excinfo:
        load_backend_specs(cfg)
    message = str(excinfo.value)
    assert needle in message
    if named is None:
        assert "<unnamed>" in message
    else:
        assert named in message


# --------------------------------------------------------------------------
# secret hygiene
# --------------------------------------------------------------------------


def _all_failure_documents() -> list[dict[str, object]]:
    return [
        payload(backend(kind="telepathy")),
        payload(backend(), backend()),
        payload(backend(), chain=["ghost"]),
        payload(backend(base_url="http://${UNSET_VAR_A}")),
        payload(backend(timeout="soon")),
        payload(backend(retry_policy={"n": 1})),
        {"chain": ["zen"], "backends": [{"name": "zen", "kind": "openai_compatible"}]},
    ]


def test_no_credential_value_ever_appears_in_an_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Given a real secret sitting in a fixture env var
    monkeypatch.setenv("SENTINEL_SECRET", SENTINEL)
    monkeypatch.setenv("R2D2_ZEN_KEY", SENTINEL)
    monkeypatch.setenv("R2D2_OC_PASSWORD", SENTINEL)
    # When every failure path is triggered
    documents = _all_failure_documents()
    messages = []
    for doc in documents:
        cfg = cfg_for(tmp_path, doc)
        try:
            load_backend_specs(cfg, strict_credentials=True)
        except BackendConfigError as exc:
            messages.append(str(exc))
    # Then each one is a real error ...
    assert len(messages) == len(documents)
    # ... and none of them leaks the secret
    for message in messages:
        assert SENTINEL not in message
        assert "sk-or-" not in message


def test_secret_from_env_is_not_echoed_when_a_later_field_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Given a secret that expanded fine into a credential ...
    monkeypatch.setenv("R2D2_ZEN_KEY", SENTINEL)
    # ... and a sibling field that cannot expand
    doc = payload(backend(api_key="${R2D2_ZEN_KEY}", base_url="http://${UNSET_VAR_B}"))
    cfg = cfg_for(tmp_path, doc)
    with pytest.raises(BackendConfigError) as excinfo:
        load_backend_specs(cfg)
    assert SENTINEL not in str(excinfo.value)
    assert "UNSET_VAR_B" in str(excinfo.value)
