# D13, D14, D15 — воспроизведение и живая проверка фиксов против настоящего `opencode serve`

Продолжение `qa/live-run-v3.md`, где все три помечены как **не исправленные** (§D13,
§D14, §D15), и `qa/live-run-v4.md`, где D9 закрыт. Этот файл фиксирует одно: что
изменилось в коде и **что на живом сервере происходит теперь**.

Ничего в `config/`, в матрицах разрешений и в чужих сессиях не менялось.

## Окружение

| | |
|---|---|
| opencode | `/home/<user>/.opencode/bin/opencode` v1.18.32, `serve --hostname 127.0.0.1 --port 4611` |
| конфиг | `OPENCODE_CONFIG_DIR=/tmp/r2d2-d13v/opencode`, там **побайтово** `config/opencode/r2d2.opencode.json` |
| изоляция | `XDG_CONFIG_HOME` / `XDG_DATA_HOME` / `XDG_CACHE_HOME` / `XDG_STATE_HOME` — все в `/tmp/r2d2-d13v/xdg/` |
| рабочее дерево | `/tmp/r2d2-d13v/workspace` (и сервер, и клиент смотрят только туда, `?directory=`) |
| пароль | сгенерирован **здесь**, `openssl rand -base64 24`, в `.env` / `.env.oc` не заглядывало; значение нигде не напечатано |
| порт 4599 | **не использовался и не трогался**; собственный сервер владельца на 46005 (pid 1575201) не тронут |
| секреты | не печатались; в этом файле нет ни одного значения |
| Telegram | `send_message` подменён наRecorder — **ни одного реального сообщения не отправлено** |

Что здесь настоящее, а что пришлось подменить, и почему:

* **Настоящие:** сервер, `OpencodeClient`, кадры `GET /event`, ответы разрешений на
  проводе, `PermissionBroker`, `TurnWatch`, `AnswerGate`, `SessionCollector`,
  `SessionSweeper` и `collect_reply`.
* **Подменено одно — триггер D15.** В живом прогоне чтение не успевало, потому что
  сервер был **занят**. Заставить opencode самого быть медленным извне процесса
  нельзя, поэтому **первое** `list_messages` задерживается на 0,8 с перед настоящим
  GET, чтобы честно промахнуться мимо `MARKER_TIMEOUT_S = 0,5` на живой, здоровый
  сервер. Чтение внутри отложенного вооружения — уже настоящий HTTP-запрос; наш
  только часы первого.
* **Подменено одно — граница Telegram.** `send_message` заменён на Recorder во всех
  трёх модулях, которые импортируют его по имени
  (`core.tools.telegram_tool`, `core.permissions`, `core.session_collector`).
* **Подменено одно — `Memory`.** Настоящий `Memory` открыл бы `db/sessions.db`
  владельца. Использован объект с тремя методами строки `pending_actions`; в
  пробе он не читается, идентификатор сессии приходит аргументом.

## D14 — два подтверждения на один ход

Задача агенту: `Выполни РОВНО ДВЕ команды в терминале, ничего больше: сначала
echo AAA, потом echo BBB.` У `r2d2-agent` матрица `bash: {"*": "ask"}`, поэтому
каждая команда поднимает свой `permission.asked`.

Два запроса пришли за **5,2 с**, и второй пришёл, **когда первый ещё висел**:

```
OK    a SECOND ask is stored behind the first, both unanswered at t+5.2s
ROW   head=per_0de9e0b530012AB4I4eBtIiyev
ROW   queued=['per_0de9e0bf7001MTFswV7Zm4krB4']
ROW   questions the user was asked: 1
```

| | было (`live-run-v3.md` §D14) | стало |
|---|---|---|
| второй запрос, когда первый висел | **отклоняет первый** за 0,55 с | встаёт в очередь `за` ним |
| вопросов на экране | 2 | **1** |
| ответов на проводе | `reject` для первого, потом `reject` по таймауту | `once`, `once` |
| ход, требующий двух подтверждений | не мог завершиться | **завершился** |

Всё, что ушло на провод — целиком, из перехвата `respond_permission`:

```
WIRE  POST permissions/per_0de9e0b530012AB4I4eBtIiyev {"response": 'once'}
TELL  Принято, выполняю: действие opencode.
TELL  Нужно подтверждение: действие opencode. Ответь в телеграм «да» или «нет».
WIRE  POST permissions/per_0de9e0bf7001MTFswV7Zm4krB4 {"response": 'once'}
```

```
== answers on the wire: ["per_0de9e0b530012AB4I4eBtIiyev -> 'once'",
                         "per_0de9e0bf7001MTFswV7Zm4krB4 -> 'once'"]
== 'always' sent: 0
== 'reject' sent: 0
```

`always` — ноль, `reject` — ноль, порядок сохранён. Первое сообщение в логе
`live-run-v3.md` §D14 было ровно противоположным: `refusing it, because the new ask
replaces it` через 0,55 с.

## D13 — план вместо результата

Та же сессия, тот же ход. Агент **озвучил план** и пошёл в инструмент:

```
TEXT  "I'll run both commands."
```

В `t+11,2 с`, то есть после **6 с тишины** — втрое больше эвристики молчания, —
при **двух неотвеченных запросах** сборщик всё ещё ждал:

```
WATCH 2 unanswered ask(s), blocked_at=msg_0de9dfa32001jEUidMLKhm8Wvn, idle not seen
GATE  finished already? False
```

`blocked_at` — это сообщение, **из которого пришёл заблокированный вызов**. Всё, что
агент написал до него, — план; всё, что после, — результат. Якорь сборщика уезжает
за него, поэтому собирается результат.

Было (`live-run-v3.md` §D13): `job a20dc44588b8 ... result: "Сделаю сводку. Сначала
соберу реальные данные с arXiv API за последнюю неделю."` — 8,2 с, план, работа
выброшена.

После `да` на оба запроса пришёл `session.idle`, и сборщик отдал **результат**:

```
== WHAT THE COLLECTOR SHIPPED ==
   'Обе команды выполнены успешно: `echo AAA` вывела `AAA`, `echo BBB` вывела `BBB`.'

== the session's assistant text, in order ==
   "I'll run both commands."
   'Обе команды выполнены успешно: `echo AAA` вывела `AAA`, `echo BBB` вывела `BBB`.'
```

Плана в отправленном теле нет. Это и есть та форма, которую прогон назвал «тихо
неправильный ответ»: раньше она выглядела как успех.

### Один кадр, который решает всё: `session.idle` есть

```
== event census for this session ==
   {'session.updated': 5, 'message.updated': 10, 'message.part.updated': 19,
    'session.status': 6, 'session.diff': 3, 'message.part.delta': 4,
    'permission.asked': 2, 'permission.replied': 2, 'session.idle': 1}
== session.idle frames seen: 1
```

Набор совпадает с U4 в `docs/11-opencode-contract.md`, включая единственный
`session.idle` и его форму `{"properties": {"sessionID": "ses_..."}}`.

Отдельно измерено то, что легко принять за дефект: **пока ход висит на
неотвеченном запросе, `session.idle` не приходит вовсе.** В первой попытке этой
проверки census дал `session.idle: 0` — и это было правильно: ход был заблокирован
вторым запросом, на который никто не ответил. Ровно поэтому C5 и требует
конъюнкции, а не одного `session.idle`.

## D15 — чтение, не успевшее за полсекунды

`GET /global/health` **непосредственно перед** — `reachable=True`, то есть сервер
здоров, а не отсутствует:

```
GET /global/health immediately before -> reachable=True
-- the voice turn answered: 'Париж'
-- the session holds 4 messages before the hand-off
```

Ровно этот сценарий из §D15: пользователь спросил, голосовой агент ответил
`Париж`, и дальше ход эскалирует.

```
WARNING d13v D15: delaying the first transcript read by 0.8s so it misses MARKER_TIMEOUT_S=0.5s
WARNING c15 opencode 'opencode': the turn in session ses_f216d0b31ffeN3WjKp3RPOc2GG of d13v-d15
        kept running but the transcript did not answer inside 0.5s (TimeoutError: ); the collector
        is armed anyway on an anchor resolved in the background, and the user is told if even that fails

-- hand_to_agent with the first read delayed past MARKER_TIMEOUT_S
   ack spoken: 'Проверяю, пришлю в телеграм.'  after 594 ms on Alice's clock
   jobs armed the instant the ack was returned: 0
   job armed 0.69s after the ack, off Alice's clock:
     {"type": "opencode_reply", "application_id": "d13v-d15",
      "session_id": "ses_f216d0b31ffeN3WjKp3RPOc2GG",
      "since_message_id": "msg_0de930203001NiQn5A57jNfv00", "timeout_s": 600.0}
   => anchor resolved to a real message, not '' : True
   => the anchor is a message that existed BEFORE the hand-off: True
```

| | было | стало |
|---|---|---|
| подтверждение | произнесено | произнесено за **594 мс** |
| задача `opencode_reply` | **не существует** | вооружена через 0,69 с **вне часов Алисы** |
| якорь | — | `msg_0de930203001NiQn5A57jNfv00`, **настоящий** id, существовавший **до** отдачи хода |
| `Париж` | записан в сессию, не доставлен никому | у него есть сборщик |

Две вещи, которые здесь неверно было бы упростить:

* **Подтверждение не ждёт якорь.** 594 мс из бюджета 4,5 с — и это правильно: якорь
  ищет фоновая задача, а не голос Алисы. Ждать на этом месте — значит вернуть D15
  с другой стороны.
* **Якорь не пустой.** Пустой `since_message_id` читается как «новее всё в сессии»,
  и в сборщике, который живёт все свои 600 с, следующий ход приехал бы в доставку
  этого. Здесь якорь — сообщение, **которое было в сессии до отдачи хода**, что
  проверено сравнением со снимком транскрипта, снятым заранее.

### Другой конец: транскрипт, который не отвечает никогда

```
ERROR c15b opencode: the turn in session ... of d13v-d15-lost ran 2s and the transcript
      never answered, so no collector could be armed on a boundary (TimeoutError: ).
      The user is told the answer is lost rather than left waiting for it.
   jobs armed: 0 (correctly none)
   what the user was told: ['Ответ агента потерян: opencode не ответил вовремя. Спросите ещё раз.']
   => the loss is stated, not silent: True
```

Ограниченная потеря ответа допустима, тихая — нет: ход уже подтверждён, значит
человек ждёт сообщения, которого не будет. Единственное честное место для такого
известия — его собственный чат.

## Чего этот прогон не доказывает

- **Не проверено, что `AnswerGate` ждёт в режиме без потока.** `EVENT_MODE = "sse"`,
  и деградация на молчание (`tests/test_answer_gate.py`) проверена офлайн, а не на
  сервере без `GET /event`.
- **Не измерено, сколько времени сборщик держит слот в худшем случае.** Замерена
  граница: ожидание кончается либо `session.idle` при пустой очереди, либо окном
  брокера плюс один свип. Живой прогон этого не касался.
- **Заголовок запроса приходит пустым** — в кадре `permission.asked` этого поля нет,
  и вопрос уходит как «действие opencode». Это отдельная находка, к D13/D14/D15 не
  относится, и в этом прогоне не исправлялась.
- **Не проверялась ветка `path=deadline` целиком** — только то, что её маркер
  вычищается на следующем ходу, офлайн-тестом.
- **Не отправлено ни одного сообщения в Telegram** и не поднимался `app/`.

## Воспроизведение

```bash
# сервер и изоляция
bash /tmp/opencode/d13v_up.sh
setsid nohup bash /tmp/r2d2-d13v/serve.sh > /tmp/r2d2-d13v/serve.log 2>&1 &

# D15 (обе половины), затем D14 + D13
cd /home/<user>/Documents/R2_yandex_station
.venv/bin/python /tmp/opencode/d13v_live.py
.venv/bin/python /tmp/opencode/d13v_focus.py
```

Проверка офлайн, что тесты действительно держат каждый фикс (мутация фикса → тест
краснеет), — `tests/test_answer_gate.py`,
`tests/test_brain_hybrid.py::test_a_transcript_read_that_failed_still_leaves_a_collector_armed`,
`tests/test_brain_hybrid.py::test_a_re_verification_that_stays_slow_leaves_the_answer_a_deadline_not_an_error`,
`tests/test_permission_broker.py::test_a_second_ask_queues_behind_the_first_and_both_can_be_answered`.
