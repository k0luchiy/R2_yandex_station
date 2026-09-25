"""Structural tests for `core.backends.base`.

This module exists to pin two claims that are easy to *say* and impossible to see:

1. `Backend` is a real, non-vacuous `runtime_checkable` Protocol — a class missing
   `complete` must be rejected by `isinstance`, and the converse (a class with the
   right method names but the wrong signature) is accepted, because runtime
   structural checks test attribute PRESENCE only. The second half is documented
   in `test_runtime_checkable_checks_presence_not_signature` so nobody later
   mistakes an `isinstance` assertion for a signature check.
2. `ToolCall` and `Choice` are relocated *verbatim*. Parity is asserted against an
   embedded copy of the original `core/providers/base.py` dataclass source and
   against a hardcoded (name, default-spec) fingerprint — importing the new class
   and asserting on it alone would prove nothing.
"""

from __future__ import annotations

import abc
import asyncio
import collections.abc
import dataclasses
import inspect
import pathlib
import sys
import types
import typing

from core.backends import base
from core.backends.base import Backend, Choice, ToolCall

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# Doubles used by the conformance tests
# ---------------------------------------------------------------------------


class ConformingBackend:
    """A Backend with the correct members and the correct signatures."""

    def __init__(self) -> None:
        self.name: str = "conforming"

    async def complete(
        self,
        messages: typing.Sequence[base.ChatMessage],
        tools: typing.Sequence[base.ToolSchema] | None = None,
        *,
        max_tokens: int = 400,
        temperature: float = 0.7,
        timeout: float = 3.0,
        model: str | None = None,
    ) -> Choice:
        del messages, tools, max_tokens, temperature, timeout
        return Choice(content="ok", provider=self.name, model=model or "test-model")

    async def aclose(self) -> None:
        return None


class MissingComplete:
    """Has `name` and `aclose`, but no `complete` — must NOT conform."""

    name: str = "missing-complete"

    async def aclose(self) -> None:
        return None


class MissingAclose:
    """Has `name` and `complete`, but no `aclose` — must NOT conform."""

    name: str = "missing-aclose"

    async def complete(self, messages: typing.Sequence[base.ChatMessage]) -> Choice:
        del messages
        return Choice(content="ok", provider=self.name, model="test-model")


class MissingName:
    """Has both coroutines but no `name` attribute — must NOT conform."""

    async def complete(self, messages: typing.Sequence[base.ChatMessage]) -> Choice:
        del messages
        return Choice(content="ok", provider="anonymous", model="test-model")

    async def aclose(self) -> None:
        return None


class WrongArityBackend:
    """Deliberately wrong signatures — the adversarial case for runtime checks."""

    name: str = "wrong-arity"

    async def complete(self) -> None:  # type: ignore[override]
        return None

    async def aclose(self, extra: int = 0) -> None:
        del extra


# ---------------------------------------------------------------------------
# Protocol shape
# ---------------------------------------------------------------------------


def test_backend_is_a_runtime_checkable_protocol_and_not_an_abc() -> None:
    """Given a Protocol: it is a Protocol, it is runtime_checkable, and it is not an ABC."""
    # When / Then
    assert getattr(Backend, "_is_protocol", False) is True
    assert getattr(Backend, "_is_runtime_protocol", False) is True
    assert issubclass(Backend, typing.Protocol)
    assert not issubclass(Backend, abc.ABC)


def test_backend_protocol_members_are_exactly_name_complete_aclose() -> None:
    """Given the Protocol: its members are the three the plan specifies — nothing else."""
    # When / Then
    assert sorted(Backend.__protocol_attrs__) == ["aclose", "complete", "name"]


# ---------------------------------------------------------------------------
# Structural conformance (the non-vacuity proof)
# ---------------------------------------------------------------------------


def test_isinstance_accepts_a_correctly_typed_backend() -> None:
    """Given a class with `name`, `complete` and `aclose`: it conforms to Backend."""
    # When / Then
    assert isinstance(ConformingBackend(), Backend) is True


def test_isinstance_rejects_a_backend_missing_complete() -> None:
    """Given a class WITHOUT `complete`: `isinstance(..., Backend)` is False.

    This is the load-bearing negative assertion: without it, an empty Protocol
    (or a misspelled member name) would sail through the positive test too.
    """
    # When / Then
    assert isinstance(MissingComplete(), Backend) is False


def test_isinstance_rejects_a_backend_missing_aclose() -> None:
    """Given a class WITHOUT `aclose`: it does not conform."""
    # When / Then
    assert isinstance(MissingAclose(), Backend) is False


def test_isinstance_rejects_a_backend_missing_name() -> None:
    """Given a class with both coroutines but no `name`: it does not conform.

    A Protocol with a data member is still checked structurally, so `name` is a
    real requirement and not documentation.
    """
    # When / Then
    assert isinstance(MissingName(), Backend) is False


def test_runtime_checkable_checks_presence_not_signature() -> None:
    """Adversarial: right method NAMES with wrong signatures still conform.

    `runtime_checkable` does `hasattr(obj, attr)` per protocol member; it never
    compares parameter lists, return types or defaults. So a backend whose
    `complete()` accepts no arguments and returns `None` is reported as a
    `Backend`. Treat `isinstance(x, Backend)` as a *wiring* check only — signature
    conformance stays a static-typing concern.
    """
    # Given / When / Then
    assert isinstance(WrongArityBackend(), Backend) is True


# ---------------------------------------------------------------------------
# complete() is genuinely awaitable and yields a provider-labelled Choice
# ---------------------------------------------------------------------------


def test_conforming_backend_complete_is_awaitable_and_populates_provider_and_model() -> None:
    """Given a conforming backend: `await complete(...)` returns a Choice naming itself.

    `Choice.provider` / `Choice.model` are required by convention: every backend
    must populate them, because the brain reports the model that answered.
    """
    # Given
    backend = ConformingBackend()
    # When
    choice = asyncio.run(backend.complete([{"role": "user", "content": "hi"}], model="opencode/x"))
    # Then
    assert isinstance(choice, Choice)
    assert choice.content == "ok"
    assert choice.provider == "conforming"
    assert choice.model == "opencode/x"


# ---------------------------------------------------------------------------
# Module contents: the split is pinned
# ---------------------------------------------------------------------------


def test_base_module_does_not_contain_parse_choice_or_provider() -> None:
    """Given the relocated module: `parse_choice` did NOT move here.

    `parse_choice` is the OpenAI-shaped response parser and belongs to the
    `openai_compatible` backend (todo 4), which reuses it verbatim. Asserting its
    absence here is what stops a future edit from quietly re-merging the two.
    """
    # When / Then
    assert not hasattr(base, "parse_choice")
    assert not hasattr(base, "Provider")
    assert "parse_choice" not in Backend.__protocol_attrs__


def test_legacy_provider_base_file_is_gone() -> None:
    """Given the relocation: `core/providers/base.py` must no longer exist.

    Guards against the stale-state failure mode where the dataclasses survive in
    both modules and the two definitions silently drift apart.
    """
    # When / Then
    assert not (REPO_ROOT / "core" / "providers" / "base.py").exists()


# ---------------------------------------------------------------------------
# Type discipline
# ---------------------------------------------------------------------------


def test_tool_schema_alias_is_a_mapping_alias_not_a_bare_dict() -> None:
    """Given the public tool-schema type: it is a Mapping alias, as the plan requires."""
    # When / Then
    assert typing.get_origin(base.ToolSchema) is collections.abc.Mapping
    assert typing.get_args(base.ToolSchema) == (str, object)


def test_complete_signature_is_fully_annotated_and_free_of_bare_dict_and_any() -> None:
    """Given `Backend.complete`: every parameter and the return value are annotated,
    and the signature mentions neither a bare `dict` nor `Any`."""
    # When
    signature = inspect.signature(Backend.complete)
    rendered = str(signature)
    # Then
    assert signature.return_annotation is not inspect.Signature.empty
    for parameter_name, parameter in signature.parameters.items():
        if parameter_name == "self":
            continue
        assert parameter.annotation is not inspect.Parameter.empty, parameter_name
    assert "dict" not in rendered
    assert "Any" not in rendered


def test_aclose_signature_is_fully_annotated_and_returns_none() -> None:
    """Given `Backend.aclose`: annotated, and typed `-> None`."""
    # When
    signature = inspect.signature(Backend.aclose)
    # Then
    assert signature.return_annotation is None
    assert [p for p in signature.parameters if p != "self"] == []


# ---------------------------------------------------------------------------
# Dataclass defaults and mutation isolation
# ---------------------------------------------------------------------------


def test_tool_call_arguments_defaults_to_an_empty_dict() -> None:
    """Given a ToolCall built with only id and name: `arguments == {}`."""
    # When / Then
    assert ToolCall(id="call_1", name="system_status").arguments == {}


def test_tool_call_arguments_defaults_are_not_shared_between_instances() -> None:
    """Given two ToolCalls: mutating one's `arguments` must not touch the other.

    This is `default_factory=dict` doing its job; a literal `arguments: dict = {}`
    would fail here and leak state between every tool call in a conversation.
    """
    # Given
    first = ToolCall(id="a", name="f")
    second = ToolCall(id="b", name="f")
    # When
    first.arguments["shell"] = True
    # Then
    assert first.arguments is not second.arguments
    assert second.arguments == {}


def test_choice_defaults_are_empty_content_fields() -> None:
    """Given `Choice(content="x")`: every other field takes its documented default."""
    # When
    choice = Choice(content="x")
    # Then
    assert choice.content == "x"
    assert choice.tool_calls == []
    assert choice.provider == ""
    assert choice.model == ""
    assert choice.raw is None


def test_choice_tool_calls_defaults_are_not_shared_between_instances() -> None:
    """Given two Choices: appending to one's `tool_calls` must not touch the other."""
    # Given
    first = Choice(content="a")
    second = Choice(content="b")
    # When
    first.tool_calls.append(ToolCall(id="a", name="f"))
    # Then
    assert first.tool_calls is not second.tool_calls
    assert second.tool_calls == []


# ---------------------------------------------------------------------------
# Verbatim-parity of the relocated dataclasses
# ---------------------------------------------------------------------------

# The dataclass half of `core/providers/base.py` before the relocation, kept
# verbatim so parity is asserted against the original code rather than against a
# restatement of it.
LEGACY_DATACLASS_SOURCE = '''\
import json
from dataclasses import dataclass, field


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict = field(default_factory=dict)


@dataclass
class Choice:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    provider: str = ""
    model: str = ""
    raw: dict | None = None
'''

# `@dataclass` resolves postponed string annotations through `sys.modules[cls.__module__]`,
# so the exec'd legacy module must be registered while it executes.
_LEGACY_MODULE_NAME = "r2d2_legacy_core_providers_base"

EXPECTED_TOOL_CALL_FINGERPRINT = (
    ("id", "<required>"),
    ("name", "<required>"),
    ("arguments", "<factory>"),
)
EXPECTED_CHOICE_FINGERPRINT = (
    ("content", "<required>"),
    ("tool_calls", "<factory>"),
    ("provider", "''"),
    ("model", "''"),
    ("raw", "None"),
)


def _fingerprint(cls: type) -> tuple[tuple[str, str], ...]:
    """Return `(field_name, default_spec)` pairs in declaration order.

    `default_spec` is `"<required>"`, `"<factory>"` (any `default_factory`, which a
    fingerprint of the *value* could not distinguish) or `repr(default)`.
    """
    fingerprint: list[tuple[str, str]] = []
    for dataclass_field in dataclasses.fields(cls):
        if dataclass_field.default is not dataclasses.MISSING:
            spec = repr(dataclass_field.default)
        elif dataclass_field.default_factory is not dataclasses.MISSING:
            spec = "<factory>"
        else:
            spec = "<required>"
        fingerprint.append((dataclass_field.name, spec))
    return tuple(fingerprint)


def test_tool_call_field_names_order_and_defaults_match_the_legacy_definition() -> None:
    """Given the relocated ToolCall: names, order and defaults equal the original."""
    # When / Then
    assert _fingerprint(ToolCall) == EXPECTED_TOOL_CALL_FINGERPRINT


def test_choice_field_names_order_and_defaults_match_the_legacy_definition() -> None:
    """Given the relocated Choice: names, order and defaults equal the original."""
    # When / Then
    assert _fingerprint(Choice) == EXPECTED_CHOICE_FINGERPRINT


def test_relocated_dataclasses_are_structurally_identical_to_the_legacy_source() -> None:
    """Given the embedded original source: it executes to classes with the same shape.

    Independent of the hardcoded fingerprints above — this one execs the real old
    code, so a typo in the expected tuples cannot make the test pass vacuously.
    """
    # Given
    legacy = types.ModuleType(_LEGACY_MODULE_NAME)
    sys.modules[_LEGACY_MODULE_NAME] = legacy
    try:
        exec(compile(LEGACY_DATACLASS_SOURCE, "<legacy core/providers/base.py>", "exec"), legacy.__dict__)
    finally:
        del sys.modules[_LEGACY_MODULE_NAME]
    # When
    legacy_tool_call = legacy.ToolCall(id="a", name="f", arguments={"x": 1})
    tool_call = ToolCall(id="a", name="f", arguments={"x": 1})
    legacy_choice = legacy.Choice(
        content="x", tool_calls=[legacy.ToolCall(id="a", name="f")], provider="p", model="m", raw={"k": 1}
    )
    choice = Choice(content="x", tool_calls=[ToolCall(id="a", name="f")], provider="p", model="m", raw={"k": 1})
    # Then
    assert _fingerprint(legacy.ToolCall) == _fingerprint(ToolCall)
    assert _fingerprint(legacy.Choice) == _fingerprint(Choice)
    assert dataclasses.asdict(legacy_tool_call) == dataclasses.asdict(tool_call) == {
        "id": "a",
        "name": "f",
        "arguments": {"x": 1},
    }
    assert dataclasses.asdict(legacy_choice) == dataclasses.asdict(choice)
    assert repr(legacy_tool_call) == repr(tool_call)
    assert repr(legacy_choice) == repr(choice)
    assert legacy_tool_call == legacy.ToolCall(id="a", name="f", arguments={"x": 1})
    assert tool_call == ToolCall(id="a", name="f", arguments={"x": 1})


def test_relocated_dataclasses_keep_plain_dataclass_semantics() -> None:
    """Given the relocated classes: plain `@dataclass` — not frozen, not slotted."""
    # When
    tool_call_params = getattr(ToolCall, "__dataclass_params__")
    choice_params = getattr(Choice, "__dataclass_params__")
    # Then
    assert dataclasses.is_dataclass(ToolCall)
    assert dataclasses.is_dataclass(Choice)
    assert tool_call_params.frozen is False
    assert choice_params.frozen is False
    assert not hasattr(ToolCall, "__slots__")
    assert not hasattr(Choice, "__slots__")
