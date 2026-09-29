import os
from dataclasses import dataclass, fields
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass

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
    r2d2_fast_deadline: float = 3.2
    r2d2_task_ack: str = "Проверяю, пришлю в телеграм."
    r2d2_needs_agent_sentinel: str = "[[NEEDS_AGENT]]"
    r2d2_voice_agent: str = "r2d2-voice"
    r2d2_task_agent: str = "r2d2-agent"
    r2d2_workspace: str = "~/r2d2-workspace"
    r2d2_permission_timeout: float = 300.0
    r2d2_session_soft_limit: int = 40
    r2d2_stale_session_seconds: float = 900.0
    r2d2_cli_path: str = "~/.r2d2/r2d2_do.py"
    r2d2_event_poll_interval: float = 2.0

    # Which human a Telegram chat belongs to, as `chat_id=application_id` pairs --
    # see `app/main.py:tg_application_id`, which is the only reader. It is a
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
