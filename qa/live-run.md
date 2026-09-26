# T21 — live end-to-end proof against a real `opencode serve`

> **This file records the state BEFORE the fixes.** Every number, verdict and
> finding below is what the run measured, and they are left exactly as measured —
> that is the value of the document. The eight defects it found (D1–D8) have since
> been fixed and committed, so nothing below describes the code as it stands now:
>
> | Defect | Fixing commit |
> |---|---|
> | D1 — SSE read timeout is the voice deadline | `ac13620` |
> | D2, D5 — escalation never collected, slow turn dropped | `4bebbcf` |
> | D3 — `/tg/webhook` cannot answer an Alice-side ask | `91d2c4f` |
> | D4 — raw sentinel delivered to Telegram | `636d327` |
> | D6, D7 — sentinel poisons the session, foreign tools unguarded | `1c89380` |
> | D8 — fallback chain has no working member | `86e8922` |
>
> Two method names moved in the later refactors, so a pointer below can send you
> to a file rather than to the old `Brain`: `_opencode_turn` is now
> `core/session_route.py:turn`, and `_collect_later` is now
> `core/session_collector.py:_collect_later`, reached through
> `SessionCollector.hand_to_agent`.

Plan: `.omo/plans/opencode-brain.md` lines 472–479. Nothing in the repository was
modified to produce this file; every number below was measured on this machine
against `opencode serve` **v1.18.32** on `127.0.0.1:4599` and R2D2 on
`127.0.0.1:8099`, both started by hand for this run.

## Verdict

**2 PASS, 4 FAIL.** The two failures are not flakes: each one is a reproducible
defect with a measured root cause, listed in [Defects](#defects) below with the
raw evidence. Nothing was patched — T21 is a proof, so the code is untouched and
`tests/` is still green.

| # | Scenario | Duration (wall) | Result |
|---|---|---|---|
| 1 | `GET /health` | 0.045 s | **PASS** |
| 2 | plain question through `/webhook` | 0.323 s cold / 3.148 s warm / 2.085 s warm | **PASS** |
| 3 | escalation to Telegram | 2.966 s | **FAIL** — ack correct, nothing ever reaches Telegram (D2) |
| 4 | permission ask → `да` → `{"response":"once"}` | 0.916 s (answer half) | **FAIL** — SSE reader missed 7 of 7 asks (D1); `/tg/webhook` cannot answer an Alice-side ask (D3) |
| 5 | laptop control via `r2d2_do open-app` | 0.245–3.553 s (never executed) | **FAIL** — the voice agent never calls the shim (D5) |
| 6 | arxiv digest | 0.270 s (ack only) | **FAIL** — the agent researches but no digest is delivered (D2) |

`grep -c "PASS" /tmp/r2d2-qa/live-run.md` returns a number that has nothing to do
with the score — it counts every line mentioning the word, including prose and the
per-scenario verdicts, and is 7 as written. The score is the table above: of the
plan's six scenarios, **2 pass and 4 fail**. The plan's acceptance target of
`grep -c "PASS" == 6` is **not met**, and no line in this file was written to make
it look met.

## Environment

| | |
|---|---|
| opencode | `/home/koluchiy/.opencode/bin/opencode` v1.18.32, `scripts/opencode_serve.sh`, `127.0.0.1:4599` |
| opencode config | `~/.r2d2/opencode/opencode.json` (0600), byte-identical to `config/opencode/r2d2.opencode.json` |
| CLI shim | `~/.r2d2/r2d2_do.py` (0755), byte-identical to `opencode/r2d2_cli/r2d2_do.py` |
| R2D2 | `.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8099`, `.env` + `.env.oc` sourced |
| workspace | `/home/koluchiy/r2d2-workspace` (empty) |
| startup line | `R2D2 started, db=.../db/sessions.db, opencode=wired, models=opencode/space-bunny-free, fallback=['openrouter']` |
| secrets | never printed. The one place a bot token appears in a raw log is quoted as `bot<REDACTED>`. `~/.config/opencode/` was never written. |

`r2d2-voice` and `r2d2-agent` are both on `opencode/space-bunny-free` — the only
usable model on this machine (C1). The strong-model gap is a known, accepted
limitation and is not counted as a failure here.

### Baseline, before any R2D2 traffic

```
GET /global/health                -> {"healthy":true,"version":"1.18.32"}
GET /session?limit=1000           -> 194 sessions      (the DEFAULT limit is 100 and
                                                            silently truncates; the
                                                            `limit` parameter is required
                                                            for an honest count)
GET /session?directory=/home/koluchiy/r2d2-workspace -> 0 sessions
titles starting with "r2d2:"      -> none
```

---

## Scenario 1 — `GET /health`

Request:

```
GET http://127.0.0.1:8099/health
```

Response (`http=200 time_total=0.045206`):

```json
{
  "status": "ok",
  "opencode": {"reachable": true, "version": "1.18.32", "base_url": "http://127.0.0.1:4599"},
  "sessions": 0,
  "chain": ["zen", "yandexgpt", "openrouter"]
}
```

`GET /diagnostics/providers` (`http=200 time_total=0.749243`) returns 20 agents
including `r2d2-voice` and `r2d2-agent`, confirming C2: the R2D2 agents are
installed and the foreign global agents are still visible alongside them.

**PASS** — the endpoint answers in 45 ms, reports the live server version, and
leaks no credential (the only value-shaped fields are the version and the base URL).

---

## Scenario 2 — a plain question through `/webhook`

`application_id` = **`t21-live-proof`**.

### 2a — cold session (C8: the first turn of a new session must not be waited on)

Request body (`session.new = false`, a real Alice `SimpleUtterance`):

```json
{"meta":{"interfaces":[{"type":"Voice"}]},
 "request":{"type":"SimpleUtterance","command":"Что такое квантовые точки? Ответь одним-двумя предложениями."},
 "session":{"new":false,"skill_id":"","application":{"application_id":"t21-live-proof"},"user":{"user_id":""}},
 "version":"1.0"}
```

Response (`http=200 time_total=0.323419`):

```json
{"response":{"text":"Проверяю, пришлю в телеграм.","tts":"Проверяю, пришлю в телеграм.","end_session":false},"version":"1.0"}
```

Server-side turn record:

```
turn route=opencode path=escalate model=opencode/space-bunny-free agent=r2d2-agent llm_ms=0 total_ms=317 escalated=True permission_asked=False msgs=1 tools=()
```

The session was created once and the agent turn was submitted, not waited on.
The agent's own answer appeared in the session 14.8 s later
(`created=1790386488580 → completed=1790386503415`) — the cold-cache cost C8
predicts, paid on the server rather than inside Alice's 4.5 s.

### 2b / 2c — warm session, the fast voice path

| command | wall | `llm_ms` | `total_ms` | spoken answer |
|---|---|---|---|---|
| `Сколько будет два плюс два? Ответь коротко.` | **3.148 s** | 3092 | 3144 | `Четыре.` |
| `Назови столицу Франции. Одно слово.` | **2.085 s** | 2029 | 2081 | `Париж.` |

Both under `R2D2_FAST_DEADLINE = 3.2 s`, both real answers from `r2d2-voice`, both
`path=voice` with `escalated=False`. The second sample is inside the p50 the
contract measured (1.667 s was p50 over 10 samples; 2.03 s here is a slower but
same-order draw).

**PASS** — a warm question is answered in under the deadline, in a valid Alice
payload, with the text the model actually produced.

---

## Scenario 3 — a question that must escalate

`application_id` = `t21-live-proof`, warm session.

Request command: `Исследуй рабочий каталог и составь подробный отчёт: какие там файлы, что каждый делает и как они связаны.`

Response (`http=200 time_total=2.965581`):

```json
{"response":{"text":"Проверяю, пришлю в телеграм.","tts":"Проверяю, пришлю в телеграм.","end_session":false},"version":"1.0"}
```

```
turn route=opencode path=escalate model=opencode/space-bunny-free agent=r2d2-agent llm_ms=2741 total_ms=2961 escalated=True permission_asked=False msgs=1 tools=()
```

The ack half is correct and the sentinel never reached Alice. The session shows
exactly what the design promises — one conversation, two agents, the protocol
marker visible only inside the session:

```
* user      r2d2-voice      Исследуй рабочий каталог и составь подробный отчёт…
* assistant r2d2-voice      [[NEEDS_AGENT]]
                           Исследовать рабочий каталог /home/koluchiy/r2d2-workspace…
* user      r2d2-agent      Пользователь попросил голосом: Исследуй рабочий каталог…
* assistant r2d2-agent      (tool calls: bash "ls -la …", bash "du -sh …")
```

**FAIL — the Telegram half.** The agent produced a report, and it was never
delivered: 45 s after the ack, `grep -c "api.telegram.org" uvicorn.log` was `0`.
The reason is in the code, not in the run: `Brain._opencode_turn` (now
`core/session_route.py:turn`) enqueues an `opencode_reply` collector job **only**
on the `OpencodeDeadlineExceeded` branch.
Both escalation branches — C8 (cold session) and the `[[NEEDS_AGENT]]` branch —
call `submit_task()` and return the ack, and then nobody ever reads the answer
back out of the session. See [D2](#d2--the-escalation-path-has-no-collector).

What *did* reach Telegram during this run were 11 messages containing the raw
protocol marker, because the deadline branch's collector has no sentinel guard of
its own:

```
job 777ca88c0741  opencode_reply  done  [[NEEDS_AGENT]]
                                       Выполнить в терминале `ls -la /tmp` и пересказать содержимое каталога.
```

---

## Scenario 4 — permission ask → `да` → `{"response":"once"}`

### 4a — the ask is raised by opencode and is visible on the wire

An always-connected independent SSE tap (`curl -N /event`, started by me, not by
R2D2) recorded every `permission.asked` the server emitted:

```json
{"id":"evt_0db5bb1a4001…","type":"permission.asked","properties":{
  "id":"per_0db5bb1a3001KW927GRy6hQa7a","sessionID":"ses_f24a63be4ffeqdiwwhyYaF84oN",
  "permission":"bash","patterns":["ls -la /home/koluchiy/r2d2-workspace 2>&1","head -50"],
  "metadata":{"command":"ls -la /home/koluchiy/r2d2-workspace 2>&1 | head -50"},
  "always":["ls *","head *"],"tool":{"messageID":"msg_0db5ba43d001…","callID":"call_function_66d838q7cm12_1"}}}
```

**R2D2's own reader never saw it.** `grep -c "opencode sse: permission.asked"`
in R2D2's log stayed at **0** for the whole run, while the tap counted **7**
distinct `permission.asked` events across five different sessions. R2D2's log
shows why, continuously:

```
06:38:08 GET  http://127.0.0.1:4599/event?directory=%2Fhome%2Fkoluchiy%2Fr2d2-workspace  "HTTP/1.1 200 OK"
06:38:11 opencode sse: stream for session ses_f24a63… failed (ReadTimeout: ), reconnecting in 5.0s
06:38:16 GET  http://127.0.0.1:4599/event…                                        "HTTP/1.1 200 OK"
06:38:20 opencode sse: stream for session ses_f24a63… failed (ReadTimeout: ), reconnecting in 5.0s
```

Root cause, measured: `core/opencode/sse.py` builds its client with
`httpx.AsyncClient(timeout=self._timeout)` where `self._timeout = spec.timeout`,
and `config/backends.json` sets that spec timeout to **3.2 s** — the *voice
deadline*. opencode's `server.heartbeat` interval on this build is **10.0 s**
(measured with a 40 s timestamped tap: gaps `[10.0, 10.0]`). A reader whose read
timeout is shorter than the heartbeat can never survive to the next one, so it
runs a 3.2 s connect / 5.0 s sleep ladder and is blind ~61 % of the time. See
[D1](#d1--the-sse-readers-read-timeout-is-the-voice-deadline).

Consequence on the wire: the tool call stays `running` until somebody answers. It
never is, and R2D2 does not know it exists, so the turn hangs.

### 4b — the broker's own path, driven against the real server

Because D1 makes discovery unreliable, the *answering* half of the broker was
driven directly, using R2D2's own shipped modules and the real `opencode serve`,
for an ask id that came off the wire:

```
$ PYTHONPATH=. .venv/bin/python /tmp/r2d2-qa/t21/broker_probe.py \
      ses_f2476c802ffeSn85bllSZdVwoK per_0db894626001z8fUwVQbBF9440 t21-live-proof-d
```

```
PROBE opencode reachable=True version=1.18.32
INFO core.permissions opencode permissions: t21-live-proof-d must confirm
     'ls -la /tmp; du -sh /tmp; find /tmp -type f | wc -l'; a durable grant (always)
     would cover ls *, du *, find *, wc *, head * for the rest of the session and is never sent
INFO httpx HTTP Request: POST https://api.telegram.org/bot<REDACTED>/sendMessage "HTTP/1.1 200 OK"
PROBE pending_actions row = {"kind": "opencode_permission", "session_id": "ses_f2476c80…",
     "permission_id": "per_0db894626001z8fUwVQbBF9440", "title": "ls -la /tmp; du -sh /tmp; …",
     "always": ["ls *","du *","find *","wc *","head *"], "requested_at": 1790389711.564}
PROBE ---- resolve_from_text('да') ----
INFO httpx HTTP Request: POST http://127.0.0.1:4599/session/ses_f2476c80…/permissions/per_0db894626001z8fUwVQbBF9440?directory=%2Fhome%2Fkoluchiy%2Fr2d2-workspace  "HTTP/1.1 200 OK"
INFO httpx HTTP Request: POST https://api.telegram.org/bot<REDACTED>/sendMessage "HTTP/1.1 200 OK"
PROBE verdict='approved' pending_row_after=None
```

And then the **same thing through the live `/webhook`**, with the Alice
`application_id`, for a second real ask:

```
$ curl -s http://127.0.0.1:8099/webhook -d '{"request":{"command":"да"}, … "application_id":"t21-live-proof-d" …}'
{"response":{"text":"Принято, выполняю.","tts":"Принято, выполняю.","end_session":false},"version":"1.0"}  http=200 time=0.915244

INFO httpx HTTP Request: POST http://127.0.0.1:4599/session/ses_f2476c80…/permissions/per_0db894633001x3LN8YY7JNX9Km?directory=…  "HTTP/1.1 200 OK"
INFO httpx HTTP Request: POST https://api.telegram.org/bot<REDACTED>/sendMessage "HTTP/1.1 200 OK"
```

The independent tap recorded what the **server** accepted:

```json
{"type":"permission.replied","properties":{"sessionID":"ses_f2476c802ffeSn85bllSZdVwoK","requestID":"per_0db894626001z8fUwVQbBF9440","reply":"once"}}
{"type":"permission.replied","properties":{"sessionID":"ses_f2476c802ffeSn85bllSZdVwoK","requestID":"per_0db894633001x3LN8YY7JNX9Km","reply":"once"}}
```

* the Telegram **confirmation question really was delivered** — the
  `sendMessage` call returned HTTP 200 on the real Bot API, twice per ask
  (the question, then `Принято, выполняю.`);
* the answer on the wire was **`"once"`**, twice;
* across the whole run, **6** `permission.replied` events were observed and
  **0** of them carried `"reply":"always"`. `always` is unrepresentable in the
  types (`PermissionAnswer = Literal["once","reject"]`, plus
  `assert ANSWERS == frozenset({"once","reject"}) and DURABLE_GRANT not in ANSWERS`),
  and the wire agrees.

### 4c — `да` through `/tg/webhook` does **not** answer it

```
$ curl -s http://127.0.0.1:8099/tg/webhook -d '{"message":{"chat":{"id":1087136471},"text":"да"}}'
{"ok":true}  http=200 time=0.876844
```

```
sqlite> select application_id, action_json from pending_actions;
t21-live-proof-d | {"kind":"opencode_permission", "permission_id":"per_0db894633001x3LN8YY7JNX9Km", …}
```

The pending row is **still there** — the answer went nowhere. R2D2's log for that
turn:

```
07:28:56,791 core.opencode.session_store tg:1087136471 -> new session ses_f2474aac4ffeNnPg1OMFCtgQF5 (r2d2:alice:tg:1087136471)
07:28:56,907 turn route=opencode path=escalate … agent=r2d2-agent llm_ms=0 total_ms=160 escalated=True
07:28:57,616 httpx HTTP Request: POST https://api.telegram.org/bot<REDACTED>/sendMessage "HTTP/1.1 200 OK"
```

`app/main.py:203` builds the Telegram turn with
`"application_id": f"tg:{chat_id}"`, while `PermissionBroker` stores the ask under
the **Alice** `application_id` the session reader was created with. The two keys
can never meet, so a `да` typed in Telegram is answered as an ordinary question
and a **new** opencode session is created for it. See
[D3](#d3--tg-webhook-cannot-answer-an-alice-side-permission-ask).

### 4d — the known substring bug, confirmed live

`core/policies.confirmation_verdict` matches an affirmative as a *substring*.
A message that merely contains `да` — `дай сводку` — is read as an approval:

```
PROBE ---- a second ask, answered with the substring probe 'дай сводку' ----
INFO core.permissions opencode permissions: t21-live-proof-d must confirm 'find /tmp -type f | wc -l' …
INFO httpx HTTP Request: POST http://127.0.0.1:4599/session/ses_f2476c80…/permissions/per_0db894626001z8fUwVQbBF9440?directory=…  "HTTP/1.1 404 Not Found"
WARNING core.permissions opencode permissions: the server took no answer for the ask per_0db894626001z8fUwVQbBF9440 …
PROBE verdict_for_'дай сводку'='rejected' pending_row_after=None
```

The broker took `дай сводку` as **"yes"** and posted a one-time approval for a
pending risky command. The 404 here is incidental (that permission id had already
been consumed in 4b); the finding is the `verdict == "yes"` branch being taken
for a sentence that is not an answer. `DENY_WORDS` is checked first, so a message
containing both — `да, не надо` — is read as a refusal, which is the intended
direction; the affirmative side has no such guard.

**FAIL** — the end-to-end path is broken by D1 (nothing asks) and by D3 (the
Telegram answer cannot arrive). The broker's answering half, the live Telegram
question and the `"once"` on the wire are all proven above.

---

## Scenario 5 — laptop control via `r2d2_do open-app`

Six attempts across three sessions, phrased as `Открой браузер…`, then
`…командой r2d2_do open-app browser`, then the literal absolute command line.
Representative responses:

| # | `application_id` | command | wall | turn record | shim invoked |
|---|---|---|---|---|---|
| 1 | `t21-live-proof-e` | Открой браузер на ноутбуке. | 0.750 s | `path=escalate llm_ms=0 total_ms=746 agent=r2d2-agent` | no |
| 2 | `t21-live-proof-e` | Открой браузер. Используй инструмент r2d2_do с подкомандой open-app и аргументом browser. | 3.530 s | `path=deadline llm_ms=3273 total_ms=3456` | no |
| 3 | `t21-live-proof-e` | Открой браузер. Тебе нужно выполнить в терминале ровно эту команду: `/home/koluchiy/.r2d2/r2d2_do.py open-app browser` | 3.444 s | `path=deadline llm_ms=3251` | no |
| 4 | `t21-live-proof-f` | Открой браузер на ноутбуке командой r2d2_do open-app browser. | 0.245 s | `path=escalate llm_ms=0 total_ms=240 agent=r2d2-agent` | no |
| 5 | `t21-live-proof-e` | запусти r2d2_do open-app browser | 3.413 s | `path=deadline llm_ms=3273` | no |
| 6 | `t21-live-proof-e` | r2d2_do open-app browser | 3.315 s | `path=deadline llm_ms=3229` | no |

Every `r2d2-voice` turn answered with the marker and no tool call:

```
* assistant r2d2-voice   [[NEEDS_AGENT]] Выполнить в терминале r2d2_do open-app browser — открыть браузер на ноутбуке.
* assistant r2d2-voice   [[NEEDS_AGENT]] Запустить в терминале команду /home/koluchiy/.r2d2/r2d2_do.py open-app browser, чтобы открыть браузер на ноутбуке.
```

`r2d2-voice` has `bash: {"*": "deny", "/home/koluchiy/.r2d2/r2d2_do.py *": "allow", …}`,
so the shim *is* reachable from the voice agent — the model simply never emits the
call. And when the voice turn overruns 3.2 s the brain takes the `deadline` branch,
which acknowledges and hands the turn to a collector but **never** calls
`submit_task`, so the agent never sees the request at all. Attempts 1 and 4
(virgin sessions, C8) did reach `r2d2-agent`, and the agent reached for a foreign
plugin tool instead:

```
* assistant r2d2-agent   Проверю, доступен ли мне инструмент `r2d2_do` — в моём списке инструментов его нет…
   TOOL list_mcp_resources           completed
* assistant r2d2-agent   MCP-сервера `r2d2` тоже нет. Проверю, существует ли `r2d2_do` как CLI-команда…
   TOOL bash {"command":"which r2d2_do r2d2 r2d2-do 2>/dev/null; …"}   running   ← permission.asked, missed (D1)
```

That last one is also a security observation: `list_mcp_resources`,
`list_mcp_resource_templates` and `chrome-devtools_list_pages` executed under
`r2d2-voice`, whose `permission` block is `"*": "deny"`. opencode's `*` key does
not cover plugin/MCP tools, so a voice agent documented as unable to change
anything on the machine can call the user's global plugin toolchain. See
[D7](#d7--plugin-and-mcp-tools-bypass-the-agents-permission-block).

The shim itself is fine. Run directly — the exact command the agent is
allowlisted to run:

```
$ /home/koluchiy/.r2d2/r2d2_do.py open-app browser
{"ok": true, "text": "Открываю browser.", "needs_confirm": false, "pending": null, "error": null}
exit=0   wall=0.926844954 s
$ pgrep -a -f xdg-open
/bin/sh /usr/bin/xdg-open https://ya.ru          ← launched by that call
```

**FAIL** — the tool works and the permission matrix would allow it, but nothing
in the shipped prompt chain makes either agent emit the call, and the deadline
branch silently drops the request on the floor.

---

## Scenario 6 — arxiv digest

`application_id` = `t21-live-proof-g` (virgin, so C8 routes straight to the
agent).

Request command: `Сделай сводку статей с arxiv по теме RAG за последние 7 дней.`

Response (`http=200 time_total=0.269741`):

```json
{"response":{"text":"Проверяю, пришлю в телеграм.","tts":"Проверяю, пришлю в телеграм.","end_session":false},"version":"1.0"}
```

```
turn route=opencode path=escalate model=opencode/space-bunny-free agent=r2d2-agent llm_ms=0 total_ms=251 escalated=True permission_asked=False msgs=1 tools=()
```

The agent did real work — it ignored `r2d2_do arxiv` and used the network tools
`r2d2-agent` is allowed to have:

```
* assistant r2d2-agent  (completed) webfetch http://export.arxiv.org/api/query?search_query=abs:%22retrieval-augmented%22&sortBy=submittedDate&sortOrder=descending&max_results=60
   → <?xml version='1.0' …><feed …><id>https://arxiv.org/api/IL/zQlZ9dga13PpzrejGtDaKCVI</id> …
* assistant r2d2-agent  TOOL bash {"command":"date -u && date"}   running   ← permission.asked, missed (D1)
```

`date -u && date` is not the allowlist entry `date`, so the ask was raised and lost
to D1; the turn is still blocked, and no digest was produced or delivered.

Two further observations, both from the code and both consistent with what the run
showed:

* the escalation path has no collector (D2), so even a completed digest would
  never have been sent anywhere;
* `core/tools/arxiv_tool.summarize_entries` calls
  `build_chain(specs, chain)` with the **unfiltered** `chain.order`, which still
  contains `opencode` (`kind: opencode_session`). `build_backends` raises
  `BackendConfigError` for that kind without a wiring, so the summariser — and
  therefore the `arxiv` job and `r2d2_do arxiv` — cannot run at all while
  `R2D2_OC_PASSWORD` is set. `core/brain.py:_call_llm` filters the session kind
  out; `arxiv_tool` does not. That path was not reached in this run because the
  turn never got that far.

**FAIL** — the ack is correct, the research happened, and the user received
nothing.

---

## Session reuse — the invariant, and the count

### Exactly one session per `application_id`

`GET /session?limit=1000` after the run, filtered by title:

| `application_id` | `session_id` | sessions with that title |
|---|---|---|
| `t21-live-proof` | `ses_f24a63be4ffeqdiwwhyYaF84oN` | **1** |
| `t21-live-proof-b` | `ses_f2494066fffeRxu5Oij5MFxIm4` | **1** |
| `t21-live-proof-c` | `ses_f2484eb0affe6Be2mInbQtLeG1` | **1** |
| `t21-live-proof-d` | `ses_f2476c802ffeSn85bllSZdVwoK` | **1** |
| `t21-live-proof-e` | `ses_f2474346affewMG7hh3Sv7Q5eo` | **1** |
| `t21-live-proof-f` | `ses_f247037d9ffedbsLd6ngz3Qc5x` | **1** |
| `t21-live-proof-g` | `ses_f246dff9cffeIE03I13nHGSMMd` | **1** |
| `tg:1087136471` | `ses_f2474aac4ffeNnPg1OMFCtgQF5` | **1** |

Eight application ids, eight sessions, **zero** duplicates — across roughly 40
turns. `t21-live-proof` alone carried 12 counted turns and
`ses_f24a63be4ffeqdiwwhyYaF84oN` for every one of them, with `r2d2-voice` and
`r2d2-agent` messages interleaved in a single transcript.

A dedicated reuse test — three consecutive turns on one `application_id`,
counting `POST /session` on the opencode side:

```
POST /session count before reuse test = 5
reuse turn 1: 200 time=3.391932   turn route=opencode path=deadline  llm_ms=3233 total_ms=3388
reuse turn 2: 200 time=3.488203   turn route=opencode path=deadline  llm_ms=3236 total_ms=3482
reuse turn 3: 200 time=3.509354   turn route=opencode path=deadline  llm_ms=3255 total_ms=3506
POST /session count after 3 reuse turns = 5        ← zero new sessions
```

### The count delta

| | before | after | delta |
|---|---|---|---|
| `GET /session?limit=1000` (all projects) | **194** | **202** | **+8** |
| `GET /session?directory=/home/koluchiy/r2d2-workspace` | **0** | **8** | **+8** |
| sessions with an `r2d2:` title | 0 | 8 | +8 |
| **pre-existing sessions that disappeared** | — | — | **0** |

**The plan's `delta == 1` is not met: the measured delta is 8.** The reason is
mine, not the system's — I had to use eight distinct `application_id`s
(`t21-live-proof`, `-b`, `-c`, `-d`, `-e`, `-f`, `-g`, plus the `tg:1087136471`
that `/tg/webhook` derives for itself) because the first session became
unusable for later scenarios: see [D6](#d6--the-sentinel-poisons-the-shared-session).
The invariant the delta is a proxy for — *one session per user, created once,
reused for ever, and nobody else's sessions touched* — holds exactly: 8 ids, 8
sessions, 0 duplicates, 0 of the owner's 194 pre-existing sessions read, modified,
deleted or reparented. With a single `application_id` the delta would be 1; the
per-id reuse test above shows `POST /session` is issued once and never again.

---

## Defects

Nothing below was fixed. Each item is a reproducible observation with the evidence
that produced it.

### D1 — the SSE reader's read timeout is the voice deadline

`core/opencode/sse.py` line ~348: `httpx.AsyncClient(timeout=self._timeout)` with
`self._timeout = spec.timeout` = **3.2 s** from `config/backends.json`. Measured
`server.heartbeat` interval on opencode 1.18.32: **10.0 s**. The reader therefore
times out before every heartbeat, sleeps on the `1 → 2 → 4 → 5 s` ladder, and is
blind ~61 % of the time. **7 of 7** `permission.asked` events observed on the wire
by an independent always-connected tap were missed by R2D2
(`grep -c "opencode sse: permission.asked"` = 0 for the whole run). Impact: the
permission broker does not work in production, and any turn whose tool needs a
confirmation hangs until opencode gives up. The contract doc's own
`HEALTH_TIMEOUT_S` / `DEADLINE_GRACE_S` pattern shows the codebase already knows
that a liveness read and a bounded call need different bounds; the event stream
was given the wrong one.

### D2 — the escalation path has no collector

`core/brain.py:_opencode_turn` (now `core/session_route.py:turn`) enqueues an
`opencode_reply` job **only** in the `except OpencodeDeadlineExceeded` branch. The C8 branch and the
`decision.kind == "escalate"` branch both call `submit_task()` and return the ack,
and nothing ever reads the reply back out of the session. The `r2d2-agent` prompt
says "Твой финальный текст доставляется в телеграм" — a promise no code keeps.
This is why scenarios 3 and 6 deliver nothing. It also means the plan's
"escalation and permission scenarios" — the two features the whole two-path design
exists for — are the two that do not work.

### D3 — `/tg/webhook` cannot answer an Alice-side permission ask

`app/main.py:203` gives the Telegram turn `application_id = f"tg:{chat_id}"`;
`PermissionBroker` keys the pending row by the **Alice** `application_id`. A `да`
sent through `/tg/webhook` therefore finds no row, is treated as an ordinary
question, and additionally creates a *second* opencode session for the same human.
Measured: the pending row survived the `да` untouched, and
`tg:1087136471 -> new session ses_f2474aac4ffeNnPg1OMFCtgQF5` was created.

### D4 — the collector ships the raw escalation sentinel to Telegram

`SessionCollector._collect_later` → `Worker._opencode_reply` → `send_message`
has no sentinel guard. `sanitize_for_speech` and `Brain._speakable` protect Alice's `text`/`tts`
only. During this run **11** `opencode_reply` jobs delivered text beginning with
the literal `[[NEEDS_AGENT]]` to the owner's Telegram chat, e.g. job
`777ca88c0741`. The user has been sent eleven protocol markers.

### D5 — the voice agent never calls the shim, and the deadline branch drops the request

`space-bunny-free` answered all six laptop-control attempts with
`[[NEEDS_AGENT]]` and no tool call, although `r2d2_do.py *` is explicitly
allowlisted for `r2d2-voice`. Because those turns also overran 3.2 s, the brain
took the deadline branch, which does not call `submit_task`, so the request never
reached the agent either. The two branches together mean a slow voice turn loses
the request entirely. The shim is installed, correct and fast (0.93 s) — it is
simply never invoked.

### D6 — the sentinel poisons the shared session

One `[[NEEDS_AGENT]]` from `r2d2-voice` stays in the session transcript forever.
The agent, reading that same transcript, then started emitting the marker itself:

```
* assistant r2d2-agent  [[NEEDS_AGENT]] Просканировать /tmp, найти все файлы размером больше 50 МБ…
* assistant r2d2-agent  (reasoning) I keep replying [[NEEDS_AGENT]]. The user keeps repeating.
                        This is expected behavior per my design. I'll respond consistently again.
```

After that the session could not do any work at all: the agent produced no tool
calls, no report, and no answer, for every subsequent turn. `POST /session/:id/summarize`
returned `true` but did not compress the transcript (39 messages, all originals,
sentinel still present), so there is no recovery short of deleting the session.
One user utterance permanently disables the agent path for that user.

### D7 — plugin and MCP tools bypass the agents' permission block

Under `r2d2-voice`, whose `permission` block is `"*": "deny"`, these executed
without asking and without a denial: `list_mcp_resources`,
`list_mcp_resource_templates`, `chrome-devtools_list_pages`. opencode's `*` key
covers the built-in tool names, not plugin- or MCP-provided ones, so the
allowlist model documented in `docs/11-opencode-backend.md` §3 does not hold for
the 11 foreign agents and the MCP servers that C2 says are always visible. The
contract's ADOPTED line assumed strict rules would contain this; they do not.

### D8 — the fallback chain has no working member

`GET /health` reported `chain: ["zen","yandexgpt","openrouter"]` and the startup
line `fallback=['openrouter']`, because `zen` and `yandexgpt` are dropped with an
explicit WARNING for empty `api_key`. The one survivor then failed:

```
WARNING r2d2 LLM backend openrouter failed: BackendStatusError('backend 'openrouter':
        HTTP 404 ({"error":{"message":"This model is unavailable for free.
        The paid version is available now - use this slug i…
```

`config/backends.json` names `inclusionai/ling-3.0-flash:free`, which OpenRouter
no longer serves for free. So with no Zen key and no Yandex credentials the chain
has **no** usable backend, and the opencode-down path can only answer
`ERROR_TEXT`. This is what the failure case in `live-run-fail.md` measured, and
the plan accepts it — but the chain is nominally declared and nominally empty,
which reads like a working degradation and is not one.

---

## Reproduction

```bash
cd /home/koluchiy/Documents/R2_yandex_station
set -a; . ./.env; . ./.env.oc; set +a
bash scripts/opencode_serve.sh &                     # 127.0.0.1:4599
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8099 &
curl -s localhost:8099/health
curl -s localhost:8099/webhook -H 'Content-Type: application/json' -d '{
  "meta":{"interfaces":[{"type":"Voice"}]},
  "request":{"type":"SimpleUtterance","command":"Назови столицу Франции. Одно слово."},
  "session":{"new":false,"skill_id":"","application":{"application_id":"demo"},"user":{"user_id":""}},
  "version":"1.0"}'
```

Raw artefacts for this run live in `/tmp/r2d2-qa/t21/`: the three R2D2 logs
(`uvicorn.log`, `uvicorn2.log`, `uvicorn3.log` — **these contain the Telegram bot
token in httpx log lines and must never be committed**), the two opencode logs,
`curlrc` (0600, holds the server password — delete it), the raw and timestamped SSE
taps, `sessions-before.json` / `sessions-after.json` / `sessions-final.json`,
`broker-probe.log`, and every request/response body captured above.
