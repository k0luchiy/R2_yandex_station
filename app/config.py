import os
from dataclasses import dataclass, fields
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass

_ROOT = Path(__file__).resolve().parent.parent


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

    server_host: str = "0.0.0.0"
    server_port: int = 8080

    db_path: str = "db/sessions.db"
    shell_enabled: bool = True
    shell_require_confirm: bool = True
    shell_timeout: float = 10.0

    llm_timeout: float = 3.0
    llm_max_tokens: int = 400
    max_history: int = 20

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
