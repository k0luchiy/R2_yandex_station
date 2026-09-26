# T21 v6 — сценарий отказа: `opencode serve` остановлен, потом поднят заново

Продолжение `qa/live-run-v6.md`; здесь ровно то, что требует план, строка 478:
остановить сервер, повторить простой вопрос, убедиться в валидном Alice JSON и в
том, что процесс остался жив, потом поднять сервер заново и убедиться, что
следующий вопрос переиспользует **ту же** сессию.

План требует от сценария отказа ровно двух вещей: «ответ всё ещё должен быть
валидным Alice JSON через цепочку фолбэков» и «`GET /health` должен показать
`opencode.reachable: false`». Обе выполнены. Третье требование — переиспользование
сессии после рестарта — оказалось отдельным измерением, и оно же стало проверкой
**D12**, который в `live-run-v3.md` был помечен как открытый.

Ничего не исправлялось: T21 — доказательство.

## Окружение

То же, что в `qa/live-run-v6.md`: `opencode serve` v1.18.32 на `127.0.0.1:4599`
через `scripts/opencode_serve.sh`, R2D2 на `127.0.0.1:8099`, `application_id` =
`t21-v4`, сессия `ses_f21385511ffe18cxi1gk0Frsdm`. Секреты не печатались, логи с
токеном бота цитируются как `bot<REDACTED>`, `curlrc` уничтожен.

## Шаг 1 — сервер остановлен

```
$ kill -TERM 3534647        # opencode serve, 22:47:31
$ ss -ltnp | grep ':4599 '
  (пусто)                   # 4599 свободен
```

## Шаг 2 — простой вопрос с упавшим сервером

```json
{"meta":{"interfaces":[{"type":"Voice"}],"lang":"ru-RU"},
 "request":{"type":"SimpleUtterance","command":"Сколько будет два плюс два? Ответь коротко."},
 "session":{"new":false,"skill_id":"","session_id":"",
            "application":{"application_id":"t21-v4","type":"Alice"},"user":{"user_id":""}},
 "version":"1.0"}
```

```
$ curl -s http://127.0.0.1:8099/webhook -H 'Content-Type: application/json' -d "$BODY" \
       -w '\n<<http=%{http_code} time_total=%{time_total}>>'
{"response":{"text":"Что-то пошло не так. Попробуй ещё раз.","tts":"Что-то пошло не так. Попробуй ещё раз.","end_session":false},"version":"1.0"}
<<http=200 time_total=1.549586>>
```

**Валидный Alice JSON, HTTP 200, 1,549586 с** — и `ERROR_TEXT`, что план для этого
случая и считает приемлемым.

Что произошло под ней, по логу:

```
22:47:34,604 WARNING core.backends.registry backend 'zen' (kind openai_compatible): dropped from the chain, 'api_key' is empty
22:47:34,604 WARNING core.backends.registry backend 'yandexgpt' (kind openai_compatible): dropped from the chain, 'api_key' is empty
22:47:35,485 WARNING core.backends.openai_compatible backend openrouter: HTTP 429, retrying once (attempt 2 of 2) after 0.4s
22:47:36,087 WARNING r2d2 LLM backend openrouter failed: BackendStatusError('backend \'openrouter\': HTTP 429
  ({"error":{"message":"Rate limit exceeded: free-models-per-day. Add 10 credits to unlock 1000 free model requests per day",
  "code":429,"metadata":{"headers":{"X-RateLimit-Limit":"50","X-RateLimit-Remaining":"0",
  "X-RateLimit-Reset":"1790467200"}})')
22:47:36,088 ERROR r2d2 handle failed
  core/brain.py:210 in _chain_turn -> choice = await rec.measure(self._call_llm(messages))
```

`X-RateLimit-Reset: 1790467200` = **2026-09-27T00:00:00Z** — тот же суточный лимит
бесплатных моделей OpenRouter, что в `live-run-v3.md` §D8 и в
`qa/d8-openrouter-probe.md`. `zen` и `yandexgpt` отброшены на пустом
`api_key` (`${R2D2_ZEN_KEY}` не задан), так что живой участник цепочки ровно один и
сегодня он исчерпан. **D8 держится.**

## Шаг 3 — процесс жив, зависимость не

```
$ ss -ltnp | grep ':8099 '
LISTEN 127.0.0.1:8099  users:(("uvicorn",pid=3536261,fd=25))

$ curl -s http://127.0.0.1:8099/health -w '\n<<http=%{http_code} time_total=%{time_total}>>'
{"status":"ok","opencode":{"reachable":false,"version":null,"base_url":"http://127.0.0.1:4599"},
 "sessions":10,"chain":["zen","yandexgpt","openrouter"]}
<<http=200 time_total=0.037317>>
```

`opencode.reachable: false`, `version: null`, HTTP 200, 37 мс, процесс на месте.
`sessions: 10` — девять строк владельца плюс `t21-v4`; упавший сервер не добавил
ни одной сессии.

**PASS** — оба требования плана, строка 478.

## Шаг 4 — сервер поднят заново, и первый вопрос после рестарта

Это и есть D12, и условие воспроизвелось само.

```
$ kill -TERM 3576656                       # второй перезапуск, 22:53:34
$ setsid nohup bash scripts/opencode_serve.sh …    # 22:53:37
  сервер ответил на /global/health примерно через 6000 мс
$ # вопрос отправлен сразу, как только порт начал отвечать: 22:53:45
```

Сервер после рестарта отвечает медленно: он строит индекс сессий, и
`GET /session` не укладывается в отведённое время. Это ровно то состояние, которое
в `live-run-v3.md` §D12 давало голый `httpx.ReadTimeout('')` и `ERROR_TEXT` против
здорового сервера:

```
22:53:48,346 WARNING r2d2 opencode: t21-v4 is bound to session
  ses_f21385511ffe18cxi1gk0Frsdm and the server did not re-verify it in time
  (opencode opencode: GET /session was accepted and did not answer within 3.2s,
  so the server is answering slowly rather than not at all; whatever it is
  working on is still running there and nothing was aborted); the turn continues
  on the bound session rather than losing the answer
```

```json
{"meta":{"interfaces":[{"type":"Voice"}],"lang":"ru-RU"},
 "request":{"type":"SimpleUtterance","command":"Назови столицу Испании. Одно слово."},
 "session":{"new":false,"skill_id":"","session_id":"",
            "application":{"application_id":"t21-v4","type":"Alice"},"user":{"user_id":""}},
 "version":"1.0"}
```

```
$ curl -s …/webhook -d "$BODY" -w '\n<<http=%{http_code} time_total=%{time_total}>>'
{"response":{"text":"Проверяю, пришлю в телеграм.","tts":"Проверяю, пришлю в телеграм.","end_session":false},"version":"1.0"}
<<http=200 time_total=9.638631>>

22:53:54,709 INFO r2d2 turn route=opencode path=deadline model=opencode/space-bunny-free
  agent=r2d2-voice llm_ms=3217 total_ms=9633 escalated=False permission_asked=False msgs=1 tools=()
```

Сборщик, вооружённый на отложенном якоре, закрылся через 59,3 с и отдал ответ:

```
job 22:53:54 -> 22:54:53  opencode_reply  done
  result: Мадрид
```

## Сводная таблица

| | `live-run-v3.md` §D12 (было) | это |
|---|---|---|
| что поднялось на не re-верифицированной сессии | `httpx.ReadTimeout('')` | типизированный `OpencodeDeadlineExceeded` с объяснением, почему это «медленно, а не отсутствует» |
| маршрут | `route= path=error`, уход в цепочку | **`route=opencode path=deadline`** |
| ответ Алисе | `ERROR_TEXT` (сервер был здоров) | **ack**, задержка 9,638631 с |
| `session_id` | та же | **та же** `ses_f21385511ffe18cxi1gk0Frsdm` |
| новых сессий | 0 | **0**; `POST /session` за весь прогон — ровно один |
| ответ доставлен пользователю | нет | **да**, `Мадрид` в Telegram |
| коллектор | — | вооружён на якоре, разрешённом в фоне, когда чтение не ответило за 0,5 с (**D15**) |

**PASS.**

## Что стоит сказать про 9,638631 с

Это **выше бюджета Алисы в 4,5 с**, и это настоящее число, а не сбой. На первом
ходе после рестарта сервера три ожидания выстраиваются в очередь: 3,2 с на
re-верификацию сессии, затем 3,2 с на голосовой ход, плюс накладные расходы. Акк
опоздал, но ответ не потерялся: он ушёл в Telegram через минуту.

Это лучше, чем `ERROR_TEXT` против работающего сервера, и хуже, чем 2,5 с.
Отдельно отмечу, что `httpx` в этом прогоне показал ReadTimeout на SSE-читателях
**только** в окнах, когда сервер был выключен (`22:47:31`–`22:48:24` и
`22:53:34`–`22:53:42`, 120 строк, все внутри этих окон), — то есть D1 не вернулся:
читатель не слепнет на живом сервере, он правильно переживает падение.

## Воспроизведение

```bash
kill -TERM "$(pgrep -f 'opencode serve --hostname 127.0.0.1 --port 4599')"
bash /tmp/r2d2-qa/t21d/ask.sh "FAIL-CASE" "Сколько будет два плюс два? Ответь коротко." t21-v4
curl -s http://127.0.0.1:8099/health
# затем поднять сервер заново и спросить что угодно в том же application_id
```
