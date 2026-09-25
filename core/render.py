import re

MAX_TEXT = 1024


def clean(text: str | None) -> str:
    if not text:
        return ""
    text = text.replace("\r", " ")
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[#*_~`>|\[\]\\]+", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = text.strip('"')
    if len(text) > MAX_TEXT:
        text = text[: MAX_TEXT - 1].rstrip() + "…"
    return text


def alice_response(text: str | None, end_session: bool = False) -> dict:
    cleaned = clean(text)
    return {
        "response": {
            "text": cleaned,
            "tts": cleaned,
            "end_session": end_session,
        },
        "version": "1.0",
    }
