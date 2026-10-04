"""Who this process serves, and how it says so before anybody asks: one human,
one `application_id`, DECLARED and never derived.

The two functions here are the only place R2D2 decides *whose* a request is, and
they are one decision on two channels. `tg_application_id` resolves a Telegram
chat to the `application_id` its Alice turns already arrive under;
`authorisation_posture` reports whether the Alice side has declared one at all.
Nothing else in R2D2 invents an identity and nothing else has to: `Brain` falls
back to `user_id` when `application_id` is absent, so an id minted from a chat id
anywhere -- a header, a session title, a row -- becomes a SECOND opencode session
for the same person, and puts every «да» in a session the permission question was
never asked in.

**Why this is a module and not two functions in the composition root.** The rule
has three readers and no owner otherwise: `app/http.py` refuses a Telegram turn
that has no binding, the composition root logs the posture once at startup, and
`app/diagnostics.py` reports the SHAPE of the declaration without resolving it.
A fact three readers need is a fact someone has to own, and the owner should not
be the module that also builds the ASGI object.

**Both names stay importable from `app/main.py`,** which is where they lived
before and where two test modules still reach for them by name. `app/main.py`
re-exports them rather than leaving a reader with two ways to spell one function;
the bodies live here so that a reader looking for the rule finds the rule and not
a composition root.
"""

from __future__ import annotations

import logging
import re
from typing import Final

from app.config import Config
from app.diagnostics import TG_APPLICATION_ID_VAR

__all__ = ["TG_BINDING_SEPARATORS", "authorisation_posture", "tg_application_id"]

logger = logging.getLogger("r2d2")

#: What separates the `chat_id=application_id` pairs of that declaration.
TG_BINDING_SEPARATORS: Final = re.compile(r"[,\s]+")


def tg_application_id(cfg: Config, chat_id: int) -> str | None:
    """The identity this Telegram chat was bound to, or `None` for no binding.

    The identity is `session.user.user_id`, NOT
    `session.application.application_id`: memory is keyed on the user id, and the
    platform scopes the application id to one app, so a phone and a Station would
    otherwise be two different people here. It is printed at startup as
    `alice_user=`.

    **The binding is declared, never derived.** One human is one user id,
    and that id is what owns their single opencode session and their single pending
    permission question -- so a Telegram turn and an Alice turn of the same person
    have to arrive under the same one, or the answer to «да» is delivered to a
    session the question was never asked in. `/tg/webhook` used to build
    `f"tg:{chat_id}"` for itself, which is the live defect in `qa/live-run.md` §4c.

    `cfg.r2d2_tg_application_id` holds `chat_id=application_id` pairs separated by
    commas or whitespace; a single-user deployment has exactly one, and a second
    chat id is bound by declaring a second pair rather than by being invented. An
    id may not contain a separator -- it is an opaque Alice `application_id`, and a
    space in one would silently truncate the binding.

    `None` is the honest answer for a chat nobody declared, and the caller must
    refuse the turn on it rather than fall back to anything. A token this build
    cannot read is a WARNING naming the variable; it binds nothing, so one typo
    cannot be mistaken for a declaration.
    """
    for token in TG_BINDING_SEPARATORS.split(cfg.r2d2_tg_application_id.strip()):
        if not token:
            continue
        declared_chat, separator, app_id = token.partition("=")
        if not separator or not declared_chat.isdigit() or not app_id.strip():
            logger.warning(
                "telegram: %s carries a token this build cannot read (%r); it expects "
                "chat_id=application_id, and this token binds nothing",
                TG_APPLICATION_ID_VAR, token,
            )
            continue
        if int(declared_chat) == chat_id:
            return app_id.strip()
    return None


def authorisation_posture(cfg: Config) -> str | None:
    """What to tell the operator at startup about who this webhook serves, or `None`.

    `Brain.authorized` fails closed, so an undeclared id is a closed webhook and not
    an open one -- which is the safe state, and also a state that looks like a broken
    skill to anyone who has not read the source. Saying it once, at startup, with the
    variable names in the message, is what turns "my skill stopped answering" into
    "I have not told it who I am".

    Returns `None` when the deployment is configured, and a message when it is not.
    The message is an operator instruction, not a diagnostic, so it is phrased as the
    command to run and never mentions an id value.
    """
    if cfg.r2d2_allow_unauthenticated:
        return (
            "R2D2_ALLOW_UNAUTHENTICATED is set: /webhook answers ANY caller, and the "
            "opencode agent and the r2d2_do shim are reachable behind it. Set "
            "ALICE_SKILL_ID and ALICE_USER_ID and unset this before exposing the port."
        )
    missing = [
        name for name, value in
        (("ALICE_SKILL_ID", cfg.alice_skill_id), ("ALICE_USER_ID", cfg.alice_user_id))
        if not value
    ]
    if not missing:
        return None
    return (
        f"{' and '.join(missing)} not set: /webhook refuses every request, because an "
        "undeclared id is nobody in particular. Register the private skill at "
        "dialogs.yandex.ru, put its skill_id and your user_id in .env, and restart. "
        "To drive the voice path before registering, set R2D2_ALLOW_UNAUTHENTICATED=1 "
        "and keep the port on loopback."
    )
