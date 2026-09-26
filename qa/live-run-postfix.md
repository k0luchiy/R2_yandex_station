# T21, second run — live end-to-end proof against a real `opencode serve`, after the D1–D8 fixes

Plan: `.omo/plans/opencode-brain.md` lines 472–479. This is the **second**
execution of todo 21. The first one is `qa/live-run.md` and
`qa/live-run-fail.md`; those two files are the pre-fix record and are left
exactly as measured — nothing below replaces them, and the D-numbering of the
new findings continues from theirs (D9–D12).

Nothing in the repository, in `config/`, in `~/.r2d2/opencode/` or in `tests/`
was changed to produce this file. Every number below was measured on this
machine against `opencode serve` **v1.18.32** on `127.0.0.1:4599` and R2D2 on
`127.0.0.1:8099`, both started by hand for this run and both stopped
afterwards.

## Verdict

**3 of the plan's six scenarios pass, 3 fail**, and the plan's acceptance target
of 6 is **not met**. No line here was written to make it look met. The plan's
check is `grep -c "PASS"`; on this file that returns **5**, of which exactly
**3** are scenario verdicts and 2 are this sentence and the one at the end that
reports the count.

| # | scenario | wall | verdict |
|---|---|---|---|
| 1 | `GET /health` | 0.057 s | pass |
| 2 | plain question through `/webhook` | 0.304 s cold / 3.397 s / **2.963 s** | pass (2.963 s < `r2d2_fast_deadline` 3.2 s) |
| 3 | escalation → Telegram | 3.317 s ack, result delivered 53.7 s later | pass |
| 4 | permission ask → `да` → `{"response":"once"}` | 6 attempts, 3.44–3.65 s each | **FAIL** — no ask was ever raised (D11, and D9) |
| 5 | laptop control via `r2d2_do open-app` | 3.242 s | **FAIL** — shim never invoked (D9) |
| 6 | arxiv digest | 3.649 s ack, 6.3 s collector | **FAIL** — no digest produced (D9) |

Three of the eight previously reported defects are demonstrably fixed by live
evidence (D1, D2, D4), two are fixed in the half that could be exercised and
still broken in the other (D5, D6), one is fixed in the part that does not need
a `permission.asked` (D3), one is confirmed fixed independently of this run
(D7), and one is rate-limited rather than absent (D8). **The permission feature
is nevertheless dead on opencode 1.18.32, for a reason neither run had found
before: D11.**

## Environment

| | |
|---|---|
| opencode | `/home/koluchiy/.opencode/bin/opencode` v1.18.32, `scripts/opencode_serve.sh`, `127.0.0.1:4599`, started fresh (no hot reload) |
| opencode config | `~/.r2d2/opencode/opencode.json` (0600), **byte-identical** to `config/opencode/r2d2.opencode.json`; carries `"*_*": "deny"`, `"todowrite": "deny"` and the rule-7 prompt |
| CLI shim | `~/.r2d2/r2d2_do.py` (0755), byte-identical to `opencode/r2d2_cli/r2d2_do.py` |
| R2D2 | `.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8099`, `.env` + `.env.oc` sourced, plus `R2D2_TG_APPLICATION_ID='1087136471=t21-final'` exported for the process only (`.env` untouched) |
| workspace | `/home/koluchiy/r2d2-workspace` |
| startup line | `R2D2 started, db=…/db/sessions.db, opencode=wired, models=opencode/space-bunny-free, fallback=['openrouter']` |
| `application_id` | **one**, `t21-final`, for every Alice turn and for the Telegram chat |
| `session_id` | **`ses_f23bb5dbeffePCgTNtG36cby0v`**, title `r2d2:alice:t21-final` |
| secrets | never printed. The one place the bot token appears (httpx INFO lines) is quoted as `bot<REDACTED>`; the raw logs live in `/tmp/r2d2-qa/t21b/` and are **not** committed |
| `~/.config/opencode/` | never written; the user's pre-existing opencode processes (5813, 5821, 24233) were never signalled |

---

## Scenario 1 — `GET /health`

```
$ curl -s http://127.0.0.1:8099/health -w '\nhttp=%{http_code} time_total=%{time_total}\n'
{"status": "ok", "opencode": {"reachable": true, "version": "1.18.32", "base_url": "http://127.0.0.1:4599"},
 "sessions": 5, "chain": ["zen", "yandexgpt", "openrouter"]}
http=200 time_total=0.057154
```

`sessions: 5` is the count of rows in `oc_sessions` — the five bindings the
first run left behind, all of them reattached at startup and none of them
touched. The only value-shaped fields are the version and the loopback URL; no
credential appears.

**PASS** — 57 ms, the live server version, no leak.

---

## Scenario 2 — a plain question through `/webhook`

### 2a — the first turn in a virgin session (C8)

```json
{"meta":{"interfaces":[{"type":"Voice"}],"lang":"ru-RU"},
 "request":{"type":"SimpleUtterance","command":"Что такое квантовые точки? Ответь одним-двумя предложениями.", …},
 "session":{"new":false,"skill_id":"","session_id":"","application":{"application_id":"t21-final","type":"Alice"},"user":{"user_id":""}},
 "version":"1.0"}
```

```
{"response":{"text":"Проверяю, пришлю в телеграм.","tts":"Проверяю, пришлю в телеграм.","end_session":false},"version":"1.0"}
http=200 time_total=0.303897

turn route=opencode path=escalate model=opencode/space-bunny-free agent=r2d2-agent llm_ms=0 total_ms=299 escalated=True …
opencode session store: t21-final -> new session ses_f23bb5dbeffePCgTNtG36cby0v (r2d2:alice:t21-final)
```

0.304 s, an ack, and the sentinel never reached Alice. The cold-cache cost C8
predicts was paid on the server: the agent's own answer landed in the session
**18.7 s** later and was delivered to Telegram (job `2d30f87cd51d`, below).

### 2b / 2c — warm session, the fast voice path

| command | wall | `llm_ms` | `total_ms` | path | spoken |
|---|---|---|---|---|---|
| `Назови столицу Франции. Одно слово.` | 3.397 s | 3245 | 3393 | `deadline` | ack (overran) |
| `Сколько будет два плюс два? Ответь коротко.` | **2.963 s** | 2898 | 2959 | `voice` | `Четыре.` |

The 2.963 s sample is under `r2d2_fast_deadline` = 3.2 s, is a real answer from
`r2d2-voice`, and is the same order as the contract's measured p50 of 1.667 s on
a slower draw. Over the whole run 6 of 19 turns took the `voice` path
(2.480–3.237 s of `total_ms`) and 9 took the `deadline` path.

The 2b sample is worth keeping: it is the D5 evidence. A voice turn that
overran 3.2 s was acknowledged **and** the request still reached the agent —
job `e62330f5f25f`, `done`, result `Париж | Четыре.`, delivered to Telegram. In
the first run that branch submitted nothing and the request was lost.

**PASS** — a warm question is answered inside the deadline, in a valid Alice
payload, with the text the model actually produced.

---

## Scenario 3 — a question that must escalate

`application_id` = `t21-final`, warm session.

Command: `Объясни подробно, как работает квантовый диод (светодиод) с точки зрения
физики полупроводников, и напиши развёрнутый технический обзор из пяти абзацев.`

```
{"response":{"text":"Проверяю, пришлю в телеграм.","tts":"Проверяю, пришлю в телеграм.","end_session":false},"version":"1.0"}
http=200 time_total=3.316558

10:52:50,921 POST …/session/ses_f23bb5dbeffePCgTNtG36cby0v/message            200
10:52:51,091 DELETE …/session/ses_f23bb5dbeffePCgTNtG36cby0v/message/msg_0dc45f7fd001ZeqtKCg9fTzm1E   200
10:52:51,180 POST …/session/ses_f23bb5dbeffePCgTNtG36cby0v/prompt_async        204
turn route=opencode path=escalate … agent=r2d2-agent llm_ms=2994 total_ms=3313 escalated=True
```

and 53.7 s after the ack:

```
10:53:44,892 POST https://api.telegram.org/bot<REDACTED>/sendMessage  "HTTP/1.1 200 OK"

sqlite> select job_id, status, result from jobs where job_id='16857a642ffe';
16857a642ffe | done | ## Квантовый диод (светодиод) с точки зрения физики полупроводников
                    **1. Зарядовое состояние и инжекция носителей.** …
```

The ack half is right, the sentinel never reached Alice, **and the result
reached Telegram** — which is exactly what the first run measured as D2
("45 s after the ack, zero `api.telegram.org` calls").

The `DELETE` at 10:52:51,091 is the D6 fix, visible on the wire: the voice
agent's routing signal is removed **before** `prompt_async`. My independent SSE
tap saw the matching event:

```json
{"id":"evt_0dc460383001bnyp1YO9b4Y08u","type":"message.removed",
 "properties":{"sessionID":"ses_f23bb5dbeffePCgTNtG36cby0v","messageID":"msg_0dc45f7fd001ZeqtKCg9fTzm1E"}}
```

**PASS** — the escalation is acknowledged, the agent does the work, and the
answer is delivered to Telegram.

---

## Scenario 4 — permission ask → `да` → `{"response":"once"}`

### 4a — no ask was ever raised

Six attempts, all in the same session, all returning the ack:

| # | command | wall |
|---|---|---|
| 1 | `Выполни в терминале ровно эту команду и перескажи её результат своими словами: ls -la /home/koluchiy/r2d2-workspace` | 3.502 s |
| 2 | `Сколько именно файлов и папок лежит сейчас в каталоге /tmp? Выполни в терминале ровно эту команду: ls -1 /tmp \| wc -l` | 3.485 s |
| 3 | `Создай в рабочем каталоге новый файл t21-permission-probe.txt …` (`write` → bucket `edit` → `ask`) | 3.443 s |
| 4 | `Прочитай файл /etc/os-release …` (outside the workspace → `external_directory` → `ask`) | 3.622 s |
| 5 | `Исследуй содержимое рабочего каталога … и запиши подробный отчёт … в файл …` | 3.655 s |
| 6 | `Используй встроенный инструмент write, чтобы создать файл …` | 3.622 s |

My always-connected tap recorded **zero** `permission.asked` and **zero**
`permission.replied` frames for the whole run, and `pending_actions` was empty
at every poll. `opencode`'s own transcript shows why, and it is not the timeout:

```json
* assistant r2d2-voice  TOOL bash error {'command': 'ls -la /home/koluchiy/r2d2-workspace'}
  error: "The user has specified a rule which prevents you from using this specific tool
          call. Here are some of the relevant rules
          [{\"permission\":\"*\",\"action\":\"allow\",\"pattern\":\"*\"},
           {\"permission\":\"*\",\"action\":\"deny\",\"pattern\":\"*\"},
           {\"permission\":\"bash\",\"pattern\":\"*\",\"action\":\"deny\"}, …]"
* assistant r2d2-agent  Команду выполнить не удалось — доступ к терминалу закрыт правилом
                       «bash: deny», терминал заблокирован …
```

`r2d2-voice` really is denied `bash` — that rule is its own. But `r2d2-agent`
is **not**: `opencode debug agent r2d2-agent` resolves its last `bash` rule to
`{"permission":"bash","pattern":"*","action":"ask"}` and lists `bash`, `edit`,
`write`, `task`, `skill`, `lsp`, `question`, `webfetch`, `websearch` as visible.
The agent read the denial's rule dump, concluded the opposite about itself, and
from then on refused every tool. That is D9, and it is what made attempts 2–6
fail. Attempt 1 is where the poisoning started, and it is the plan's own
scenario-4 request.

### 4b — even with a permanent connection, the reader cannot see an ask (D11)

D1's fix is real (§ "D1" below): R2D2's reader for this session made **one**
`GET /event` at 10:51:20 and produced **zero** reconnects over 45 minutes. It
was connected the entire time. And it still saw nothing, because opencode
1.18.32 does not send an SSE `event:` line at all.

My 30-minute tap of `GET /event`, line by line:

```
1090  data:
1091  <blank>
   0  event:      ← the stream never carries one
109  frames with "type":"server.heartbeat"
```

and the shipped decoder, fed a verbatim wire line:

```python
sample = 'data: {"id":"evt_x","type":"permission.asked","properties":
          {"id":"per_x","sessionID":"ses_x","permission":"bash","always":["ls *"]}}'
decode_frame("", [body]).type                 -> 'message'            # what frames() passes
decode_frame("permission.asked", [body]).type -> 'permission.asked'  # what the code needs
decode_frame("permission.asked", [body]).permission_id -> 'per_x'
```

`core/opencode/sse_frames.py:decode_frame` takes the event name from the SSE
`event:` field and falls back to the literal `SSE_DEFAULT_EVENT = "message"`.
It never looks at `payload["type"]`, which is where this build puts the name.
Every real frame therefore decodes as `type="message"`, so
`EventSource.run()` never logs `permission.asked`, `SessionReaders._handler`
returns on the first line (`if event.type != PERMISSION_ASKED`), and
`PermissionBroker.on_permission_requested` is **never called** — no matter how
long the reader stays connected. `docs/11-opencode-contract.md` U4's line
"opencode always sends one" is contradicted by the wire it was measured on, and
the unit tests cannot see it because they feed the decoder synthetic frames
that do carry an `event:` line.

### 4c — the identity half of D3 is proven, and no second session was minted

```
$ curl -s http://127.0.0.1:8099/tg/webhook -H 'Content-Type: application/json' \
    -d '{"message":{"chat":{"id":1087136471},"text":"да"}}' -w '\nhttp=%{http_code} time_total=%{time_total}\n'
{"ok":true}
http=200 time_total=3.344124

11:09:35,080 POST …/session/ses_f23bb5dbeffePCgTNtG36cby0v/message   200
11:09:35,082 turn route=opencode path=voice … agent=r2d2-voice llm_ms=2748 total_ms=2864
11:09:35,543 POST https://api.telegram.org/bot<REDACTED>/sendMessage  "HTTP/1.1 200 OK"
```

A `да` typed in Telegram was answered in **`ses_f23bb5dbeffePCgTNtG36cby0v`** —
the same session as every Alice turn, the same `application_id` — and the
answer went back to the chat. `POST /session` was **not** called: the total for
the entire run is 1, and the only `tg:1087136471` string anywhere in the log is
the startup line that reattaches the owner's pre-existing database row:

```
10:50:59,442 opencode events: watching session ses_f2474aac4ffeNnPg1OMFCtgQF5 of tg:1087136471 …
```

With no pending ask the broker correctly returned `unrelated` and the text was
answered as an ordinary question, so this proves the **identity** half of D3 and
not the `{"response":"once"}` half. The unbound-chat refusal could not be
exercised: `/tg/webhook` accepts only `chat_id == TELEGRAM_CHAT_ID`, so a
second, undeclared chat cannot be presented to it at all.

**FAIL** — the plan's acceptance for this scenario is
`{"response":"once"}` on the wire, and no ask existed to answer. The root cause
is D11; the reason I could not work around it with a second session is D9.

---

## Scenario 5 — laptop control via `r2d2_do open-app`

Command: `Открой браузер на ноутбуке.`

```
{"response":{"text":"Браузер открыть не могу: у меня нет ни управления графической оболочкой,
 "ни доступа к терминалу — в этой сессии доступны только чтение файлов и заблокированный
 bash. Такой запрос нужно передать агенту с правом запуска программ.", …},"version":"1.0"}
http=200 time_total=3.242300
```

The shim was never invoked: no `r2d2_do.py` process, no `xdg-open` child, and
`r2d2-voice` produced **no** tool call at all — it did not even emit
`[[NEEDS_AGENT]]`, so the refusal was spoken directly to Alice. The same
refusal is in its reasoning: *"I have no such tool — no shell, no GUI control …
bash is blocked"* — the D9 belief, held by the voice agent about its own bash,
where it is **true**, and generalised from the single denied
`ls -la /home/koluchiy/r2d2-workspace` in attempt 1 of scenario 4.

For the record, the shim itself is fine and remains allowlisted for both
agents:

```
$ /home/koluchiy/.opencode/bin/opencode debug agent r2d2-agent | jq '.permission[-20:]'
…
{"permission":"bash","pattern":"*","action":"ask"}
{"permission":"bash","pattern":"/home/koluchiy/.r2d2/r2d2_do.py *","action":"allow"}
…
```

**FAIL** — the matrix would allow it and the shim runs in 0.93 s standalone
(measured in the first run), but nothing in the session makes either agent emit
the call. D5's "deadline branch drops the request" half is fixed; its "the
voice agent never calls the shim" half is not, and D9 is why it now fails
faster than before.

---

## Scenario 6 — arxiv digest

Command: `Сделай сводку статей с arxiv по теме RAG за последние 7 дней.`

```
{"response":{"text":"Проверяю, пришлю в телеграм.", …},"version":"1.0"}
http=200 time_total=3.648608

job 31b26fdadd77  done  dur=6.3s
  Сделать не могу: сетевого доступа к arxiv у меня здесь нет, а придумывать статьи
  и аннотации я не стану. Задача уходит агенту с доступом в интернет.
```

The ack is right, the collector ran, and the delivery half works — the text
reached Telegram (`sendMessage` 200 at 11:03:49,680, 0.7 s after the job
finished). But there is no digest: `r2d2-agent` has `webfetch: allow` and
`websearch: allow` (verified in 4c) and simply refused. In the first run the
same scenario produced **60 real arxiv entries** via `webfetch` before the ask
that D1 lost. The difference is again the transcript: that run had eight fresh
sessions, this one had a poisoned one.

**FAIL** — the delivery path is proven, the work is not done.

---

## Session reuse — the invariant, and the counts

### One session, one `application_id`, no second mint

| | |
|---|---|
| `POST /session` issued by R2D2, whole run | **1** (10:51:20,685) |
| sessions titled `r2d2:alice:t21-final` | **1** — `ses_f23bb5dbeffePCgTNtG36cby0v` |
| `GET /session?directory=/home/koluchiy/r2d2-workspace` | 8 → 9, **delta +1** |
| sessions with an `r2d2:` title | 16 → 17, **delta +1** |
| `GET /session?limit=1000` (all projects) | 189 → 185, delta **−4** |

The plan's `delta == 1` is met on both attributable measures (+1 in the
workspace, +1 among `r2d2:`-titled sessions) and on the only count R2D2 can
influence: it issued exactly one `POST /session` in 19 turns, and the binding
in `db/sessions.db` was never rewritten.

The all-projects count went **down** by 4, and the reason is not R2D2. Five
sessions present in the "before" snapshot are absent from the "after" one:

```
ses_f23bde5efffe4BxecnVpSdGDdF  'T21 re-run live end-to-end proof (@Sisyphus-Junior subagent)'
ses_f23d0f18cffeZgs6c3IgSxBgt7  'Docs consistency pass after fixes (@Sisyphus-Junior subagent)'
ses_f23decb3dffe4MONu8oQtbJ2Cs  'Split core/opencode/client.py under ceiling (@Sisyphus-Junior subagent)'
ses_f23f36247ffeYGi5vHY5jgDUaa  'Split core/permissions.py under LOC ceiling (@Sisyphus-Junior subagent)'
ses_f23f3a427ffeR6jmCfoWwkhxVg  'Split core/brain.py under LOC ceiling (@Sisyphus-Junior subagent)'
```

All five have `directory = /home/koluchiy/Documents/R2_yandex_station`, none is
in `db/sessions.db`, and R2D2 issued no `abort` and no `DELETE /session` at any
point in the run. They belong to the *other* opencode process running on this
box, which shares the same session store — the all-projects count is therefore
not a measure R2D2 can be held to, and the workspace-scoped count is.

### The user's pre-existing sessions

| check | result |
|---|---|
| pre-existing sessions whose **title** changed | **0** |
| pre-existing sessions whose **directory** changed (reparented) | **0** |
| pre-existing sessions whose `updated` timestamp moved | **0** of the 184 that survived |
| pre-existing sessions read, written or deleted by R2D2 | **0** — the only routes R2D2 used were `GET /session?directory=…` (list), `GET /session/:id/message` for the session it owns, and `GET /event` |
| the first run's 8 `r2d2:alice:t21-*` / `r2d2:alice:tg:1087136471` sessions | all 8 still present, untouched |
| `db/sessions.db` rows from the first run | all 5 still present, untouched, and all 5 reattached at startup |

Baseline for comparison: the first run found **194** all-project sessions; this
run found **189** before it started, the difference being five more sessions the
other opencode process has since reaped.

---

## The eight defects, re-tested

| # | first-run claim | verdict now | the evidence |
|---|---|---|---|
| **D1** | SSE read timeout = the 3.2 s voice deadline, blind ~61 % of the time, 0 of 7 asks caught | **demonstrably fixed (the mechanism)** — and it is not the gating defect | blind rate measured live on the real server, on the real session, with R2D2's own `EventSource`: pre-fix read bound 3.2 s → `blind_s 31.23` of `wall_s 60.21` = **51.9 %**, 8 reconnects climbing 1→2→4→5×5; shipped `event_read_timeout` 30.0 s → `blind_s 0.0`, **0.0 %**, 0 reconnects. R2D2's live reader made **1** `GET /event` in 45 min with **0** `stream … failed` and **0** `stream ended` lines |
| **D2** | both escalation branches enqueue no collector, so the agent's answer is never delivered | **demonstrably fixed** | 12 `opencode_reply` jobs for `t21-final`, **12 `done`, 0 `error`**, one per escalating turn, including both branches that had none: cold-session/C8 `2d30f87cd51d` (18.7 s) and the sentinel branch `16857a642ffe` (53.1 s); plus the deadline branch `e62330f5f25f` (8.2 s). 13 `sendMessage` 200s in the log, 12 of them job deliveries, each within a fraction of a second of that job's `updated_at`; the 13th is the Telegram reply in 4c |
| **D3** | `/tg/webhook` derives `tg:<chat_id>`, so `да` cannot answer and a second session is minted | **demonstrably fixed for the identity**; the `once` half was not exercisable | `да` in chat 1087136471 answered in `ses_f23bb5dbeffePCgTNtG36cby0v`, the Alice session; `POST /session` total 1; no `tg:1087136471` session minted. The unbound-chat refusal is unreachable in a one-chat deployment |
| **D4** | the raw `[[NEEDS_AGENT]]` token reached Telegram 11 times | **demonstrably fixed** | `[[NEEDS_AGENT]]` occurrences: **0** of 13 Telegram messages, **0** of 12 stored job results, **0** of the 19 Alice `text`/`tts` values |
| **D5** | a voice turn over 3.2 s is acknowledged but never submitted | **the dropped-request half is fixed**; the "voice agent never calls the shim" half is **still broken** | deadline-branch turn 2b produced job `e62330f5f25f`, `done`, delivered. But scenario 5: the voice agent emitted no tool call at all, so the shim is still never invoked |
| **D6** | one `[[NEEDS_AGENT]]` poisons the shared session for ever | **partially fixed** | the sentinel-branch message **is** deleted before `prompt_async` (`DELETE …/message/msg_0dc45f7fd001ZeqtKCg9fTzm1E` → 200, matching `message.removed` on the wire), and the agent emitted the token **0** times across 12 further agent turns. **But 3 sentinel-bearing assistant messages are still in the transcript**, all from `path=deadline` turns, where the sweep runs while the voice turn is still in flight and finds nothing to delete; no later sentinel-branch turn occurred to sweep them. The harm did not materialise; the precondition is still there |
| **D7** | plugin/MCP tools ran with no human in the loop; the real hole was `r2d2-agent`'s `"*": "ask"` catch-all | **confirmed fixed** (independently of this run's scenarios) | `opencode debug agent r2d2-agent`, 65 rules, resolving to `… {"permission":"*","action":"ask"} → {"permission":"*_*","action":"deny"} → {"permission":"todowrite","action":"deny"} …`; `tools` map: `call_omo_agent`, `interactive_bash`, `look_at`, `skill_mcp`, `session_list`, `session_read`, `session_search`, `session_info`, `background_output`, `background_cancel`, `todowrite` all **`false`** (11 of 11), while `bash`, `read`, `glob`, `grep`, `edit`, `write`, `task`, `skill`, `lsp`, `question`, `webfetch`, `websearch` are `true`. Nothing foreign executed in this run |
| **D8** | the fallback chain has no working member | **the member exists and is called; it is rate-limited today** | with opencode down the chain called `openrouter` and got `HTTP 429 {"message":"Rate limit exceeded: free-models-per-day…","metadata":{"headers":{"X-RateLimit-Limit":"50","X-RateLimit-Remaining":"0","X-RateLimit-Reset":"1790467200"}}}` — the 50/day free cap this run's first agent sweep exhausted. The plan's failure-case acceptance (graceful `ERROR_TEXT`, process up) is met |

`grep -c "PASS"` on this file: **5** — three scenario verdicts, plus the two
sentences that state the number. Three of the plan's six scenarios pass.

---

## New findings — D9 … D12

Nothing below was fixed. T21 is a proof, so the code, the config and the tests
are exactly as they were.

### D9 (new, high) — one tool denial in the shared transcript disables every tool, for ever, for both agents

Measured, reproduced five times in one session:

1. `r2d2-voice` calls `bash` with a command outside its own allowlist
   (`ls -la /home/koluchiy/r2d2-workspace`). opencode denies it and the denial
   text **enumerates the effective rules**, including
   `{"permission":"bash","pattern":"*","action":"deny"}` — which is
   `r2d2-voice`'s own rule, not `r2d2-agent`'s.
2. `r2d2-agent` reads that in the shared transcript and concludes bash is denied
   for it too: *"Выполнить команду не вышло: доступ к bash в этой сессии
   запрещён правилом `bash: * → deny` в конфигурации opencode … Обходить
   запрет другим инструментом или повторять попытку я не буду."*
3. From then on it refuses every tool, including ones it demonstrably has:
   *"список доступных мне инструментов не содержит ни `write`, ни `edit` — только
   `read`, `glob`, `grep` и заблокированный `bash`"* — while
   `opencode debug agent r2d2-agent` lists `write: true`, `edit: true`,
   `bash: true`, `webfetch: true`.
4. `r2d2-voice` is poisoned the same way, in the other direction: it refuses the
   allowlisted shim after one denied bash call (scenario 5).

The transcript is the shared context the whole one-session-per-human design
rests on, and nothing prunes it: `routing.transcript_sweep` removes assistant
messages carrying the **sentinel**, and a tool-permission denial is a different
kind of message. `POST /session/:id/summarize` is not a recovery either — the
first run already measured it answering `true` and compressing nothing.

### D10 (new, high) — the plan's own scenario order is unsafe

The plan asks for the permission scenario (4) before laptop control (5) and the
arxiv digest (6), in one session. Under D9, exercising 4 permanently disables
5 and 6. That is exactly what happened: 4 was attempted first and 5 and 6 then
failed for a reason that has nothing to do with what they test. Had 5 and 6 been
run before 4 on this session, both would have had a fair chance — the first run
proved the arxiv path can fetch 60 real entries. This is a plan-ordering
consequence, not a code change, and it is worth deciding deliberately.

### D11 (new, critical) — the SSE decoder cannot read this opencode build's event names

Measured, in §4b above. `GET /event` on 1.18.32 emits **no `event:` line**; the
name is `payload["type"]`. `core/opencode/sse_frames.py:decode_frame` uses the
SSE field and falls back to `"message"`. So `EventSource` yields
`type="message"` for every frame, `SessionReaders._handler` drops all of them on
its first line, `PermissionBroker` is never invoked, and `turn_is_complete` can
never be satisfied from the live stream. **The permission feature does not work
on opencode 1.18.32 at all** — D1's fix, though real and necessary, does not
reach it. `docs/11-opencode-contract.md` U4 (and `docs/11-opencode-backend.md`
§8) assert that opencode always sends an `event:` line; on the wire it does not,
and every unit test of the decoder feeds it a frame that has one.

### D12 (new, medium) — two operational facts about a live server and a cold one

*Right after an `opencode serve` restart*, the first plain question returned
`ERROR_TEXT` although the server was healthy:

```
11:10:36,574 GET http://127.0.0.1:4599/global/health  "HTTP/1.1 200 OK"
11:10:39,782 WARNING r2d2 opencode 'opencode' could not answer t21-final; the turn falls back
                    to the chain: ReadTimeout('')
turn route= path=error model= agent=r2d2-voice llm_ms=1266 total_ms=4520
```

The failing call was the session re-verification `GET /session` (the
freshly-restarted server rebuilding its index over 185 sessions), and it
surfaced as a bare `httpx.ReadTimeout` rather than the typed
`OpencodeDeadlineExceeded` that `SessionRoute.turn` catches — so the turn fell
through to a chain with no working member. The next two questions were
`route=opencode path=deadline` in the same session.

*The in-repo suite cannot run while a live `opencode serve` holds 4599.* With my
server up, `pytest tests/ -q` gave `1 failed, 905 passed, 1 skipped`, the failure
being `tests/test_e2e_stack.py::test_a_turn_that_outran_the_budget_is_collected_into_telegram`
with `GET http://127.0.0.1:4599/event → HTTP 401 Unauthorized` — the real server
answering the test's fake-server traffic. With both servers stopped:

```
906 passed, 1 skipped in 74.36s
```

---

## Reproduction

```bash
cd /home/koluchiy/Documents/R2_yandex_station
set -a; . ./.env; . ./.env.oc; set +a
export R2D2_TG_APPLICATION_ID='<chat_id>=<application_id>'   # declared, never derived
bash scripts/opencode_serve.sh &                              # 127.0.0.1:4599
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8099 &
curl -s localhost:8099/health
curl -s localhost:8099/webhook -H 'Content-Type: application/json' -d '{
  "meta":{"interfaces":[{"type":"Voice"}]},
  "request":{"type":"SimpleUtterance","command":"Назови столицу Франции. Одно слово."},
  "session":{"new":false,"skill_id":"","application":{"application_id":"t21-final"},"user":{"user_id":""}},
  "version":"1.0"}'
```

Committed with this file, because they are the reproduction of D1 and D11 and
they are read-only:

| file | what it does |
|---|---|
| `qa/d1-blind-rate.py` | two `EventSource`s on one real session, one at the pre-fix read bound and one at the shipped one, reporting connected/blind seconds over a window |
| `qa/d1-blind-rate.out` | its output: 51.9 % blind pre-fix, 0.0 % shipped |
| `qa/sse-frame-proof.py` | reads `GET /event` from the real server and then runs a verbatim wire line through the shipped `decode_frame` |
| `qa/sse-frame-proof.out` | its output: the stream has no `event:` line, and the decoder turns `permission.asked` into `message` |

Raw artefacts for this run stay in `/tmp/r2d2-qa/t21b/` and are **not**
committed: `uvicorn.log` (contains the Telegram bot token in httpx URLs — quote
it only as `bot<REDACTED>`), `opencode.log` / `opencode2.log`, `sse-tap.log`
(30 minutes of the global event stream, 1090 frames), `sessions-before.json` /
`sessions-after.json` (the owner's own session titles), `transcript-final.json`,
`curlrc` (0600, holds the server password), and every request/response body.
