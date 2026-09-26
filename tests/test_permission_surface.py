"""The permission seam's public surface, pinned across the three-module split.

`core/permissions.py` used to hold the persisted record, the sentences the user
reads and the broker in one file. It is three files now, and the thing that rots
silently is not the behaviour -- `tests/test_permission_broker.py` covers that
against the real client, the real `Memory` and the real Telegram route -- but the
SURFACE around it: a name that walked out of the module three callers import from,
a re-export that quietly turned a definition into an alias, or a stored row that no
longer reads back. None of those fails loudly; all of them are somebody's
three-line diff six weeks from now.

So these are structural checks, deliberately not behavioural ones:

* every name the module exposed before the split still resolves there, so
  `core/brain.py`, `app/opencode_route.py` and the existing suite need no edit;
* a moved name is an ALIAS of the new module's object, not a copy of it;
* the broker is still DEFINED in `core/permissions.py`, because that file's source
  is what `test_the_module_cannot_answer_with_a_durable_grant` parses -- a
  re-export would leave those guards reading a file that no longer holds the
  answer seam, which is the shape of a security test that quietly checks nothing;
* the two new modules import nothing from this project, so neither can close an
  import cycle through the broker;
* `as_record` still writes the six fields of a row an older build already wrote.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Final

from core import pending_permission, permission_words, permissions

#: Every non-private name `core/permissions.py` exposed before the split, `__all__`
#: or not: the declared surface plus the constants the broker itself reads. A name
#: that moved to a leaf is still on this list, because the old path has to keep
#: serving whoever imported it from there.
SURFACE: Final[frozenset[str]] = frozenset({
    "ACCEPTED", "ANSWERS", "APPROVE_ONCE", "APPROVED", "ASK_SOURCE", "DURABLE_GRANT",
    "KIND", "PREVIEW_CHARS", "PendingPermission", "PermissionAnswer", "PermissionBroker",
    "PermissionVerdict", "QUESTION", "REFUSE", "REFUSED", "REJECTED", "REJECTED",
    "SWEEP_INTERVAL_S", "UNANSWERABLE", "UNANSWERED", "UNRELATED", "UNTITLED",
})

#: The session and permission U4 recorded, and the mask it offered for the ask.
SESSION_ID: Final = "ses_f266522f4ffee31XNquJ1D3qjC"
PERMISSION_ID: Final = "per_0d99aed41001iOUiwXDd6vx2NP"
TITLE: Final = "echo R2D2SPIKERUN3K"
MASKS: Final = ("echo *",)
REQUESTED_AT: Final = 1_700_000_000.0


def test_every_name_the_old_module_exposed_still_resolves_there():
    # Given: the surface `core/permissions.py` exposed before the split
    # When: each name is looked up on the module the old callers import from
    missing = sorted(name for name in SURFACE if not hasattr(permissions, name))
    # Then: not one of them costs a call site an edit
    assert missing == [], f"core.permissions no longer exposes {missing}"


def test_a_moved_name_is_an_alias_and_not_a_copy():
    # Given: the two leaves that own what moved out of the broker's module
    # When / Then: the old path hands out the very same objects, so a value built
    # through one path is the value the broker's annotations mean
    assert permissions.PendingPermission is pending_permission.PendingPermission
    assert permissions.KIND is pending_permission.KIND
    assert permissions.QUESTION is permission_words.QUESTION
    assert permissions.preview is permission_words.preview
    assert permissions.PREVIEW_CHARS is permission_words.PREVIEW_CHARS


def test_the_broker_is_still_defined_where_the_source_guards_read_it():
    # Given: `tests/test_permission_broker.py`, which parses this file's source for
    # the four answer paths, the one call to the wire and the absent third value
    source = Path(permissions.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    # When: what this file defines
    defined = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }
    # Then: the class and its three answer-path internals are DEFINED here, so those
    # guards are reading the code they were written to police
    assert {"PermissionBroker", "_post", "_tell", "_claim_for"} <= defined


def test_the_two_new_modules_are_leaves_and_cannot_close_a_cycle():
    # Given: the modules the broker now imports
    # When: their import statements are read
    project_imports = [
        (module.__name__, node.module)
        for module in (pending_permission, permission_words)
        for node in ast.walk(ast.parse(Path(module.__file__).read_text(encoding="utf-8")))
        if isinstance(node, ast.ImportFrom) and node.module
        and node.module.split(".")[0] in {"app", "core"}
    ]
    # Then: nothing in this project is reachable from either of them, so the record
    # and the sentences sit below the broker instead of beside it
    assert project_imports == []


def test_the_stored_row_is_the_one_an_older_build_already_wrote():
    # Given: an ask written by any build, before or after the split
    ask = permissions.PendingPermission(SESSION_ID, PERMISSION_ID, TITLE, MASKS, REQUESTED_AT)
    # When: it is stored, and read back
    record = ask.as_record()
    # Then: the blob is field for field the one in every existing pending row, and
    # it reads back as the same value -- a renamed key would orphan a stored ask
    assert record == {
        "kind": "opencode_permission",
        "session_id": SESSION_ID,
        "permission_id": PERMISSION_ID,
        "title": TITLE,
        "always": list(MASKS),
        "requested_at": REQUESTED_AT,
    }
    assert permissions.PendingPermission.from_record(record) == ask
