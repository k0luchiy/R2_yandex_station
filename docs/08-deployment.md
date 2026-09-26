# Развёртывание: запуск сервера, opencode, туннель, настройка навыка

Документ пошагово описывает, как поднять R2D2 с нуля: от зависимостей до
работающего навыка на Станции.

Порядок важен: сначала поднимается **opencode-сервер** (это теперь мозг), потом
сам R2D2, потом туннель, потом регистрация навыка.

## 0. Предварительные требования

- Linux-ноутбук с Python 3.11+ и доступом в интернет.
- **Установленный opencode** версии 1.18.32 (`~/.opencode/bin/opencode`) с
  активной учёткой Zen: без неё мозг отвечать не будет. На машине также лежат
  устаревшие копии 1.18.21 и 1.18.5 — не используйте их, и не полагайтесь на
  `opencode` из `PATH`.
- Рабочий каталог для сессий, отдельный от домашней папки:
  `/home/koluchiy/r2d2-workspace`. Его нужно создать заранее — сервер
  использует свой cwd как каталог сессий по умолчанию.
- Яндекс-аккаунт (тот же, что на Станции).
- Для Telegram — аккаунт и бот (см. шаг 6).
- Ключи цепочки фолбэков (см. шаг 5). **Без них система работает** — просто
  при недоступном opencode ходы будут падать в аварийный ответ.

## 1. Структура репозитория

```
R2_yandex_station/
├── README.md
├── docs/                       # эта документация
├── .env.example                # шаблон конфигурации R2D2
├── .env.oc.example             # шаблон конфигурации opencode-сервера
├── app/                        # FastAPI: main, config, diagnostics, opencode_route
├── core/                       # brain, backends, opencode, tools, worker, memory
├── config/
│   ├── backends.json           # реестр бэкендов и порядок фолбэка
│   └── opencode/r2d2.opencode.json  # два агента и их разрешения
├── opencode/r2d2_cli/r2d2_do.py     # шим инструментов для агента
├── scripts/
│   ├── opencode_serve.sh       # лаунчер сервера opencode
│   ├── r2d2-opencode.service   # systemd-юнит пользователя для него
│   ├── install_r2d2_opencode_config.sh  # ставит конфиг агентов и шим
│   ├── run_server.sh           # запуск uvicorn
│   └── tunnel.sh               # запуск cloudflared
├── db/                         # sqlite (создаётся автоматически)
└── requirements.txt
```

## 2. Установка opencode-сервера (мозг)

### 2.1 Каталог сессий

```bash
mkdir -p /home/koluchiy/r2d2-workspace
```

Сервер не создаёт его сам и не откажется: лаунчер проверяет и выходит с
ошибкой, если каталога нет. Путь в конфиге (`R2D2_WORKSPACE`) и путь в
`.env.oc` (`R2D2_OC_WORKSPACE`) должны совпадать.

### 2.2 Конфигурация R2D2 для opencode

```bash
bash scripts/install_r2d2_opencode_config.sh
```

Скрипт копирует `config/opencode/r2d2.opencode.json` в `~/.r2d2/opencode`
(каталог 0700, файл 0600) и ставит шим в `~/.r2d2/r2d2_do.py` (0755). Он
**только копирует файлы** и ничего не запускает: процесс opencode принадлежит
systemd-юниту, и включать его — отдельное ручное решение.

> Почему нельзя просто положить конфиг в `~/.config/opencode/`: переменная
> `OPENCODE_CONFIG_DIR` **складывается** с глобальным конфигом, а не заменяет
> его. Свой каталог — это слой, а не изоляция, и собственные правила
> разрешений в нём обязаны быть строгими. Глобальный `opencode.json` при этом
> **не модифицируется** никогда.

### 2.3 `.env.oc` — пароль сервера

```bash
cp .env.oc.example .env.oc
chmod 0600 .env.oc
openssl rand -base64 24          # → вставить в R2D2_OC_PASSWORD
```

`.env.oc` — живой файл с паролем, режим 0600 и в `.gitignore`. Обязательные
значения:

```dotenv
R2D2_OC_PORT=4599
R2D2_OC_USERNAME=opencode
R2D2_OC_PASSWORD=<сгенерированный пароль>
R2D2_OC_WORKSPACE=/home/koluchiy/r2d2-workspace
R2D2_OC_CONFIG_DIR=/home/koluchiy/.r2d2/opencode
```

**Пароль не может быть пустым.** `scripts/opencode_serve.sh` в этом случае
печатает объяснение и выходит с кодом 1, потому что opencode без
`OPENCODE_SERVER_PASSWORD` не предупреждает, а отвечает на все маршруты каждому
локальному процессу на машине. Пустая строка и строка из пробелов считаются
пустыми.

**Тот же пароль нужен R2D2 как клиенту.** В `config/backends.json` этот бэкенд
авторизуется как `${R2D2_OC_USERNAME}` / `${R2D2_OC_PASSWORD}`, и обе ссылки
подставляются из окружения самого R2D2, то есть из его `.env`. Значение в
`.env.oc` вооружает сервер; без него в `.env` бэкенд opencode выпадет из
реестра с WARNING, и маршрут работать не будет.

### 2.4 systemd-юнит пользователя

```bash
mkdir -p ~/.config/systemd/user
cp scripts/r2d2-opencode.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now r2d2-opencode
systemctl --user status r2d2-opencode
```

Юнит читает `EnvironmentFile=.env.oc`, запускает `scripts/opencode_serve.sh` и
перезапускается через 3 с. Частота перезапусков ограничена: без этого отказ
лаунчера печатал бы своё объяснение каждые три секунды вечно. Если юнит
застрял в `failed` после исправления `.env.oc`:

```bash
systemctl --user reset-failed r2d2-opencode
systemctl --user start r2d2-opencode
```

Проверка:

```bash
curl -u opencode:"$R2D2_OC_PASSWORD" http://127.0.0.1:4599/global/health
# {"healthy":true,"version":"1.18.32"}
```

## 3. Локальный запуск R2D2

```bash
cd R2_yandex_station
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# заполнить .env (шаг 5)

.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8099
```

Проверка:

```bash
curl http://127.0.0.1:8099/health
# {"status":"ok","opencode":{"reachable":true,"version":"1.18.32"},"sessions":0,"chain":["zen","yandexgpt","openrouter"]}
```

`reachable: false` — opencode-сервер не отвечает. Это **не поломка R2D2**:
процесс живет, `/health` отдаёт 200, и ходы уходят в цепочку фолбэков. Подробнее
о degraded-запуске — в разделе «Возможные проблемы».

Проверка webhook без туннеля (пример из [04-alice-protocol.md](04-alice-protocol.md)):

```bash
curl -X POST http://127.0.0.1:8099/webhook -H "Content-Type: application/json" -d '{
  "meta": {"locale":"ru-RU","interfaces":{}},
  "request": {"type":"SimpleUtterance","command":"привет"},
  "session": {
    "message_id":0,"session_id":"t1","skill_id":"<SKILL_ID>",
    "user":{"user_id":"<USER_ID>"},"application":{"application_id":"a1"},
    "new":true
  },
  "state":{}, "version":"1.0"}'
```

Живой каталог моделей и агентов:

```bash
curl -u opencode:"$R2D2_OC_PASSWORD" http://127.0.0.1:4599/config/providers
```

## 4. Публикация через cloudflared (HTTPS)

Алисе нужен публичный HTTPS-URL с валидным сертификатом. Используем cloudflared.

### Вариант А: быстрый туннель (для теста, URL меняется)

```bash
cloudflared tunnel --url http://127.0.0.1:8099
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
    service: http://127.0.0.1:8099
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

> Если нет своего домена на Cloudflare — подойдёт вариант А, но URL меняется
> при каждом перезапуске, что неудобно для Endpoint в консоли Диалогов.

### Автозапуск R2D2 (systemd, опционально)

`~/.config/systemd/user/r2d2.service`:

```ini
[Unit]
Description=R2D2 server (the Alice skill)
After=network.target
Requires=r2d2-opencode.service

[Service]
WorkingDirectory=/home/<user>/R2_yandex_station
ExecStart=/home/<user>/R2_yandex_station/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8099
Restart=always

[Install]
WantedBy=default.target
```

`Requires=` — не обязателен: R2D2 штатно работает без opencode (цепочка
фолбэков), и не поднимать его автоматически тоже правильно. Строка показана
потому, что порядок «мозг, потом шлюз» обычно удобнее.

Обратите внимание на bind: `127.0.0.1`, а не `0.0.0.0`. Наружу торчит только
туннель, поэтому порт R2D2 не должен быть доступен из локальной сети.

## 5. Переменные R2D2 (`.env`)

Полный актуальный список с комментариями — в `.env.example`. Минимально
необходимое:

```dotenv
# --- opencode-сервер (клиентская половина) ---
R2D2_OC_USERNAME=opencode
R2D2_OC_PASSWORD=<тот же пароль, что в .env.oc>
R2D2_BACKENDS_PATH=config/backends.json
R2D2_FAST_DEADLINE=3.2
R2D2_TASK_ACK=Проверяю, пришлю в телеграм.
R2D2_NEEDS_AGENT_SENTINEL=[[NEEDS_AGENT]]
R2D2_VOICE_AGENT=r2d2-voice
R2D2_TASK_AGENT=r2d2-agent
R2D2_WORKSPACE=/home/koluchiy/r2d2-workspace
R2D2_PERMISSION_TIMEOUT=300
R2D2_SESSION_SOFT_LIMIT=40
R2D2_STALE_SESSION_SECONDS=900
R2D2_CLI_PATH=/home/koluchiy/.r2d2/r2d2_do.py
R2D2_EVENT_POLL_INTERVAL=2.0

# --- цепочка фолбэков (необязательна) ---
R2D2_ZEN_KEY=<ключ Zen из ~/.local/share/opencode/auth.json, запись opencode>
OPENROUTER_API_KEY=sk-or-...
YANDEX_API_KEY=...
YANDEX_FOLDER_ID=...

# --- Telegram ---
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# --- Алиса (whitelist) ---
ALICE_SKILL_ID=
ALICE_USER_ID=

# --- прочее ---
SERVER_HOST=127.0.0.1
SERVER_PORT=8099
DB_PATH=db/sessions.db
SHELL_ENABLED=true
SHELL_TIMEOUT_SECONDS=10
```

### 5.1 `R2D2_ZEN_KEY` — что это и когда нужен

Это ключ из записи `opencode` в `~/.local/share/opencode/auth.json` (либо из
консоли Zen). В `config/backends.json` он подставляется как `${R2D2_ZEN_KEY}` в
поле `api_key` бэкенда `zen` и нужен **только цепочке фолбэков** — сам
opencode-маршрут ходит в opencode под учёткой сервера, и этот ключ его не
касается.

Практически: если Zen не пополнен, фолбэк на `zen` вернёт отказ по балансу и
ход уйдёт дальше по цепочке. Это не поломка, но и не польза, поэтому при
непополненном балансе честнее оставить поле пустым — бэкенд выпадет из цепочки
с WARNING, и в логе будет видно, что он не причина отказа.

Документация и примеры используют только плейсхолдеры вида `${ИМЯ_ПЕРЕМЕННОЙ}`.
Реальные значения живут в `.env` и `.env.oc`, оба в `.gitignore`.

## 6. Telegram-бот

1. В Telegram → `@BotFather` → `/newbot` → имя `R2D2_Butler` → получаешь токен.
2. Узнать свой `chat_id`: написать боту `/start`, затем
   `curl https://api.telegram.org/bot<TOKEN>/getUpdates` → `chat.id`.
3. Прописать в `.env`: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
4. Опционально поставить webhook для бота:
   `https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://r2d2.example.com/tg/webhook`

Telegram — единственный канал, который умеет **доставлять**: Алиса не может
прислать ответ позже. Поэтому через него приходят и длинные результаты, и
вопросы о подтверждении команд.

## 7. Настройка навыка в Яндекс.Диалогах (ручной шаг)

Этот шаг **не автоматизируется** — навык регистрируется в консоли вручную.

1. Открой [dialogs.yandex.ru](https://dialogs.yandex.ru/) → «Создать диалог» →
   тип «Навык Алисы».
2. **Общие сведения:**
   - Название: `Р2Д2` (2–5 слов; одно слово допустимо для уникального бренда).
   - Активационные имена: пробуем `Р2Д2`, `Р2`, `Р-два-дэ-дэ`. Если
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

> `ALICE_SKILL_ID` и `ALICE_USER_ID` из консоли копируются в `.env` вручную.
> Пока они пустые, whitelist не срабатывает — см. [09-security.md](09-security.md).

## 8. Чек-лист запуска

- [ ] `opencode serve` отвечает на `/global/health` с версией 1.18.32.
- [ ] `~/.r2d2/opencode/opencode.json` установлен, `~/.r2d2/r2d2_do.py` на месте.
- [ ] Юнит `r2d2-opencode` в состоянии `active`, пароль непустой.
- [ ] `R2D2_OC_PASSWORD` одинаков в `.env.oc` и `.env`.
- [ ] `/health` показывает `opencode.reachable: true`.
- [ ] Сервер отвечает на `/health`, webhook-тест через curl возвращает валидный JSON.
- [ ] cloudflared-URL отвечает по HTTPS.
- [ ] В консоли Диалогов проверка Endpoint прошла, доступ «Приватный».
- [ ] Бот Telegram отвечает, `chat_id` верный.
- [ ] «Алиса, запусти навык Р2Д2» → слышим приветствие.
- [ ] «Р2, что такое квантовые точки» → короткий ответ голосом.
- [ ] Тот же вопрос второй раз → сессия та же (контекст живой).
- [ ] «Р2, сделай сводку статей про RAG» → мгновенное подтверждение + сводка в ТГ.

## 9. Возможные проблемы

| Проблема | Решение |
|---|---|
| `opencode: server unreachable` в логе | `systemctl --user status r2d2-opencode`; чаще всего пустой `R2D2_OC_PASSWORD` или отсутствующий `~/.r2d2/opencode` |
| Юнит `r2d2-opencode` в `failed` | `journalctl --user -u r2d2-opencode`; после исправления `.env.oc` — `systemctl --user reset-failed r2d2-opencode` |
| `opencode=wired, models=unverified` при старте | Сервер не ответил на старте; маршрут поднят, но модели не проверены. Проверить `curl -u opencode:… /config/providers` |
| `backend 'opencode' … dropped from the chain, 'password' is empty` | `R2D2_OC_PASSWORD` не в `.env` R2D2 (только в `.env.oc`) |
| `GET /config/providers does not list fast_model=…` | Модель недоступна на этой машине; см. [11-opencode-backend.md](11-opencode-backend.md), C1 |
| «Некорректный SSL-сертификат» | Использовать cloudflared/ngrok, не самоподписанный cert |
| Навык «не отвечает» | Проверить лог: превышен дедлайн (`path=deadline`) или `path=error` |
| Активационное имя не распознаётся | Пробовать другие варианты; выдуманным словам нужно «обучение» (недели) |
| Не приходят сообщения в ТГ | Проверить `TELEGRAM_BOT_TOKEN`/`CHAT_ID`, webhook, доступ к api.telegram.org |
| Ответ из opencode всегда идёт в ТГ | Сессия холодная: первый ход стоит 15.5–18.6 с и всегда уходит агенту. Второй ход уже голосом |
