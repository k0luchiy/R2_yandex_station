from app.config import Config
from core.providers.base import Provider
from core.providers.openrouter import OpenRouterProvider
from core.providers.yandexgpt import YandexGPTProvider


def get_provider(name: str | None, cfg: Config) -> Provider:
    key = (name or cfg.llm_provider).strip().lower()
    if key in ("yandex", "yandexgpt", "yandex-gpt"):
        return YandexGPTProvider(cfg)
    return OpenRouterProvider(cfg)
