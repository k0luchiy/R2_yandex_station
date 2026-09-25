# Развёртывание: запуск сервера, туннель, настройка навыка

Документ пошагово описывает, как поднять R2D2 с нуля: от зависимостей до
работающего навыка на Станции.

## 0. Предварительные требования

- Linux-ноутбук с Python 3.11+ и доступом в интернет.
- Яндекс-аккаунт (тот же, что на Станции).
- Для Telegram — аккаунт и бот (см. шаг 4).
- Для LLM — ключ OpenRouter и/или Yandex Cloud (см. [05-llm-provider.md](05-llm-provider.md)).

## 1. Структура репозитория

```
R2_yandex_station/
├── README.md
├── docs/                       # эта документация
├── .env.example                # шаблон конфигурации
├── app/
│   ├── main.py                 # FastAPI-приложение
│   ├── config.py               # чтение .env
│   └── ...
├── core/                       # brain, providers, tools, worker, memory
├── db/                         # sqlite (создаётся автоматически)
├── pyproject.toml / requirements.txt
└── scripts/
    ├── run_server.sh           # запуск uvicorn
    └── tunnel.sh               # запуск cloudflared
```

## 2. Локальный запуск сервера

```bash
cd R2_yandex_station
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt        # fastapi, uvicorn, openai, httpx, aiosqlite, psutil, pydantic

cp .env.example .env
# заполнить .env (ключи см. ниже)

uvicorn app.main:app --host 0.0.0.0 --port 8080
```

Проверка:

```bash
curl http://localhost:8080/health
# {"status":"ok"}
```

Проверка webhook без туннеля (пример из [04-alice-protocol.md](04-alice-protocol.md)):

```bash
curl -X POST http://localhost:8080/webhook -H "Content-Type: application/json" -d '{
  "meta": {"locale":"ru-RU","interfaces":{}},
  "request": {"type":"SimpleUtterance","command":"привет"},
  "session": {
    "message_id":0,"session_id":"t1","skill_id":"<SKILL_ID>",
    "user":{"user_id":"<USER_ID>"},"application":{"application_id":"a1"},
    "new":true
  },
  "state":{}, "version":"1.0"}'
```

## 3. Публикация через cloudflared (HTTPS)

Алисе нужен публичный HTTPS-URL с валидным сертификатом. Используем cloudflared.

### Вариант А: быстрый туннель (для теста, URL меняется)

```bash
# установить cloudflared: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/
cloudflared tunnel --url http://localhost:8080
# → https://<random>.trycloudflare.com  (нужен каждый раз заново)
```

### Вариант Б: постоянный туннель (рекомендуется)

```bash
cloudflared tunnel login                          # откроется браузер, авторизация
cloudflared tunnel create r2d2                    # создаёт туннель с ID
cloudflared tunnel route dns r2d2 r2d2.example.com # свой домен на Cloudflare
```

`~/.cloudflared/config.yml`:

```yaml
tunnel: <TUNNEL_ID>
credentials-file: /home/<user>/.cloudflared/<TUNNEL_ID>.json
ingress:
  - hostname: r2d2.example.com
    service: http://localhost:8080
  - service: http_status:404
```

Запуск:

```bash
cloudflared tunnel run r2d2
# → https://r2d2.example.com отвечает, сертификат валиден
```

Проверить снаружи:

```bash
curl https://r2d2.example.com/health
```

> Если нет своего домена на Cloudflare — подойдёт вариант А или бесплатный
> ngrok (но у ngrok URL меняется на free-тарифе; для прод-режима лучше
> постоянный туннель).

### Автозапуск (опционально, systemd)

`/etc/systemd/system/r2d2.service`:

```ini
[Unit]
Description=R2D2 server
After=network.target

[Service]
WorkingDirectory=/home/<user>/R2_yandex_station
ExecStart=/home/<user>/R2_yandex_station/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8080
Restart=always

[Install]
WantedBy=multi-user.target
```

И аналогичный юнит для `cloudflared tunnel run r2d2`. Включить:

```bash
sudo systemctl enable --now r2d2 cloudflared
```

## 4. Telegram-бот

1. В Telegram → `@BotFather` → `/newbot` → имя `R2D2_Butler` → получаешь токен.
2. Узнать свой `chat_id`: написать боту `/start`, затем
   `curl https://api.telegram.org/bot<TOKEN>/getUpdates` → `chat.id`.
3. Прописать в `.env`: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
4. Опционально поставить webhook для бота:
   `https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://r2d2.example.com/tg/webhook`

## 5. Настройка навыка в Яндекс.Диалогах

1. Открой [dialogs.yandex.ru](https://dialogs.yandex.ru/) → «Создать диалог» →
   тип «Навык Алисы».
2. **Общие сведения:**
   - Название: `Р2Д2` (2–5 слов; одно слово допустимо для уникального бренда).
   - Активационные имена: пробуем `Р2Д2`, `Р2`, `R2`, `Р-два-дэ-дэ`. Если
     короткие латинские не принимаются (мин. длина), оставляем только валидные
     (см. [02-alice-requirements.md](02-alice-requirements.md)).
   - Иконка: любая картинка (для приватного не критично).
3. **Backend:**
   - Endpoint URL: `https://r2d2.example.com/webhook` (проверка «Проверить»
     пройдёт, если сервер отвечает).
4. **Доступ:**
   - Тип доступа: **«Приватный»** (только для тебя). Автомодерация быстрая.
5. **Проверка голосом:**
   - Убедись, что на Станции и в телефоне залогинен тот же Яндекс-аккаунт.
   - Скажи: «Алиса, запусти навык Р2Д2», затем «Р2, привет», «Р2, что ты умеешь».
6. Если хочешь в каталог (не обязательно) — смени доступ на «Публичный» и
   пройди полную модерацию (до 3 дней).

## 6. `.env.example`

```dotenv
# LLM
LLM_PROVIDER=openrouter            # openrouter | yandexgpt
FALLBACK_PROVIDER=                 # например yandexgpt (опционально)
OPENROUTER_API_KEY=
OPENROUTER_MODEL=deepseek/deepseek-chat
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
YANDEX_API_KEY=
YANDEX_FOLDER_ID=
YANDEXGPT_MODEL=yandexgpt-lite-5   # для голосовых
YANDEXGPT_MODEL_BIG=yandexgpt-pro-5.1  # для фоновых сводок

# Telegram
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# Алиса (whitelist)
ALICE_SKILL_ID=
ALICE_USER_ID=

# Сервер
SERVER_HOST=0.0.0.0
SERVER_PORT=8080
R2D2_TUNNEL_URL=https://r2d2.example.com

# Безопасность
SHELL_ALLOWED_PREFIXES=ls,pwd,df,ps,cat,echo,date,uptime,whoami,git status,free
SHELL_RISKY_KEYWORDS=rm,mkfs,shutdown,sudo,reboot,kill,pkill,dd,> /dev,chmod 777,chown
SHELL_TIMEOUT_SECONDS=10
```

## 7. Чек-лист запуска

- [ ] Сервер отвечает на `/health`.
- [ ] Webhook-тест через curl возвращает валидный JSON.
- [ ] cloudflared-URL отвечает по HTTPS.
- [ ] В консоли Диалогов проверка Endpoint прошла.
- [ ] Навык приватный, активационные имена заданы.
- [ ] Бот Telegram отвечает, `chat_id` верный.
- [ ] «Алиса, запусти навык Р2Д2» → слышим приветствие.
- [ ] «Р2, что ты умеешь» → краткий список.
- [ ] «Р2, какой заряд у ноутбука» → работает инструмент.
- [ ] «Р2, отправь в телеграм тест» → сообщение приходит.

## 8. Возможные проблемы

| Проблема | Решение |
|---|---|
| «Некорректный SSL-сертификат» | Использовать cloudflared/ngrok, не самоподписанный cert |
| Навык «не отвечает» | Сервер не поднят или не успел за 4,5 с; см. логи |
| Активационное имя не распознаётся | Пробовать другие варианты; выдуманным словам нужно «обучение» (недели) |
| Не приходят сообщения в ТГ | Проверить `TELEGRAM_BOT_TOKEN`/`CHAT_ID`, webhook, доступ к api.telegram.org |
| LLM медленный/ошибка | Сменить модель/провайдера, проверить таймауты, ретраи |
