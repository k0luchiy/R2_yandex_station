# 11 — Контракт opencode-сервера, измеренный на этой машине

**Источник истины для todos 6, 8, 9 и 14.** Не пересказ документации: всё ниже
измерено против живого `opencode serve` v1.18.32 на `127.0.0.1:4598`.

> **Про пути в этом документе.** Замеры сделаны на одной машине, и её домашний
> каталог — часть обстановки, а не обобщение. Там, где путь относится к решению
> (какой каталог конфига, какой `WorkingDirectory`), он записан через `~` или
> `/home/<user>`, и факт от этого не меняется. Там, где путь — часть **сырого
> ответа сервера**, он оставлен как есть: переписать его значило бы переписать
> то, что сервер прислал. Проверить воспроизводимость замеров — отдельная задача,
> и этот документ её не решает.

| | |
|---|---|
| Бинарь | `~/.opencode/bin/opencode` (1.18.32, первый в `PATH`) |
| Health | `{"healthy":true,"version":"1.18.32"}` |
| Конфиг сервера | `OPENCODE_CONFIG_DIR=/tmp/r2d2-qa/spike-config` (отдельный каталог) |
| cwd сервера | `/tmp/r2d2-qa/workspace` |
| Harness | `scripts/spike/probe_opencode.py` |
| Сырые данные | `/tmp/r2d2-qa/spike.json` |
| Провал-харнисс | `/tmp/r2d2-qa/spike-fail.log` |

Команда, которой всё измерено:

```bash
.venv/bin/python scripts/spike/probe_opencode.py \
  --base-url http://127.0.0.1:4598 --out /tmp/r2d2-qa/spike.json
```

Каждый вызов имеет жёсткий таймаут (60 с для измерений, 20 с для остального), поэтому
зависший сервер не может заблокировать замер. При недоступном сервере пробник печатает
`opencode unreachable at http://127.0.0.1:4598` и выходит с кодом 2, **не создавая файл
с нулями** — это и есть проверка «падает громко».

---

## Сводная таблица

| # | Вопрос | Наблюдение | Решение |
|---|---|---|---|
| U1 | Изолирует ли `OPENCODE_CONFIG_DIR` агентов и `permission`, не ломая Zen-ключ? | **Частично.** Агенты и правила из скретч-конфига применяются, но поверх них по-прежнему видны 11 агентов и 15 провайдеров глобального конфига. Zen-ключ работает. | `ADOPTED:` строка U1 |
| U2 | `system` в теле сообщения — per-message или session-persistent? | **Per-message.** Каждое user-сообщение хранит своё поле `system`. | `ADOPTED:` строка U2 |
| U3 | Какую форму принимает параметр `tools`? | `Record<string, boolean>` — объект «имя инструмента → включён». | `ADOPTED:` строка U3 |
| U4 | Точные строки SSE `event.type` для permission и завершения хода? | `permission.asked` / `permission.replied`; **события «конец хода» не существует** — ход закрывает `session.idle`. **Имя приходит в теле JSON, полем `"type"`: `event:`-строки в потоке нет (1090 `data:` и 0 `event:` за 30 мин).** | `ADOPTED:` строка U4; коррекция D11 в разделе U4 |
| U5 | Может ли `POST /session` задать рабочий каталог? | **Да — query-параметром `?directory=`, не телом.** | `ADOPTED:` строка U5 |
| U6 | Реальная задержка `POST /session/:id/message`? | `space-bunny-free`: **p50 1.667 с**, p95 2.247 с. `muse-spark-1.3-contributor-free` **недоступен** (403). | `ADOPTED:` строка U6 |
| U7 | Чем на самом деле ограничивается инструмент агента — `permission` или чем-то ещё? | **Видимостью, и только `deny` её отнимает.** `ask` не спрашивает ничего для инструмента, который сам не спрашивает. | `ADOPTED:` строка U7 |
| U8 | Что opencode пишет в общую на человека сессию, когда отказывает в вызове инструмента? | **Часть `tool` с `state.status: "error"`, а `state.error` перечисляет действующие правила разрешений — матрицу того агента, которому отказали. Текстовой части в таком сообщении нет вовсе. Тот же `status: "error"` бывает и у отказа самого инструмента (`Transport error`), поэтому различать их надо по началу `state.error`.** | `ADOPTED (пересмотрено):` строка U8 |

---

## U1 — изоляция конфига и живость Zen-ключа

**Вопрос.** Изолирует ли `OPENCODE_CONFIG_DIR=<scratch>` определения агентов и правила
`permission`, не сломав Zen-учётку (она лежит в `~/.local/share/opencode/auth.json`,
вне каталога конфига)?

**Наблюдение.** Скретч-конфиг задал `permission: {"*":"deny","websearch":"allow"}` и
четыре агента (`spike-deny`, `spike-ask`, `spike-voice`, `spike-bashonly`).

Агенты скретча видны, но **глобальный конфиг пользователя тоже загружен**:

```json
"scratch_agents_visible": ["spike-ask", "spike-bashonly", "spike-deny", "spike-voice"],
"foreign_agent_count": 11,
"foreign_agents_still_visible": ["Atlas - Plan Executor", "Metis - Plan Consultant",
  "Momus - Plan Critic", "Prometheus - Plan Builder", "Sisyphus - ultraworker",
  "Sisyphus-Junior", "librarian", "multimodal-looker", "oracle", "prd-maker",
  "task-clarifier"],
"global_providers_visible": ["cerebras", "cline-pass", "google", "googleai", "llm7",
  "lmstudio", "minimax", "nvidia", "openai", "opencode-go", "openrouter", "orcarouter",
  "pollinations", "tokenrouter", "zai"]
```

`prd-maker` и `task-clarifier` определены в `~/.config/opencode/opencode.json`
(верхний уровень, ключ `agent`), остальные девять — из плагина
`oh-my-openagent@latest`, объявленного в `~/.config/opencode/opencode.jsonc`.
То есть `OPENCODE_CONFIG_DIR` **складывается** с глобальным конфигом, а не заменяет его.

Агентские правила при этом имеют приоритет: в `GET /agent` правила скретч-конфига
стоят **последними** в списке, а opencode применяет «после совпадения побеждает
последнее правило». Доказано поведением, а не только чтением списка:

```json
"spike_deny_bash_effective_rule": {"permission": "*", "pattern": "*", "action": "deny"},
"deny_turn_status": 200, "deny_turn_mode": "spike-deny", "deny_turn_text": "NO_BASH_TOOL",
"deny_tool_parts": 0, "deny_marker_executed": false
```

Агент, которому запрещено всё, ответил `NO_BASH_TOOL` и не выполнил ни одного
инструмента. Zen-ключ при этом жив:

```json
"deny_turn_model": "opencode/space-bunny-free", "deny_turn_finish": "stop",
"deny_turn_input_tokens": 170, "credential_works": true
```

> `credential_works` в пробнике — это `finish == "stop"` и `input > 0`, а не «тело не
> пустое»: ответ 200 с пустым текстом здесь означает провал, а не успех (см. U6).

**ADOPTED:** `OPENCODE_CONFIG_DIR=~/.r2d2/opencode` использовать как
единственный слой конфига, **но считать его слоем поверх глобального, а не изоляцией**:
11 чужих агентов (`Sisyphus`, `oracle`, `prd-maker`, …) и 15 чужих провайдеров останутся
видны всегда, поэтому R2D2 обязан (а) адресовать агентов по точному имени
`r2d2-voice` / `r2d2-agent`, никогда не по «последнему определению», и (б) держать
собственные правила `permission` максимально строгими (`"*": "deny"` + белый список),
потому что глобальные правила снизу не перекрываются. Zen-учётку скретч-конфиг не
ломает — она лежит вне `OPENCODE_CONFIG_DIR` и подхватывается автоматически. Это и есть
запланированный fallback U1 («принять, что глобальные провайдеры видны только на чтение»),
подтверждённый замером, а не выбранный по незнанию.

---

## U2 — параметр `system`: per-message или session-persistent

**Вопрос.** `system` в теле `POST /session/:id/message` действует на одно сообщение или
на всю сессию?

**Наблюдение.** Два сообщения с разными `system`, затем `GET /session/:id/message`:

```json
"sent_systems": ["Begin every reply with the tag [A].", "Begin every reply with the tag [B]."],
"first_reply":  "[A] Hello! How can I help?",
"second_reply": "[B] Hello again!",
"first_reply_used_own_system": true,
"second_reply_used_own_system": true,
"second_reply_reused_first_system": false,
"stored_user_systems": ["Begin every reply with the tag [A].",
                        "Begin every reply with the tag [B]."]
```

Второй ход исполнил именно второй `system`, и оба значения независимо сохранены в
user-сообщениях.

⚠️ **Форма ответа `GET /session/:id/message` — не плоский список сообщений**, а
`[{"info": {...}, "parts": [...]}]`. Роль, `system`, `agent` и `model` лежат внутри
`info`. Чтение `m["role"]` верхнего уровня молча даёт `None` — это уже укусило пробник
на первой попытке.

⚠️ Первая версия теста спрашивала «в какой системе ты работаешь?» и была
нестабильна: модель на один слово повторяла токен из собственной истории, из-за чего
второй прогон дал противоположный вывод. Поэтому проверка построена на
следовании инструкции (тег-префикс), а не на самоотчёте модели.

**ADOPTED:** `system` — **per-message**, можно использовать. Но R2D2 всё равно
положит системный промпт в **определения агентов** (`r2d2-voice` / `r2d2-agent`), а
не в поле `system` каждого запроса: так промпт версионируется в конфиге рядом с
`permission` того же агента, и его нельзя случайно затереть опечаткой в коде
вызова. Плановый fallback U2 («только определения агентов, никогда параметр
`system`») сохраняется как основной путь, а per-message `system` остаётся
допустимым точечным перекрытием. Коллектор ответов обязан читать
`GET /session/:id/message` как `[{info, parts}]`.

---

## U3 — форма параметра `tools`

**Вопрос.** Какую форму принимает `tools` в теле сообщения?

**Наблюдение.** Из `GET /doc` на живом сервере, дословно:

```json
"tools": {
  "type": "object",
  "additionalProperties": { "type": "boolean" }
}
```

То есть **`Record<string, boolean>`** — плоский объект «имя инструмента → включён»,
а не список. Проверено и в рантайме: `"tools": ["bash"]` отклоняется валидатором.

```json
"tools_as_list_wrong_type": {"status": 400,
  "body": "{\"name\":\"BadRequest\",\"data\":{\"message\":\"Expected object | null, got [\\\"bash\\\"]\\n  at [\\\"tools\\\"]\",\"kind\":\"Payload\"}}"}
```

Поведенческий A/B на агенте `spike-bashonly` (`permission: {"*":"deny","bash":"ask"}`) —
единственном агенте, у которого достижим ровно один инструмент:

| Тело сообщения | `permission.asked` | Инструмент |
|---|---|---|
| ключ `tools` отсутствует | **да** | баш запрашивает разрешение |
| `"tools": {"bash": false}` | **нет** | баш недоступен |

```json
"tools_key_changes_behaviour": true,
"case_no_tools_key":      {"tools_sent": null,               "raised_permission_asked": true},
"case_tools_bash_false":  {"tools_sent": {"bash": false},    "raised_permission_asked": false}
```

⚠️ A/B обязан идти через `prompt_async`, а не через блокирующий `POST /message`:
при `bash: ask` блокирующий вызов **никогда не возвращается** — пробник на этом
вис до таймаута. Плюс: на сервере присутствует окружение инструментов харнеса
(`interactive_bash` и др.), поэтому наивный A/B даёт ложноположительный маркер:
модель обходит `bash` другим инструментом. Agent-first конфигурация здесь обязательна.

**ADOPTED:** `tools` принимает `Record<string, boolean>` (`{"bash": false}`), и ключ
действительно перекрывает `permission` агента на время хода. R2D2 будет полагаться на
**агентные** `tools`/`permission` (todo 10), а не на per-message `tools`: у
голосового агента инструментов нет вовсе, и исключать их поштучно в каждом вызове —
лишняя точка отказа. Плановый fallback U3 («только агентные `tools`/`permission`»)
подтверждён замером. Побочный факт для todo 10: в `GET /agent` поле `tools`
возвращается как `null`, а объявление `tools: {"bash": false}` в конфиге нормализуется
в правила `permission` (`bash → deny`) — проверять надо `permission`, не `tools`.

---

## U4 — точные строки SSE-событий

**Вопрос.** Какие точные строки `event.type` приходят при запросе разрешения и при
завершении хода?

**Наблюдение.** `GET /event` — **глобальный** поток всех сессий; фильтровать нужно по
`properties.sessionID`. Первый кадр — `server.connected`. Полный набор, реально
наблюдённый за один ход с разрешением (в порядке первого появления):

```json
"distinct_types_in_order": ["server.connected", "session.updated", "message.updated",
  "message.part.updated", "session.status", "session.diff", "message.part.delta",
  "permission.asked", "permission.replied", "session.idle"],
"counts": {"server.connected": 1, "session.updated": 5, "message.updated": 10,
  "message.part.updated": 14, "session.status": 6, "session.diff": 3,
  "message.part.delta": 4, "permission.asked": 1, "permission.replied": 1,
  "session.idle": 1}
```

Дополнительно в других прогонах наблюдались `server.heartbeat`, `session.created`,
`tui.toast.show`, `catalog.updated`, `integration.updated`, `plugin.added`,
`reference.updated` — то есть поток несёт и служебные, и чужие сессионные события.

### U4-коррекция (D11) — поля `event:` на проводе нет

Ранее в коде по U4 было записано «opencode всегда присылает `event:`-строку».
**Провод это опровергает.** Отдельный замер `GET /event` на этой же сборке 1.18.32,
30 минут на сервере владельца, построчный счёт полей:

```
1090  data:
1091  <blank>
   0  event:      ← строки `event:` в потоке нет вообще
 109  кадров с "type":"server.heartbeat"
```

Короткое подтверждение на скретч-сервере того же бинаря — `qa/d11-wire-tap.py`,
вывод в `qa/d11-wire-tap.out`:

```
lines starting with 'data:':      44
lines starting with 'event:':      0
lines starting with ':' (comment): 0
bodies by their own "type":      server.connected, permission.asked,
                                 permission.replied, session.idle,
                                 message.part.delta, server.heartbeat, session.updated,
                                 message.updated, message.part.updated, session.status,
                                 session.diff, session.created
decode_frame() types, as shipped: {'message': 44}
```

Итог, который и есть суть D11: **имя события приходит в теле JSON, верхнеуровневым
полем `"type"`**, а не в SSE-поле `event:`. Отсюда и `server.heartbeat`: его тело —
`{"id":"evt_...","type":"server.heartbeat","properties":{}}`, пустой `properties`
объект, без `sessionID`, поэтому фильтр сессии отбрасывает его на DEBUG и оператор
его не видит.

Ниже, в разделе «Событие запроса разрешения», все тела приведены дословно — и в них
`"type"` стоит вторым ключом, после `"id"`. Это и есть настоящая форма кадра:

```json
{"id":"evt_0dc8e0d96001ENniA8TFQh84Kv","type":"permission.asked","properties":
  {"id":"per_0dc8e0d950015YF13XN1QQAEcz","sessionID":"ses_f237204c2ffew0YlIQMRoGkbrk",
   "permission":"bash","patterns":["echo R2D2D11TAP"],
   "metadata":{"command":"echo R2D2D11TAP"},"always":["echo *"],
   "tool":{"messageID":"msg_0dc8dfc20001Ay94pxsy3GeYG4",
           "callID":"call_function_rds009nllcpm_1"}}}
```

**Почему это не было видно в тестах.** Юнит-тесты `tests/test_sse.py` кормили
декодер рукописными кадрами, у которых `event:`-строка есть, — то есть формой,
которую сервер не присылает никогда. 906 зелёных тестов ничего не говорили о
проводе. Настоящие кадры теперь зафиксированы дословно в
`tests/test_sse_wire_frame.py`, и девять из его тестов падают на прежнем декодере.

**Правило разбора, зафиксированное кодом** (`core/opencode/sse_frames.py`):

1. поле `event:` кадра, если оно есть и непустое, — выигрывает: это единственное
   место, где спецификация SSE позволяет серверу назвать событие, и сервер,
   который назвал, не додумывается;
2. иначе `"type"` тела — то, что делает opencode 1.18.32, и единственный путь
   настоящего кадра;
3. иначе `SSE_DEFAULT_EVENT` (`"message"`), чтобы кадр без имени декодировался, а
   не ронял поток.

Кадр, назвавший себя в обоих местах по-разному, берёт `event:` и пишет WARNING:
расхождения в этом протоколе не бывает, а молчаливый выбор одного из двух имён —
это ровно тот способ, которым живой на вид поток перестаёт что-либо раздавать.


**Событие запроса разрешения, дословно:**

```json
{"id": "evt_0d99aed42001vMs6TtgkoCqJxX",
 "type": "permission.asked",
 "properties": {
   "id": "per_0d99aed41001iOUiwXDd6vx2NP",
   "sessionID": "ses_f266522f4ffee31XNquJ1D3qjC",
   "permission": "bash",
   "patterns": ["echo R2D2SPIKERUN3K"],
   "metadata": {"command": "echo R2D2SPIKERUN3K"},
   "always": ["echo *"],
   "tool": {"messageID": "msg_0d99ae16a0015kUjEPJoOW15k8",
            "callID": "call_function_98vuii2wlh91_1"}}}
```

**Событие ответа на разрешение, дословно:**

```json
{"id": "evt_0d99aedc9001k68v7FEQ2eqnlp",
 "type": "permission.replied",
 "properties": {"sessionID": "ses_f266522f4ffee31XNquJ1D3qjC",
                "requestID": "per_0d99aed41001iOUiwXDd6vx2NP", "reply": "once"}}
```

Ответ на разрешение: `POST /session/:id/permissions/per_...` с `{"response":"once"}`
возвращает **HTTP 200 с телом `true`** (не 204 и не пустое тело), после чего инструмент
действительно выполняется (`tool_marker_executed_after_once: true`).

Ключевое отрицательное наблюдение: **события «ход завершён» не существует**. Нет ни
`turn.completed`, ни `message.completed`. `message.updated` приходит 10 раз за ход и
меняет `info` по мере сборки, текст стримится через `message.part.delta`, а закрывает
ход единственное событие:

```json
"session.idle"  ->  {"properties": {"sessionID": "ses_..."}}
```

Хвост потока после `permission.asked` (28 событий) заканчивается
`message.part.delta → message.part.updated → message.updated → session.idle`.

Дополнительно из `GET /doc`: для каждого события есть и V2-вариант строки —
`permission.v2.asked` / `permission.v2.replied` и семейство `session.next.*`
(`session.next.step.started`, `session.next.text.delta`, `session.next.tool.called`, …).
На этой сборке замечены только строки без `.v2`/`.next`; полагаться на V2 нельзя.

**ADOPTED:** Использовать **живой SSE (`EVENT_MODE = "sse"`), а не fallback F1
(polling) и не F2 ( deny-режим)** — поток отдаёт всё нужное. Константы для todo 7:

| Константа | Значение |
|---|---|
| `PERMISSION_ASKED` | `permission.asked` |
| `PERMISSION_REPLIED` | `permission.replied` |
| `TURN_COMPLETE` | `session.idle` |
| `TEXT_DELTA` | `message.part.delta` |
| `CONNECTED` | `server.connected` |

Обязательные правила для todo 7 и 14: (1) `GET /event` глобальный — фильтровать по
`properties.sessionID`, иначе R2D2 ответит на чужой запрос разрешения; (2) `TURN_COMPLETE`
определяется как `session.idle` **плюс** отсутствие `permission.asked` без парного
`permission.replied`, инаше ход с ожиданием подтверждения будет объявлен завершённым
premature; (3) никогда не отправлять `response: "always"` — доступен `always`-список
в `properties.always` (`["echo *"]`), и один `always` навсегда разрешает эту маску
команд; (4) V2-строки (`permission.v2.asked`, `session.next.*`) не использовать.

---

## U5 — рабочий каталог сессии

**Вопрос.** Может ли `POST /session` задать рабочий каталог сессии, или он фиксирован
cwd сервера?

**Наблюдение.** Три запроса к одному серверу с cwd `/tmp/r2d2-qa/workspace`:

| Запрос | Ответ |
|---|---|
| `POST /session` без параметра | `directory: /tmp/r2d2-qa/workspace` (= cwd сервера) |
| `POST /session?directory=/tmp/r2d2-qa/other` | `directory: /tmp/r2d2-qa/other` ✅ |
| `POST /session` с ключом `directory` в **теле** | `200`, `directory: /tmp/r2d2-qa/workspace` ❌ ключ проигнорирован |
| `POST /session?directory=/nonexistent-zzz` | `200`, `directory: /nonexistent-zzz` — **без валидации** |

```json
"directory_query_honoured": true,
"directory_body_key": {"status": 200, "sent": "/tmp/r2d2-qa/other",
                       "directory": "/tmp/r2d2-qa/workspace"},
"no_directory_param": {"directory": "/tmp/r2d2-qa/workspace", "projectID": "global"}
```

Параметр `directory` — это query-параметр, и он есть почти у всех маршрутов
(`/session`, `/session/:id`, `/session/:id/message`, `/event`, `/agent`,
`/session/status`, `/project/current`). Он же фильтрует список сессий:
`GET /session?directory=/tmp/r2d2-qa/other` вернул только сессии этого каталога.

`GET /project/current` **не зависит** от параметра и не пригоден для проверки:

```json
"project_current_without_directory_param": {"id": "global", "worktree": "/", "sandboxes": []}
"project_current_with_directory_param":    {"id": "global", "worktree": "/", "sandboxes": []}
```

⚠️ Несуществующий каталог принимается без ошибки, поэтому опечатка в `?directory=`
создаст сессию, чьи файловые инструменты работают с несуществующим корнем.
`POST /session` в теле имеет `additionalProperties: false` по спецификации, но
неизвестные ключи **не отвергаются** (см. «Ошибки» ниже) — доверять валидации схемы
нельзя.

**ADOPTED:** Каталог сессии задаётся **query-параметром `?directory=<абс. путь>` на
`POST /session`**, а не телом; R2D2 обязан передавать его на `POST /session`,
`POST /session/:id/message` и `GET /event`. Это лучше планового fallback U5
(«поднять сервер с cwd = рабочий каталог»): каталог задаётся на сессию, а не на
процесс. Оба делаются — сервер в systemd всё равно запускается с
`WorkingDirectory=/home/<user>/r2d2-workspace` (todo 12), потому что `?directory=`
не валидируется, и R2D2 обязан сам проверять, что путь существует, до создания
сессии. Поле `directory` в теле не использовать.

---

## U6 — реальная задержка

**Вопрос.** Реальная задержка `POST /session/:id/message` с `space-bunny-free` (10
выборок) и `muse-spark-1.3-contributor-free` (3 выборки)?

**Наблюдение — `opencode/space-bunny-free`, 10 выборок, одна сессия (свежий сервер,
без прогрева):**

| n | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 |
|---|---|---|---|---|---|---|---|---|---|---|
| с | 1.592 | 1.667 | 1.781 | 2.247 | 1.886 | 1.719 | 1.657 | 1.604 | 1.667 | 1.505 |

```json
"latency_space_bunny_free": {"n": 10, "min": 1.505, "p50": 1.667, "p95": 2.247,
                             "max": 2.247, "failed": 0}
```

Первый ход на холодном кэше — отдельная величина: **15.5 с** тела ответа
(`cache.write` 1914 токенов, `input` 90) и **18.6 с** на стену, из них ~3 с — запуск
до первого токена. Измерено в разведочном прогоне до сбора `spike.json`, на том же
сервере и в той же сессии работы. Это первая реплика новой сессии, и она не
укладывается ни в какой бюджет Алисы.

**Наблюдение — `muse-spark-1.3-contributor-free`: недоступен.**
Модель присутствует в `GET /config/providers` со `status: "active"`, но каждый вызов
возвращает **HTTP 200 с `info.error`** — то есть статус успеха при провале:

```json
"error": {"name": "APIError",
          "data": {"message": "OpenCode's free tier can only be used from within OpenCode",
                   "statusCode": 403, "isRetryable": false}}
```

Свип всех `*-free` моделей каталога `opencode` плюс платные кандидаты:

| Модель | с | Годна | Ответ Zen |
|---|---|---|---|
| `space-bunny-free` | 4.353 | **да** | — |
| `muse-spark-1.3-contributor-free` | 1.079 | нет | 403 free tier can only be used from within OpenCode |
| `ling-3.0-flash-fin-free` | 0.673 | нет | 403 free tier can only be used from within OpenCode |
| `mimo-v2.6-flash-free` | 0.861 | нет | 403 free tier can only be used from within OpenCode |
| `nemotron-3-ultra-free` | 0.905 | нет | 403 free tier can only be used from within OpenCode |
| `nemotron-3.5-lightning-free` | 0.824 | нет | 403 free tier can only be used from within OpenCode |
| `muse-spark-1.3` | 60.044 | нет | не ответил вовсе, обрыв на таймауте 60 с |
| `gpt-5.4-nano` | 16.39 | нет | 402 Upstream request failed: Insufficient account funds |
| `qwen3.8-flash` | 0.972 | нет | 402 Insufficient account funds |
| `gemini-3.5-flash-lite` | 1.036 | нет | 402 Insufficient account funds |

```json
"usable_models": ["space-bunny-free"],
"latency_measured_alternative": null,
"latency_muse_spark_1_3_contributor_free": {"n": 0, "p50": null, "p95": null,
  "requested_model_usable": false,
  "note": "no free alternative is usable from `opencode serve`; see model_sweep"}
```

**Измерение альтернативы выполнить не удалось: годной альтернативы нет.** Единственная
рабочая модель — `space-bunny-free`. Все прочие `*-free` отклоняются Zen-ом на 403, все
платные — на 402 из-за пустого баланса.

⚠️ **Ловушка «успешного» 200:** `POST /session/:id/message` возвращает `200` даже когда
модель не ответила. Признак отказа — `info.error` в теле плюс нулевые токены и пустой
`parts`. R2D2 обязан проверять содержимое ответа, а не статус.

**ADOPTED:** `R2D2_FAST_DEADLINE` = **3.2 с** (измеренные p50 1.667 с, p95 2.247 с дают
запас; плановый fallback предписывал `min(3.6, 4.5 − 1.2)` = 3.3 с — 3.2 с совпадает в
пределах шума и оставлен как в плане). Ключевое изменение против плана:
**`task_model` и `summarize_model` НЕ могут быть `opencode/muse-spark-1.3-contributor-free`**
— эта модель не отвечает с `opencode serve`. Единственный годный идентификатор —
`opencode/space-bunny-free`, и он же используется для голосового пути, для задач и для
суммаризации. Измеренной альтернативы для сильной модели нет, поэтому пункт «3 выборки
по muse-spark» остаётся **непокрытым**: если нужна отдельная сильная модель, требуется
пополнение Zen-баланса, после чего свип надо перезапустить. До пополнения агентный путь
идёт на `space-bunny-free` и полагается на суммаризацию сервера, а не на отдельную
модель. Первая реплика новой сессии (15.5 с тела / 18.6 с на стену на холодном кэше)
обязана выполняться асинхронно с ответом «отвечу в телеграм».

---

## U7 — чем на самом деле ограничивается инструмент

**Вопрос.** `OPENCODE_CONFIG_DIR` по U1 остаётся слоем поверх глобального конфига,
значит у агента есть чужие инструменты: плагин владельца и его MCP-серверы. Может
ли `permission` агента их отнять — и если да, то какой именно приём?

**Где смотрели.** Три независимых источника на этой машине, opencode v1.18.32.

**1. Код.** В бандле три функции, и между ними нет ничего общего с «матрицей
разрешений» из этого документа:

- `Permission.evaluate(permission, pattern, ...rulesets)` — `findLast` по
  совпавшему правилу, по умолчанию `ask`. Это про **внутристрочные** проверки.
- `Permission.disabled(toolNames, ruleset)` — фильтр видимости. Инструмент
  отбирается, **только** если последнее совпавшее с его корзиной правило имеет
  `pattern` ровно `"*"` и `action` `"deny"`.
- `Permission.visibleTools(tools, ruleset)` — то, что реально уходит в модель:
  `jd(e) = filter(tools, (tool) => user.tools[tool] !== false && !disabled.has(tool))`.

Корзина — **не имя инструмента**. `disabled` отображает его через таблицу:

| Инструмент | Корзина |
|---|---|
| `edit`, `write`, `apply_patch` | `edit` |
| `list_mcp_resources`, `list_mcp_resource_templates`, `read_mcp_resource` | `read` |
| всё остальное, включая **любой инструмент плагина и любой MCP-инструмент** | **его собственное имя** |

Сопоставление шаблонов — обычный глоб: `*` → `.*`, `?` → `.`, якоря `^…$`, флаг
`dotall`; хвост `" *"` делается необязательным.

**2. Считает ли opencode это сам.** `opencode debug agent <имя>` печатает
`{tool: bool}`, посчитанный ровно этой функцией. Скретч-конфиг R2D2, оба агента:

| | `r2d2-voice` (`*` → `deny`) | `r2d2-agent` (`*` → `ask`) |
|---|---|---|
| `call_omo_agent`, `interactive_bash`, `look_at`, `skill_mcp` | `false` | **`true`** |
| `session_list`, `session_read`, `session_search`, `session_info` | `false` | **`true`** |
| `background_output`, `background_cancel`, `todowrite` | `false` | **`true`** |
| `bash`, `read`, `glob`, `grep` | `true` | `true` |
| `edit`, `write`, `task`, `webfetch`, `websearch`, `skill`, `lsp`, `question` | `false` | `true` |

**3. Спрашивает ли инструмент разрешение.** `McpCatalog.convertTool` собирает
`execute`, который вызывает `client.callTool` — и больше ничего. `ctx.ask` в нём
нет. Единственное место, где MCP-инструмент задаёт вопрос, — `CodeMode`, то есть
режим, которого у R2D2 нет.

**Наблюдение.** Три вывода, и первый из них опровергает исходную формулировку
дефекта:

1. **`*` накрывает плагин и MCP.** Живой отчёт D7 утверждал, что `*` покрывает
   «встроенные имена, но не плагинные и MCP-шные». Счётчик это опровергает: у
   `r2d2-voice` каждый чужой инструмент `false`. Причина, по которой отчёт
   увидел вызовы под голосовым агентом, — другая: те вызовы принадлежали ходу
   `r2d2-agent`, и вTranscript этого самого прогона они так и помечены.
2. **`ask` не enforceable.** Инструмент, который не вызывает `ctx.ask`, не
   останавливается ничем: у `r2d2-agent` все 11 чужих инструментов `true`, и
   `interactive_bash` (это shell) в том числе. Единственный приём, который
   работает, — отнять видимость, а она отнимается только `deny`.
3. **Корзина `read` не отделима.** Три MCP-инструмента живут в корзине `read`, а
   правило для корзины целиком задаётся её последним правилом. Пока `read`
   разрешён, а он разрешён обоим агентам осознанно, `list_mcp_resources` виден
   и запретить его иначе, чем вместе с `read`, нельзя.

**ADOPTED:** для чужой поверхности объявлять `deny`, а не `ask`, и объявлять его
**по форме имени**, потому что перечислить чужие инструменты нельзя: MCP-инструмент
opencode называет `<сервер>_<инструмент>`, и правило `"*_*": "deny"` накрывает эту
поверхность целиком вместе с плагином владельца. Оно ставится **вторым** ключом,
сразу после `"*"`, потому что `*` обязан оставаться первым (U1), а объявленные
ключи — решаться своими собственными правилами. Инструмент без `_` в имени
(измеренно — `todowrite`) получает правило от себя. Всё это проверяется офлайн в
`tests/test_foreign_tool_surface.py`, который переписывает `disabled` и
`Wildcard.match` как есть, и подтверждено живым `opencode debug agent`.
Ограничение третьего пункта принимается как свойство opencode, а не как недосмотр
R2D2, и проговорено в [09-security.md](09-security.md) §8.6.

**Как получено.** `opencode debug agent r2d2-voice` и `r2d2-agent` на скретч-конфиге
`/tmp/r2d2-qa/d67/config` (порт 4612, throwaway), плюс `strings` по бинарнику
`~/.opencode/bin/opencode`. `~/.config/opencode/`, `auth.json`,
`opencode.db` и чужие сессии не читались и не менялись; в вывод `GET /config`
попадает ключ MCP-сервера, поэтому этот маршрут в доказательство не включён.

---

## U8 — что opencode пишет в общую сессию при отказе в инструменте

**Вопрос.** Сессия на человека одна и ею пользуются оба агента, а матрицы
разрешений у них разные (U1, U7). Когда opencode отказывает агенту в вызове
инструмента, он пишет причину отказа в эту общую историю — и второй агент её
читает. Что именно лежит в сообщении, в какой части оно лежит и что из этого
может вычистить R2D2, — от этого зависит выбор между тремя вариантами.

**Где смотрели.** Живой `opencode serve` v1.18.32 на порту **4614** (throwaway),
`OPENCODE_CONFIG_DIR=/tmp/r2d2-d9/opencode` с двумя агентами R2D2 и изолированные
`XDG_CONFIG_HOME` / `XDG_DATA_HOME` / `XDG_CACHE_HOME` / `XDG_STATE_HOME`, рабочий
каталог `/tmp/r2d2-d9/workspace`. `~/.r2d2/`, `~/.config/opencode/`, порт 4599 и
чужие сессии не читались и не менялись.

**Наблюдение 1 — отказ это `tool`-часть, а не текст.** Голосовой агент вызвал
`bash` с командой вне своего белого списка, opencode отказал и сохранил сообщение
`msg_0dc6c234a001iSnbaBxS08nCF1` дословно так. Внутри `error` — сырой ответ
сервера, поэтому он сохранён побайтно, вместе с абсолютными путями
измерительной машины в перечислении правил: это ровно то, что прислал opencode,
и переписывать пути здесь значило бы подменить наблюдение. Сам вывод — тоже
замер этой машины, о чём сказано в преамбуле.

```json
{
  "id": "prt_0dc6c39b8001HZK2wNIzb34H5c",
  "sessionID": "ses_f2393e965ffeFgLClrZ3rOgMEQ",
  "messageID": "msg_0dc6c234a001iSnbaBxS08nCF1",
  "type": "tool",
  "callID": "call_function_3rkcv34hpeet_1",
  "tool": "bash",
  "state": {
    "status": "error",
    "input": {
      "command": "ls -la /tmp/r2d2-d9/workspace"
    },
    "error": "The user has specified a rule which prevents you from using this specific tool call. Here are some of the relevant rules [{\"permission\":\"*\",\"action\":\"allow\",\"pattern\":\"*\"},{\"permission\":\"*\",\"action\":\"deny\",\"pattern\":\"*\"},{\"permission\":\"*\",\"action\":\"deny\",\"pattern\":\"*\"},{\"permission\":\"bash\",\"pattern\":\"*\",\"action\":\"deny\"},{\"permission\":\"bash\",\"pattern\":\"/home/koluchiy/.r2d2/r2d2_do.py *\",\"action\":\"allow\"},{\"permission\":\"bash\",\"pattern\":\"python3 /home/koluchiy/.r2d2/r2d2_do.py *\",\"action\":\"allow\"},{\"permission\":\"bash\",\"pattern\":\"/home/koluchiy/Documents/R2_yandex_station/.venv/bin/python /home/koluchiy/.r2d2/r2d2_do.py *\",\"action\":\"allow\"},{\"permission\":\"bash\",\"pattern\":\"upower *\",\"action\":\"allow\"},{\"permission\":\"bash\",\"pattern\":\"cat /sys/class/power_supply/*\",\"action\":\"allow\"},{\"permission\":\"bash\",\"pattern\":\"df *\",\"action\":\"allow\"},{\"permission\":\"bash\",\"pattern\":\"free *\",\"action\":\"allow\"},{\"permission\":\"bash\",\"pattern\":\"uname *\",\"action\":\"allow\"},{\"permission\":\"bash\",\"pattern\":\"hostname *\",\"action\":\"allow\"},{\"permission\":\"bash\",\"pattern\":\"ps *\",\"action\":\"allow\"},{\"permission\":\"bash\",\"pattern\":\"uptime\",\"action\":\"allow\"},{\"permission\":\"bash\",\"pattern\":\"date\",\"action\":\"allow\"},{\"permission\":\"bash\",\"pattern\":\"/home/koluchiy/.r2d2/r2d2_do.py shell *\",\"action\":\"deny\"},{\"permission\":\"bash\",\"pattern\":\"python3 /home/koluchiy/.r2d2/r2d2_do.py shell *\",\"action\":\"deny\"},{\"permission\":\"bash\",\"pattern\":\"/home/koluchiy/Documents/R2_yandex_station/.venv/bin/python /home/koluchiy/.r2d2/r2d2_do.py shell *\",\"action\":\"deny\"}]",
    "time": {
      "start": 1790404475369,
      "end": 1790404475610
    }
  }
}
```

Запрос при этом лежит в `state.input` (то есть **что именно было запрещено, R2D2
знает**), а перечисленные правила — в `state.error`. Части сообщения:
`step-start`, `reasoning`, `tool`, `step-finish`. **Ни одной части `type: "text"`
в сообщении нет.**

**Наблюдение 2 — перечисленные правила принадлежат отказанному агенту.** В дампе
есть `{"permission":"bash","pattern":"*","action":"deny"}` — это правило
`r2d2-voice`; у `r2d2-agent` тот же ключ `bash` начинается с `"*": "ask"`, и
`opencode debug agent r2d2-agent` показывает `bash: true`. `r2d2-agent` в этой же
сессии сразу после отказа вызвал `glob` четыре раза и получил результат каждого
раза, то есть **инструменты у него были и остались**. Поэтому вред дампа не в том,
что он кого-то действительно ограничивает: он в том, что читающий агент принимает
чужие правила за свои. Условие детерминировано (текст в общей истории остаётся
навсегда), а вред зависит от того, как модель его прочитает.

**Наблюдение 3 — `GET /session/:id/message` этого не показывает.** `MessageRecord`
(`core/opencode/wire.py`) собирает `text` **только** из частей `type: "text"`,
поэтому отказанный ход приходит к вызывающему с `text == ""`:

```json
{
  "id": "msg_0dc6c234a001iSnbaBxS08nCF1",
  "role": "assistant",
  "agent": "r2d2-voice",
  "modelID": "space-bunny-free",
  "providerID": "opencode"
}
```

**Наблюдение 4 — пустой текст не отличает отказ от успеха.** В той же сессии
четыре **успешных** вызова `glob` сохранены так же: `step-start`, `tool` со
`state.status: "completed"`, `step-finish`, и тоже без текстовой части. Значит
«ассистентское сообщение с пустым текстом» — это и отказ, и успешный ход с
инструментами, и удалять такие сообщения нельзя: вместе с отказом уйдёт и запись
обо всём, что сработало.

**Наблюдение 5 — отказ переживает всё, что есть у R2D2.** `routing.transcript_sweep`
удаляет сообщения с маркером шлюза и по определению не видит отказ (наблюдение 3);
`POST /session/:id/summarize` отвечает `true` и ничего не сжимает (замерено в
первом прогоне). Ни один механизм в этом репозитории отказ не убирает.

**Наблюдение 6 — `state.status == "error"` бывает и не у отказа.** В той же сессии
`webfetch` на `https://no-such-host.invalid/page` сохранён с
`state.status: "error"`, `state.input` — этот URL, а `state.error` — строка
`Transport error (GET https://no-such-host.invalid/page)`. То есть признак
`status == "error"` отделяет отказ от успеха, но **не** отказ enforcement от
отказа самого инструмента: различать их приходится по содержимому `state.error`.

**Наблюдение 7 — отказ воспроизводится дословно.** Повторный живой замер на
`127.0.0.1:4610` (throwaway `OPENCODE_CONFIG_DIR`, изолированные `XDG_*`, другое
рабочее дерево) дал `state.error`, **побайтово совпадающий** с записанным выше на
4614: та же фраза, тот же порядок правил, тот же `{"permission":"bash","pattern":
"*","action":"deny"}`. Проверяется офлайн в
`tests/test_opencode_client.py::test_the_captured_refusal_is_byte_identical_to_the_contract_u8_record`.
Фикстуры всех трёх сообщений — в `tests/fake_opencode.py`.

**ADOPTED (пересмотрено).** Первая редакция этого раздела гласила: отказ **не
вычищать**, а запретить его читать как своё правилом в промпте обоих агентов.
Наблюдения 3–5 это обосновывали, и живой прогон
[live-run-v3.md](../qa/live-run-v3.md) их подтвердил: правило было отдано
серверу, агент его получил, и вред всё равно проявился. Правило, значит, не
рычаг.

Сейчас принято: **отказ удаляется из общей сессии тем же свипом, что и маркер
эскалации.** Наблюдение 3 перестало быть препятствием — `MessageRecord` получил
поле `refused`, которое читается с `tool`-части. Ключ признака — **не** пустота
(наблюдение 4) и **не** один лишь `state.status` (наблюдение 6), а оба условия
вместе: `state.status == "error"` и `state.error`, открывающийся фразой opencode о
запрете. Направление отказа выбрано так, чтобы при переформулировке этой фразы
сервером отказы остались на месте (снова открытый D9), а не потерялись.

Правило в промпте обоих агентов **осталось** — не как фикс D9, а как покрытие
ходов, где свип не уходит: он запускается только на эскалации, а ход, породивший
отказ, часто заканчивается ответом пользователю и эскалацией не оборачивается
вовсе. Проверено офлайн в `tests/test_r2d2_opencode_config.py` и end-to-end на
живом прогоне [D9](../qa/live-run-v4.md): отказ удалён, а `r2d2-agent` в той же
сессии вызвал `webfetch` и получил `status: "completed"`.

---

## Ошибки: что выглядит как сбой

Проверено на живом сервере; `kind` в поле `data` — `Payload` для тела, `Params` для
query/пути.

| Что послали | HTTP | Тело |
|---|---|---|
| несуществующий `agent` (+ несуществующая модель) | **500** | `{"name":"UnknownError","data":{"message":"Unexpected server error. Check server logs for details.","ref":"err_XXXX"}}` |
| несуществующий `agent` | **500** | то же |
| несуществующая модель | **200** | **успех!** ответ отдан другой моделью (`opencode-go/muse-spark-1.3-contributor`), `parts` без текста |
| тело без `parts` | 400 | `{"name":"BadRequest","data":{"message":"Missing key\n  at [\"parts\"]","kind":"Payload"}}` |
| `"tools": ["bash"]` | 400 | `{"name":"BadRequest","data":{"message":"Expected object \| null, got [\"bash\"]\n  at [\"tools\"]","kind":"Payload"}}` |
| `response: "maybe"` | 400 | `{"name":"BadRequest","data":{"message":"Expected \"once\" \| \"always\" \| \"reject\", got \"maybe\"\n  at [\"response\"]","kind":"Payload"}}` |
| несуществующий `sessionID` | 404 | `{"name":"NotFoundError","data":{"message":"Session not found: ses_..."}}` |
| несуществующий `permissionID` | 404 | `{"_tag":"PermissionNotFoundError","requestID":"per_...","message":"Permission request not found: per_..."}` |
| `sessionID`, не проходящий `^ses` | 500 | `{"name":"UnknownError","data":{"message":"Unexpected server error...","ref":"err_XXXX"}}` |
| неизвестный ключ в теле | 200 | ключ **молча проигнорирован** (в одном прогоне — зависание на таймауте) |

Три вывода, обязательных для todos 6 и 9:

1. **Неизвестный `agent` — это 500, а не 400.** `OpencodeError` должен подниматься на
   не-2xx, но текст ошибки сервера бесполезен: реальная причина только в логах сервера
   по `ref`. Имя агента надо валидировать у себя через `GET /agent` при старте.
2. **Неизвестный `modelID` не является ошибкой** — opencode молча переключается на
   другую модель и отвечает 200. Идентификаторы моделей обязаны проверяться против
   `GET /config/providers` при старте, иначе «fast path» незаметно уедет на другую
   модель (в замере — на `opencode-go/muse-spark-1.3-contributor`).
3. **`remember` в теле ответа на разрешение принимается**, хотя в спецификации объявлен
   только `response`:

```json
"permission_body_with_remember_on_real_permission": {"status": 200, "body": "true", "accepted": true}
```

Проверено на **настоящем** запросе разрешения: с `{"response":"reject","remember":true}`
сервер отвечает `200 true`. Неизвестные ключи тела не отвергаются, поэтому на
`additionalProperties: false` полагаться нельзя. R2D2 всё равно **не отправляет
`remember`**: ответ `once` ничего не запоминает, а `always` запрещён политикой.

---

## Что проверено и НЕ сломано

`GET /global/health` → `{"healthy":true,"version":"1.18.32"}`;
`POST /session` → 200 c `id`, `slug`, `projectID`; `GET /session/:id`;
`POST /session/:id/message` (блокирующий, возвращает `{info, parts}`);
`POST /session/:id/prompt_async` → **204**; `GET /event` (SSE, первый кадр
`server.connected`); `GET /config/providers`; `GET /agent`; `GET /doc` (OpenAPI 3.1.0,
162 пути); `GET /project/current`.

Изменений в `~/.config/opencode/`, `~/.local/share/opencode/opencode.db` и
`~/.local/share/opencode/auth.json` не вносилось. Zen-учётка не читалась и не
печаталась. Скретч-сервер работал на порту 4598 и снимался в `trap`; после прогона
`pgrep -a opencode` показывает только 5 процессов пользователя.
