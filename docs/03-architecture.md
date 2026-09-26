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
uvicorn (FastAPI, локально на ноутбуке, порт 8099)
        │
        ▼
┌────────────────────────────────────────────────────────────┐
│  app/main.py — FastAPI, композиционный корень              │
│   ├─ GET  /health           → liveness + opencode.reachable │
│   ├─ POST /webhook          → обработчик Алисы              │
│   │      └─ middleware: валидация (skill_id, user_id)       │
│   ├─ POST /tg/webhook       → приём апдейтов от Telegram    │
│   ├─ GET  /diagnostics/providers → живой каталог opencode   │
│   └─ app/opencode_route.py — сборка маршрута opencode:      │
│          клиент → store → бэкенд → брокер → читалки событий │
├────────────────────────────────────────────────────────────┤
│  core/                                                      │
│   ├─ brain.py        — один ход: маршрутизация и фолбэк     │
│   ├─ routing.py      — сентинел [[NEEDS_AGENT]] + 4 guard'а │
│   ├─ backends/       — конфигурируемая абстракция LLM       │
│   │   ├─ base.py            — протокол Backend, Choice     │
│   │   ├─ config_loader.py   — разбор config/backends.json  │
│   │   ├─ registry.py        — kind → конструктор, цепочка  │
│   │   ├─ opencode_session.py— сессия opencode за Backend    │
│   │   └─ openai_compatible.py — OpenRouter/Zen/YandexGPT   │
│   ├─ opencode/       — клиент живого сервера opencode      │
│   │   ├─ client.py         — 14 HTTP-маршрутов             │
│   │   ├─ wire.py           — типы и разбор ответов         │
│   │   ├─ sse.py            — GET /event, 3 EVENT_MODE      │
│   │   ├─ models.py         — ворота C1 по каталогу моделей │
│   │   └─ session_store.py  — 1 сессия на пользователя      │
│   ├─ permissions.py  — брокер permission.asked (Telegram)   │
│   ├─ tools/          — скиллы R2D2                          │
│   │   ├─ registry.py — имя → объект инструмента             │
│   │   ├─ telegram_tool.py                                   │
│   │   ├─ laptop_tool.py   (status, open_app)                │
│   │   ├─ shell_tool.py   (риск-гейт)                       │
│   │   ├─ arxiv_tool.py                                      │
│   │   └─ tg_tool.py                                         │
│   ├─ async_worker.py — фоновые долгие задачи (asyncio)      │
│   ├─ metrics.py      — одна запись на ход (route, path, ms) │
│   ├─ memory.py       — sqlite: сессии, oc_sessions, jobs     │
│   ├─ policies.py     — классификация команд (риск)          │
│   └─ render.py       — формирование ответа Алисе (лимиты)   │
├────────────────────────────────────────────────────────────┤
│  config/backends.json — реестр бэкендов и порядок фолбэка   │
│  config/opencode/r2d2.opencode.json — два агента R2D2       │
│  opencode/r2d2_cli/r2d2_do.py — шим инструментов для агента │
│  .env / .env.oc / db/sessions.db                            │
└────────────────────────────────────────────────────────────┘
        │  HTTP basic, 127.0.0.1:4599
        ▼
opencode serve (systemd --user, НЕ управляется R2D2)
        │
        ▼
opencode/space-bunny-free через подписку opencode
```

Подробное описание бэкенда — в
[11-opencode-backend.md](11-opencode-backend.md); измеренный контракт сервера —
в [11-opencode-contract.md](11-opencode-contract.md).

## Компоненты

### 1. Webhook-слой (`app/main.py`, `render.py`)
- Принимает `POST /webhook`, проверяет `session.skill_id` и `session.user.user_id`
  против белого списка (см. [09-security.md](09-security.md)).
- Разбирает запрос по [04-alice-protocol.md](04-alice-protocol.md).
- Возвращает JSON `response` с `text`/`tts` (жёсткая обрезка до 1024 символов).
- `/health` отдаёт 200 всегда: недоступный opencode — это деградация
  зависимости, а не поломка процесса, и поле `opencode.reachable` сообщает о ней.

### 2. Brain (`core/brain.py`) — маршрутизация хода
- Единая точка принятия решений. Каждый ход сначала идёт в **сессию opencode**
  пользователя, и только если она недоступна или отказала — в цепочку бэкэков.
- Поток:
  1. Спец-интенты (помощь / что умеешь / приветствие / выход) → мгновенный
     фикс-ответ, без LLM.
  2. Ответ на ожидающее подтверждение opencode, если оно есть.
  3. Проверка живости opencode и **открытие сессии пользователя** (одна на всех,
     создаётся один раз, переиспользуется).
  4. Голосовой ход: агент `r2d2-voice`, дедлайн `R2D2_FAST_DEADLINE` (3,2 с).
     Холодная сессия (0 сообщений) идёт сразу в агента — она стоит 15–19 с.
  5. Сентинел `[[NEEDS_AGENT]]` в ответе → эскалация: асинхронный ход агента
     `r2d2-agent` + подтверждение голосом, результат в Telegram.
  6. Дедлайн → ответ не отменяется: ход дорабатывается на сервере, **и ход
     всё равно уходит `r2d2-agent`** одним вызовом сборщика, а его ответ
     забирает фоновый воркер и уходит в Telegram.
  7. Отказ opencode (в том числе 200 с `info.error`) → цепочка фолбэков.
- Все три эскалирующие ветки (холодная сессия, сентинел, дедлайн) проходят через
  один `SessionCollector.hand_to_agent`: он и отдаёт ход агенту, и ставит
  сборщик, поэтому «ровно один сборщик на эскалацию» обеспечено кодом, а не
  соглашением.
- Поддержка подтверждения рискованных команд через `pending_action` в памяти.
- Подробнее: [07-latency-strategy.md](07-latency-strategy.md).

### 3. Сентинел и четыре guard'а (`core/routing.py`)
- Голосовой агент не умеет работать и признаёт это строкой `[[NEEDS_AGENT]]`
  в начале ответа.
- `parse_voice_reply` режет ответ по этому маркеру и **выбрасывает хвост**,
  `sanitize_for_speech` возвращает пустую строку для любого текста с маркером,
  а `Brain._speakable` проглотает маркер на сыром тексте до очистки markdown.
- Четвёртый, `for_human`, стоит на исходящей границе в Telegram: сборщик читает
  текст ассистента прямо из сессии и не отличает ответ от сигнала маршрутизации,
  поэтому маркер снимается с любого текста, идущего человеку. В отличие от
  первых двух он не выбрасывает сообщение целиком — сводка, процитировавшая
  маркер в одной фразе, всё равно должна прийти.
- Четыре независимые проверки: одна ловит структурно, другая — тотально, третья —
  по порядку вызовов, четвёртая — по направлению. Подробнее:
  [11-opencode-backend.md](11-opencode-backend.md).

### 4. Бэкенды LLM (`core/backends/`)
- Единый структурный протокол `async def complete(messages, tools) -> Choice`.
  Набор бэкендов **не зашит в код**: он целиком в `config/backends.json`.
- Два `kind`:
  - `opencode_session` — ответ из постоянной сессии opencode. Первичный путь;
    собирается композиционным корнем, потому что клиент, хранилище сессий,
    конфигурация и читалка событий нужны все сразу.
  - `openai_compatible` — один клиент на три схемы авторизации
    (`bearer`, `yandex` c `api_key` или `iam_token`): Zen, OpenRouter, YandexGPT.
- Порядок фолбэка — поле `chain`. Бэкенд без обязательного поля выпадает из
  цепочки с WARNING, а не роняет реестр; пустая цепочка — ошибка конфигурации.
- Добавить провайдера — один объект JSON, без кода. Подробнее:
  [05-llm-provider.md](05-llm-provider.md).

### 5. Клиент opencode (`core/opencode/`)
- `client.py` — 14 маршрутов `opencode serve` с явными таймаутами: 13 вызовов
  `_request` в самом файле плюс живой `GET /event`, который читает SSE, а не
  запрашивает (его читает `sse.py`).
- `transport.py` — соединение, которое эти маршруты делят: адрес, basic-auth,
  `?directory=`, общее тело хода, non-2xx как исключение.
- `sse.py` — чтение `GET /event` (глобальный поток, фильтр по `sessionID`),
  режимы `sse` / `poll` / `deny`.
- `models.py` — проверка всех `model id` против каталога сервера на старте:
  неизвестная модель для opencode **не ошибка**, он молча подставит другую.
- `session_store.py` — одна сессия на пользователя, пересоздание мёртвой
  привязки, сборщик зависших `busy`-сессий.
- `wire.py` — типы и разбор: `200` с `info.error` это отказ, а не ответ.
- Подробнее: [11-opencode-backend.md](11-opencode-backend.md).

### 6. Инструменты (`core/tools/`)
- Реестр инструментов: имя → JSON-schema (для function calling) + функция исполнения.
- Инструменты MVP:
  - `tg_send` — отправить сообщение в Telegram.
  - `system_status` — батарея / CPU / память / аптайм.
  - `open_app` — запустить приложение.
  - `run_shell` — произвольная команда (с классификацией риска и подтверждением).
  - `arxiv_search` — поиск статей и подготовка сводки (долгая задача).
- Те же обработчики доступны агенту opencode через шим
  `opencode/r2d2_cli/r2d2_do.py`, который разрешён точечным белым списком
  `bash` в конфигурации агентов. Подробнее: [06-tools.md](06-tools.md).

### 7. Async-воркер (`core/async_worker.py`)
- Очередь на `asyncio` + таблица `jobs` в SQLite (переживает рестарт).
- Два типа задач: сборка arxiv-сводки и `opencode_reply` — сбор ответа хода,
  который не уложился в голосовой бюджет.
- Возврат результата не через Алису (она не умеет пушить), а через Telegram.
- Подробнее: [07-latency-strategy.md](07-latency-strategy.md).

### 8. Память (`core/memory.py`)
- SQLite `db/sessions.db`.
- Таблицы: `sessions`, `messages`, `pending_actions`, `jobs`, `oc_sessions`.
- `oc_sessions` — соответствие `application_id` → `session_id` opencode. Это
  **единственное**, что R2D2 помнит о беседе: сама беседа живёт в сессии.
- `pending_actions` — одна строка на пользователя: либо подтверждение shell,
  либо ожидающий ответ opencode (по ключу `kind` они различаются).
- Храним последние ~20 сообщений на ключ, обрезаем длинные (нужно только для
  цепочки фолбэков — сессионному пути история не нужна).

### 9. Замеры (`core/metrics.py`)
- Ровно одна строка на ход: `route`, `path`, `model`, `agent`, `llm_ms`,
  `total_ms`, `escalated`, `permission_asked`.
- `path` — что ход сделал с запросом: `voice` / `escalate` / `deadline` / `error`.
- Ни одно поле не приходит из `${...}`, поэтому секрет в такую строку попасть
  не может.

### 10. Telegram (`app/main.py` POST `/tg/webhook`, `telegram_tool.py`)
- Бот из BotFather. `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (свой чат).
- `/tg/webhook` — приём команд боту и **ответов на подтверждения opencode**.
- Отправка длинного контента всегда через Telegram.
- `application_id` для Telegram-хода **объявлен, а не выведен**: переменная
  `R2D2_TG_APPLICATION_ID` хранит пары `chat_id=application_id` (запятые или
  пробелы), и `tg_application_id()` в `app/main.py` — единственный её читатель.
  Один человек — один `application_id`, а он владеет и единственной сессией
  opencode, и единственным ожидающим вопросом, поэтому `да` из Telegram отвечает
  на вопрос, заданный голосом Алисы.
- Чат, которому пара не объявлена, **отказывается**: сообщение не отвечает, в
  лог уходит WARNING с именем переменной, идентичность не выдумывается. Раньше
  `/tg/webhook` мнил `tg:<chat_id>`, и это стоило вторую сессию opencode на
  одного человека и `да`, доставленный не туда.
- Переменная в этом развёртывании ещё не задана: она станет ею после
  регистрации навыка Алисы, а до тех пор ни юнит opencode, ни навык не
  настроены вручную.

## Поток запроса (типичный)

**Тёплый голосовой путь** (обычный вопрос):

```
Алиса: «Р2, что такое квантовые точки»
  → POST /webhook (request.type=SimpleUtterance, command="что такое квантовые точки")
  → валидация ok
  → сессия r2d2:alice:<app_id> уже есть
  → POST /session/<id>/message  {agent: r2d2-voice, model: opencode/space-bunny-free}
  → ответ: "Квантовые точки — это крошечные полупроводниковые частицы…"
  → guard'ы пропускают его в text/tts
  → Алиса озвучивает ответ (1.5–2.2 с)
```

**Эскалация** (нужна настоящая работа):

```
Алиса: «Р2, сделай сводку статей про RAG»
  → POST /webhook
  → POST /session/<id>/message  {agent: r2d2-voice}
  → ответ: "[[NEEDS_AGENT]]Сводка статей про RAG за неделю."
  → parse_voice_reply: kind=escalate, spoken=""  (хвост выброшен)
  → POST /session/<id>/prompt_async {agent: r2d2-agent} → 204
  → Алиса: «Проверяю, пришлю в телеграм.»
  … агент работает, ответ уходит в Telegram
```

## Конфигурация (`.env`)

Полный и актуальный список переменных — в `.env.example`. Ключевое:

```dotenv
# opencode-сервер (тот же пароль нужен R2D2 как клиенту)
R2D2_OC_USERNAME=opencode
R2D2_OC_PASSWORD=<из .env.oc, никогда не коммитить>

# Маршрут opencode
R2D2_FAST_DEADLINE=3.2
R2D2_TASK_ACK=Проверяю, пришлю в телеграм.
R2D2_NEEDS_AGENT_SENTINEL=[[NEEDS_AGENT]]
R2D2_VOICE_AGENT=r2d2-voice
R2D2_TASK_AGENT=r2d2-agent
R2D2_WORKSPACE=/home/koluchiy/r2d2-workspace
R2D2_PERMISSION_TIMEOUT=300
R2D2_BACKENDS_PATH=config/backends.json

# Цепочка фолбэков (нужна только если opencode недоступен)
R2D2_ZEN_KEY=...
OPENROUTER_API_KEY=...
YANDEX_API_KEY=...
YANDEX_FOLDER_ID=...

# Telegram
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
R2D2_TG_APPLICATION_ID=<chat_id>=<application_id Алисы>   # привязка чата к человеку

# Алиса
ALICE_SKILL_ID=...                 # из консоли Диалогов
ALICE_USER_ID=...                  # твой session.user.user_id (whitelist)

# Сервер
SERVER_HOST=0.0.0.0
SERVER_PORT=8099
```

> `LLM_PROVIDER` и `FALLBACK_PROVIDER` больше не управляют выбором: порядок
> бэкендов и состав цепочки задаёт `config/backends.json`.

## Запуск (справочно, детали в 08-deployment.md)

```bash
systemctl --user enable --now r2d2-opencode    # сервер opencode
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8099
```
