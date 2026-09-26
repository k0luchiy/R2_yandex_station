# T21 — failure case: `opencode serve` stopped

The plan's line 478, executed against the same live stack as `live-run.md`.
Everything here happened after the six scenarios, on the same R2D2 process
(uvicorn pid 1756799) and the same opencode server.

## 1. Stop the server

```
$ kill 1655663
$ ss -ltnp | grep -c 4599
0
```

The port is free; the process is gone.

## 2. `/health` must report the degradation, not a fault

```
$ curl -s -o /dev/null -w 'http=%{http_code} time_total=%{time_total}\n' http://127.0.0.1:8099/health
http=200 time_total=0.069582
```

```json
{
  "status": "ok",
  "opencode": {"reachable": false, "version": null, "base_url": "http://127.0.0.1:4599"},
  "sessions": 5,
  "chain": ["zen", "yandexgpt", "openrouter"]
}
```

`opencode.reachable: false` and `version: null`, with the process still serving
`200`. No credential appears anywhere in the body.

**PASS** for this half of the check.

## 3. The same plain question must still be valid Alice JSON

Request — byte-identical to the one that produced `Париж.` in scenario 2c, on the
same `application_id` (`t21-live-proof-g`):

```json
{"meta":{"interfaces":[{"type":"Voice"}]},
 "request":{"type":"SimpleUtterance","command":"Назови столицу Франции. Ответь одним словом."},
 "session":{"new":false,"skill_id":"","application":{"application_id":"t21-live-proof-g"},"user":{"user_id":""}},
 "version":"1.0"}
```

Response (`http=200 time_total=0.636361`):

```json
{"response":{"text":"Что-то пошло не так. Попробуй ещё раз.","tts":"Что-то пошло не так. Попробуй ещё раз.","end_session":false},"version":"1.0"}
```

R2D2's own record of the attempt:

```
WARNING r2d2 LLM backend openrouter failed: BackendStatusError('backend 'openrouter': HTTP 404
        ({"error":{"message":"This model is unavailable for free. The paid version is available
        now - use this slug i…
ERROR   r2d2 handle failed
INFO    r2d2 turn route= path=error model= agent= llm_ms=596 total_ms=632 escalated=False permission_asked=False msgs=0 tools=()
```

and the process is still there:

```
$ curl -s -m 5 -o /dev/null -w 'health http=%{http_code}\n' http://127.0.0.1:8099/health
health http=200
```

The graceful `ERROR_TEXT` the plan allows, delivered in 0.64 s, as valid Alice
JSON with `version: "1.0"`, no 500 and no stack trace.

**PASS** for this half too — but note *why* it is `ERROR_TEXT` and not a
fallback-backend answer. The chain R2D2 walked was:

| backend | outcome |
|---|---|
| `zen` | dropped at startup, WARNING `backend 'zen' (kind openai_compatible): dropped from the chain, 'api_key' is empty` — `R2D2_ZEN_KEY` is unset |
| `yandexgpt` | dropped at startup, same WARNING — `YANDEX_API_KEY` is empty |
| `openrouter` | credentialed, called, **HTTP 404** `This model is unavailable for free` |

`config/backends.json` points `openrouter` at `inclusionai/ling-3.0-flash:free`,
which OpenRouter no longer serves on the free tier. The chain therefore has zero
working members on this machine, so the *only* possible answer with opencode down
is `ERROR_TEXT`. `docs/05-llm-provider.md` already flags the free-OpenRouter
instability; this is the concrete consequence — the degradation is nominal, not
functional. Recorded as **D8** in `live-run.md`.

## 4. Restart, and the next question must reuse the SAME `session_id`

```
$ bash scripts/opencode_serve.sh &
$ curl -s -K <auth> http://127.0.0.1:4599/global/health
{"healthy":true,"version":"1.18.32"}
```

Same request body as step 3, unchanged:

```
$ curl -s http://127.0.0.1:8099/webhook -H 'Content-Type: application/json' --data-binary @body.json \
       -w 'http=%{http_code} time_total=%{time_total}\n'
{"response":{"text":"Проверяю, пришлю в телеграм.","tts":"Проверяю, пришлю в телеграм.","end_session":false},"version":"1.0"}
http=200 time_total=3.436347
```

R2D2's log for that turn, and the session-creation counter across it:

```
07:41:08,472 INFO r2d2 turn route=opencode path=deadline model=opencode/space-bunny-free
                  agent=r2d2-voice llm_ms=3244 total_ms=3431 escalated=False permission_asked=False msgs=1 tools=()

POST /session count before restart = 5
POST /session count after  restart = 5        ← zero new sessions
```

`route=opencode` — the brain came straight back on the opencode path, with no
restart of R2D2 and no re-probe. The binding was never disturbed:

```
sqlite> select session_id from oc_sessions where application_id='t21-live-proof-g';
ses_f246dff9cffeIE03I13nHGSMMd
```

and the server still holds exactly that one session for that title:

```
GET /session?limit=1000
total sessions on server = 202
r2d2 sessions            = 8
  ses_f24a63be4ffeqdiwwhyYaF84oN r2d2:alice:t21-live-proof
  ses_f2494066fffeRxu5Oij5MFxIm4 r2d2:alice:t21-live-proof-b
  ses_f2484eb0affe6Be2mInbQtLeG1 r2d2:alice:t21-live-proof-c
  ses_f2476c802ffeSn85bllSZdVwoK r2d2:alice:t21-live-proof-d
  ses_f2474346affewMG7hh3Sv7Q5eo r2d2:alice:t21-live-proof-e
  ses_f247037d9ffedbsLd6ngz3Qc5x r2d2:alice:t21-live-proof-f
  ses_f246dff9cffeIE03I13nHGSMMd r2d2:alice:t21-live-proof-g
  ses_f2474aac4ffeNnPg1OMFCtgQF5 r2d2:alice:tg:1087136471
count for t21-live-proof-g = 1
```

The opencode server had been stopped and restarted, so its in-memory session index
was rebuilt from disk; `OcSessionStore._resolve_locked` re-checked the binding
against `GET /session`, found the same id, re-bound nothing and issued no
`POST /session`. That is the reuse path working across a server restart, which is
the strongest form of the check the plan asks for.

**PASS.**

## One thing that went wrong while doing this

The first `opencode serve` restart died on its own about 40 s after starting,
with nothing but this in its log:

```
opencode server listening on http://127.0.0.1:4599
MaxListenersExceededWarning: Possible EventTarget memory leak detected.
  11 event listeners added to [M$]. MaxListeners is undefined. …
```

and no exit line, no stack trace, no further output. The machine was under memory
pressure at the time (15 GiB total, 10 GiB used, swap 16 GiB fully consumed), so
an OOM kill is the likely cause, but opencode logged nothing that says so. A
second restart came up clean and served the rest of the run. This is an observation
about `opencode serve` 1.18.32, not about R2D2, and it is not counted as a
scenario result — but an operator watching only R2D2's log would have seen nothing
but a successful turn.

## Summary

| check | result |
|---|---|
| `opencode serve` stopped | port 4599 free, process gone |
| `/health` with opencode down | **PASS** — `200`, `reachable: false`, `version: null`, no credentials |
| plain question still valid Alice JSON | **PASS** — `ERROR_TEXT`, `200`, 0.636 s, process stays up |
| fallback actually answers | **degraded** — chain empty (D8), `ERROR_TEXT` is the only reachable answer |
| restart, same `session_id`, no new session | **PASS** — `POST /session` 5 → 5, binding and server both unchanged |
