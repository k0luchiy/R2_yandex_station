"""How risky a command is, what a human's reply to a question means, and whether a
command would print a credential.

Three decisions that share a file because all three are about a human saying
something short and consequential, or about a human about to say it: `risk_level`
decides whether a command is confirmed out loud before it runs,
`confirmation_verdict` decides whether a reply is an answer to a question R2D2
already asked, and `credential_source` decides whether an ask is a question R2D2
is willing to put to a human at all.

**The confirmation reader matches WHOLE WORDS, and the affirmative side is the
reason.** A «да» approves a command the user never agreed to run, so a word may
only be found as a word: `да` is a substring of `дай`, `задача`, `награда`,
`удар`, `сдать`, and `ок` is a substring of `рокировка`. Read as substrings, an
ordinary sentence approved a pending permission -- proved live in
`qa/live-run.md` §4d, where `дай сводку` was posted to opencode as `once`. The
message is cut into word tokens instead, so casing, punctuation, emoji and
whitespace are all outside the comparison and cannot create a match.

`DENY_WORDS` is still read first, so `да, не надо` is a refusal, and a message
with no confirmation word in it is `None` rather than a guess: `None` is what
leaves a pending ask pending for the real answer, and what the timeout sweep
later refuses.

**`credential_source` is a closed list of SOURCES, not a list of scary commands,
and that is the whole argument.** The harm it exists for is measured
(`qa/live-run-v9.md` F2): the agent asked to run
`env | grep -iE 'telegram|tg_|bot'`, the user said `да`, and the Telegram bot
token landed in the opencode transcript -- a store that is permanent, that the
model re-reads on every later turn, and that anybody with the server's HTTP basic
password can read. So the question is not "does this command look dangerous"; it
is "would this command's output be somewhere a secret lives". There are exactly
two such places in this deployment: the process environment the agent's `bash`
inherits from the opencode server, and the files the owner keeps credentials in.
Both are named below, both are short, and both can be read in one sitting -- which
is what makes the barrier auditable, and what a growing denylist of "commands
that look like exfiltration" never is. A denylist is the wrong shape here because
the shapes are unbounded and the SOURCES are not.

**The cost is stated rather than hidden, twice.** It is a *refusal to ask*, so a
question the user would have said «да» to never reaches them: the agent is told
the call was refused and moves on, which is the direction this project already
fails in everywhere else. And the barrier is sound against the measured path and
against its obvious neighbours, not against a determined rewrite -- `printenv`,
`env`, `/proc/self/environ` and `cat .env` are closed, `python3 -c "import os;
print(os.environ)"` is not, and no reader should take the second sentence as
quieted. The structural closure is not in this file: it is that the opencode
server should not hold R2D2's other credentials in its environment at all, and
that is a deployment fact (`scripts/r2d2-opencode.service`, `.env.oc`) rather
than a line of code here.
"""

import re
from collections.abc import Iterable

SAFE_PREFIXES = (
    "ls", "pwd", "df", "ps", "cat", "echo", "date", "uptime", "whoami",
    "free", "du", "uname", "hostname", "git status", "git diff",
)

RISKY_KEYWORDS = (
    "rm ", "rm -", "rmdir", "mkfs", "shutdown", "sudo", "reboot", "kill",
    "pkill", "dd ", "chmod 777", "chmod +x", "chown", "mount", "fdisk",
    "mke2fs", "format", "wipe", "> /dev", "crontab",
)

CAUTION_KEYWORDS = (
    "systemctl", "service ", "apt ", "dnf ", "pacman", "pip install",
    "git pull", "npm install", "killall",
)

AFFIRM_WORDS = (
    "да", "подтверждаю", "подтверди", "выполняй", "валяй", "давай", "ок",
    "окей", "согласен", "конечно", "точно", "выполнить", "выполняем", "ага",
    "да да", "угу",
)

DENY_WORDS = (
    "нет", "не надо", "отмена", "отмени", "отменить", "стоп", "хватит",
    "не", "неа", "остановись",
)

#: Shell command words that PRINT the process environment. The `bash` tool runs as a
#: child of the opencode server, so its environment is the server's -- and that is
#: where `R2D2_OC_PASSWORD` lives, and, on a deployment that exports them into the
#: unit, the Telegram token with it. Whole names and not patterns, because the head
#: of a command is what decides: `set -e` is not a leak and is refused anyway, which
#: is the right direction for a barrier.
ENVIRONMENT_COMMANDS: tuple[str, ...] = (
    "env", "printenv", "set", "export", "declare", "typeset", "compgen", "envsubst",
)

#: Files this deployment keeps a credential in. An entry is either an exact basename
#: (`.env`, `id_rsa`) or a `*` suffix (so `*.pem` matches `server.pem` and not
#: `.pem` alone), matched against the basename of any path token -- which is why
#: `/srv/environments/app.yaml` is not a `.env` and `id_rsa` does not match
#: `id_rsa_note.txt`. `.example` is excluded by `_is_example` rather than by not
#: being listed: the committed examples are documentation and the matrix allows
#: reading them on purpose, so blocking them would teach the agent to route around
#: a barrier instead of around the thing the barrier is for.
CREDENTIAL_FILES: tuple[str, ...] = (
    ".env", ".env.oc", "auth.json", "credentials", ".netrc", ".pgpass",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
    "*.pem", "*.key", "*.p12", "*.pfx", "*.keystore",
)

#: A shell variable whose NAME says it holds a credential. This is a shape and not a
#: list of the deployment's own names on purpose: every one of them
#: (`TELEGRAM_BOT_TOKEN`, `R2D2_OC_PASSWORD`, `R2D2_ZEN_KEY`, `OPENROUTER_API_KEY`,
#: `YANDEX_API_KEY`) matches, and so does the next one added to a unit file, which a
#: list of names would have to be edited to catch. `$HOME` and `$PATH` are what keep
#: the shape usable -- a command that interpolates any variable at all would refuse
#: half the shell.
CREDENTIAL_VARIABLE = re.compile(
    r"\$(?:\{)?[A-Za-z_][A-Za-z0-9_]*(?:TOKEN|PASSWORD|PASSWD|SECRET|CREDENTIAL|API_?KEY|_KEY)(?:[A-Za-z0-9_]*)?\}?\b"
)

#: `/proc/<pid>/environ` is that same environment reached through the filesystem, so
#: it is a source in its own right and not a special case of `CREDENTIAL_FILES`.
#: Anchored on `/proc/` and on the whole final component, because a directory named
#: `environments` is an ordinary word and a substring test would refuse `ls` on one.
ENVIRON_PATH: str = "/proc/"
ENVIRON_NAME: str = "environ"

#: What separates two stages of a shell line. opencode's `bash` takes a POSIX shell,
#: so a pipeline, a list, a subshell and a command substitution are the four ways a
#: second command hides inside the first -- and each is its own head word.
_STAGE_SEPARATORS = re.compile(r"[|;&()\n`$]|\$\{|\|>")


def _head_word(stage: str) -> str:
    """The command `stage` would run: its first token, without a directory prefix."""
    tokens = stage.strip().split()
    return tokens[0].rsplit("/", 1)[-1] if tokens else ""


def _is_example(path: str) -> bool:
    """Whether a path token is one of the committed `*.example` files."""
    return path.rsplit("/", 1)[-1].endswith(".example")


def _is_credential_file(token: str) -> bool:
    """Whether a path token names a file in `CREDENTIAL_FILES`, whole-basename only."""
    name = token.rsplit("/", 1)[-1]
    return any(
        name.endswith(entry[1:]) if entry.startswith("*") else name == entry
        for entry in CREDENTIAL_FILES
    )


def _is_process_environment(token: str) -> bool:
    """Whether a path token is the `/proc/<pid>/environ` pseudo-file."""
    return ENVIRON_PATH in token and token.endswith(f"/{ENVIRON_NAME}")


def credential_source(command: str) -> str | None:
    """The credential this command could print, or `None` if it reads nothing secret.

    A SOURCE and not a verdict about danger: the string is what the broker tells the
    user it refused to ask about, so it names a place and never a value. The value is
    read out of R2D2's own environment by a subprocess R2D2 does not run and cannot
    scrub afterwards, which is why this is decided BEFORE the question is asked.

    Two passes, and the second exists because the first cannot see the case that
    matters most: `cat .env` has a harmless head word and a credential in its
    argument. Head words are read per pipeline stage, so `env | grep token` and
    `foo && printenv` are both caught; path basenames are read on every token of
    every stage; and `/environ` is matched on the way through.

    The variable pass closes the same environment reached without naming it:
    `echo $TELEGRAM_BOT_TOKEN` prints the token with no `env`, no path, and a head
    word on the allowlist. It is the same SOURCE as `env`, so it returns the same
    string rather than a second kind of answer, and it is the pass whose absence
    would make the first two look complete -- a reader who tested `cat .env` and
    `env | grep` would otherwise believe the list is closed.
    """
    if CREDENTIAL_VARIABLE.search(command):
        return "окружение процесса"
    stages = _STAGE_SEPARATORS.split(command)
    for stage in stages:
        if stage.strip() and _head_word(stage) in ENVIRONMENT_COMMANDS:
            return "окружение процесса"
    for stage in stages:
        for token in stage.split():
            if _is_example(token):
                continue
            if _is_process_environment(token):
                return "окружение процесса"
            if _is_credential_file(token):
                return "файл с учётными данными"
    return None


def risk_level(command: str) -> str:
    low = command.strip().lower()
    low = re.sub(r"\s+", " ", low)
    if not low:
        return "safe"
    if any(k in low for k in RISKY_KEYWORDS):
        return "dangerous"
    if any(k in low for k in CAUTION_KEYWORDS):
        return "caution"
    return "safe"


#: A word is a maximal run of word characters, so a confirmation word can only be
#: matched whole: anything that is not a word character -- a space, a comma, a
#: full stop, a hyphen, an emoji -- ends the token instead of joining it.
_WORD = re.compile(r"\w+")


def _phrases(words: Iterable[str]) -> dict[int, frozenset[tuple[str, ...]]]:
    """A word set as token tuples, grouped by how many words each entry is.

    Grouping by length is what lets a multi-word entry (`да да`, `не надо`) be
    matched as a phrase without turning every single word into a prefix test
    again -- the two sets above stay the only vocabulary, and nothing here
    hard-codes a word of its own.
    """
    grouped: dict[int, set[tuple[str, ...]]] = {}
    for word in words:
        tokens = tuple(word.casefold().split())
        if tokens:
            grouped.setdefault(len(tokens), set()).add(tokens)
    return {width: frozenset(phrases) for width, phrases in grouped.items()}


_AFFIRM_PHRASES = _phrases(AFFIRM_WORDS)
_DENY_PHRASES = _phrases(DENY_WORDS)


def _mentions(tokens: list[str], phrases: dict[int, frozenset[tuple[str, ...]]]) -> bool:
    """Whether any run of adjacent tokens is one of the set's phrases."""
    return any(
        tuple(tokens[start : start + width]) in wanted
        for width, wanted in phrases.items()
        for start in range(len(tokens) - width + 1)
    )


def confirmation_verdict(text: str | None) -> str | None:
    """What a reply decides: `"yes"`, `"no"`, or `None` when it decides nothing.

    `None` is a real answer and not a shrug: `core/permissions.py` turns it into
    `unrelated`, which leaves the pending ask exactly where it was -- still
    answerable, and refused by the timeout sweep if nobody answers it. Guessing
    is what the substring reader did, and an approval guessed out of an ordinary
    sentence is arbitrary code execution on somebody's laptop.

    Denial is read before agreement, because `DENY_WORDS` holds the bare `не` and
    `да, не надо` is a refusal in Russian.
    """
    if not text:
        return None
    tokens = _WORD.findall(text.casefold())
    if _mentions(tokens, _DENY_PHRASES):
        return "no"
    if _mentions(tokens, _AFFIRM_PHRASES):
        return "yes"
    return None
