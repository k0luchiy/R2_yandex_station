import httpx

from app.config import Config
from core.providers.base import Choice, Provider


class OpenRouterProvider(Provider):
    name = "openrouter"

    def __init__(self, cfg: Config):
        self.cfg = cfg

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
        url = f"{self.cfg.openrouter_base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.cfg.openrouter_api_key}",
            "Content-Type": "application/json",
        }
        payload: dict = {
            "model": model or self.cfg.openrouter_model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = tools
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
        return self.parse_choice(resp.json(), self.name, model or self.cfg.openrouter_model)
