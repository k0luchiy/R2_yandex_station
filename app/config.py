import os
from dataclasses import dataclass, fields
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _expand_home(value: str) -> str:
    """`~` and `$HOME`/`${HOME}` in a configured path, expanded once.

    The installer writes `${HOME}/…` into `.env.oc.example` and `scripts/install.sh`
    renders the real path into the generated unit, so a path reaches the config in
    three different spellings depending on who wrote it. Expanding here means the
    opencode client only ever receives an absolute path, and it is the same
    normalisation the launcher does in `scripts/opencode_serve.sh`.
    """
    for spelling in ("${HOME}", "$HOME"):
        if spelling in value:
            return value.replace(spelling, str(Path.home()))
    return str(Path(value).expanduser())


@dataclass
class Config:
    """Every knob one deployment has, and the two rules that govern all of them.

    One dataclass, loaded once from the environment by `Config.load`, is the whole
    configuration surface: there is no second source of truth anywhere in R2D2. A
    field is read through the uppercase of its name, so `r2d2_fast_deadline` is
    `R2D2_FAST_DEADLINE`; a field with no environment variable is still settable by
    constructing `Config` directly and is there for the tests, not for an operator.

    No `.env` file is merged at import: what `Config.load` sees in `os.environ` is
    the whole configuration, so "configured entirely from the environment" is a
    state a deployment can actually be in. A process that wants a file sources it
    explicitly before starting (the opencode server's systemd unit names its
    `EnvironmentFile`; the gateway's launcher does not source one, so export the
    variables or source the file in the shell that runs it). An import-time merge
    of whatever `.env` sits next to the checkout is a silent second source -- and
    once carried the owner's live tokens into a process that believed it was
    isolated.

    **Two of these fields are the difference between a private skill and a remote
    control for the machine.** `alice_skill_id` and `alice_user_id` are how
    `/webhook` knows who it is serving, and `Brain.authorized` refuses every request
    while either is unset. A deployment that has not decided who it is for serves
    nobody. `r2d2_allow_unauthenticated` is the single documented way past that, for
    driving the voice path before a skill is registered, and startup says so in an
    ERROR. Behind `/webhook` sit the opencode agent and the `r2d2_do` shim, so the
    empty-value case is the one this class is shaped around.

    The second rule is the fallback chain. `backends_path` points at
    `config/backends.json`, which declares the backends and their order; a backend
    left without a credential is dropped with a WARNING naming the field rather
    than failing the process, because a dead primary should not mean a dead
    assistant. What must not happen is a silent substitution of a model the
    operator did not choose -- opencode answers an unknown `modelID` with HTTP 200
    from a *different* model, which is finding C1 in `docs/11-opencode-contract.md`
    and the reason `validate_models` exists.

    Two path-shaped fields, `r2d2_workspace` and `r2d2_cli_path`, default to a home
    reference rather than a machine, and are read through `resolved_workspace()` and
    `resolved_cli_path()` so the `~` is expanded in one place.
    """

    llm_provider: str = "openrouter"
    fallback_provider: str = ""

    openrouter_api_key: str = ""
    openrouter_model: str = "deepseek/deepseek-chat"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"

    yandex_api_key: str = ""
    yandex_folder_id: str = ""
    yandex_base_url: str = "https://llm.api.cloud.yandex.net/foundationModels/v1"
    yandex_auth_mode: str = "api_key"
    yandex_model: str = "yandexgpt-lite-5"
    yandex_model_big: str = "yandexgpt-pro-5.1"

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    alice_skill_id: str = ""
    alice_user_id: str = ""
    #: Development only. Set it to serve the webhook before the Alice skill is
    #: registered. `Brain.authorized` refuses everything otherwise, so a deployment
    #: that forgets the two ids above is closed rather than open -- and the startup
    #: check in `app/main.py` names this variable in the refusal it logs.
    r2d2_allow_unauthenticated: bool = False

    server_host: str = "127.0.0.1"
    server_port: int = 8080

    db_path: str = "db/sessions.db"
    shell_enabled: bool = True
    shell_require_confirm: bool = True
    shell_timeout: float = 10.0

    llm_timeout: float = 3.0
    llm_max_tokens: int = 400
    max_history: int = 20

    # --- opencode-backed brain (see .omo/plans/opencode-brain.md, todo 2) ---
    backends_path: str = "config/backends.json"
    #: The voice deadline, shipped at the project's own formula `min(3.6, 4.5 - 1.2)`
    #: = 3.3 s rather than at a hand-picked round number. It was 3.2 -- a round-down of
    #: the same formula -- and 3.2 was never measured: it came from p50 1.667 s / p95
    #: 2.247 s on opencode 1.18.32, and on 1.18.33 the same machine and model gave
    #: eleven voice turns of `2977 3132 3230 3236 3243 3244 3246 3249 3256 3274
    #: 3284` ms, of which NINE ran past 3.2 s and TEN fit inside 3.3 s. 3.6 would win
    #: one more and still leave only 0.9 s against Alice's hard 4.5 s.
    #: `test_the_shipped_deadline_is_the_formula_and_not_a_rounded_down_literal` is what
    #: holds the number to the arithmetic rather than to this comment.
    r2d2_fast_deadline: float = 3.3
    r2d2_task_ack: str = "Проверяю, пришлю в телеграм."
    r2d2_needs_agent_sentinel: str = "[[NEEDS_AGENT]]"
    r2d2_voice_agent: str = "r2d2-voice"
    r2d2_task_agent: str = "r2d2-agent"
    r2d2_workspace: str = "~/r2d2-workspace"
    r2d2_permission_timeout: float = 300.0
    r2d2_session_soft_limit: int = 40
    r2d2_stale_session_seconds: float = 900.0
    #: How long a session may sit UNUSED before its binding is dropped, so a
    #: deployment accumulates one row per real user rather than one per test
    #: run, per throwaway identity and per abandoned experiment. Deliberately
    #: far longer than `r2d2_stale_session_seconds`, which is about a wedged
    #: TURN and not about retention: unbinding a session a user still wants
    #: costs them their conversation, so the window has to exceed any plausible
    #: gap between two questions. `0` disables unbinding entirely.
    r2d2_session_retention_seconds: float = 2_592_000.0
    r2d2_cli_path: str = "~/.r2d2/r2d2_do.py"
    r2d2_event_poll_interval: float = 2.0

    # Which human a Telegram chat belongs to, as `chat_id=application_id` pairs --
    # see `app/identity.py:tg_application_id`, which is the only reader. It is a
    # DECLARATION because the two channels have to be the same person: one
    # application id is one opencode session and one pending permission question,
    # and `/tg/webhook` used to mint `tg:<chat_id>` for itself, which gave that
    # person a second session and put every answer where no question had been
    # asked. Left empty, a Telegram chat has no identity at all and nothing is
    # minted for it.
    r2d2_tg_application_id: str = ""

    @classmethod
    def load(cls) -> "Config":
        kwargs = {}
        for f in fields(cls):
            key = f.name.upper()
            if key not in os.environ:
                continue
            raw = os.environ[key]
            if f.type is bool:
                kwargs[f.name] = raw.strip().lower() in ("1", "true", "yes", "on")
            elif raw.strip() == "":
                # `SERVER_PORT=` in a .env is a blank line, not a number, and
                # `int("")` raised ValueError at IMPORT time -- `app = build_app()`
                # runs on import, so uvicorn died on a traceback instead of on the
                # operator's typo. A blank reads as "not declared", which is what
                # the dataclass default already says.
                continue
            elif f.type is int:
                kwargs[f.name] = int(raw)
            elif f.type is float:
                kwargs[f.name] = float(raw)
            else:
                kwargs[f.name] = raw
        return cls(**kwargs)

    def resolved_db_path(self) -> str:
        p = Path(self.db_path)
        if not p.is_absolute():
            p = _ROOT / p
        p.parent.mkdir(parents=True, exist_ok=True)
        return str(p)

    def resolved_workspace(self) -> str:
        """`r2d2_workspace` with `~` and `$HOME` expanded.

        Every session opencode creates is scoped to this directory by a `?directory=`
        query parameter (finding C6), so a deployment that leaves it pointing at the
        author's home either refuses to wire or, worse, scopes a stranger's session
        into a directory they did not choose. The default is a home reference so the
        committed file names no machine, and it is expanded here so the opencode
        client never has to know what a tilde is.
        """
        return _expand_home(self.r2d2_workspace)

    def resolved_cli_path(self) -> str:
        """`r2d2_cli_path` with `~` and `$HOME` expanded, for the same reason."""
        return _expand_home(self.r2d2_cli_path)

    @property
    def telegram_chat_id_int(self) -> int | None:
        return int(self.telegram_chat_id) if self.telegram_chat_id else None

    @property
    def yandex_model_uri(self) -> str:
        if self.yandex_model.startswith("gpt://"):
            return self.yandex_model
        return f"gpt://{self.yandex_folder_id}/{self.yandex_model}"

    @property
    def yandex_model_big_uri(self) -> str:
        if self.yandex_model_big.startswith("gpt://"):
            return self.yandex_model_big
        return f"gpt://{self.yandex_folder_id}/{self.yandex_model_big}"
