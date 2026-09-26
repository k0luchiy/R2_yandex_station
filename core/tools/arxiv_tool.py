import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import httpx

from app.config import Config
from core.backends.config_loader import load_backend_specs
from core.backends.openai_compatible import BackendError
from core.backends.registry import build_chain, fallback_chain
from core.tools.base import ToolContext, ToolResult

SCHEMA = {
    "type": "function",
    "function": {
        "name": "arxiv_search",
        "description": (
            "Найти свежие научные статьи на arxiv и отправить сводку в телеграм. "
            "Вызывай ТОЛЬКО когда пользователь явно просит «сводка статей», «поиск статей», "
            "«что нового с arxiv» и подобное. Для обычных вопросов не используй."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Поисковый запрос"},
                "days": {"type": "integer", "description": "Сколько дней смотреть, по умолчанию 7"},
                "max_results": {"type": "integer", "description": "Сколько статей, по умолчанию 5"},
                "spoken_reply": {
                    "type": "string",
                    "description": "Короткая фраза, которую услышит пользователь сразу",
                },
            },
            "required": ["query", "spoken_reply"],
        },
    },
}

_ATOM = "{http://www.w3.org/2005/Atom}"


def _within_days(date_str: str, days: int) -> bool:
    try:
        d = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    except ValueError:
        return True
    return (datetime.now(timezone.utc) - d).days <= days


async def arxiv_fetch(query: str, max_results: int = 5, days: int | None = None) -> list[dict]:
    params = {
        "search_query": f"all:{query}",
        "sortBy": "submittedDate",
        "sortOrder": "descending",
        "max_results": max_results,
    }
    async with httpx.AsyncClient(timeout=25.0) as client:
        resp = await client.get("https://export.arxiv.org/api/query", params=params)
        resp.raise_for_status()
    root = ET.fromstring(resp.content)
    entries = []
    for el in root.findall(f"{_ATOM}entry"):
        published = (el.findtext(f"{_ATOM}published") or "").strip()
        if days is not None and not _within_days(published, days):
            continue
        entries.append(
            {
                "title": (el.findtext(f"{_ATOM}title") or "").strip().replace("\n", " "),
                "summary": (el.findtext(f"{_ATOM}summary") or "").strip(),
                "link": (el.findtext(f"{_ATOM}id") or "").strip(),
                "published": published,
            }
        )
        if len(entries) >= max_results:
            break
    return entries


async def summarize_entries(cfg: Config, query: str, entries: list[dict]) -> str:
    chain, specs = load_backend_specs(cfg)
    body = "\n\n".join(
        f"{i}. {e['title']}\n{e['summary'][:900]}" for i, e in enumerate(entries, 1)
    )
    system = (
        "Ты — редактор научных дайджестов. Составь краткую сводку статей на русском "
        "языке, до 2500 символов. Для каждой статьи: название и одна-две фразы о сути. "
        "В конце — один вывод о главном тренде."
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": f"Тема: {query}\n\nСтатьи:\n{body}"},
    ]
    # The model now comes from the spec (the `gpt://folder/model` URI for
    # YandexGPT), which replaces the old `cfg.yandex_model_big_uri` override.
    # The session backend is filtered out the way `core/brain.py` filters it:
    # this function has no `OpencodeWiring`, so handing `build_chain` an order
    # that still contains `opencode` raises `BackendConfigError` instead of
    # summarising -- which is every deployment where `R2D2_OC_PASSWORD` is set.
    backends = build_chain(specs, fallback_chain(specs, chain))
    try:
        for backend in backends:
            try:
                choice = await backend.complete(
                    messages, max_tokens=1200, temperature=0.4, timeout=60.0
                )
            except (BackendError, httpx.HTTPError):
                continue
            if choice.content and choice.content.strip():
                return choice.content.strip()
            break
    finally:
        for backend in backends:
            await backend.aclose()
    return "\n".join(f"• {e['title']} — {e['link']}" for e in entries)


def build_digest(query: str, entries: list[dict], summary: str) -> str:
    header = f"Сводка по теме «{query}»\n\n"
    links = "\n\nСсылки:\n" + "\n".join(f"{i}. {e['title']}: {e['link']}" for i, e in enumerate(entries, 1))
    return header + summary + links


async def handler(ctx: ToolContext, args: dict) -> ToolResult:
    query = (args.get("query") or "").strip()
    if not query:
        return ToolResult("Не понял, что искать.", ok=False)
    days = int(args.get("days") or 7)
    max_results = max(1, min(int(args.get("max_results") or 5), 10))
    spoken = args.get("spoken_reply") or "Собираю сводку, пришлю в телеграм."
    return ToolResult(
        text=spoken,
        is_async=True,
        job={"type": "arxiv", "query": query, "days": days, "max_results": max_results},
    )
