# D9 — воспроизведение и живая проверка фикса против настоящего `opencode serve`

Продолжение `qa/live-run-v3.md`, где D9 измерен как **не исправленный**: правило
в промпте обоих агентов было отдано серверу, агент его получил, и вред всё равно
проявился. Этот файл фиксирует две вещи: (1) воспроизведение дефекта и
дословную фикстуру, (2) живую end-to-end проверку фикса, которую предыдущая
попытка провести не смогла.

Ничего в `config/`, в матрицах разрешений и в чужих сессиях не менялось.

## Окружение

| | |
|---|---|
| opencode | `/home/koluchiy/.opencode/bin/opencode` v1.18.32, `serve --hostname 127.0.0.1 --port 4610` |
| конфиг | `OPENCODE_CONFIG_DIR=/tmp/r2d2-d9v/opencode`, там **побайтово** `config/opencode/r2d2.opencode.json` |
| изоляция | `XDG_CONFIG_HOME` / `XDG_DATA_HOME` / `XDG_CACHE_HOME` / `XDG_STATE_HOME` — все в `/tmp/r2d2-d9v/` |
| рабочее дерево | `/tmp/r2d2-d9v/workspace` (сервер и клиент смотрят только туда, `?directory=`) |
| пароль | `OPENCODE_SERVER_PASSWORD` задан, `~/.r2d2/`, `~/.config/opencode/`, `opencode.db`, `db/` не читались и не менялись |
| порт 4599 | **не использовался и не трогался**; собственный `opencode serve --port 45512` (pid 235191) не сигналился |
| секреты | не печатались; в этом файле нет ни одного значения |

Матрицы, отданные живым `GET /agent` на этом сервере, — те же, что в
`config/opencode/r2d2.opencode.json`: у `r2d2-voice` `bash: {"*": "deny"}`,
`webfetch: deny`; у `r2d2-agent` `bash: {"*": "ask"}`, `webfetch: allow`,
`websearch: allow`, `task: ask`.

## Воспроизведение

Три запроса к живому серверу, каждый в своей сессии, все через **настоящий**
`core.opencode.client.OpencodeClient`.

### Отказ (ядро дефекта)

Голосовой агент, одна команда вне его белого списка:

```
r2d2-voice: «Проверь, работает ли интернет: выполни в терминале ровно одну команду
            curl -s --max-time 10 https://example.com и скажи, что вернулось.»
```

Ответ голосового агента — prose, а отказ лежит **отдельным** сообщением:

```
* msg_u1  r2d2-voice  'Проверь, работает ли интернет: выполни в терминале ровно одну команду curl …'
* msg_d1  r2d2-voice  ''                       <- text ПУСТ, частей type:"text" нет вовсе
* msg_a2  r2d2-voice  'Не могу — эту команду мне выполнять запрещено, так что проверить интернет не получилось.'
      tool bash status=error
```

`state` отказа, дословно:

```json
{"status": "error",
 "input": {"command": "curl -s --max-time 10 https://example.com; echo \"EXIT: $?\""},
 "error": "The user has specified a rule which prevents you from using this specific tool call. Here are some of the relevant rules [{\"permission\":\"*\",\"action\":\"allow\",\"pattern\":\"*\"}, … {\"permission\":\"bash\",\"pattern\":\"*\",\"action\":\"deny\"}, …]",
 "time": {"start": 1790410337180, "end": 1790410337483}}
```

**Побайтово совпадает** с записью U8 (порт 4614): та же фраза, тот же порядок
правил, тот же `{"permission":"bash","pattern":"*","action":"deny"}`. Проверено
офлайн: `tests/test_opencode_client.py::test_the_captured_refusal_is_byte_identical_to_the_contract_u8_record`
(sha256 первых 16 hex совпадают). Значит это тот же дефект, а не похожий.

### Успешный ход с инструментом (то, что нельзя удалять)

Тот же агент в другой сессии забрал страницу:

```
* msg_ok  r2d2-agent  ''                       <- тоже ПУСТ, тоже без text-части
      tool webfetch status=completed
      state.input  {"url": "https://example.com", "format": "markdown"}
      state.output "Example Domain\n\n# Example Domain\n\n…"
```

**Отказ и успех неразличимы через `text`.** Оба — `""`. Это и есть причина, по
которой ключом может быть только `tool`-часть.

### Отказ инструмента по своей причине (чего удалять нельзя)

```
* msg_netfail  r2d2-agent  ''
      tool webfetch status=error
      state.error "Transport error (GET https://no-such-host.invalid/page)"
```

`status: "error"` — и это **не** enforcement. Значит одного статуса мало: нужно
**и** начало `state.error`.

Все три сообщения живут в одной сессии по дизайну (одна сессия на человека), и
все три лежат в общей истории, поэтому три фикстуры и закреплены в
`tests/fake_opencode.py`.

## Что сделал фикс

`MessageRecord` получил поле `refused`; `list_messages` заполняет его из
`tool`-части; `transcript_sweep` считает отказ таким же служебным состоянием, как
маркер эскалации, и удаляет его **тем же проходом, из того же снимка, что и
якорь**. Ничего нового на маршрутах не появилось, `session_collector.py` не
менялся: `signal_ids` уже означали «сообщения, которые не должны остаться».

## Живая проверка end-to-end

Гоняется **настоящий** `core.session_collector.SessionCollector.hand_to_agent` и
настоящий `OpencodeClient` против живого сервера. Заглушены три вещи, которых в
пробе быть не может: очередь задач (её строка в SQLite не то, что проверяется),
логгер и `store` (в вызове он не читается — идентификатор сессии приходит
аргументом, а настоящий store открыл бы SQLite владельца).

Две сессии, каждая своя (одна сессия на человека — это дизайн; для контроля
нужен свежий человек). В ** poisoned** сессии сначала выращивается отказ под
голосовым агентом, потом — тот же самый `hand_to_agent`.

### Контроль, отказа ранее не было

```
================ control: session ses_f23265f71ffeOw7rQVmOpHMjEM ================
   ack: 'Проверяю, пришлю в телеграм.'
   job enqueued: [{'type': 'opencode_reply', 'application_id': 'd9v-control-3',
                   'session_id': 'ses_f23265f71ffeOw7rQVmOpHMjEM',
                   'since_message_id': '', 'timeout_s': 600.0}]
--- control: after the agent turn (3 messages) ---
  * msg_… r2d2-agent: 'Пользователь попросил голосом: Скачай страницу https://example.com …'
  * msg_… r2d2-agent: ''
  * msg_… r2d2-agent: 'Готово — страницу скачал инструментом **webfetch** (не через bash). …'
      tool webfetch status=completed
   TOOL CALLS: [('webfetch', 'completed')]
```

`webfetch` → `completed`. `r2d2-agent` позвал инструмент, который его матрица
разрешает, и получил результат.

### После отказа — тот же вызов в той же сессии

```
================ poisoned: session ses_f2326460affe4xfSghv7rtNS1e ================
-- planting a denial under the VOICE agent --
  r2d2-voice -> speak: 'Не могу — эту команду мне выполнять запрещено, так что проверить интернет не получилось.'
   after 'Проверь, работает ли интернет: выполни в терминале р': refusals stored = 1
--- poisoned: after the voice turn (3 messages) ---
  * msg_0dcd9ba34001pKjqK2bEdIo5hr r2d2-voice: 'Проверь, работает ли интернет: …'
  * msg_0dcd9ba83001WO1YzYCI4KMlWC r2d2-voice: ''            <- ОТКАЗ, tool bash status=error
  * msg_0dcd9c693001Y6vJ500RkEF1WG r2d2-voice: 'Не могу — эту команду мне выполнять запрещено, …'
-- hand_to_agent: the real collector, the real client, the real server --
   ack: 'Проверяю, пришлю в телеграм.'
   job enqueued: [{'type': 'opencode_reply', 'application_id': 'd9v-poisoned-3',
                   'session_id': 'ses_f2326460affe4xfSghv7rtNS1e',
                   'since_message_id': 'msg_0dcd9c693001Y6vJ500RkEF1WG', 'timeout_s': 600.0}]
--- poisoned: after the agent turn (5 messages) ---
  * msg_0dcd9ba34001pKjqK2bEdIo5hr r2d2-voice: 'Проверь, работает ли интернет: …'
  * msg_0dcd9c693001Y6vJ500RkEF1WG r2d2-voice: 'Не могу — эту команду мне выполнять запрещено, …'
  * msg_0dcd9cdf7001VLJ3FTXKG3c2fk r2d2-agent: 'Пользователь попросил голосом: Скачай страницу …'
  * msg_0dcd9ce62001OfJf9sVHIw7KNe r2d2-agent: ''
  * msg_0dcd9d61f0012hY5PrzCGowOGn r2d2-agent: 'Готово — страницу удалось получить через `webfetch` …'
      tool webfetch status=completed
   TOOL CALLS: [('webfetch', 'completed')]
   refusals still stored: 0
```

Сверка снимков транскрипта до и после свипа:

```
BEFORE the sweep: ['msg_0dcd9ba34001pKjqK2bEdIo5hr', 'msg_0dcd9ba83001WO1YzYCI4KMlWC', 'msg_0dcd9c693001Y6vJ500RkEF1WG']
AFTER  the sweep: ['msg_0dcd9ba34001pKjqK2bEdIo5hr', 'msg_0dcd9c693001Y6vJ500RkEF1WG', 'msg_0dcd9cdf7001…', 'msg_0dcd9ce62001…', 'msg_0dcd9d61f001…']
DELETED BY ID   : ['msg_0dcd9ba83001WO1YzYCI4KMlWC']
user request kept : True
prose answer kept : True
rule dump left    : False
```

Итог:

| | вызовы инструментов | отказов в сессии после хода |
|---|---|---|
| контроль | `[('webfetch', 'completed')]` | 0 |
| после отказа | `[('webfetch', 'completed')]` | 0 |

**Отказ под голосовым агентом, после которого `r2d2-agent` всё равно пользуется
инструментами, которые его матрица разрешает.** Это и есть регрессия D9, и она
проверена вживую, а не офлайн.

## Что сохранилось, а что нет

Из трёх сообщений, которые были в сессии до свипа:

- **запрос пользователя** (`role: user`) — остался;
- **prose-ответ отказавшего агента** («Не могу — эту команду мне выполнять
  запрещено») — остался, и именно он стал якорем сборщика;
- **сам отказ** с дампом правил — удалён.

Цена решения — запись о том, что opencode отказал команде, уходит вместе с
отказом. Обмен выбран в пользу удаления: вред от хранения (мёртвый агент)
измерен в `live-run-v3.md` и больше, чем потеря одной строки истории, а
пользователь всё равно знает, что команда не выполнилась, из prose-ответа и из
того, что задача не была сделана.

## Чего этот прогон не доказывает

- Не проверялось, что **голосовой** агент перестаёт читать чужой отказ: он
  эскалирует почти на каждой формулировке, и вырастить ему отказ в нужный момент
  не удалось (три формулировки подряд ушли в `[[NEEDS_AGENT]]`). Для этого нужен
  отдельный сценарий, а не подгонка фразы.
- Не проверялась ветка `path=deadline`, где свип уходит, пока голосовой ход ещё
  пишет: там отказ может прийти позже и снимет его следующая эскалация.
- Не отправлено ни одного сообщения в Telegram и не поднимался `app/`: этот
  прогон про маршрутизацию и очистку, а не про доставку.
