# 11 — Бэкенд opencode: агенты, разрешения, маршрутизация

**Что это за документ.** R2D2 — тонкий голосовой шлюз перед opencode-агентом
владельца. Этот файл описывает, как именно: какой HTTP-поверхностью
`opencode serve` пользуется R2D2, что умеют два агента, как работает сентинел
эскалации, как R2D2 отвечает на запросы разрешения и что происходит, когда
opencode недоступен.

**Источник истины — код, а не этот документ.** Все числа в разделе 7 измерены
на живой машине; доказательства и метод измерения лежат в
[11-opencode-contract.md](11-opencode-contract.md). Согласованность
поддерживается машинно: `tests/test_docs.py` сверяет каждый путь из раздела 2 с
`core/opencode/client.py`, каждую модель — с `config/backends.json`, матрицы
разрешений — с `config/opencode/r2d2.opencode.json`, а каждый блок кода в этом
файле проверяется на дословность.

> **Как читать блоки кода.** Блок с пометкой `verbatim <файл>` — дословная
> выдержка из указанного файла репозитория; `tests/test_docs.py` ищет её
> подстрокой в исходнике и падает, если это пересказ. Блоки на `text`,
> `dotenv`, `console` и `bash` — схемы и команды оператора, а не исходники.

---

## 1. Что изменилось и почему это не «ещё один провайдер»

Раньше R2D2 был навыком Алисы, который сам вызывал API LLM. Теперь каждый ход
попадает в **одну постоянную сессию opencode на пользователя**, и отвечает её
**собственная подписка opencode**. Отсюда три следствия, которые и определяют
всю остальную архитектуру:

1. **Контекст живёт в сессии, а не в SQLite.** SQLite хранит только соответствие
   `application_id` → `session_id`; сама беседа остаётся на стороне opencode.
2. **Инструменты принадлежат агенту, а не R2D2.** У голосового агента их почти
   нет, у рабочего — полный набор, и R2D2 только разрешает или запрещает вызов.
3. **Рискованное действие требует подтверждения человека.** opencode
   останавливает ход и ждёт; отвечает R2D2, а не модель.

Сервер opencode запускается **systemd-юнитом пользователя**
(`scripts/r2d2-opencode.service`), слушает только `127.0.0.1:4599` и требует
пароль. R2D2 никогда его не запускает, не убивает и не перезапускает: в
`app/` нет ни одного API управления процессом, и `tests/test_serve_unit.py`
проверяет это чтением исходника. Если сервера нет, R2D2 всё равно поднимается и
отвечает из цепочки фолбэков.

---

## 2. HTTP-поверхность, которую использует R2D2

Четырнадцать маршрутов. Таблица читается как контракт: всё, чего здесь нет, R2D2 на
сервер не отправляет.

| Метод | Путь | Что R2D2 с этим делает | Где |
|---|---|---|---|
| `GET` | `/global/health` | проверка живости сервера, таймаут 1.5 с; единственный маршрут без `?directory=` | `core/opencode/client.py` → `health()` |
| `POST` | `/session` | создать сессию пользователя с заголовком `r2d2:alice:<application_id>` | `core/opencode/client.py` → `create_session()` |
| `GET` | `/session` | найти сессию по точному заголовку; `?directory=` фильтрует список | `core/opencode/client.py` → `find_session()`, `list_sessions()` |
| `POST` | `/session/{session_id}/message` | **голосовой ход**: блокирующий, с дедлайном, агент `r2d2-voice` | `core/opencode/client.py` → `send_message()` |
| `POST` | `/session/{session_id}/prompt_async` | **агентный ход**: отправлен и не ждём ответа, агент `r2d2-agent`, сервер отвечает 204 | `core/opencode/client.py` → `send_message_async()` |
| `GET` | `/session/{session_id}/message` | прочитать переписку как `{info, parts}`; найти маркер ответа и собрать текст агента; увидеть, что в ходе был **отказ** | `core/opencode/client.py` → `list_messages()` |
| `DELETE` | `/session/{session_id}/message/{message_id}` | **удалить из общей сессии то, что opencode писал в неё для себя**: маркер эскалации и отказ в вызове инструмента. До отправки хода агенту | `core/opencode/client.py` → `delete_message()` |
| `POST` | `/session/{session_id}/summarize` | сжать длинную сессию, когда сообщений больше `r2d2_session_soft_limit` | `core/opencode/client.py` → `summarize()` |
| `POST` | `/session/{session_id}/abort` | убить зависшую сессию: сервер считает её `busy` и ей не пользовались дольше `r2d2_stale_session_seconds` | `core/opencode/client.py` → `abort()` |
| `POST` | `/session/{session_id}/permissions/{permission_id}` | **ответ на запрос разрешения**: только `once` или `reject` | `core/opencode/client.py` → `respond_permission()` |
| `GET` | `/session/status` | статусы всех сессий для сборщика зависших | `core/opencode/client.py` → `session_status()` |
| `GET` | `/config/providers` | каталог моделей: ворота C1 на старте, до первого хода | `core/opencode/client.py` → `providers()` |
| `GET` | `/agent` | список определений агентов: имена проверяются, а не угадываются | `core/opencode/client.py` → `agents()` |
| `GET` | `/event` | живой SSE-поток: `permission.asked` и конец хода | `core/opencode/sse.py` → `EventSource.events()` |

### 2.1 Пять правил, без которых это не работает

**Аутентификация — HTTP basic.** Пароль приходит в `OPENCODE_SERVER_PASSWORD`,
R2D2 отправляет его как basic-auth. Лаунчер **отказывается стартовать** без
пароля, потому что сервер без него не предупреждает, а просто отвечает на все
маршруты каждому локальному процессу:

```verbatim scripts/opencode_serve.sh
if [ -z "${PASSWORD//[[:space:]]/}" ]; then
  echo "opencode_serve.sh: refusing to start." >&2
  echo "  R2D2_OC_PASSWORD is empty, so OPENCODE_SERVER_PASSWORD would be empty too" >&2
  echo "  and every local process could drive this server with no authentication." >&2
  echo "  Put a password in .env.oc (chmod 0600) and start the unit again." >&2
  exit 1
fi
```

**Каталог — query-параметр, и сервер его не проверяет.** Ключ `directory` в теле
запроса принимается с 200 и молча игнорируется, а несуществующий путь в
query тоже принимается. Поэтому проверка живёт в клиенте, рядом с параметром,
который она охраняет:

```verbatim core/opencode/transport.py
    def _scoped_params(self) -> Mapping[str, str]:
        """The `?directory=` every session-scoped route needs -- checked locally (C6).

        The server accepts a directory that does not exist and answers 200, so a
        typo would hand the agent's file tools a root that is not there. The check
        lives here, inseparable from the parameter it guards.
        """
        if not os.path.isdir(self._directory):
            raise OpencodeError(
                f"opencode {self._name}: workspace {self._directory!r} does not exist and the server "
                "does not validate ?directory=, so no session-scoped request was sent"
            )
        return {"directory": self._directory}
```

**Дедлайн — это не отмена.** Голосовой ход, переживший `r2d2_fast_deadline`,
продолжает работать на стороне сервера, а ответ забирает фоновый сборщик.
Поэтому у клиента два разных таймаута:

```verbatim core/opencode/client.py
#: A liveness probe must not eat the voice budget, so it gets its own bound.
HEALTH_TIMEOUT_S: Final = 1.5
#: The request timeout for a deadline-bounded call is the deadline plus this, so
#: `asyncio.wait_for` is always what fires first and the caller always sees
#: `OpencodeDeadlineExceeded` rather than a bare `httpx.TimeoutException`.
DEADLINE_GRACE_S: Final = 0.5
```

**Поток событий не читается голосовым дедлайном.** `GET /event` — не запрос: это
соединение, которое живёт столько, сколько живёт сессия, и opencode отправляет
в него `server.heartbeat` раз в **10,0 с** (измерено на этой машине, U4).
Наехав на тот же `timeout: 3.2` — это бюджет Алисы, а не потока — читатель не
дожидался даже первого сердцебиения: он переподключался по лестнице
`3,2 с / 5 с` и был слеп большую часть каждого окна. Живой прогон показал это
буквально: **7 из 7** `permission.asked`, отправленных сервером в провод, читатель
не увидел, а брокер разрешений, который живёт только на этом событии, не
работает совсем. Поэтому у потока своя граница — `event_read_timeout` в
`config/backends.json`, — а `timeout` остаётся дедлайном запроса:

```verbatim core/backends/config_loader.py
#: The bound on ONE read of the long-lived opencode event stream. It is a different
#: measurement from `timeout` and needs a different number: opencode heartbeats
#: every 10.0 s on the build that was measured, so a read bound below that cannot
#: survive to the next heartbeat and the reader reconnects instead of listening --
#: blind, and missing `permission.asked`, for a large part of every window. Three
#: heartbeats of slack keeps a healthy connection through a dropped or delayed one,
#: and still bounds how long a genuinely dead server goes unnoticed by something
#: far shorter than the 300 s the permission broker waits before it rejects an
#: unanswered ask. See `core/opencode/sse.py`.
DEFAULT_EVENT_READ_TIMEOUT: Final = 30.0
```

```verbatim core/opencode/sse.py
    def _timeouts(self) -> httpx.Timeout:
        """The request deadline for everything that is a request, and the stream's own
        bound for the one read that is supposed to wait.

        Sharing a single number here is what made the reader blind: the read bound
        has to outlast a 10.0 s heartbeat, and the request deadline must not, because
        a caller waiting on a turn has 3.2 s of Alice budget. httpx applies the read
        timeout per socket read, so one long bound is exactly the patience an idle
        stream needs and never a ceiling on the whole connection.
        """
        return httpx.Timeout(
            connect=self._deadline_s,
            read=self._read_timeout_s,
            write=self._deadline_s,
            pool=self._deadline_s,
        )
```

Тридцать секунд — это три сердцебиения запаса: одно потерянное или задержавшееся
не рвёт живую связь, а сервер, который действительно умер, обнаруживается намного
быстрее, чем `r2d2_permission_timeout` (300 с), по которому брокер отказывает в
неотвеченном вопросе. Длинная граница не делает выключение долгим: читатель
освобождается отменой задачи, а не ожиданием своей границы, и
`tests/test_sse_stream_timeout.py` проверяет и то, и другое через настоящий сокет —
`MockTransport` не имеет таймаутов и этот дефект увидеть не мог.

**HTTP 200 — это не ответ.** Отказ модели лежит в поле `info.error` внутри
успешного 200, поэтому проверка содержимого предшествует чтению `parts`. Ниже —
маршрут, который отвечает на запрос разрешения, и заодно весь ответ на вопрос
«а что если послать always»:

```verbatim core/opencode/client.py
    async def respond_permission(
        self, session_id: str, permission_id: str, response: Literal["once", "reject"]
    ) -> bool:
        """`POST /session/:id/permissions/:permissionID {"response": ...}` -> bool.

        `"always"` is deliberately NOT in the type. The server offers it, and one
        `always` permanently grants the whole command mask the event advertised in
        `properties.always` (e.g. `["echo *"]`), so it has to be unrepresentable at
        this signature rather than merely unused by today's caller. A "once" answer
        is forgotten with the turn, which is the only durable cost of answering.
        """
        answered = await self._request(
            "POST",
            f"/session/{session_id}/permissions/{quote(permission_id, safe='')}",
            params=self._scoped_params(),
            json_body={"response": response},
        )
        return decode(answered, bool)
```

---

## 3. Два агента

### 3.1 Почему по имени, а не «последний»

`OPENCODE_CONFIG_DIR` **складывается** с глобальным конфигом, а не заменяет его:
при установленном R2D2-конфиге opencode по-прежнему видит 11 чужих агентов
(`Sisyphus`, `oracle`, `prd-maker`, …) и 15 чужих провайдеров. Отсюда два
правила, на которых стоит весь файл `config/opencode/r2d2.opencode.json`:

1. **Обращаться только по точному имени** — `r2d2-voice` и `r2d2-agent`.
   Позиционный или нечёткий выбор из 13 агентов — это монетка, которая падает
   на `oracle`.
2. **Решать каждое разрешение явно, `*` первым ключом.** Правило opencode —
   «после совпадения побеждает последнее», а глобальные правила лежат ниже и
   ключом `*` здесь не отменяются. Пропущенный ключ — это не запрет, это
   наследование глобальной конфигурации, где у того же ключа вполне может быть
   `allow`.

Ни один из агентов не объявляет `tools`: в `GET /agent` это поле читается как
`null`, а объявленный `tools` нормализуется в правила `permission`. Проверять
надо `permission`.

### 3.2 `r2d2-voice` — голосовой агент

Отвечает за `r2d2_fast_deadline` секунд, не имеет права ничего менять в системе
и обязан быть коротким. Блок разрешений **дословно** из
`config/opencode/r2d2.opencode.json`:

```verbatim config/opencode/r2d2.opencode.json
      "permission": {
        "*": "deny",
        "read": {
          "*": "allow",
          "*.env": "deny",
          "*.env.*": "deny",
          "*.env.example": "allow"
        },
        "edit": "deny",
        "glob": "allow",
        "grep": "allow",
        "bash": {
          "*": "deny",
          "/home/koluchiy/.r2d2/r2d2_do.py *": "allow",
          "python3 /home/koluchiy/.r2d2/r2d2_do.py *": "allow",
          "/home/koluchiy/Documents/R2_yandex_station/.venv/bin/python /home/koluchiy/.r2d2/r2d2_do.py *": "allow",
          "upower *": "allow",
          "cat /sys/class/power_supply/*": "allow",
          "df *": "allow",
          "free *": "allow",
          "uname *": "allow",
          "hostname *": "allow",
          "ps *": "allow",
          "uptime": "allow",
          "date": "allow",
          "/home/koluchiy/.r2d2/r2d2_do.py shell *": "deny",
          "python3 /home/koluchiy/.r2d2/r2d2_do.py shell *": "deny",
          "/home/koluchiy/Documents/R2_yandex_station/.venv/bin/python /home/koluchiy/.r2d2/r2d2_do.py shell *": "deny"
        },
        "task": "deny",
        "skill": "deny",
        "lsp": "deny",
        "question": "deny",
        "webfetch": "deny",
        "websearch": "deny",
        "external_directory": "deny",
        "doom_loop": "deny"
      },
```

Три вещи в нём неочевидны:

- `read` разрешён, но `*.env` и `*.env.*` запрещены, а `*.env.example`
  разрешён. Агент читает репозиторий и не читает секреты.
- `bash` — это не «включён», это список из двенадцати разрешённых команд плюс
  `*` в `deny`. В списке три формы вызова шима (см. [06-tools.md](06-tools.md))
  и девять read-only проб (`upower *`, `df *`, `uptime`, `date`, …).
- Три правила `deny` на `r2d2_do.py shell *` идут **после** allow и перекрывают
  его: allow на `r2d2_do.py *` сам по себе включал бы подкоманду `shell`.

### 3.3 `r2d2-agent` — рабочий агент

Делает настоящую работу, отвечает подробно (его текст уходит в Telegram, а не
в колонку), и всё, что способно изменить машину, спрашивает разрешения. Блок
разрешений **дословно**:

```verbatim config/opencode/r2d2.opencode.json
      "permission": {
        "*": "ask",
        "*_*": "deny",
        "todowrite": "deny",
        "read": {
          "*": "allow",
          "*.env": "deny",
          "*.env.*": "deny",
          "*.env.example": "allow"
        },
        "edit": "ask",
        "glob": "allow",
        "grep": "allow",
        "bash": {
          "*": "ask",
          "/home/koluchiy/.r2d2/r2d2_do.py *": "allow",
          "python3 /home/koluchiy/.r2d2/r2d2_do.py *": "allow",
          "/home/koluchiy/Documents/R2_yandex_station/.venv/bin/python /home/koluchiy/.r2d2/r2d2_do.py *": "allow",
          "upower *": "allow",
          "cat /sys/class/power_supply/*": "allow",
          "df *": "allow",
          "free *": "allow",
          "uname *": "allow",
          "hostname *": "allow",
          "ps *": "allow",
          "uptime": "allow",
          "date": "allow"
        },
        "task": "ask",
        "skill": "ask",
        "lsp": "ask",
        "question": "ask",
        "webfetch": "allow",
        "websearch": "allow",
        "external_directory": "ask",
        "doom_loop": "ask"
      },
```

Отличия от голосового агента, которые стоит запомнить:

| Ключ | `r2d2-voice` | `r2d2-agent` | Почему |
|---|---|---|---|
| `*` | `deny` | `ask` | у агента каждое незнакомое действие проходит через человека, у голосового — не проходит никогда |
| `edit` | `deny` | `ask` | редактирование файлов нужно для работы, но не для ответа на вопрос |
| `task`, `skill`, `lsp`, `question` | `deny` | `ask` | вложенные агенты и LSP — сила, не необходимость; в голосовом пути это лишние секунды |
| `webfetch`, `websearch` | `deny` | `allow` | ответ на вопрос не требует сети; исследование требует |
| `external_directory` | `deny` | `ask` | выход за пределы рабочего каталога — всегда решение человека |
| `doom_loop` | `deny` | `ask` | зацикливание настолько дорого, что в голосовом пути оно запрещено |

### 3.4 Что R2D2 отправляет в этих полях

```verbatim config/backends.json
    {"name": "opencode", "kind": "opencode_session", "base_url": "http://127.0.0.1:4599",
     "username": "${R2D2_OC_USERNAME}", "password": "${R2D2_OC_PASSWORD}",
     "voice_agent": "r2d2-voice", "task_agent": "r2d2-agent",
     "fast_model": "opencode/space-bunny-free", "task_model": "opencode/space-bunny-free",
     "summarize_model": "opencode/space-bunny-free", "timeout": 3.2,
     "event_read_timeout": 30.0},
```

`${VAR}` подставляется из окружения за один проход. Отсутствующая переменная в
поле-учётке (`api_key`, `username`, `password`) оставляет поле пустым и печатает
WARNING с **именем** переменной; такой бэкенд выпадает из цепочки с причиной, а
не роняет реестр. Отсутствующая переменная в любом другом поле — ошибка
разбора.

---

## 4. Сентинел `[[NEEDS_AGENT]]`

### 4.1 Протокол

Голосовой агент не умеет работать, но умеет признать это. Правило 7 его промпта:
если задача требует исследования, файлов, кода, нескольких шагов или не влезает
в три предложения, ответ начинается **ровно** с `[[NEEDS_AGENT]]`, а сразу за
ним идёт одна короткая строка с описанием задачи. Больше в этом ответе ничего
нет.

```text
r2d2-voice  «найди последние статьи про RAG и сделай сводку»
                 │
                 ▼
           [[NEEDS_AGENT]]Сводка статей про RAG за неделю.
                 │
                 ├── parse_voice_reply ──► kind="escalate", spoken="", task_hint="Сводка статей…"
                 │
                 ├── submit_task (prompt_async) ──► 204, голосом «Проверяю, пришлю в телеграм.»
                 │
                 └── спустя N секунд: opencode_reply job ──► collect_reply ──► Telegram
```

Схема обещает ровно один сборщик на эскалацию — и это теперь не соглашение, а
свойство кода. Все три ветки, которые уходят агенту (C8, сентинел, дедлайн),
возвращаются **через один метод**: `SessionCollector.hand_to_agent` в
`core/session_collector.py`. Он и отдаёт ход агенту, и вооружает сборщик, и
больше нигде во всём сервере не пишется задача `opencode_reply` и не вызывается
`submit_task`. Поэтому ветка не может забыть сборщик (само возвращение
подтверждения **и есть** этот вызов) и не может завести второй.

Живой прогон показал, чего стоило это обещание, пока его не держал код: ветки
эскалации не ставили сборщик вообще, агент исследовал 60 статей на arxiv, а
пользователь не получил ничего.

Значение сентинела — поле `r2d2_needs_agent_sentinel`, а не литерал в коде,
поэтому переименовать его можно одной правкой конфигурации:

```verbatim app/config.py
    backends_path: str = "config/backends.json"
    r2d2_fast_deadline: float = 3.2
    r2d2_task_ack: str = "Проверяю, пришлю в телеграм."
    r2d2_needs_agent_sentinel: str = "[[NEEDS_AGENT]]"
    r2d2_voice_agent: str = "r2d2-voice"
    r2d2_task_agent: str = "r2d2-agent"
    r2d2_workspace: str = "/home/koluchiy/r2d2-workspace"
    r2d2_permission_timeout: float = 300.0
    r2d2_session_soft_limit: int = 40
    r2d2_stale_session_seconds: float = 900.0
    r2d2_cli_path: str = "/home/koluchiy/.r2d2/r2d2_do.py"
    r2d2_event_poll_interval: float = 2.0
```

### 4.2 Четыре guard'а между сентинелом и человеком

Их четыре, и они независимы — каждый ловит то, что другие пропускают. Первые три
стоят между сентинелом и голосом Алисы, четвёртый — на исходящей границе в
Telegram, куда Алиса отправить не может.

| # | Guard | Где | Что ловит |
|---|---|---|---|
| 1 | `parse_voice_reply` | `core/routing.py` | **структурный.** Разрезает ответ по первому вхождению сентинела и **выбрасывает хвост**. Данные для эскалации физически не являются значением, которое можно передать дальше |
| 2 | `sanitize_for_speech` | `core/routing.py` | **тотальный.** Возвращает `""` для `None` и для *любой* строки, содержащей сентинел. Существует для того, кто дойдёт до `text`/`tts` в обход первого guard'а |
| 3 | `Brain._speakable` | `core/brain.py` | **по времени.** Глотает сентинел на **сыром** тексте каждого хода, до `render.clean`, который сносит скобки сентинела и обрезает по 1024 символа |
| 4 | `for_human` | `core/routing.py` | **на границе Telegram.** Собиратель читает текст ассистента прямо из сессии и не знает, ответ это или сигнал маршрутизации, поэтому функция снимает токен с **любого** текста, предназначенного человеку. В отличие от первых двух она не выбрасывает сообщение целиком: сводка на 60 статей, процитировавшая токен в одной фразе, всё равно должна прийти |

Третий — единственный, чья ошибка была бы неочевидна: guard после чистителя
искал бы строку, которую чиститель уже разобрал, и пропустил бы сентинел,
лежащий за срезом длины. Поэтому он стоит раньше всех.

Четвёртый отличается от остальных обратной стороной. Сессия хранит **оба** вида
сообщений ассистента — ответ агента и `[[NEEDS_AGENT]]` голосового агента, — и
различить их на выходе нечем, поэтому guard стоит на выходе, а не в сборщике:
один и тот же `_deliver` в `core/async_worker.py` и `_tell` в
`core/permissions.py` гоняют через него всё, что предназначено человеку. Живой
прогон измерил, что именно этот вход был открыт: 11 сообщений с сырым
маркером дошли до Telegram владельца.

```verbatim core/routing.py
def parse_voice_reply(raw: str, *, sentinel: str) -> RouteDecision:
    """Route one voice reply, discarding everything from the sentinel onward.

    The sentinel is matched **literally** with `str.find` -- it is
    `[[NEEDS_AGENT]]`, a regex character class to anyone careless, so no regex
    engine ever sees it. The **first** occurrence splits, so a doubled sentinel
    cannot smuggle a second payload past the guard, and the text before it is
    the whole hint. Input without a sentinel is returned untouched: routing
    neither cleans nor truncates, because `render.clean` runs later and must
    never be the thing that decides whether a sentinel survived.
    """
    _require_sentinel(sentinel)
    index = raw.find(sentinel)
    if index < 0:
        return RouteDecision(kind="speak", spoken=raw, task_hint="")
    return RouteDecision(kind="escalate", spoken="", task_hint=raw[:index].strip())
```

```verbatim core/routing.py
def sanitize_for_speech(raw: str | None, *, sentinel: str) -> str:
    """The last thing a string passes through on its way to a speaker.

    Total by construction: `""` for `None` and for *any* input containing the
    sentinel, the input itself otherwise -- unchanged, because a sanitiser that
    also reformats is a second, differently-buggy `render.clean`. Dropping the
    whole string rather than the sentinel alone is deliberate: a reply that
    asked to be escalated has nothing speakable in it, and a partial strip
    would leave the model reading its own instruction aloud.
    """
    _require_sentinel(sentinel)
    if raw is None or sentinel in raw:
        return ""
    return raw
```

```verbatim core/brain.py
    def _speakable(self, text: str) -> str:
        """The third guard: no reply carrying the escalation sentinel reaches Alice.

        Total, and it runs on the RAW text of every turn before ``render.clean``,
        which strips the brackets the sentinel is written with and truncates at
        1024 characters. A guard placed after the cleaner would be searching for a
        string the cleaner had already dismantled, and would miss a sentinel past
        the cut -- the one place a truncation-blind guard leaks.
        """
        spoken = routing.sanitize_for_speech(text, sentinel=self.cfg.r2d2_needs_agent_sentinel)
```

```verbatim core/routing.py
def for_human(raw: str | None, *, sentinel: str) -> str:
    """The part of `raw` a human may read: the token is out, the answer is in.

    The collector reads assistant text straight out of a session, and a session
    holds BOTH kinds of assistant message: the agent's answer, and the voice
    agent's `[[NEEDS_AGENT]]` routing signal. Telling them apart is this
    module's job, so this is where the Telegram boundary is guarded -- one
    function every outbound body passes through, rather than a strip in each
    producer that can be forgotten by the next one.
```

Плюс два свойства, которые делают схему рабочей:

- **Пустой сентинел — исключение, а не «совпадение со всем».** Подстрока `""`
  есть в любой строке, поэтому пустой сентинел либо тихо отключил бы эскалацию
  (модель могла бы произнести настоящий сентинел вслух), либо заглушил бы голос
  вообще. Оба исхода неприемлемы, поэтому это громкая ошибка конфигурации.
- **Разбор не чистит и не обрезает.** Это делает `render.clean` — и обязательно
  **после** того, как сентинел уже исчез. Обрезка не должна решать, выжил ли
  сентинел.

### 4.3 Пятый guard: служебное состояние opencode не остаётся в сессии

Четыре guard'а выше смотрят в одну сторону — **наружу, к человеку**. Живой прогон
показал, что отказ был ровно в противоположную сторону: сентинел лежит в том самом
артефакте, который проект называет постоянным контекстом, поэтому `r2d2-agent`
читает собственную историю, копирует токен — и сессия перестаёт работать
совсем. Измерено: после одного `[[NEEDS_AGENT]]` агент не сделал ни одного
вызова инструмента, а `POST /session/:id/summarize` ответил `true` и ничего не
сжал (39 сообщений, все оригиналы, сентинел на месте). **Поэтому фикс не может
опираться на суммаризацию** — токен, переживший её, отравляет сессию навсегда.

Поэтому пятый guard удаляет сообщение, а не маскирует его. Один
`GET /session/{session_id}/message` читает транскрипт один раз и решает сразу
оба вопроса — что удалить и где заякорить сборщик, — а удаление идёт **до**
`prompt_async`:

```verbatim core/routing.py
def transcript_sweep(records: Sequence[MessageRecord], *, sentinel: str) -> TranscriptSweep:
    """The stored enforcement state in one transcript, and the anchor left behind.
```

Порядок здесь не оптимизация, а единственная правильная последовательность.
Якорь, указывающий на удаляемое сообщение, — это якорь, который сервер уже не
отдаёт, а `collect_reply` читает ненаходимый маркер как «новее всего» и
отправляет в Telegram **всю переписку**. Объявление `TranscriptSweep` поэтому
несёт оба значения и заморожено: их используют два модуля по дороге к разным
границам.

#### Что удаляется, а что нет

Свип удаляет **два** вида stored-состояния, и оба — по одному признаку: роль
сообщения `assistant` плюс свой признак содержимого.

| | признак | чем читается |
|---|---|---|
| маркер эскалации | сентинел в `text` | `record.text` |
| отказ в вызове инструмента | `tool`-часть, `state.status == "error"`, `state.error` начинается с фразы opencode о запрете | `record.refused` |

Что при этом **не** удаляется, определено ролью, а не строкой: сообщение
`user`, в котором пользователь произнёс сентинел вслух, остаётся. Один контекст
на человека существует ради того, чтобы содержимое пользователя выживало.

Отказ в вызове инструмента удаляется тоже — это изменение относительно
предыдущей редакции этого раздела, и оно сделано по измерению
([11-opencode-contract.md](11-opencode-contract.md), U8; подробно в
[09-security.md](09-security.md) §8.8). Раньше отказ здесь объявлялся
неудаляемым, потому что `MessageRecord` собирал текст только из частей
`type: "text"`, отказанный ход приходил с пустым `text`, и свип его физически не
видел. Видимость добавлена: `list_messages` теперь отдаёт и признак отказа, и
`transcript_sweep` решает по нему.

**Ключ признака — не пустота.** «Ассистентское сообщение без текста» — это ещё и
каждый успешный ход с инструментами: в живой сессии четыре завершившихся вызова
`glob` сохранены с частями `step-start` / `tool` (`state.status: "completed"`) /
`step-finish` и точно так же без текстовой части. Удаление по пустоте снесло бы
запись обо всём, что сработало. Поэтому признак читается с `tool`-части и
требует **двух** условий: `state.status == "error"` **и** `state.error` открывается
фразой opencode о запрете. Второе условие не избыточно: в той же живой сессии
`webfetch` на несуществующий хост сохранён с `state.status: "error"` и строкой
`Transport error (GET …)`, и это результат работы инструмента, а не состояние
enforcement — запись пользователя, а не мусор.

Цена решения названа прямо: удаляя отказ, R2D2 убирает запись о том, что opencode
отказал команде, которую просил пользователь. Обмен выбран в пользу удаления,
потому что вред от хранения измерен и больше: живой прогон показал, что агент,
прочитавший чужую матрицу, объявлял «у меня нет ни одного инструмента» и
отказывался звать даже те, что его собственная матрица разрешает. Что
пользователь сохраняет: его own запрос (сообщение `user` не удаляется никогда) и
plain-prose ответ отказавшего агента следующим сообщением — «Не могу — эту
команду мне выполнять запрещено» — который лежит в части `text` и остаётся.
Проверено на живом прогоне: [D9](../qa/live-run-v4.md).

Одна честная оговорка. Ветка `deadline` отдаёт ход агенту, когда голосовой ход
ещё **идёт** на стороне сервера, — в этот момент ни сообщения с сентинелом, ни
отказа в транскрипте ещё нет, и удалять нечего. Они могут прийти позже, и тогда
их снимет следующая же эскалация: развёртка ищет все сохранённые сигналы, а не
только что записанный, поэтому сессия, отравленная прошлым ходом, чинится сама.
Гарантия «в том же ходе» относится к ветке сентинела — к той, где R2D2 увидел
маркер своими глазами. По той же причины правило про чужой отказ осталось в
промптах обоих агентов: ход, который породил отказ, часто заканчивается ответом
пользователю и эскалацией не оборачивается вовсе, а этот свип запускается
только на эскалации.

---

## 5. Брокер разрешений

### 5.1 Поток

```text
  opencode                 R2D2                         Telegram
     │                       │                              │
     │  permission.asked     │                              │
     │  (sessionID отфильтрован)                           │
     ├──────────────────────►│                              │
     │                       │ pending_actions: kind=opencode_permission
     │                       │ ← строка ЗАПИСЫВАЕТСЯ ПЕРВОЙ │
     │                       │   ключ = application_id      │
     │                       │                              │
     │                       │ «Нужно подтверждение: …»     │
     │                       ├─────────────────────────────►│
     │                       │      (чат этого человека)   │
     │                       │◄──── «да» ──────────────────┤
     │                       │ confirmation_verdict → yes    │
     │                       │ pending_actions очищена       │
     │  POST …/permissions/… │                              │
     │◄──────────────────────┤ {"response": "once"}         │
     │  200 true             │                              │
     │                       │ «Принято, выполняю: …»       │
     │                       ├─────────────────────────────►│
```

Порядок в середине важен: **строка пишется раньше, чем уходит вопрос**. Обратный
порядок оставил бы вопрос, на который ответу некуда долететь.

Схема читается как «любой текст в Telegram отвечает на любой вопрос», и это не
так. Строка лежит под `application_id` того человека, чей ход её поднял, а
`application_id` Telegram-хоста **объявлен**, а не выведен из `chat_id`
(раздел 10). До брокера доходит только сообщение из привязанного чата **того же**
человека: остальные не находят своей строки и читаются как обычный вопрос.
Именно поэтому `да` из Telegram и может ответить на вопрос, заданный голосом
Алисы, — и раньше не мог: `/tg/webhook` мнил себе `tg:<chat_id>`, получал
вторую сессию на одного человека и отвечал в сессию, где вопрос не задавали.

### 5.2 Область ответов — ровно два значения

```verbatim core/permissions.py
#: The whole answer domain. `respond_permission` carries the same `Literal` in
#: `core.opencode.client`, so a third value does not typecheck on the way to the wire
#: either -- the invariant lives in the type, and the test reads the source.
PermissionAnswer: TypeAlias = Literal["once", "reject"]
#: The one approval: run this once. It is forgotten with the turn.
APPROVE_ONCE: Final[PermissionAnswer] = "once"
#: The one refusal -- and the answer every timeout and every failure in this module
#: posts, because an unanswered risky command must default to not running.
REFUSE: Final[PermissionAnswer] = "reject"
#: What the server offers in `properties.always` and what this module refuses by
#: name, so the refusal is a value the guard test can check rather than a comment.
DURABLE_GRANT: Final = "always"
#: Everything this module may ever answer with, and the assertion of the invariant.
ANSWERS: Final[frozenset[str]] = frozenset({APPROVE_ONCE, REFUSE})
assert ANSWERS == frozenset({"once", "reject"}) and DURABLE_GRANT not in ANSWERS
```

Сервер предлагает третий вариант и показывает в `properties.always` маску
команд, которую этот вариант разрешил бы навсегда (`["echo *"]` в замере).
Поэтому `always` невозможно даже **выразить** в типах: его нет ни в
`PermissionAnswer`, ни в сигнатуре `respond_permission` на клиенте, ни в
`ANSWERS`. Инвариант живёт в типе, а не в комментарии.

### 5.3 Четыре ситуации, четыре ответа

| Ситуация | Что делает брокер | Что слышит пользователь |
|---|---|---|
| opencode спросил | пишет строку, логирует маску, спрашивает в Telegram | «Нужно подтверждение: {действие}. Ответь в телеграм «да» или «нет».» |
| пользователь ответил «да» | `{"response": "once"}`, строка очищается | «Принято, выполняю: {действие}.» |
| пользователь ответил «нет» | `{"response": "reject"}`, строка очищается | «Отклонено: {действие}.» |
| ответа нет дольше `r2d2_permission_timeout` (300 с) | сборщик по таймауту отказывает, строка очищается | «Подтверждение не получено, действие отклонено.» |

Тонкости, которые стоит знать:

- **Отказ — значение по умолчанию.** Нет ответа, сервер недоступен, ответил
  `200 false`, сессия 404, часы в строке нечитаемы — всё заканчивается `reject`.
  Строка очищается даже тогда: запрос, на который ответить уже нельзя, — это
  подтверждение, из которого пользователь не может выйти.
- **`approved` возвращается только если сервер подтвердил приём ответа.** Меньше
  этого «можно выполнять» не говорится никогда.
- **Одна строка на пользователя.** Новый запрос вытесняет старый, и вытесненный
  отказывается — к этому моменту его уже никто не может подтвердить.
- **Повтор того же события не продлевает окно.** `requested_at` остаётся
  исходным, поэтому переподключение SSE не даёт запросу второй шанс прожить
  вечно.
- **Маски только логируются.** Они не отправляются никуда: в теле ответа на
  разрешение уходит один ключ с одним из двух значений.

### 5.4 Откуда берётся вопрос, в зависимости от режима

```verbatim core/permissions.py
#: The plan's sweep cadence, and the window the operator dials with
#: `r2d2_permission_timeout`.
SWEEP_INTERVAL_S: Final = 30.0
#: Where an ask comes from in each live `EVENT_MODE` -- the only thing the two live
#: modes differ in. A mode that is not in this table is the inert one, which is the
#: direction to be wrong in: a build that does not know its own mode must not answer
#: permissions.
ASK_SOURCE: Final[Mapping[str, str]] = MappingProxyType(
    {
        "sse": "as `permission.asked` on the opencode event stream",
        "poll": "from the polling collector that reads the same session",
    }
)
```

Режима, которого нет в этой таблице, не существует: он инертен, и брокер на
старте говорит об этом в INFO и не отвечает ни на что. Отказывать —
восстановимо, отвечать неправильно — нет.

---

## 6. Цепочка фолбэков

### 6.1 Что в ней объявлено и что из этого используется

| Имя | `kind` | Адрес | Роль |
|---|---|---|---|
| `opencode` | `opencode_session` | `http://127.0.0.1:4599` | **маршрут, не член цепочки**: попытка уже была, а второй заход на тот же хост съел бы бюджет, которого у хода уже нет |
| `zen` | `openai_compatible` | `https://opencode.ai/zen/v1` | первый фолбэк, та же модель напрямую |
| `yandexgpt` | `openai_compatible` | `https://llm.api.cloud.yandex.net/foundationModels/v1` | второй фолбэк, серверы в РФ |
| `openrouter` | `openai_compatible` | `https://openrouter.ai/api/v1` | последний фолбэк |

Членство в цепочке не означает работоспособность. Бэкенд, у которого пусто поле,
без которого он не может позвонить, **выпадает с WARNING, называющим бэкенд и
поле** — и цепочка продолжает собираться. Пустой результат — ошибка
конфигурации, а не пустой список:

```verbatim core/backends/registry.py
REQUIRED_FIELDS: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {
        "opencode_session": ("base_url", "fast_model"),
        "openai_compatible": ("base_url", "model"),
    }
)
#: Credentials per kind. `opencode_session` authenticates with basic auth, whose
#: password is the mandatory half; the username has a server-side default.
#: `openai_compatible` sends a bearer or Api-Key header built from `api_key`.
REQUIRED_CREDENTIALS: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {
        "opencode_session": ("password",),
        "openai_compatible": ("api_key",),
    }
)
```

Исключение одно и оно по построению: `opencode_session` нельзя построить из
одной спецификации — адаптеру нужен клиент, хранилище сессий, конфигурация и
(при `EVENT_MODE` = `sse`) читатель событий. Их собирает композиционный корень и
передаёт одним значением. Спецификация такого kind без сборки — ошибка
конфигурации, а не «построится позже».

### 6.2 Как добавить провайдера за шесть строк

Один объект в массиве `backends` и, если нужен другой порядок, его имя в
`chain`. Кода это не касается вообще.

```text
    {"name": "groq", "kind": "openai_compatible", "base_url": "https://api.groq.com/openai/v1", "api_key": "${GROQ_API_KEY}", "model": "llama-3.3-70b-versatile", "auth_style": "bearer"},
```

Шесть полей — это минимум для `openai_compatible`: имя, kind, адрес, ключ,
модель, схема авторизации. `auth_style` бывает `bearer` или `yandex`; при
`yandex` дополнительно `auth_mode` (`api_key` либо `iam_token`). Пропущенное
поле-учётка оставляет бэкенд непригодным — и он выпадет с причиной, а не молча.

### 6.3 Как поменять модель в одну строку

Три строки в том же объекте, и больше нигде — ни в коде, ни в конфиге агентов:

| Строка | Где | Как выглядит сейчас |
|---|---|---|
| `fast_model` | `config/backends.json` | `"fast_model": "opencode/space-bunny-free",` |
| `task_model` | `config/backends.json` | `"task_model": "opencode/space-bunny-free",` |
| `summarize_model` | `config/backends.json` | `"summarize_model": "opencode/space-bunny-free",` |

Перед перезапуском проверьте, что новый идентификатор есть в каталоге сервера
(`core/opencode/models.py` делает это на старте): **неизвестный `model id` — не
ошибка**, opencode молча подставит другую модель и ответит 200, а R2D2 ответил
бы голосом от мозга, который никто не выбирал. Пополнение баланса Zen меняет
ровно эти три строки и больше ничего.

---

## 7. Задержки: что измерено и что это значит для 4,5 с

### 7.1 Измерено

| Величина | Значение | Как получено |
|---|---|---|
| p50 голосового хода | **1.667 с** | 10 выборок, одна сессия, свежий сервер |
| p95 голосового хода | **2.247 с** | те же 10 выборок |
| первый ход в новой сессии, тело ответа | **15.5 с** | холодный кэш: `cache.write` 1914 токенов |
| первый ход в новой сессии, на стену | **18.6 с** | из них ~3 с — запуск до первого токена |

Полные ряды и метод — в [11-opencode-contract.md](11-opencode-contract.md),
раздел U6. Это измерения **одной** модели: `opencode/space-bunny-free`.
Другой годной модели на этой машине нет (раздел 9, C1), поэтому заменять эти
цифры нечем.

### 7.2 Что это значит для бюджета Алисы

`r2d2_fast_deadline` = 3.2 с — это p95 плюс запас, и оно укладывается в
4,5 с вместе с распознаванием речи и доставкой. Два вывода из таблицы:

1. **Первый ход пользователя не может ждать.** 15.5–18.6 с не влезают ни в
   какой голосовой бюджет, поэтому ход с пустой сессией сразу уходит агенту, а
   пользователь получает подтверждение. Это и есть C8 в коде:

```verbatim core/session_route.py
        session_id = await wiring.store.resolve(app_id)
        if not await self._prepare(wiring, app_id, session_id):
            # C8: the first message in a fresh session costs 15.5-18.6s, so it is
            # submitted to the agent and acknowledged rather than waited on.
            self._handoff(rec, wiring)
            return await self.collector.hand_to_agent(wiring, app_id, session_id, command)
        current_application_id.set(app_id)
```

2. **Дедлайн не отменяет ход, но и не прощает его забыть.** Голосовой ход, не
   уложившийся в 3.2 с, продолжает работать на сервере; R2D2 отвечает
   пользователю подтверждением, **а ход всё равно уходит `r2d2-agent`**, и воркер
   забирает ответ позже и отправляет в Telegram. Отменять было бы уничтожение
   уже оплаченной работы.

   Раньше эта ветка только подтверждала и **никому ничего не отдавала**: запрос
   просто исчезал. Живой прогон это посчитал — шесть попыток управления ноутбуком
   подряд перебрали дедлайн, и агент не увидел ни одной. Причина в том, что на
   дедлайне **нельзя знать**, ответит запоздалый голосовой ход или попросит
   агента, поэтому работа отдаётся тому, кто её делает.

   Повторной работы здесь не будет, и это не «надеемся»: голосовой ход, идущий на
   сервере, **не отменяется никогда** и даёт одну короткую строку, а не результат
   работы; якорь сборщика (`since_message_id`) читается **до** отправки хода
   агенту, поэтому окно доставки не может переиграть уже сказанное.

### 7.3 Два пути

| | Голосовой путь | Агентный путь |
|---|---|---|
| Агент | `r2d2-voice` | `r2d2-agent` |
| Маршрут | `POST /session/{session_id}/message`, блокирующий | `POST /session/{session_id}/prompt_async`, 204 |
| Дедлайн | `r2d2_fast_deadline` = 3.2 с | нет: сервер работает, R2D2 не ждёт |
| Что слышит пользователь | ответ голосом | «Проверяю, пришлю в телеграм.» |
| Куда уходит результат | в `text`/`tts` Алисы | в Telegram через `opencode_reply` |
| Вход | тёплая сессия, без сентинела | холодная сессия (C8), сентинел, **превышен дедлайн** |

Все три входа в агентный путь идут **одним** вызовом `hand_to_agent`, который
отдаёт работу и ставит сборщик; на ветке дедлайна он, вопреки прежнему коду,
вызывается тоже — см. пункт 2 выше.

Запись одного хода — одна строка в логе, и её словарь зафиксирован в коде:

```verbatim core/metrics.py
#: Which brain answered the turn. `opencode` is the persistent-session route,
#: `fallback` is the provider chain in `config/backends.json`.
ROUTE_OPENCODE: Final = "opencode"
ROUTE_FALLBACK: Final = "fallback"

#: What the turn did with the request; see the module docstring for the
#: vocabulary. `voice` is the default because most turns are answered in place.
PATH_VOICE: Final = "voice"
PATH_ESCALATE: Final = "escalate"
PATH_ERROR: Final = "error"
PATH_DEADLINE: Final = "deadline"
```

`route=opencode` с `path=voice` — уложился; `path=escalate` — ушёл агенту;
`path=deadline` — не уложился и ответ заберёт воркер; `path=error` — сработала
graceful-ветка. Ни в одном поле нет ничего, что пришло из `${...}`, поэтому
секрет в такую строку попасть не может.

---

## 8. `EVENT_MODE`: три режима

```verbatim core/opencode/sse_frames.py
#: The event names U4 recorded, verbatim (C5). `GET /doc` also advertises V2
#: spellings (`permission.v2.asked`, `session.next.*`) which this build was never
#: seen sending, so they are not defined here and must not be used.
CONNECTED: Final = "server.connected"
PERMISSION_ASKED: Final = "permission.asked"
PERMISSION_REPLIED: Final = "permission.replied"
TURN_COMPLETE: Final = "session.idle"
TEXT_DELTA: Final = "message.part.delta"

#: The three ways R2D2 can learn what a turn did, per U4's ADOPTED line: only "sse"
#: is live, and "poll" (todo 9's collector) and "deny" (refuse every mutation) are
#: the pre-agreed degradations, kept typed so they stay reachable.
EventMode: TypeAlias = Literal["sse", "poll", "deny"]
EVENT_MODE: Final[EventMode] = "sse"
#: SSE's own type for a frame that named itself in neither place. opencode names
#: every frame in its body and never in the `event:` field (U4, re-measured), so
#: this is the shape of a frame from a third server, not of opencode's -- decoded
#: rather than discarded, because an unnamed frame is still a frame.
SSE_DEFAULT_EVENT: Final = "message"
```

### 8.0 Откуда берётся имя события (D11)

Имя приходит **из тела JSON**, верхнеуровневым полем `"type"`, а не из SSE-строки
`event:`. Это измерено, а не выведено: замер `GET /event` на 1.18.32 дал 1090 строк
`data:` и **ноль** строк `event:` за 30 минут, а короткое подтверждение на скретч-
сервере — 44 `data:` и 0 `event:` (`qa/d11-wire-tap.py`, `qa/d11-wire-tap.out`).
Все имена из таблицы выше — `server.connected`, `permission.asked`,
`permission.replied`, `session.idle`, `message.part.delta` — лежат в поле `"type"`.

Настоящий кадр выглядит так; строки `event:` в нём нет вообще:

```text
data: {"id":"evt_0dc8e0d96001ENniA8TFQh84Kv","type":"permission.asked","properties":{"id":"per_0dc8e0d950015YF13XN1QQAEcz", … }}
```

Поэтому `decode_frame` читает имя в таком порядке:

1. **поле `event:` кадра, если оно есть и непустое, — выигрывает.** Это единственное
   место, где спецификация SSE позволяет серверу назвать событие; сервер, который
   назвал, не додумывается, даже если тело говорит другое;
2. **иначе `"type"` тела** — то, что делает эта сборка, и единственный путь
   настоящего кадра;
3. **иначе `SSE_DEFAULT_EVENT`** — кадр, не назвавший себя нигде, декодируется
   («безымянный кадр всё ещё кадр»), и падать тут нельзя: этот разбор стоит в
   читателе, который отвечает на запросы разрешений.

Порядок зафиксирован пятью тестами в `tests/test_sse_wire_frame.py`: только
`event:`, только `"type"`, оба и совпадают, оба и расходятся, ни одного. Кадр, назвавший
себя в обоих местах по-разному, берёт `event:` и пишет WARNING.

**Почему это было не видно.** Юнит-тесты кормили декодер кадрами с `event:`-строкой —
формой, которой сервер не присылает никогда, — поэтому 906 зелёных тестов ничего не
говорили о проводе. `tests/fake_opencode.py` теперь тоже шлёт настоящую форму, и
настоящие тела из замера лежат в тестах дословно; девять тестов падают на прежнем
декодере.

| `event_mode` | Что работает | Что это значит операционно | Кто отвечает на `permission.asked` |
|---|---|---|---|
| `sse` | живой поток `GET /event` на каждую сессию | **текущий режим.** Вопрос о разрешении приходит в том же ходе, в котором он возник | да, из потока |
| `poll` | сборщик опрашивает `GET /session/{session_id}/message` | сервер или сеть молчат про событие; вопрос всплывает с задержкой в `r2d2_event_poll_interval` (2.0 с), а не мгновенно | да, из опроса той же сессии |
| `deny` | поток не читается | режим отказа: конфигурация агентов сама запрещает `bash`, `edit` и `external_directory`, поэтому вопрос **не может прийти** | нет, и это не уязвимость: спрашивать не о чем |

Сейчас жив только `sse`; остальные два — заранее оговорённые деградации,
оставленные типизированными, чтобы они оставались достижимыми, а не
превратились в фольклор. Режима, которого нет в таблице, не существует: сборка,
не знающая своего режима, не отвечает на разрешения.

### 8.1 Конец хода — это конъюнкция, а не одно событие

События «ход завершён» у opencode нет: `message.updated` приходит десяток раз
за ход, пока ответ ещё собирается, а закрывает ход `session.idle` — и
присылается он даже тогда, когда инструмент висит на неотвеченном запросе
разрешения. Поэтому конец хода — это `session.idle` **и** отсутствие
`permission.asked` без парного `permission.replied`:

```verbatim core/opencode/sse_frames.py
def turn_is_complete(events: Iterable[OpencodeEvent]) -> bool:
    """Whether `events` end a finished turn -- the C5 rule 2 conjunction.

    `session.idle` alone is not enough: the server sends it even while a tool is
    blocked on a permission answer, so idle alone would declare the turn over before
    it is and the reply would never be collected. A reply matches one ask, by
    `requestID` against that ask's `id`. Single-pass over any iterable, because
    todos 9 and 14 hand it whatever their consumer collected.
    """
    idle = False
    waiting: set[str] = set()
    for event in events:
        if event.type == PERMISSION_ASKED:
            asked = event.permission_id
            if asked is not None:
                waiting.add(asked)
        elif event.type == PERMISSION_REPLIED:
            answered = event.request_id
            if answered is not None:
                waiting.discard(answered)
        elif event.type == TURN_COMPLETE:
            idle = True
    return idle and not waiting
```

И ещё одно, что ломает наивную реализацию: поток `GET /event` **глобальный** и
несёт события всех сессий, включая чужие. Фильтр по `properties.sessionID`
стоит внутри `EventSource.events()`, между разбором кадра и любой возможной
передачей обработчику, так что обойти его нечем. Единственное исключение —
`server.connected`: у этого кадра нет `sessionID`, потому что он описывает
соединение, а не сессию, и он же доказывает, что переподключение удалось.

---

## 9. C1–C8: измеренные факты, не переобсуждаем

Эти восемь утверждений измерены на живой машине (см.
[11-opencode-contract.md](11-opencode-contract.md) и раздел «SPIKE CORRECTIONS»
плана). Код ссылается на них по букве, и пересматривать их можно только новым
замером, а не рассуждением.

| # | Утверждение | Где в коде |
|---|---|---|
| **C1** | **Сильной модели не существует.** `opencode serve` отклоняет каждую бесплатную Zen-модель, кроме `opencode/space-bunny-free`: HTTP 200 с внутренним 403 `FreeTierError`. Платные — внутренний 402, баланс пуст. **HTTP 200 не успех:** отказ лежит в теле. Все `model id` проверяются против `GET /config/providers` на старте | `core/opencode/models.py`, `core/opencode/wire.py` |
| **C2** | `OPENCODE_CONFIG_DIR` **складывается** с глобальным конфигом, а не изолирует его: 11 чужих агентов и 15 чужих провайдеров остаются видимы. Отсюда адресация по точному имени и явное решение каждого ключа разрешений | `config/opencode/r2d2.opencode.json` |
| **C3** | `system` действует **на одно сообщение**. Системные промпты лежат в определениях агентов, а не в теле каждого запроса | `config/opencode/r2d2.opencode.json` |
| **C4** | `tools` — это `Record<string, boolean>`, и он перекрывает `permission` агента на время хода. Но в `GET /agent` поле `tools` читается как `null`, а объявленный `tools` нормализуется в правила `permission` — проверять надо `permission` | `config/opencode/r2d2.opencode.json` |
| **C5** | Строки событий точные: `server.connected`, `permission.asked`, `permission.replied`, `session.idle`, `message.part.delta`. События «ход завершён» **не существует**. `GET /event` глобальный — фильтровать по `properties.sessionID`. `always` не отправлять никогда. `server.heartbeat` идёт раз в **10,0 с**, поэтому граница чтения потока — `event_read_timeout`, а не голосовой `timeout` | `core/opencode/sse.py`, `core/opencode/sse_frames.py`, `core/permissions.py` |
| **C6** | Каталог сессии — **query-параметр** `?directory=`. Тот же ключ в теле принимается с 200 и молча игнорируется, а сам путь сервер **не проверяет** — проверяет клиент | `core/opencode/transport.py`, `core/opencode/session_store.py` |
| **C7** | `GET /session/{session_id}/message` возвращает `{info, parts}`, а не плоский список: чтение `role` верхнего уровня молча даёт `None`. Неизвестный агент — **500** с бесполезным телом; неизвестный `model id` — **не ошибка** | `core/opencode/wire.py`, `core/opencode/models.py` |
| **C8** | Измеренная задержка: p50 1.667 с, p95 2.247 с, `r2d2_fast_deadline` = 3.2 с. **Первый ход в новой сессии — 15.5–18.6 с** и не должен стоять на синхронном голосовом пути | `app/config.py`, `core/brain.py` |

### 9.1 Свип моделей: почему в трёх слотах одна и та же строка

| Модель | Ответ Zen | Итог |
|---|---|---|
| `opencode/space-bunny-free` | 4.353 с | **годится** |
| `opencode/muse-spark-1.3-contributor-free` | 403 `FreeTierError` | не годится |
| `opencode/ling-3.0-flash-fin-free` | 403 `FreeTierError` | не годится |
| `opencode/mimo-v2.6-flash-free` | 403 `FreeTierError` | не годится |
| `opencode/nemotron-3-ultra-free` | 403 `FreeTierError` | не годится |
| `opencode/nemotron-3.5-lightning-free` | 403 `FreeTierError` | не годится |

Текст отказа дословно: `OpenCode's free tier can only be used from within
OpenCode`. Платные кандидаты отклоняются внутренним 402 `Insufficient account
funds`. Измеренной альтернативы нет, поэтому агентный путь сегодня не сильнее
голосового и полагается на серверное сжатие сессии, а не на отдельную модель.
Пункт «замерить muse-spark» остаётся непокрытым по существу, а не по забывчивости.

---

## 10. Сессия на пользователя

```verbatim core/opencode/session_store.py
#: The prefix of the one title R2D2 gives a user's session. It carries the
#: application id, so a session is findable again even if the database is lost.
TITLE_PREFIX: Final = "r2d2:alice:"
#: The only `GET /session/status` value that may be aborted.
BUSY: Final = "busy"


def title_for(application_id: str) -> str:
    """`f"r2d2:alice:{application_id}"` -- the only title R2D2 ever creates."""
    return f"{TITLE_PREFIX}{application_id}"
```

Заголовок несёт `application_id`, поэтому сессия находится заново даже при
потерянной базе. `resolve()` — единственный путь, который создаёт сессию, и он
под блокировкой на `application_id`: привязка проверяется против `GET /session`,
и мёртвая заменяется. Сборщик зависших трогает только те сессии, которые сервер
называет `busy` **и** которыми никто не пользовался дольше
`r2d2_stale_session_seconds`: медленный ход — не мёртвый, и первый ход в новой
сессии обрывать нельзя.

### 10.1 Откуда берётся `application_id` в Telegram

Один человек — один `application_id`, и он же владелец единственной сессии
opencode и единственного ожидающего вопроса. Поэтому **привязка объявлена, а не
выведена**: `R2D2_TG_APPLICATION_ID` в окружении, парами
`chat_id=application_id`, разделёнными запятой или пробелом.

```verbatim app/main.py
def tg_application_id(cfg: Config, chat_id: int) -> str | None:
    """The `application_id` this Telegram chat was bound to, or `None` for no binding.

    **The binding is declared, never derived.** One human is one `application_id`,
    and that id is what owns their single opencode session and their single pending
    permission question -- so a Telegram turn and an Alice turn of the same person
    have to arrive under the same one, or the answer to «да» is delivered to a
    session the question was never asked in. `/tg/webhook` used to build
    `f"tg:{chat_id}"` for itself, which is the live defect in `qa/live-run.md` §4c.
```

Чат, которому пара не объявлена, **отказывается**: сообщение не отвечает, и в
лог уходит WARNING, называющий переменную и подсказывающий её форму. Идентичность
не выдумывается — `tg:<chat_id>` в коде больше нет, потому что это и была
причина живой ошибки: вторая сессия opencode на одного человека и `да`,
доставленный в сессию, где вопрос не задавали. Нечитаемый токен в переменной
тоже ничего не привязывает и тоже даёт WARNING, так что опечатка не может
выглядеть как объявление.

**В этом развёртывании переменная ещё не задана** и станет ею только после
регистрации навыка Алисы (шаг 7 в [08-deployment.md](08-deployment.md)) — до
этого чата у Telegram нет, и оба ручных шага (юнит и навык) ещё впереди.

Агентный ход уходит и не ждёт ответа — 204 означает, что ход теперь принадлежит
серверу:

```verbatim core/backends/opencode_session.py
    async def submit_task(self, session_id: str, text: str) -> None:
        """`POST /session/:id/prompt_async` with the AGENT agent: submitted, not awaited.

        Fire and forget by contract -- the 204 means the server owns the turn now,
        and the reply is collected later by `collect_reply`. The client's own
        per-request timeout bounds this call, so a stalled server cannot hold the
        acknowledgement the speaker is waiting for.
        """
        await self._wiring.client.send_message_async(
            session_id, text, agent=self._spec.task_agent, model=self._spec.task_model
        )
```

---

## 11. Карта модулей

Каждый модуль под `core/` и что он делает. Новый модуль обязан здесь появиться:
`tests/test_docs.py` сверяет эту таблицу с файловой системой.

| Модуль | Роль |
|---|---|
| `core/async_worker.py` | очередь фоновых задач: arxiv-сводка и сбор ответа opencode; `Worker._deliver` — **единственная** исходящая граница проекта для текста, предназначенного человеку (снимает сентинел через `routing.for_human` перед `send_message`) |
| `core/backends/base.py` | протокол `Backend` и типы ответа (`Choice`, `ToolCall`, иерархия ошибок) |
| `core/backends/config_loader.py` | разбор `config/backends.json`: `${VAR}`, проверка `kind`, `BackendSpec` |
| `core/backends/openai_compatible.py` | один OpenAI-совместимый клиент для `zen`, `openrouter`, `yandexgpt` |
| `core/backends/opencode_session.py` | сессия opencode за обычным интерфейсом `Backend` плюс два агентных метода |
| `core/backends/registry.py` | `kind` → конструктор, фильтр непригодных, порядок цепочки |
| `core/brain.py` | один ход: маршрутизация, фолбэк, замер |
| `core/memory.py` | SQLite: `sessions`, `oc_sessions`, `pending_actions`, `jobs` |
| `core/metrics.py` | одна запись на ход: `route`, `path`, `llm_ms`, `total_ms` |
| `core/opencode/client.py` | **14 маршрутов `opencode serve`** по таблице раздела 2: 13 вызовов `_request` в этом файле плюс живой `GET /event`, который читает SSE, а не запрашивает (его читает `core/opencode/sse.py`) |
| `core/opencode/models.py` | проверка `model id` по `GET /config/providers` (ворота C1) |
| `core/opencode/session_store.py` | одна сессия на пользователя плюс сборщик зависших |
| `core/opencode/sse.py` | чтение `GET /event`: соединение, границы таймаутов, фильтр по сессии |
| `core/opencode/sse_frames.py` | словарь событий opencode и разбор кадра SSE (`OpencodeEvent`, `turn_is_complete`) |
| `core/opencode/transport.py` | соединение с `opencode serve`: адрес, basic-auth, `?directory=` (C6), тело хода, non-2xx как исключение |
| `core/opencode/wire.py` | типы и разбор ответов opencode, иерархия `OpencodeError` |
| `core/pending_permission.py` | неотвеченный запрос как значение: `pending_actions` (`kind`, `as_record`/`from_record`) |
| `core/permissions.py` | брокер `permission.asked`: вопрос, ответ, отказ по таймауту |
| `core/permission_words.py` | шесть фраз, которые читает пользователь, и обрезка заголовка для лога |
| `core/policies.py` | риск команды и разбор ответа «да/нет» |
| `core/render.py` | ответ Алисе: `text`/`tts`, обрезка 1024, чистка markdown |
| `core/routing.py` | сентинел эскалации, четыре guard'а против его озвучивания человеку и развёртка, вычищающая его из сессии |
| `core/session_collector.py` | эскалация: отдать ход агенту и вооружить сборщик ответа в Telegram |
| `core/session_route.py` | ход в сессии opencode: C8, дедлайн, сентинел, `HybridWiring` |
| `core/tools/arxiv_tool.py` | поиск статей на arxiv и сборка сводки |
| `core/tools/base.py` | `ToolContext` и `ToolResult` |
| `core/tools/laptop_tool.py` | заряд, память, аптайм, запуск приложений |
| `core/tools/registry.py` | реестр инструментов для function calling |
| `core/tools/shell_tool.py` | команда через риск-гейт `core.policies` |
| `core/tools/telegram_tool.py` | отправка в Telegram |
| `core/tools/tg_tool.py` | инструмент `tg_send` для function calling |
