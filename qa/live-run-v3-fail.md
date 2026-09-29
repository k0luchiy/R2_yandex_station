# T21, third run — the failure case: `opencode serve` down, and the restart

Plan: `.omo/plans/opencode-brain.md` line 478. Companion to
`live-run.md` (attempt 3), which holds the six scenarios, the session proof and
the D1–D15 verdict table. The earlier failure cases are `qa/live-run-fail.md`
(attempt 1) and `qa/live-run-postfix-fail.md` (attempt 2); they are left exactly
as measured.

Nothing was changed in the repository, in `config/`, in `~/.r2d2/opencode/` or
in `tests/` to produce this file. Every number was measured on this machine
against `opencode serve` **v1.18.32** on `127.0.0.1:4599` and R2D2 on
`127.0.0.1:8099`, both started by hand; the server was killed and restarted by
hand inside this file and both processes are stopped now
(`ss -ltnp` shows 4599 and 8099 free).

## Verdict

**PASS, with D12 recurring exactly as attempt 2 recorded it.** With the server
down the response is still valid Alice JSON, the process stays up, `/health`
reports `opencode.reachable: false`, and after the restart the next question
reuses the **same** `session_id` with no new session. The one question
immediately after the restart still returns `ERROR_TEXT` against a healthy
server, for the same reason as in attempt 2 — reported below as the known open
defect it is, not as a new one.

## Environment

Identical to `live-run.md`: `application_id` `t21-v3`, `session_id`
**`ses_f235aebaeffeTZp7uX1Mp7FNe3`**, `R2D2_TG_APPLICATION_ID='424242=t21-v3'`
exported into the R2D2 process only, `.env` and `.env.oc` sourced, `.env` not
edited. Secrets are quoted as `bot<REDACTED>`; the raw log stays in
`/tmp/r2d2-qa/t21c/` and is not committed.

---

## 1 — the server is stopped

```
$ date -Is
2026-09-26T12:53:14+05:00
$ kill 2412340            # the opencode serve this run started
$ ss -ltnp | grep ':4599 ' || echo "4599 free"
4599 free
```

R2D2 was not restarted and nothing about it was reconfigured.

## 2 — `GET /health` reports the degradation in a field

```
$ curl -s http://127.0.0.1:8099/health -w '\nhttp=%{http_code} time_total=%{time_total}\n'
{"status":"ok","opencode":{"reachable":false,"version":null,"base_url":"http://127.0.0.1:4599"},
 "sessions":9,"chain":["zen","yandexgpt","openrouter"]}
http=200 time_total=0.034904
```

`status` stays `ok` — the process is serving — while `opencode.reachable` is
`false` and `version` is `null`. No credential appears in the body.

## 3 — the plain question, repeated with the brain offline

Request (the same shape as scenario 2):

```json
{"meta":{"interfaces":[{"type":"Voice"}],"lang":"ru-RU"},
 "request":{"type":"SimpleUtterance","command":"Назови столицу Франции. Одно слово."},
 "session":{"new":false,"skill_id":"","session_id":"",
            "application":{"application_id":"t21-v3","type":"Alice"},"user":{"user_id":""}},
 "version":"1.0"}
```

```
{"response":{"text":"Что-то пошло не так. Попробуй ещё раз.",
             "tts":"Что-то пошло не так. Попробуй ещё раз.","end_session":false},"version":"1.0"}
http=200 time_total=2.152615
```

Valid Alice JSON, inside the 4.5 s budget, with `response.text`, `response.tts`
and `version: "1.0"` present. The process stayed up and answered `/health` again
0.1 s later:

```
{"status":"ok","opencode":{"reachable":false,"version":null,"base_url":"http://127.0.0.1:4599"}, …}
http=200 time_total=0.044204
```

What the log says the turn did:

```
12:53:19.317 WARNING core.backends.registry backend 'zen' … dropped from the chain, 'api_key' is empty
12:53:19.317 WARNING core.backends.registry backend 'yandexgpt' … dropped from the chain, 'api_key' is empty
12:53:20.893 INFO  httpx HTTP Request: POST https://openrouter.ai/api/v1/chat/completions "HTTP/1.1 429 Too Many Requests"
12:53:20.895 WARNING core.backends.openai_compatible backend openrouter: HTTP 429, retrying once (attempt 2 of 2) after 0.4s
12:53:21.435 INFO  httpx HTTP Request: POST https://openrouter.ai/api/v1/chat/completions "HTTP/1.1 429 Too Many Requests"
12:53:21.438 WARNING r2d2 LLM backend openrouter failed: BackendStatusError('backend \'openrouter\': HTTP 429
                ("error":{"message":"Rate limit exceeded: free-models-per-day. Add 10 credits to unlock 1000 free
                model requests per day","code":429,"metadata":{"headers":{"X-RateLimit-Limit":"50",
                "X-RateLimit-Remaining":"0","X-RateLimit-Reset":"1790467200"}})
12:53:21.439 ERROR r2d2 handle failed
12:53:21.444 INFO  r2d2 turn route= path=error model= agent= llm_ms=2124 total_ms=2149 escalated=False permission_asked=False msgs=0 tools=()
```

**D8, re-measured:** the chain has a working *member* — `openrouter` is built and
called, with one retry — and it is rate-limited today. The free daily cap is
50 requests, `X-RateLimit-Remaining: 0`, reset at `1790467200` =
**2026-09-27T00:00:00Z**. `zen` and `yandexgpt` are dropped with an explicit
WARNING for an empty `api_key`, which is the same state attempt 2 measured. The
plan's failure-case acceptance — valid Alice JSON through the chain, process up
— is met, and it is met by a graceful `ERROR_TEXT` rather than by an answer.

The client also noticed the server was gone, with a typed error rather than a
traceback, and every session reader backed off on its ladder:

```
12:53:21.606 WARNING core.opencode.client opencode: http://127.0.0.1:4599 is not usable: All connection attempts failed
12:53:23.213 WARNING core.opencode.sse opencode sse: stream for session ses_f235aebaeffeTZp7uX1Mp7FNe3 failed (ConnectError: All connection attempts failed), reconnecting in 5.0s
   … one line per bound session, nine in all …
```

That is the reconnect ladder doing its job for a **down** server, which is a
different situation from D1's live server answering slower than the read bound:
across the 16 minutes the server was up, the same readers recorded **0** failures
and caught **5 of 5** permission asks (`live-run.md`, D1).

---

## 4 — the server is restarted

```
$ date -Is
2026-09-26T12:53:40+05:00
$ bash scripts/opencode_serve.sh &     # fresh process, same launcher, same config
$ ss -ltnp | grep ':4599 '
LISTEN 0  512  127.0.0.1:4599  0.0.0.0:*  users:(("opencode",pid=2445826,fd=17))
```

### 4a — the first question after the restart: D12, again

```
$ curl -s …/webhook -d '… "command":"Сколько будет два плюс два? Ответь коротко." …'
{"response":{"text":"Что-то пошло не так. Попробуй ещё раз.", …},"version":"1.0"}
http=200 time_total=4.220720
```

```
12:53:40.686 INFO  httpx HTTP Request: GET http://127.0.0.1:4599/global/health "HTTP/1.1 200 OK"
12:53:43.901 WARNING r2d2 opencode 'opencode' could not answer t21-v3; the turn falls back to the chain: ReadTimeout('')
12:53:44.886 INFO  r2d2 turn route= path=error model= agent=r2d2-voice llm_ms=970 total_ms=4217 escalated=False permission_asked=False msgs=0 tools=()
```

**This is D12, unchanged and not fixed.** The server answered `/global/health`
200 eight seconds earlier and is healthy throughout; the call that failed is the
session re-verification `GET /session` (the freshly-restarted server rebuilding
its index over 203 sessions), and it surfaced as a bare `httpx.ReadTimeout`
instead of the typed `OpencodeDeadlineExceeded` that `SessionRoute.turn` catches.
So the turn fell through to a chain with no working member and returned
`ERROR_TEXT` at 4.22 s. Reported as the known open defect from
`qa/live-run-postfix.md`, unchanged by the eleven fixes, with the same
reproduction and the same log line as attempt 2.

### 4b — the next question reuses the same session

```
$ curl -s …/webhook -d '… "command":"Сколько будет два плюс два? Ответь коротко." …'
{"response":{"text":"Проверяю, пришлю в телеграм.", …},"version":"1.0"}
http=200 time_total=3.454140

12:54:04.058 INFO r2d2 turn route=opencode path=deadline model=opencode/space-bunny-free agent=r2d2-voice llm_ms=3248 total_ms=3450 escalated=False permission_asked=False msgs=1 tools=()
```

and the counts that prove no new session was minted:

| check | value |
|---|---|
| `POST /session` issued by R2D2, whole run including the restart | **3** — one per `application_id`, at 12:36:41, 12:46:11, 12:49:25; **none after 12:49:25** |
| `opencode session store: … -> new session` lines in the log | **3**, the same three |
| `GET /session?directory=/home/<user>/r2d2-workspace&limit=1000` after the restart | **12** — the 9 that existed before this run, plus one per id used |
| sessions titled `r2d2:alice:t21-v3` | **1** — `ses_f235aebaeffeTZp7uX1Mp7FNe3` |
| the post-restart turn's `route=opencode … agent=r2d2-voice` | the same `ses_f235aebaeffeTZp7uX1Mp7FNe3` |
| the answer to that turn | job `3e1b1eec4ba5`, `done` 19.2 s later, result `4`, delivered to Telegram (`sendMessage` 200 at 12:54:23.687) |

The `db/sessions.db` binding `t21-v3 → ses_f235aebaeffeTZp7uX1Mp7FNe3` was never
rewritten, and the restart did not cost the human their session.

---

## What was left behind

* `opencode serve` on 4599 — **stopped**. `ss -ltnp` shows the port free.
* R2D2 on 8099 — **stopped**. `ss -ltnp` shows the port free.
* The independent `curl -N /event` tap — **stopped**.
* The `curlrc` that held the opencode server password — **deleted**.
* The owner's own `opencode serve --port 45512` (pid 235191) — **untouched**,
  still listening, never signalled.
* `~/.config/opencode/` — never written.
* `.env`, `.env.oc` — never edited.
* The 8 stale attempt-1 sessions, attempt 2's `t21-final`, the stale
  `tg:424242` row and all five of their `db/sessions.db` rows — **all still
  present, untouched**, reattached as event readers at the next startup and not
  written to. Exact state in `live-run.md` §"the user's pre-existing sessions".
* This run's own three sessions — **left in place**:
  `r2d2:alice:t21-v3` = `ses_f235aebaeffeTZp7uX1Mp7FNe3`,
  `r2d2:alice:t21-v3b` = `ses_f2352390dffe0vWH4DiDYI6xTJ`,
  `r2d2:alice:t21-v3c` = `ses_f234f4446ffeeQin8GAOllNEpT`.
* The Telegram chat 424242 — **22 messages** from this run between 12:36:59
  and 12:54:23, every one of them a `sendMessage` answered `HTTP/1.1 200 OK`
  (timestamps in `/tmp/r2d2-qa/t21c/uvicorn.log`). They are **10 collector
  deliveries** — one per `opencode_reply` job that closed with a result; the
  eleventh job closed `error` and sent nothing — plus **12** from the broker and
  from `/tg/webhook`: a question per ask, the notices for the two refusals, and
  the reply each of the three `да`s sent back into the chat. `live-run.md` §D4
  accounts for the collector half and shows that none of them contained a raw
  `[[NEEDS_AGENT]]`. So the chat holds, from this run: three agent answers
  (2 + 2, the `ls -la` report, the `4`), the D6 echo, the laptop refusal, the
  arxiv refusal, the D9 probe's interim line, the arxiv interim line, the
  `да`-turned-question answer, four permission questions, two refusal notices
  and three `да` receipts. Deleting them is the owner's call, not mine.

## The suite, with both servers stopped

```
$ .venv/bin/python -m pytest tests/ -q
932 passed, 1 skipped in 73.28s (0:01:13)
```

Run after the shutdown, because `qa/live-run-postfix.md` §D12 records that the
in-repo suite cannot pass while a live `opencode serve` holds 4599 — the real
server answers one test's fake-server traffic with `401`. Nothing was skipped or
deselected to reach that number.
