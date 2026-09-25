# Архитектура сервера R2D2

## Общая схема

```
Яндекс Станция / приложение Алисы
        │  голос → распознавание → текст
        ▼
Яндекс.Диалоги (платформа)
        │  POST /webhook (JSON, валидный HTTPS)
        ▼
cloudflared tunnel (постоянный публичный URL: https://r2d2.<...>.trycloudflare.com или свой домен)
        ▼
uvicorn (FastAPI, локально на ноутбуке, порт 8080)
        │
        ▼
┌────────────────────────────────────────────────────────────┐
│  app/main.py — FastAPI                                      │
│   ├─ GET  /health           → {"status":"ok"}               │
│   ├─ POST /webhook          → обработчик Алисы              │
│   │      └─ middleware: валидация (skill_id, user_id)       │
│   ├─ POST /tg/webhook       → приём апдейтов от Telegram    │
│   └─ GET  /admin            → (опц.) мини-панель логов      │
├────────────────────────────────────────────────────────────┤
│  core/                                                      │
│   ├─ brain.py        — оркестрация: промпт + tools + LLM    │
│   ├─ providers/      — абстракция LLM (OpenRouter, Yandex)  │
│   │   ├─ base.py     — интерфейс Provider                    │
│   │   ├─ openrouter.py                                      │
│   │   └─ yandexgpt.py                                       │
│   ├─ tools/          — скиллы R2D2                          │
│   │   ├─ registry.py — имя → объект инструмента             │
│   │   ├─ telegram_tool.py                                   │
│   │   ├─ laptop_tool.py   (shell, status, apps)             │
│   │   ├─ arxiv_tool.py                                      │
│   │   └─ web_tool.py                                        │
│   ├─ async_worker.py — фоновые долгие задачи (asyncio)      │
│   ├─ memory.py       — sqlite: история диалогов, состояния  │
│   ├─ policies.py     — классификация команд (риск)          │
│   └─ render.py       — формирование ответа Алисе (лимиты)   │
├────────────────────────────────────────────────────────────┤
│  config.py / .env                                           │
│  db/sessions.db (SQLite)                                    │
└────────────────────────────────────────────────────────────┘
```

## Компоненты

### 1. Webhook-слой (`app/main.py`, `render.py`)
- Принимает `POST /webhook`, проверяет `session.skill_id` и `session.user.user_id`
  против белого списка (см. [09-security.md](09-security.md)).
- Разбирает запрос по [04-alice-protocol.md](04-alice-protocol.md).
- Возвращает JSON `response` с `text`/`tts` (жёсткая обрезка до 1024 символов).

### 2. Brain (`core/brain.py`)
- Единая точка принятия решений.
- Поток:
  1. Спец-интенты (помощь / что ты умеешь / приветствие) → мгновенный фикс-ответ.
  2. Восстановление истории диалога из SQLite.
  3. Вызов LLM с system-промптом и списком tools.
  4. Если модель вернула tool-вызов → исполнить (или запросить подтверждение).
  5. Сформировать финальный короткий ответ (см. [07-latency-strategy.md](07-latency-strategy.md)).
- Поддержка подтверждения рискованных команд через `pending_action` в памяти.

### 3. Провайдеры LLM (`core/providers/`)
- Единый интерфейс `async def chat(messages, tools) -> Choice`.
- `OpenRouterProvider` — `base_url=https://openrouter.ai/api/v1`, `OPENROUTER_API_KEY`.
- `YandexGPTProvider` — `base_url` Yandex Foundation Models (OpenAI-совместимый),
  `YANDEX_FOLDER_ID`, `YANDEX_API_KEY`.
- Выбор активного провайдера: `LLM_PROVIDER=openrouter|yandexgpt` из `.env`.
- Ретраи (1–2 попытки) + запасной провайдер при ошибке основного.
- Подробнее: [05-llm-provider.md](05-llm-provider.md).

### 4. Инструменты (`core/tools/`)
- Реестр инструментов: имя → JSON-schema (для function calling) + функция исполнения.
- Инструменты MVP:
  - `tg_send` — отправить сообщение в Telegram.
  - `system_status` — батарея / CPU / память / аптайм.
  - `open_app` — запустить приложение.
  - `run_shell` — произвольная команда (с классификацией риска и подтверждением).
  - `arxiv_search` — поиск статей и подготовка сводки (долгая задача).
  - `web_search` — (опционально, расширение).
- Подробнее: [06-tools.md](06-tools.md).

### 5. Async-воркер (`core/async_worker.py`)
- Очередь на `asyncio`. Задачи типа «собрать сводку → отправить в Telegram».
- Возврат результата не через Алису (она не умеет пушить), а через Telegram.
- Подробнее: [07-latency-strategy.md](07-latency-strategy.md).

### 6. Память (`core/memory.py`)
- SQLite `db/sessions.db`.
- Таблицы: `sessions`, `messages`, `pending_actions`, `jobs`.
- Ключ сессии — `application_id` (устойчив между запусками навыка), плюс
  `session_id` для текущей сессии.
- Храним последние ~20 сообщений на ключ, обрезаем длинные.

### 7. Telegram (`app/main.py` POST `/tg/webhook`, `tg_send`)
- Бот из BotFather. `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (свой чат).
- `/tg/webhook` — приём команд боту (полезно для «отчётов» и тестов).
- Отправка длинного контента всегда через Telegram.

## Поток запроса (типичный)

```
Алиса: «R2, открой браузер»
  → POST /webhook (request.type=SimpleUtterance, command="открой браузер")
  → validacija ok
  → brain: [system, user: "открой браузер"] → LLM (tools=[open_app, ...])
  → LLM: tool_call open_app(app="браузер") + spoken_reply="Открываю браузер"
  → исполняем open_app → ответ text="Открываю браузер"
  → Алиса озвучивает «Открываю браузер»
```

## Конфигурация (`.env`)

```dotenv
# LLM
LLM_PROVIDER=openrouter            # openrouter | yandexgpt
OPENROUTER_API_KEY=<redacted>          # никогда не коммитить реальный ключ
OPENROUTER_MODEL=deepseek/deepseek-chat   # быстрая модель
YANDEX_API_KEY=...
YANDEX_FOLDER_ID=...
YANDEXGPT_MODEL=yandexgpt-lite-5    # или yandexgpt-pro-5.1

# Telegram
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=123456789

# Алиса
ALICE_SKILL_ID=...                 # из консоли Диалогов
ALICE_USER_ID=...                  # твой session.user.user_id (whitelist)

# Сервер
SERVER_HOST=0.0.0.0
SERVER_PORT=8080
R2D2_TUNNEL_URL=https://r2d2.example.com
```

## Запуск (справочно, детали в 08-deployment.md)

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8080
```
