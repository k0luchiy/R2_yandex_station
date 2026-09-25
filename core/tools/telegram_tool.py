import httpx

from app.config import Config


async def send_message(cfg: Config, text: str, chat_id: int | None = None) -> bool:
    if not cfg.telegram_bot_token:
        return False
    target = chat_id if chat_id is not None else cfg.telegram_chat_id_int
    if target is None:
        return False
    url = f"https://api.telegram.org/bot{cfg.telegram_bot_token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(url, json={"chat_id": target, "text": text[:4096]})
            return resp.status_code == 200
    except httpx.HTTPError:
        return False
