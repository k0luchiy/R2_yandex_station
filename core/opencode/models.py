"""Which model ids the opencode server actually offers -- plan todo 9's C1 gate.

Spike correction C1 is the defect this module exists for. opencode treats an
unknown `modelID` as a non-event: it substitutes a different model, answers
**HTTP 200** and the reply reads like a normal answer. Nothing downstream can
notice, so the only safe moment to catch it is before the first turn, against
`GET /config/providers`.

**What this gate can and cannot prove, and why the two failures are different
classes.** It proves EXISTENCE: the configured id is one the server lists. It
cannot prove USABILITY. The todo-1 sweep measured `muse-spark-1.3-contributor-free`
listed with `status: "active"` and refusing every call with an inner 403
`FreeTierError` (`docs/11-opencode-contract.md`, U6), so on this machine a
listed model and a usable model are the same set of one only by luck. No startup
check on this route can separate them, and pretending otherwise would be inventing
certainty. The usable half is caught where it is observable instead -- the turn
itself -- by `OpencodeClient.send_message` reading `info.error`, which surfaces as
`OpencodeErrorEnvelope` rather than a spoken answer.

So the three failures here are three different operator problems:

* `OpencodeModelUnconfigured` -- a slot has no id at all, so opencode would answer
  from its own default. A config omission, fixable in `config/backends.json`.
* `OpencodeModelUnknown` -- the catalogue is readable and the id is not in it.
  A typo or a model this server build does not carry.
* `OpencodeModelCheckFailed` -- the catalogue could not be read or parsed. The
  check itself failed, so reporting "unknown model" would send the operator after
  a config typo that is not the cause -- and silently assuming "fine" is exactly
  the misleading success C1 describes.

The catalogue is fetched once and parsed once per provider, so a three-slot config
costs exactly one `GET /config/providers`.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Final

import httpx

from core.backends.config_loader import BackendSpec
from core.opencode.client import OpencodeClient
from core.opencode.wire import OpencodeError, split_model

__all__ = [
    "MODEL_ROLES",
    "OpencodeModelCheckFailed",
    "OpencodeModelUnconfigured",
    "OpencodeModelUnknown",
    "verify_models",
]

log = logging.getLogger(__name__)

#: The configured model slots, in the order they are reported when wrong.
MODEL_ROLES: Final[tuple[str, ...]] = ("fast_model", "task_model", "summarize_model")


class OpencodeModelUnconfigured(OpencodeError):
    """A model slot has no id, so opencode would pick its own default (C1)."""


class OpencodeModelUnknown(OpencodeError):
    """`GET /config/providers` does not list a configured model -- C1, at startup.

    The one model failure that is cheap to detect: the id is absent, so opencode
    substitutes another model and answers 200, and nothing downstream ever does.
    """


class OpencodeModelCheckFailed(OpencodeError):
    """`GET /config/providers` could not be read, so nothing could be verified.

    Deliberately NOT the same class as `OpencodeModelUnknown`: "I could not check"
    and "the model is absent" send the operator to different places, and reporting
    the second when the first happened would send them after a config typo that is
    not the cause.
    """


async def verify_models(spec: BackendSpec, client: OpencodeClient) -> tuple[str, ...]:
    """Every model `spec` configures, checked against the server; return them.

    Raises `OpencodeModelUnconfigured`, `OpencodeModelUnknown` or
    `OpencodeModelCheckFailed` as described in the module docstring, naming the
    offending slot. The return value is the DISTINCT model ids that were verified,
    which is what the caller logs.
    """
    roles = {role: getattr(spec, role) for role in MODEL_ROLES}
    unconfigured = [role for role, model in roles.items() if not model]
    if unconfigured:
        raise OpencodeModelUnconfigured(
            f"opencode {spec.name!r}: no model configured for {', '.join(unconfigured)}; "
            "opencode would answer from its own default model, which R2D2 does not choose (C1)"
        )
    offered = _Catalogue(await _providers(spec, client), spec.name)
    unknown = [
        f"{role}={model!r}" for role, model in roles.items() if not offered.has(model)
    ]
    if unknown:
        raise OpencodeModelUnknown(
            f"opencode {spec.name!r}: GET /config/providers does not list {', '.join(unknown)}; "
            "an unknown modelID is not an error to opencode (it answers 200 from a different model), "
            "so R2D2 would answer from a brain nobody chose (C1)"
        )
    verified = tuple(dict.fromkeys(roles.values()))
    log.info("opencode %s: models verified against the server: %s", spec.name, verified)
    return verified


async def _providers(spec: BackendSpec, client: OpencodeClient) -> Mapping[str, object]:
    """`GET /config/providers`, or a typed failure that is NOT "unknown model"."""
    try:
        return await client.providers()
    except (httpx.HTTPError, OpencodeError) as exc:
        raise OpencodeModelCheckFailed(
            f"opencode {spec.name!r}: GET /config/providers could not be read ({exc}), so it is "
            "UNKNOWN whether the configured models exist; refusing to assume they do"
        ) from exc


class _Catalogue:
    """`GET /config/providers` read once, answering "is this model offered?".

    Owns the per-provider parse so it happens exactly once per provider even when
    all three slots point at the same one -- which is the shipped config, and the
    reason this is a class with state rather than a function with a cache
    argument.
    """

    def __init__(self, document: Mapping[str, object], backend: str) -> None:
        self._document = document
        self._backend = backend
        self._models: dict[str, frozenset[str]] = {}

    def has(self, model: str) -> bool:
        provider, model_id = split_model(model)
        if provider not in self._models:
            self._models[provider] = self._listed(provider)
        return model_id in self._models[provider]

    def _listed(self, provider: str) -> frozenset[str]:
        """The model ids the document lists for `provider`.

        The measured answer is a list of provider objects under `"providers"`; the
        document itself is accepted as the list too, since that is the other shape
        the spike probe had to handle. Anything else is a failure to READ, not a
        missing model: a catalogue this code cannot parse must not be reported as
        "your model does not exist", which would send the operator to fix a config
        typo that is not the cause.
        """
        listed = self._document.get("providers", self._document)
        if not isinstance(listed, list) or not all(isinstance(e, Mapping) for e in listed):
            raise OpencodeModelCheckFailed(
                f"opencode {self._backend!r}: GET /config/providers did not answer with a list of "
                f"providers, so it is unknown whether {provider!r} has any models"
            )
        for entry in listed:
            if (entry.get("id") if "id" in entry else entry.get("_id")) != provider:
                continue
            models = entry.get("models")
            if not isinstance(models, Mapping):
                raise OpencodeModelCheckFailed(
                    f"opencode {self._backend!r}: GET /config/providers lists {provider!r} without a "
                    "'models' object, so its model ids could not be read"
                )
            return frozenset(str(model_id) for model_id in models)
        return frozenset()
