# T21, second run — failure case: `opencode serve` stopped

Plan line 478, executed after the six scenarios of `qa/live-run-postfix.md`, on
the same R2D2 process (uvicorn pid 2204905) and against the same
`application_id` (`t21-final`, session `ses_f23bb5dbeffePCgTNtG36cby0v`).
Both servers were started by hand for this run and both were stopped at the end.

## 1. Stop the server

```
$ kill 2203935
$ ss -ltnp | grep -c 4599
0
```

The port is free and the process is gone.

## 2. `/health` must report the degradation, not a fault

```
$ curl -s http://127.0.0.1:8099/health -w '\nhttp=%{http_code} time_total=%{time_total}\n'
{"status": "ok", "opencode": {"reachable": false, "version": null, "base_url": "http://127.0.0.1:4599"},
 "sessions": 6, "chain": ["zen", "yandexgpt", "openrouter"]}
http=200 time_total=0.048256
```

`opencode.reachable: false` and `version: null`, with the process still serving
`200`. No credential appears anywhere in the body.

**PASS** for this half.

## 3. The same plain question must still be valid Alice JSON

Request, byte-identical in shape to the one that produced `Четыре.` in
scenario 2c, on the same `application_id`:

```json
{"meta":{"interfaces":[{"type":"Voice"}],"lang":"ru-RU"},
 "request":{"type":"SimpleUtterance","command":"Назови столицу Франции. Ответь одним словом."},
 "session":{"new":false,"skill_id":"","session_id":"","application":{"application_id":"t21-final","type":"Alice"},"user":{"user_id":""}},
 "version":"1.0"}
```

```
{"response":{"text":"Что-то пошло не так. Попробуй ещё раз.",
             "tts":"Что-то пошло не так. Попробуй ещё раз.","end_session":false},"version":"1.0"}
http=200 time_total=1.148824
```

and the process is still there:

```
$ curl -s -m 5 -o /dev/null -w 'health http=%{http_code}\n' http://127.0.0.1:8099/health
health http=200
```

R2D2's own record of the attempt:

```
11:10:18,xxx WARNING core.opencode.client opencode: http://127.0.0.1:4599 is not usable:
                     All connection attempts failed
11:10:20,532 INFO    httpx HTTP Request: POST https://openrouter.ai/api/v1/chat/completions
                     "HTTP/1.1 429 Too Many Requests"
11:10:20,535 WARNING core.backends.openai_compatible backend openrouter: HTTP 429, retrying once
                     (attempt 2 of 2) after 0.4s
11:10:41,060 WARNING r2d2 LLM backend openrouter failed: BackendStatusError('backend
                     'openrouter': HTTP 429 ({"error":{"message":"Rate limit exceeded:
                     free-models-per-day. Add 10 credits to unlock 1000 free model requests
                     per day","code":429,"metadata":{"headers":{"X-RateLimit-Limit":"50",
                     "X-RateLimit-Remaining":"0","X-RateLimit-Reset":"1790467200'}})
11:10:20,783 INFO    r2d2 turn route= path=error model= agent= llm_ms=1069 total_ms=1145
                     escalated=False permission_asked=False msgs=0 tools=()
```

**PASS** for this half — the graceful `ERROR_TEXT` the plan allows, in 1.15 s,
as valid Alice JSON with `version: "1.0"`, no 500 and no stack trace to a user.

Note *why* it is `ERROR_TEXT`, because the reason is D8's current state:

| backend | outcome |
|---|---|
| `zen` | dropped at startup, WARNING `backend 'zen' (kind openai_compatible): dropped from the chain, 'api_key' is empty` — `R2D2_ZEN_KEY` is unset |
| `yandexgpt` | dropped at startup, same WARNING — `YANDEX_API_KEY` is empty |
| `openrouter` | credentialed, called, **HTTP 429** `free-models-per-day`, `X-RateLimit-Limit: 50`, `X-RateLimit-Remaining: 0`, `X-RateLimit-Reset: 1790467200` |

`config/backends.json` points `openrouter` at
`inclusionai/ling-3.0-flash-sante:free`, the id a 17-id probe found answering
9/9 in 0.84–1.85 s. That id is still correct; the free tier is capped at 50
requests a day and this run's own agent turns exhausted it. So D8 is fixed in
the sense that the chain has a member that is *called and answers* when it is
under the cap, and rate-limited today. The degradation is therefore graceful
but currently vacuous, exactly as the plan allows it to be.

## 4. Restart, and the next question must reuse the SAME `session_id`

```
$ bash scripts/opencode_serve.sh &
$ curl -s -K <auth> http://127.0.0.1:4599/global/health
{"healthy":true,"version":"1.18.32"}
```

The first question after the restart (**4.520 s**):

```
{"response":{"text":"Что-то пошло не так. Попробуй ещё раз.", …},"version":"1.0"}
http=200 time_total=4.524003

11:10:36,574 GET http://127.0.0.1:4599/global/health  "HTTP/1.1 200 OK"
11:10:39,782 WARNING r2d2 opencode 'opencode' could not answer t21-final; the turn falls back
                    to the chain: ReadTimeout('')
11:10:41,065 INFO    r2d2 turn route= path=error model= agent=r2d2-voice llm_ms=1266 total_ms=4520
```

The server was healthy — the health probe in the same turn answered `200` — and
the turn still came back `ERROR_TEXT`. The failing call was the session
re-verification `GET /session` inside `OcSessionStore.resolve`, against a server
that had just rebuilt its index over 185 sessions, and it surfaced as a bare
`httpx.ReadTimeout` rather than the typed `OpencodeDeadlineExceeded` that
`SessionRoute.turn` catches — so the turn fell through to a chain with no
working member. This is recorded as **D12** in `live-run-postfix.md` and is not
fixed.

The next two questions, same body, same process, no restart of R2D2:

```
http=200 time_total=3.737171   turn route=opencode path=deadline … total_ms=3733
http=200 time_total=3.637053   turn route=opencode path=deadline … total_ms=3633
```

Both back on the opencode route, and both posted into the **same** session:

```
POST …/session/ses_f23bb5dbeffePCgTNtG36cby0v/message   200
POST …/session/ses_f23bb5dbeffePCgTNtG36cby0v/message   200

POST /session count, whole run, before and after the restart = 1   (10:51:20,685, unchanged)
sqlite> select application_id, session_id, title, message_count from oc_sessions
          where application_id='t21-final';
t21-final | ses_f23bb5dbeffePCgTNtG36cby0v | r2d2:alice:t21-final | 17
```

and the server still holds exactly one session for that title:

```
GET /session?limit=1000
  sessions in /home/<user>/r2d2-workspace   9  (8 before this run, +1 = mine)
  sessions titled r2d2:alice:t21-final        1  ses_f23bb5dbeffePCgTNtG36cby0v
  POST /session issued by R2D2                 1
```

The server had been stopped and restarted, so its in-memory session index was
rebuilt from disk; `OcSessionStore._resolve_locked` re-checked the binding
against `GET /session`, found the same id, re-bound nothing and issued no
`POST /session`. That is the reuse path working across a server restart, which
is the strongest form of the check the plan asks for.

**PASS** — the same `session_id`, no new session, no re-probe, and the route
recovered on its own.

## Summary

| check | result |
|---|---|
| `opencode serve` stopped | port 4599 free, process gone |
| `/health` with opencode down | **PASS** — `200`, `reachable: false`, `version: null`, no credentials, 0.048 s |
| plain question still valid Alice JSON | **PASS** — `ERROR_TEXT`, `200`, 1.149 s, process stays up |
| fallback actually answers | **degraded** — `zen`/`yandexgpt` uncredentialed, `openrouter` HTTP 429 `free-models-per-day` (D8's current state) |
| first question after the restart | **FAIL** — `ERROR_TEXT` in 4.520 s from a bare `httpx.ReadTimeout` on the session re-verification, D12 |
| restart, same `session_id`, no new session | **PASS** — `POST /session` 1 → 1, binding and server both unchanged, the two following turns back on `route=opencode` |

## Everything stopped

```
$ ss -ltnp | grep -E ':4599|:8099'
(nothing)
```

R2D2 shut down gracefully — `6 reader(s) stopped`, `Application shutdown
complete.` — and no process this run started is left. The user's pre-existing
opencode processes (5813, 5821, 24233) were never signalled, and
`~/.config/opencode/` was never written.

The full suite, with both servers stopped (it cannot share port 4599 with a live
`opencode serve` — see D12):

```
$ .venv/bin/python -m pytest tests/ -q
906 passed, 1 skipped in 74.36s
```
