#!@R2D2_REPO@/.venv/bin/python
"""R2D2's tools as a one-line-JSON CLI: the ONLY sanctioned path from the
opencode agent to this machine (plan todo 11).

The installed copy lives at ``~/.r2d2/r2d2_do.py`` and is allowlisted in
``config/opencode/r2d2.opencode.json``, so it may run as the bare path, as
``python3 <path>`` or under the venv interpreter.  Every subcommand builds a
real ``ToolContext`` and calls an **existing** ``core/tools/*`` handler; no tool
logic is reimplemented here -- ``shell`` in particular goes through
``shell_tool.handler`` and therefore ``core.policies.risk_level`` unchanged.

THE VENV IS DISCOVERED, NEVER ASSUMED.  ``VENV_PY`` and the shebang above are
one template -- ``@R2D2_REPO@`` is the checkout this file belongs to -- and
``scripts/install.sh`` substitutes the real path into the copy it writes to
``~/.r2d2/r2d2_do.py``.  A template is what makes this file portable: a
committed absolute path is a path to somebody else's machine, and the failure it
caused was measured -- ``os.execv`` on a missing interpreter raises before any
handler runs, so the agent got a raw traceback on stderr and *nothing* on
stdout, which is the one thing the contract below forbids.  A copy that was
never substituted (run straight out of a checkout, or copied by hand) finds its
own venv from ``__file__`` instead, and one that cannot is told to run the
installer.  It is never re-exec'd into an interpreter that does not exist.

STDOUT CONTRACT -- exactly one line of JSON, nothing else::

    {"ok": bool, "text": str, "needs_confirm": bool, "pending": object|null, "error": str|null}

``text`` is what a human or Alice should say, ``pending`` is the machine
round-trip of a refused action, ``error`` is a diagnostic.  A usage error uses
the same shape: an agent that must parse stdout must never be handed a usage
paragraph or a traceback instead.  (``--help`` is the one exception -- argparse
usage, by design.)  EXIT CODES: ``0`` ok, ``1`` failure, ``2`` needs
confirmation and the command was NOT run.

GUARDS between an LLM-supplied string and an executed shell command, in order:
(1) ``shell`` calls ``shell_tool.handler`` unchanged, so ``risk_level`` decides:
a dangerous command returns ``needs_confirm`` + ``pending`` and exits 2 having
executed nothing -- and the WHOLE string is classified, so a trailing
``; rm -rf /`` is caught with the harmless head.  (2) ``--approved`` (which sets
``ToolContext(approved=True)``) is refused unless the operator put
``R2D2_APPROVED_TOKEN`` in this process's environment; the agent cannot set that
variable, so it cannot grant itself approval.  (3) A refusal is never silent:
exit 2 with the question in ``text``, so the agent relays it instead of
inventing a result.  (4) A failure is never dressed as success: an exception
becomes ``ok: false`` plus a redacted ``error``, never a traceback on stdout,
never exit 0.  (5) Configured credentials are redacted from everything emitted
-- stdout, stderr, any Telegram payload -- so a voice-injected ``cat .env``
cannot dump them into the agent's context or a chat; ``pending`` is exempt
because mangling the command would corrupt the confirmed retry.  (6) SIGINT and
SIGTERM during a long command end as one JSON error line and a non-zero exit, so
an interrupted run is never read as a clean one -- and since the gate refuses
before execution, no dangerous command ever starts to be cut short.

TEST SEAMS (inert unless set; ``tg`` delivery only, never the risk gate or
``shell``): ``R2D2_TEST_NO_TG=1`` records each would-be send as one JSON line
into ``R2D2_TEST_TG_LOG`` instead of calling Telegram, and
``R2D2_TEST_TG_RAISE=1`` makes that stub raise an exception embedding the bot
token, which is how the redaction is proven.  ``R2D2_LOG_LEVEL`` sets stderr.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, NoReturn

# --- venv re-exec, before ANY project import -------------------------------- #
# opencode's bash tool may run this file under the SYSTEM python, which has none
# of the project's dependencies; re-exec under the venv interpreter instead of
# failing. `R2D2_DO_REEXEC` is the loop guard. `sys.prefix` -- not
# `sys.executable`, which resolves to /usr/bin/python3.13 for BOTH interpreters on
# this machine and would let a dependency-less python3.13 skip the re-exec -- is
# what actually answers "am I already inside the venv".
EXIT_OK, EXIT_FAIL, EXIT_CONFIRM = 0, 1, 2

#
# `@R2D2_REPO@` below, and the shebang on line 1, are the SAME template: the
# installer substitutes the checkout it was run from, so the committed file names
# no machine. A plain string literal, not an f-string, because the test that pins
# the contract reads this line out of the source and the shebang must be the same
# text.
VENV_PY = "@R2D2_REPO@/.venv/bin/python"
VENV_DIR = Path(VENV_PY).parent.parent
REPO_ROOT = VENV_DIR.parent
#: A directory is believed to be this shim's checkout only if it holds the shim's
#: own source, so an unrelated `$HOME/.venv` cannot redirect `core` imports at
#: another project's tree.
CHECKOUT_MARKER = Path("opencode") / "r2d2_cli" / "r2d2_do.py"


def _own_venv() -> Path | None:
    """The checkout's venv, found from `__file__` alone; `None` when there is none.

    Nearest ancestor first, so a copy run straight out of a git clone -- never
    substituted -- re-execs instead of dying in `os.execv`.
    """
    for base in Path(__file__).resolve().parents:
        if not (base / CHECKOUT_MARKER).is_file():
            continue
        candidate = base / ".venv" / "bin" / "python"
        if candidate.is_file():
            return candidate
    return None


def _contract(ok: bool, text: str = "", *, needs_confirm: bool = False,
              pending: object | None = None, error: str | None = None) -> dict[str, object]:
    """The stdout contract as one object, for the guards that run before `ToolResult`."""
    return {"ok": ok, "text": text, "needs_confirm": needs_confirm,
            "pending": pending, "error": error}


if not Path(VENV_PY).is_file():
    # An unsubstituted copy: fall back to what `__file__` can prove. REPO_ROOT
    # stays derived from the venv, because that derivation is the contract.
    if (found := _own_venv()) is not None:
        VENV_PY, VENV_DIR, REPO_ROOT = str(found), found.parent.parent, found.parent.parent.parent

if os.environ.get("R2D2_DO_REEXEC") != "1" and Path(sys.prefix).resolve() != VENV_DIR.resolve():
    if not Path(VENV_PY).is_file():
        # Measured, not hypothetical: `os.execv` on a missing interpreter raises
        # before any handler runs, and the agent is then handed a traceback on
        # stderr and NOTHING on stdout -- the one output it cannot parse.
        _msg = (f"no project venv at {VENV_PY}; run `bash scripts/install.sh` from the "
                f"checkout, or invoke this shim through <checkout>/.venv/bin/python")
        print(json.dumps(_contract(False, error=_msg), ensure_ascii=False), flush=True)
        raise SystemExit(EXIT_FAIL)
    os.environ["R2D2_DO_REEXEC"] = "1"
    try:
        os.execv(VENV_PY, [VENV_PY, os.path.abspath(__file__), *sys.argv[1:]])
    except OSError as exc:  # a venv that vanished between the check and the call
        _msg = f"cannot re-exec into {VENV_PY}: {exc}"
        print(json.dumps(_contract(False, error=_msg), ensure_ascii=False), flush=True)
        raise SystemExit(EXIT_FAIL) from None
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

APPLICATION_ID_ENV = "R2D2_APPLICATION_ID"
APPROVED_ENV = "R2D2_APPROVED_TOKEN"
DEFAULT_APPLICATION_ID = "r2d2-cli"
TEST_NO_TG_ENV = "R2D2_TEST_NO_TG"
TEST_TG_RAISE_ENV = "R2D2_TEST_TG_RAISE"
TEST_TG_LOG_ENV = "R2D2_TEST_TG_LOG"
#: Environment variables whose values must never reach stdout, stderr or
#: Telegram. Names only -- the values are read from the environment at emit
#: time, never stored here.
SECRET_ENV_NAMES = (
    "TELEGRAM_BOT_TOKEN", "OPENROUTER_API_KEY", "YANDEX_API_KEY",
    "R2D2_ZEN_KEY", "R2D2_OC_PASSWORD", APPROVED_ENV,
)
MIN_SECRET_LENGTH = 8
REDACTED = "***"

try:
    from app.config import Config
    from core.memory import Memory
    from core.tools import arxiv_tool, laptop_tool, shell_tool
    from core.tools.base import ToolContext, ToolResult
    from core.tools.telegram_tool import send_message
except ImportError as exc:  # no project dependencies under this interpreter
    # The caller is an LLM, and a traceback on stderr with nothing on stdout is
    # the one failure it cannot act on -- so answer with the contract anyway.
    _line = _contract(False, error=f"R2D2 is not importable here ({exc}); use {VENV_PY}")
    print(json.dumps(_line, ensure_ascii=False), flush=True)
    raise SystemExit(EXIT_FAIL) from None

_LOGGER = logging.getLogger("r2d2_do")


class _UsageError(Exception):
    """argparse refused the command line; reported through the stdout contract."""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        # argparse's own behaviour (usage on stderr, exit 2) would collide with
        # exit 2 == needs confirmation and would leave stdout unparseable.
        raise _UsageError(f"{self.prog}: {message}")


def _redact(text: str) -> str:
    for name in SECRET_ENV_NAMES:
        secret = os.environ.get(name, "")
        if len(secret) >= MIN_SECRET_LENGTH:
            text = text.replace(secret, REDACTED)
    return text


def _record_send(text: str) -> None:
    """TEST SEAM: append one JSON line per would-be send; no-op without a path."""
    path = os.environ.get(TEST_TG_LOG_ENV)
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"text": text}, ensure_ascii=False) + "\n")


async def _send(cfg: Config, text: str) -> bool:
    """Deliver `text` to Telegram through the existing tool, redacting secrets."""
    if os.environ.get(TEST_NO_TG_ENV) == "1":
        _record_send(_redact(text))
        if os.environ.get(TEST_TG_RAISE_ENV) == "1":
            raise RuntimeError(f"telegram stub failed for bot {cfg.telegram_bot_token}")
        return True
    return await send_message(cfg, _redact(text))


@asynccontextmanager
async def _context(approved: bool) -> AsyncIterator[ToolContext]:
    cfg = Config.load()
    memory = await Memory(cfg.resolved_db_path()).connect()
    application_id = os.environ.get(APPLICATION_ID_ENV, DEFAULT_APPLICATION_ID)
    try:
        yield ToolContext(cfg, memory, application_id, _LOGGER, approved=approved)
    finally:
        await memory.close()


async def _cmd_status() -> ToolResult:
    async with _context(approved=False) as ctx:
        return await laptop_tool.handler(ctx, {})


async def _cmd_open_app(name: str) -> ToolResult:
    # `resolve_app` maps a name to a FIXED command from `laptop_tool.APPS` or
    # returns None; the caller's string is never spliced into a shell line.
    async with _context(approved=False) as ctx:
        return await laptop_tool.open_app(ctx, {"app": name})


async def _cmd_tg(text: str) -> ToolResult:
    if not text.strip():
        return ToolResult("Нечего отправлять.", ok=False)
    async with _context(approved=False) as ctx:
        if not await _send(ctx.cfg, text):
            return ToolResult(
                "Не смог отправить в Telegram.", ok=False, error="telegram send failed"
            )
    return ToolResult(f"Сообщение отправлено в Telegram ({len(text)} символов).")


async def _cmd_arxiv(query: str, days: int, max_results: int) -> ToolResult:
    async with _context(approved=False) as ctx:
        planned = await arxiv_tool.handler(
            ctx, {"query": query, "days": days, "max_results": max_results}
        )
        if not planned.ok or not planned.job:
            return planned
        job = planned.job
        entries = await arxiv_tool.arxiv_fetch(
            job["query"], max_results=job["max_results"], days=job["days"]
        )
        if not entries:
            return ToolResult(
                f"По запросу «{job['query']}» за последние {job['days']} дней статей на arxiv не нашёл."
            )
        summary = await arxiv_tool.summarize_entries(ctx.cfg, job["query"], entries)
        return ToolResult(arxiv_tool.build_digest(job["query"], entries, summary))


async def _cmd_shell(command: str, approved: bool) -> ToolResult:
    async with _context(approved=approved) as ctx:
        result = await shell_tool.handler(ctx, {"command": command})
        # The stdout contract has no `tg_send` key, so long output is delivered
        # rather than dropped; the short ack stays in `text`.
        if result.tg_send:
            await _send(ctx.cfg, result.tg_send)
        return result


async def _dispatch(args: argparse.Namespace) -> ToolResult:
    match args.subcommand:
        case "status":
            return await _cmd_status()
        case "open-app":
            return await _cmd_open_app(args.name)
        case "tg":
            return await _cmd_tg(args.text)
        case "arxiv":
            return await _cmd_arxiv(args.query, args.days, args.max_results)
        case "shell":
            return await _cmd_shell(args.command, args.approved)
        case _:
            raise AssertionError(f"unhandled subcommand {args.subcommand!r}")


def _payload(result: ToolResult) -> dict[str, object]:
    return _contract(
        bool(result.ok), _redact(result.text),
        needs_confirm=bool(result.needs_confirm), pending=result.pending,
        error=_redact(result.error) if result.error else None,
    )


def _emit(result: ToolResult) -> int:
    """Print the single line of stdout and map the result onto the exit table."""
    print(json.dumps(_payload(result), ensure_ascii=False), flush=True)
    if result.needs_confirm:
        return EXIT_CONFIRM
    return EXIT_OK if result.ok else EXIT_FAIL


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="r2d2_do",
        description="R2D2 tools for the opencode agent. One line of JSON on stdout.",
    )
    subcommands = parser.add_subparsers(dest="subcommand", required=True)
    subcommands.add_parser("status", help="заряд, cpu, память, аптайм")
    open_app = subcommands.add_parser("open-app", help="запустить приложение из списка")
    open_app.add_argument("name", help="browser, код, терминал, телеграм, spotify...")
    tg = subcommands.add_parser("tg", help="отправить сообщение в телеграм")
    tg.add_argument("text", help="текст сообщения, отправляется дословно")
    arxiv = subcommands.add_parser("arxiv", help="сводка статей с arxiv по запросу")
    arxiv.add_argument("query")
    arxiv.add_argument("--days", type=int, default=7, help="сколько дней смотреть")
    arxiv.add_argument("--max", dest="max_results", type=int, default=5, help="сколько статей")
    shell = subcommands.add_parser("shell", help="команда через риск-гейт R2D2 (опасное -> 2)")
    shell.add_argument("command", help="команда; опасная потребует подтверждения")
    shell.add_argument("--approved", action="store_true", help=(
        "только для повтора после подтверждения пользователем; отклоняется без "
        f"{APPROVED_ENV} в окружении"))
    return parser


def _interrupt(signum: int, _frame: object) -> NoReturn:
    raise KeyboardInterrupt(f"signal {signum}")


def _install_signal_handlers() -> None:
    """Own SIGINT as well as SIGTERM, deterministically.

    `asyncio.run` hijacks SIGINT: it cancels the main task and swallows the first
    interrupt, and whether the run then dies or limps on to `cfg.shell_timeout`
    with a misleading "timeout" depends on where in the event loop the signal
    landed -- measured at 1.6s on one run and 10.6s on the next, same input.
    Owning the handler suppresses `Runner`'s hijack (it only installs one while
    SIGINT is still the default), so an interrupt always raises at once.
    """
    signal.signal(signal.SIGINT, _interrupt)
    signal.signal(signal.SIGTERM, _interrupt)


def _log_level() -> int:
    """The stderr level; an unparsable R2D2_LOG_LEVEL is not worth a traceback."""
    return logging.getLevelNamesMapping().get(os.environ.get("R2D2_LOG_LEVEL", "").upper(),
                                             logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        stream=sys.stderr,
        level=_log_level(),
        format="r2d2_do %(levelname)s %(message)s",
        force=True,
    )
    try:
        args = _parser().parse_args(argv)
    except _UsageError as exc:
        _LOGGER.error("%s", exc)
        return _emit(ToolResult("", ok=False, error=str(exc)))
    if getattr(args, "approved", False) and not os.environ.get(APPROVED_ENV):
        return _emit(ToolResult("", ok=False, error=(
            f"--approved is refused: {APPROVED_ENV} is not set, so this is not a user-confirmed "
            "retry. Relay the confirmation question and re-run once the user has said yes.")))
    _install_signal_handlers()
    try:
        return _emit(asyncio.run(_dispatch(args)))
    except KeyboardInterrupt as exc:
        return _emit(ToolResult("", ok=False, error=f"прервано: {str(exc) or 'сигнал'}"))
    except Exception as exc:
        _LOGGER.exception("command failed")
        return _emit(ToolResult("", ok=False, error=f"{type(exc).__name__}: {exc}"))


if __name__ == "__main__":
    raise SystemExit(main())
