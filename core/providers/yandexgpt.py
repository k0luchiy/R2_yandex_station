import httpx

from app.config import Config
from core.providers.base import Choice, Provider


class YandexGPTProvider(Provider):
    name = "yandexgpt"

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def _headers(self) -> dict:
        if self.cfg.yandex_auth_mode.lower() == "iam":
            return {
                "Authorization": f"Bearer {self.cfg.yandex_api_key}",
                "Content-Type": "application/json",
            }
        return {
            "Authorization": f"Api-Key {self.cfg.yandex_api_key}",
            "Content-Type": "application/json",
        }

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        *,
        max_tokens: int = 400,
        temperature: float = 0.7,
        timeout: float = 3.0,
        model: str | None = None,
    ) -> Choice:
        url = f"{self.cfg.yandex_base_url.rstrip('/')}/chat/completions"
        model_uri = model or self.cfg.yandex_model_uri
        payload: dict = {
            "model": model_uri,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = tools
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, headers=self._headers(), json=payload)
            resp.raise_for_status()
        return self.parse_choice(resp.json(), self.name, model_uri)
