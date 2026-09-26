# T21, third run — live end-to-end proof against a real `opencode serve`

Plan: `.omo/plans/opencode-brain.md` lines 472–479. This is the **third**
execution of todo 21. The first is `qa/live-run.md`, the second is
`qa/live-run-postfix.md`; both are left exactly as measured and nothing below
replaces them. The D-numbering continues from theirs, so the three findings this
run adds are **D13–D15**.

Nothing in the repository, in `config/`, in `~/.r2d2/opencode/` or in `tests/`
was changed to produce this file. T21 is a proof, so every defect found here is
reported and left in place. Every number below was measured on this machine
against `opencode serve` **v1.18.32** on `127.0.0.1:4599` and R2D2 on
`127.0.0.1:8099`, both started by hand for this run and both stopped afterwards
(`ss -ltnp` shows 4599 and 8099 free; the owner's own
`opencode serve --port 45512`, pid 235191, was never signalled).

## Verdict

**4 of the plan's six scenarios pass, 2 fail.** The plan's acceptance target of
6 is **not met**, and no line here was written to make it look met. The plan's
check is `grep -c "PASS"`; on this file that number counts prose as well as
verdicts, and the score is the table.

| # | scenario | wall (s) | verdict |
|---|---|---|---|
| 1 | `GET /health` | 0.050 | **PASS** |
| 2 | plain question through `/webhook` | 0.378 cold / **1.615 – 2.326** warm ×4 / 4.477 and 4.761 overran | **PASS** — under `r2d2_fast_deadline` 3.2 s, with two recorded caveats (D15, and 4.761 s > Alice's 4.5 s) |
| 3 | escalation → Telegram | 3.530 ack, Telegram 8.3 s later | **FAIL** — the ack is right and a message arrived, but the message was the routing signal stripped of its token, not the agent's answer: `r2d2-agent` emitted `[[NEEDS_AGENT]]` itself (D6 recurred) |
| 4 | permission ask → `да` → `{"response":"once"}` | 3.725 turn, 1.190 answer, tool ran 25 s | **PASS** — first time ever, through the whole R2D2 stack (D11) |
| 5 | laptop control via `r2d2_do open-app` | 3.509 | **FAIL** — the shim was never invoked; the agent refused on prompt grounds (D5's second half) |
| 6 | arxiv digest | 3.460 ack, 28.8 s collector | **FAIL** — no digest: the agent read a stored voice-agent denial as its own matrix (D9) |

**The permission loop — the feature D11 was blocking — works.** Scenario 4 is the
first live proof in three attempts: a real `permission.asked` off the wire,
delivered by R2D2's own SSE reader, asked in Telegram, answered with a `да` typed
into `/tg/webhook`, and `{"response":"once"}` on the wire, with the confirmed
command then actually executed.

**D9 is not fixed.** The prompt rule that was supposed to stop one agent reading
the other's refusal as its own matrix did not stop it, and the harm was measured
directly, with a positive control in the same session.

## Environment

| | |
|---|---|
| opencode | `/home/koluchiy/.opencode/bin/opencode` v1.18.32, `scripts/opencode_serve.sh`, `127.0.0.1:4599`, started **fresh** (no hot reload), restarted once for the failure case |
| opencode config | `~/.r2d2/opencode/opencode.json` (0600), **byte-identical** to `config/opencode/r2d2.opencode.json`; `r2d2-voice` 14 rules, `r2d2-agent` 16 including `"*_*": "deny"`; both prompts carry the D9 rule (voice rule 10, agent rule 8) — confirmed served live by `GET /diagnostics/providers` |
| CLI shim | `~/.r2d2/r2d2_do.py` (0755), byte-identical to `opencode/r2d2_cli/r2d2_do.py` |
| R2D2 | `.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8099`, `.env` + `.env.oc` sourced, plus `R2D2_TG_APPLICATION_ID='1087136471=t21-v3'` exported **for the process only** (`.env` untouched, as it is the owner's) |
| workspace | `/home/koluchiy/r2d2-workspace` |
| startup line | `R2D2 started, db=…/db/sessions.db, opencode=wired, models=opencode/space-bunny-free, fallback=['openrouter']` |
| `application_id` | **`t21-v3`** for every Alice turn and for the Telegram chat. Two extra ids, `t21-v3b` and `t21-v3c`, were opened **only** for the D10 disambiguation and the D9 measurement, and are accounted for separately below |
| `session_id` | **`ses_f235aebaeffeTZp7uX1Mp7FNe3`**, title `r2d2:alice:t21-v3` |
| `db/sessions.db` before | 6 `oc_sessions` rows: `t21-live-proof-d`, `-e`, `-f`, `-g`, `t21-final`, `tg:1087136471`; all 6 reattached at startup, none rewritten |
| secrets | never printed. The one place a bot token appears (httpx INFO lines) is quoted as `bot<REDACTED>`; the raw logs stay in `/tmp/r2d2-qa/t21c/` and are **not** committed. The `curlrc` that held the server password was written 0600 and **deleted** at the end |
| `~/.config/opencode/` | never written; the owner's other opencode processes were never signalled |

### Baseline, before any R2D2 traffic

```
GET /global/health                -> {"healthy":true,"version":"1.18.32"}
GET /session?limit=1000           -> 200 sessions   (the default limit is 100 and
                                     silently truncates; limit is required for
                                     an honest count)
GET /session?directory=/home/koluchiy/r2d2-workspace&limit=1000 -> 9 sessions
sessions with an "r2d2:" title    -> 17
```

---

## Scenario 1 — `GET /health`

```
$ curl -s http://127.0.0.1:8099/health -w '\nhttp=%{http_code} time_total=%{time_total}\n'
{"status":"ok","opencode":{"reachable":true,"version":"1.18.32","base_url":"http://127.0.0.1:4599"},
 "sessions":6,"chain":["zen","yandexgpt","openrouter"]}
http=200 time_total=0.050342
```

`GET /diagnostics/providers` (`http=200 time_total=0.821920`) returns the live
agent catalog, including both R2D2 agents with their full effective rule lists —
`r2d2-agent` resolving to `… {"permission":"*","action":"ask"} →
{"permission":"*_*","action":"deny"} → {"permission":"todowrite","action":"deny"}
… {"permission":"bash","pattern":"*","action":"ask"}`, and `r2d2-voice` to
`{"permission":"*","pattern":"*","action":"deny"}`. The only value-shaped fields
in `/health` are the version and the loopback URL; no credential appears.

**PASS** — 50 ms, the live server version, no leak.

---

## Scenario 2 — a plain question through `/webhook`

`application_id` = `t21-v3`. Every request below is a real Alice
`SimpleUtterance`; the full bodies are in `/tmp/r2d2-qa/t21c/ask.sh`'s output
and `turns.tsv`.

### 2a — cold session (C8: the first turn of a new session is not waited on)

```json
{"meta":{"interfaces":[{"type":"Voice"}],"lang":"ru-RU"},
 "request":{"type":"SimpleUtterance","command":"Сколько будет два плюс два? Ответь коротко."},
 "session":{"new":false,"skill_id":"","session_id":"",
            "application":{"application_id":"t21-v3","type":"Alice"},"user":{"user_id":""}},
 "version":"1.0"}
```

```
{"response":{"text":"Проверяю, пришлю в телеграм.","tts":"Проверяю, пришлю в телеграм.","end_session":false},"version":"1.0"}
http=200 time_total=0.377840

12:36:41,357 INFO core.opencode.session_store opencode session store: t21-v3 -> new session ses_f235aebaeffeTZp7uX1Mp7FNe3 (r2d2:alice:t21-v3)
12:36:41,632 INFO r2d2 turn route=opencode path=escalate model=opencode/space-bunny-free agent=r2d2-agent llm_ms=0 total_ms=372 escalated=True permission_asked=False msgs=1 tools=()
```

0.378 s, an ack, the sentinel never reached Alice, and **one** `POST /session`
for the whole run's main id. The cold-cache cost C8 predicts was paid on the
server: the agent's own answer landed 17.5 s later (`created=12:36:41 →
completed=12:36:56`) and was delivered to Telegram as job `487643e12927`.

### 2b / 2c — the two warm samples that overran, and what they cost

| command | wall | `llm_ms` | `total_ms` | path | answer |
|---|---|---|---|---|---|
| `Назови столицу Франции. Одно слово.` | **4.477 s** | 3305 | 4474 | `deadline` | **lost** — see D15 |
| `Сколько будет два плюс два? Ответь коротко.` | **4.761 s** | 3255 | 4757 | `deadline` | job `05ec4ad87b5b`, `done` 6.5 s later, result `4`, delivered |

Both overran 3.2 s because the server was still busy with 2a's cold agent turn
(finished 12:36:56). Both took the deadline branch and **both submitted the work
to the agent** — the D5 fix holding. **4.761 s is above Alice's 4.5 s webhook
budget**, so on those two turns the ack itself arrived late; that is a real
number, not a scenario failure.

### 2d1–2d4 — the fast voice path, once the session was warm

| command | wall | `llm_ms` | `total_ms` | path | spoken |
|---|---|---|---|---|---|
| `Сколько будет два плюс два? Ответь коротко.` | **2.326 s** | 2224 | 2322 | `voice` | `Четыре.` |
| same | **1.768 s** | 1716 | 1764 | `voice` | `4` |
| same | **1.711 s** | 1658 | 1707 | `voice` | `4` |
| same | **1.615 s** | 1560 | 1611 | `voice` | `4` |

Four consecutive real answers from `r2d2-voice`, all under
`r2d2_fast_deadline` = 3.2 s, `escalated=False`, no tools.

**PASS** — a warm question is answered inside the deadline, in a valid Alice
payload, with the text the model actually produced. Two caveats are recorded
above and neither is a pass being invented: the two overran samples are real and
one of them lost its answer to D15.

---

## Scenario 3 — a question that must escalate

`application_id` = `t21-v3`, warm session.

Command: `Объясни подробно, как работает квантовый диод (светодиод) с точки зрения
физики полупроводников, и напиши развёрнутый технический обзор из пяти абзацев.`

```
{"response":{"text":"Проверяю, пришлю в телеграм.","tts":"Проверяю, пришлю в телеграм.","end_session":false},"version":"1.0"}
http=200 time_total=3.529903

12:38:43,425 INFO r2d2 turn route=opencode path=deadline model=opencode/space-bunny-free agent=r2d2-voice llm_ms=3247 total_ms=3526 escalated=False permission_asked=False msgs=1 tools=()
```

The ack is right and the sentinel never reached Alice. A Telegram message
**did** arrive 8.3 s after the ack (`sendMessage` 200 at 12:38:52.217, job
`e0974a5334ac`) — but this is what it contained:

```
[[NEEDS_AGENT]]
Написать развёрнутый технический обзор из пяти абзацев о физике полупроводников и принципе работы квантового диода (светодиода).
```

**The agent emitted the routing signal itself.** The transcript, verbatim:

```
[16] user      r2d2-voice   12:38:40  Объясни подробно, как работает квантовый диод …
[17] assistant r2d2-voice   12:38:40 → 12:38:45  [[NEEDS_AGENT]] / Написать развёрнутый …
[18] user      r2d2-agent   12:38:43  Пользователь попросил голосом: Объясни подробно …
[19] assistant r2d2-agent   12:38:45 → 12:38:48  [[NEEDS_AGENT]] / Написать развёрнутый …
```

`[17]` is the voice agent's marker, still `running` when `_collect_later` read
the transcript at 12:38:43.3 — so the sweep found nothing to delete and used
`[17]` itself as the anchor (`since_message_id: msg_0dca6e4e8001ma4YgrJ2MfudxK`).
The agent then read `[17]` while generating and answered with the same token
(`[19]`), which is the first message after the anchor — so the collector shipped
it. `routing.for_human` dropped the token line, which is why the owner received a
stripped echo of their own request instead of a protocol marker and instead of
an essay. **This is D6 recurring, on the `path=deadline` branch, exactly the
residue attempt 2 recorded as "the harm did not materialise; the precondition is
still there".** The harm has now materialised.

**The session healed, and that is worth recording too.** The next sentinel-branch
turn (scenario 4) swept all three messages at once — my independent tap saw
`message.removed` for `msg_0dca6e4e8…` (`[17]`), `msg_0dca6f8ce…` (`[19]`) and
`msg_0dca9024…` — and the agent did real work in the very next turn. So the
poison is recoverable by a later sweep; it is not recoverable by the turn that
created it.

**FAIL** — an ack and a Telegram message, but not the result the message was
supposed to carry.

---

## Scenario 4 — permission ask → `да` → `{"response":"once"}`

`application_id` = `t21-v3`.

### 4a — the ask arrives through R2D2's own reader, for the first time

Command: `Выполни в терминале ровно эту команду и перескажи её результат своими
словами: ls -la /home/koluchiy/r2d2-workspace`

```
{"response":{"text":"Проверяю, пришлю в телеграм.", …},"version":"1.0"}
http=200 time_total=3.725124

12:41:02,116 INFO r2d2 turn route=opencode path=escalate model=opencode/space-bunny-free agent=r2d2-agent llm_ms=3240 total_ms=3721 escalated=True permission_asked=False msgs=1 tools=()
12:41:05,874 INFO core.opencode.sse opencode sse: permission.asked ls -la /home/koluchiy/r2d2-workspace
12:41:05,886 INFO core.permissions opencode permissions: t21-v3 must confirm 'ls -la /home/koluchiy/r2d2-workspace'; a durable grant (always) would cover ls * for the rest of the session and is never sent
12:41:06,676 INFO httpx HTTP Request: POST https://api.telegram.org/bot<REDACTED>/sendMessage "HTTP/1.1 200 OK"
```

My always-connected independent tap (`curl -N /event`, started by me, not by
R2D2) recorded the same frame on the wire:

```json
{"id":"evt_0dca91dce001oPumCeg7p3kLSI","type":"permission.asked","properties":{
  "id":"per_0dca91dcd0017YE2YM5bRcTJ6X","sessionID":"ses_f235aebaeffeTZp7uX1Mp7FNe3",
  "permission":"bash","patterns":["ls -la /home/koluchiy/r2d2-workspace"],
  "metadata":{"command":"ls -la /home/koluchiy/r2d2-workspace"},"always":["ls *"],
  "tool":{"messageID":"msg_0dca90fc0001l5Wc3nOOEgHiJE","callID":"call_function_oo5hyk4hzj89_1"}}}
```

and the pending row landed keyed by the **Alice** `application_id`:

```
sqlite> select application_id, action_json from pending_actions;
t21-v3 | {"kind": "opencode_permission", "session_id": "ses_f235aebaeffeTZp7uX1Mp7FNe3",
          "permission_id": "per_0dca91dcd0017YE2YM5bRcTJ6X",
          "title": "ls -la /home/koluchiy/r2d2-workspace", "always": ["ls *"],
          "requested_at": 1790408465.8745213}
```

### 4b — `да` typed into `/tg/webhook`, and `{"response":"once"}` on the wire

```
$ curl -s http://127.0.0.1:8099/tg/webhook -H 'Content-Type: application/json' \
    -d '{"message":{"chat":{"id":1087136471},"text":"да"}}' -w '\nhttp=%{http_code} time_total=%{time_total}\n'
{"ok":true}
http=200 time_total=1.190040

12:42:00,516 INFO httpx HTTP Request: POST http://127.0.0.1:4599/session/ses_f235aebaeffeTZp7uX1Mp7FNe3/permissions/per_0dca91dcd0017YE2YM5bRcTJ6X?directory=%2Fhome%2Fkoluchiy%2Fr2d2-workspace "HTTP/1.1 200 OK"
```

The independent tap recorded what the **server** accepted:

```json
{"type":"permission.replied","properties":{"sessionID":"ses_f235aebaeffeTZp7uX1Mp7FNe3",
 "requestID":"per_0dca91dcd0017YE2YM5bRcTJ6X","reply":"once"}}
```

The pending row was gone afterwards, and the command then ran:

```
[20] assistant r2d2-agent 12:41:02 → 12:42:00  tool bash status=completed  {"command":"ls -la /home/koluchiy/r2d2-workspace"}
[21] assistant r2d2-agent 12:42:00 → 12:42:09  "## Результат `ls -la /home/koluchiy/r2d2-workspace` …"
```

and the report was collected (job `44e03d5b5c13`, 71.2 s) and delivered to
Telegram (`sendMessage` 200 at 12:42:13.854, 0.5 s after the job closed).

The `да` was answered in the **Alice** session `ses_f235aebaeffeTZp7uX1Mp7FNe3`,
not in a Telegram-derived one: `POST /session` was issued **3** times in the whole
run, once per `application_id` used, and never for a `да`. The stale
`tg:1087136471` row from attempt 1 was reattached at startup and never used.

### 4c — a second full loop, same session

Command: `Скачай страницу https://example.com инструментом webfetch и скажи, что на
ней написано.` The agent chose `bash` + `curl` (see D9 for why), an ask was
raised, and:

```
$ curl -s …/tg/webhook -d '{"message":{"chat":{"id":1087136471},"text":"да"}}' …
{"ok":true}   http=200 time_total=1.197189

12:52:25,279 POST http://127.0.0.1:4599/session/ses_f235aebaeffeTZp7uX1Mp7FNe3/permissions/per_0dcb2570e0019y23qjoEVIdqZX → 200
tap: {"type":"permission.replied","properties":{…,"reply":"once"}}

[37] assistant r2d2-agent 12:51:05 → 12:52:25  tool bash completed {"command":"curl -s --max-time 15 https://example.com","timeout":30000}
[38] assistant r2d2-agent 12:52:25 → 12:52:34  "## Готово — страницу скачал ✅ … Example Domain …"
```

### 4d — every ask and every answer of the run

| session | ask | answered |
|---|---|---|
| `ses_f235aebaeffe…` (`t21-v3`) | `per_0dca91dcd0017YE2YM5bRcTJ6X` `ls -la …` | **`once`** |
| `ses_f2352390dffe0…` (`t21-v3b`) | `per_0dcadd85100168ZUMBBR0Bah9w` `curl … arxiv` | `reject` (D14) |
| `ses_f2352390dffe0…` (`t21-v3b`) | `per_0dcadd972001lrMQfAzqZB68Lu` `curl … arxiv` | `reject` (300 s sweep) |
| `ses_f234f4446ffe…` (`t21-v3c`) | `per_0dcb0cb33001LGomHxd9VmPgsc` `command -v r2d2_do` | **never** (unanswerable, see §D10) |
| `ses_f235aebaeffe…` (`t21-v3`) | `per_0dcb2570e0019y23qjoEVIdqZX` `curl … example.com` | **`once`** |

**5 asks, 4 answers, 2 × `once`, 2 × `reject`, 0 × `always`.** The count of
`"reply":"always"` frames on the wire is **0**, and it is unrepresentable in the
types besides.

**PASS** — the whole loop, end to end, through `/webhook` and `/tg/webhook`, for
the first time in three attempts.

---

## Scenario 5 — laptop control via `r2d2_do open-app`

`application_id` = `t21-v3` (plan order).

Command: `Открой браузер на ноутбуке командой r2d2_do open-app browser.`

```
{"response":{"text":"Проверяю, пришлю в телеграм.", …},"version":"1.0"}
http=200 time_total=3.509424

12:43:10,255 INFO r2d2 turn route=opencode path=deadline model=opencode/space-bunny-free agent=r2d2-voice llm_ms=3272 total_ms=3505 escalated=False …
```

The shim was never invoked — no `r2d2_do` process, no `xdg-open` child, no tool
call of any kind from either agent in that turn. The agent's own report, job
`9b6b4106dbe2`, delivered to Telegram:

> ## Не выполнено — инструмент недоступен
> Команду выполнить не смог: **`r2d2_do` у меня в этой сессии нет**.
> 1. **Свой набор инструментов** — доступны только: `Bash`, `Read`, `Write`, … Ни одного `r2d2_do` среди них нет.
> …
> ### Почему я не открыл браузер «обходным путём»
> Могу через `Bash` дёрнуть что-то вроде `xdg-open` … Но сознательно этого не делаю, потому что:
> - **Мои правила требуют**, чтобы действия с системой и приложениями шли строго через `r2d2_do` …

`r2d2_do` is a **CLI**, not a tool. The matrix would allow it
(`{"permission":"bash","pattern":"/home/koluchiy/.r2d2/r2d2_do.py *","action":"allow"}`,
served live by `/diagnostics/providers`), but the agent looked for a *tool* of
that name, found none, and treated the prompt's rule 2 as a prohibition on the
one path that would have worked. It had `Bash` in hand and chose not to use it.

**FAIL** — the same failure as attempts 1 and 2, unchanged by eleven fixes.
D5's "a voice turn overrunning 3.2 s is dropped" half is fixed (every deadline
turn above submitted its work); D5's "the voice agent never calls the shim" half
is **still broken**.

---

## Scenario 6 — arxiv digest

`application_id` = `t21-v3` (plan order).

Command: `Сделай сводку статей с arxiv по теме RAG за последние 7 дней.`

```
{"response":{"text":"Проверяю, пришлю в телеграм.", …},"version":"1.0"}
http=200 time_total=3.460342

12:44:13,358 INFO r2d2 turn route=opencode path=deadline model=opencode/space-bunny-free agent=r2d2-voice llm_ms=3257 total_ms=3456 escalated=False …
```

Job `c5f60317ffb8`, `done` 28.8 s later, delivered (12:44:42.676):

> ## Не выполнено — доступ к сети у меня закрыт
> Попробовал проверить, работает ли сеть с ноутбука, и получил **отказ по правам**. Это моё действующее правило, поэтому останавливаюсь и жду вашего решения.
> ```
> bash,  pattern: *                        → deny   ← весь Bash закрыт
> ```
> … правило **явно оставляет** несколько команд (`df`, `free`, `uptime`, `uname`, `ps`, `date`, `cat /sys/class/power_supply/*`) и пути к `r2d2_do.py` …
> | `Task` (делегировать исследование субагенту) | ❌ нет в наборе |
> | `Websearch` | ❌ нет в наборе |
> | `Webfetch` | ❌ нет в наборе |

**Every claim in that answer is false for `r2d2-agent`**, and the transcript says
where it came from. The turn that produced it:

```
[27] assistant r2d2-voice 12:44:10 → 12:44:22   tool bash status=error
     input {"command":"command -v curl && curl -s … http://export.arxiv.org/api/query?…"}
     error  "The user has specified a rule which prevents you from using this specific tool call.
             Here are some of the relevant rules [{…},{"permission":"bash","pattern":"*","action":"deny"},
             {"permission":"bash","pattern":"/home/koluchiy/.r2d2/r2d2_do.py *","action":"allow"}, …]"
[28] user      r2d2-agent 12:44:13  Пользователь попросил голосом: Сделай сводку статей с arxiv …
[29] assistant r2d2-agent 12:44:22 → 12:44:38  "## Не выполнено — доступ к сети у меня закрыт"
```

`{"permission":"bash","pattern":"*","action":"deny"}` is **`r2d2-voice`'s** rule.
`r2d2-agent`'s own `bash` resolves to `{"permission":"bash","pattern":"*","action":"ask"}`
and both `webfetch` and `websearch` resolve to `allow` — all three served live
by `GET /diagnostics/providers` on this very server. The denial was written at
12:44:22, the agent's answer was generated at 12:44:22, and the agent read the
other agent's matrix as its own. **This is D9, reproduced in the full stack.**

**FAIL** — the delivery path works, the work is not done.

---

## D10 — the plan's scenario order, and what actually caused these failures

D10 is the observation that under D9, exercising the permission scenario before
laptop control and the arxiv digest turns both into failures. This run tested it
directly: both scenarios were re-run **first**, in a fresh session, with no
permission scenario and no earlier turn of any kind in that session.

### Scenario 6 re-run first, alone (`t21-v3b`)

```
$ … command: "Сделай сводку статей с arxiv по теме RAG за последние 7 дней."   application_id: t21-v3b
{"response":{"text":"Проверяю, пришлю в телеграм.", …},"version":"1.0"}
http=200 time_total=0.220040

12:46:11,506 INFO r2d2 turn route=opencode path=escalate model=opencode/space-bunny-free agent=r2d2-agent llm_ms=0 total_ms=216 escalated=True
```

The agent immediately reached for the real thing — `curl` against
`export.arxiv.org` — and **two** asks were raised, both delivered to Telegram:

```
tap: permission.asked per_0dcadd85100168ZUMBBR0Bah9w  bash curl -s --max-time 60 "http://export.arxiv.org/api/query?search_query=all:%22retrieval-augmented%20generation%22&…"
tap: permission.asked per_0dcadd972001lrMQfAzqZB68Lu  bash curl -s --max-time 60 "http://export.arxiv.org/api/query?search_query=abs:%22retrieval-augmented%22+OR+abs:%22RAG%22&…"
12:46:16,339 WARNING … t21-v3b still has the unanswered ask per_0dcadd85100168ZUMBBR0Bah9w …; refusing it, because the new ask replaces it
```

So the arxiv path is **not** D9-poisoned when it goes first — it did more real
work in its first four seconds than it did in the whole plan-order run. **The
plan's ordering is therefore not what broke scenario 6.** Two other things did:

* **D14**, above: the first ask was refused 0.55 s after it was raised because a
  second ask replaced it. A turn that needs two confirmations cannot complete.
* **D13**, below: the collector had already closed at 8.2 s with the agent's
  *first sentence*, so even a completed digest would not have been delivered.
* and, in this probe only, the ask could not be answered at all: the chat is
  bound to `t21-v3`, so a `да` from chat 1087136471 is routed to `t21-v3`'s
  broker, which had no pending row, and was answered as an ordinary question.
  That is the D3 design working as written — one chat, one identity — and it is
  a limitation of running a probe under a second id, not a defect.

### Scenario 5 re-run first, alone (`t21-v3c`)

```
$ … command: "Открой браузер на ноутбуке командой r2d2_do open-app browser."   application_id: t21-v3c
{"response":{"text":"Проверяю, пришлю в телеграм.", …},"version":"1.0"}
http=200 time_total=0.186215

12:49:25,212 INFO r2d2 turn route=opencode path=escalate model=opencode/space-bunny-free agent=r2d2-agent llm_ms=0 total_ms=180 escalated=True
12:49:29,017 INFO core.opencode.sse opencode sse: permission.asked command -v r2d2_do; echo "---exit: $?"
12:49:29,030 INFO core.permissions opencode permissions: t21-v3c must confirm 'command -v r2d2_do; echo "---exit: $?"'; …
```

Different behaviour from the plan order: the agent did reach for the terminal,
to find out whether `r2d2_do` exists. It is sitting on that ask, which the bound
chat cannot answer, and job `731c55da49f4` closed `error` at 231.6 s with
`All connection attempts failed` — because I stopped `opencode serve` at
12:53:14 while it was polling. **The failure is the unanswerable ask, not D9 and
not the plan's order**: no denial exists anywhere in `t21-v3c`'s transcript.

**D10 verdict: not reproduced as a cause.** Neither scenario 5 nor scenario 6
was broken by running after the permission scenario. Scenario 5 fails on its own
in a clean session (D5), and scenario 6 fails on its own for D9 + D13 + D14. The
plan's ordering hazard is real in principle — it is what D9 *would* do — but it
is not what happened here, and this file does not blame it.

---

## D9 — the mitigation, measured

The fix for D9 is a rule in each agent's prompt (rule 10 in `r2d2-voice`, rule 8
in `r2d2-agent`): a refusal left in the shared history belongs to the agent that
hit it, opencode checks every call against the reader's own rules, and a stored
refusal is no reason to stop calling tools one may use. The agent that wrote it
could not reproduce the harm live and reported the rule as an unverified
mitigation. **This is the measurement of that rule.**

The refusal was planted by scenario 6's own voice turn, message `[27]` above
(`r2d2-voice`, `tool bash status=error`, `state.error` enumerating
`r2d2-voice`'s matrix). It is still in the transcript. Then, in the same session:

```
[34] user      r2d2-voice   12:50:55  Скачай страницу https://example.com инструментом webfetch …
[35] assistant r2d2-voice   12:50:55 → 12:51:05  "## Не выполнено — `webfetch` у меня нет … `bash` запрещён, поэтому через `curl` страницу я тоже не возьму."
[36] user      r2d2-agent   12:50:58  Пользователь попросил голосом: Скачай страницу https://example.com …
[37] assistant r2d2-agent   12:51:05 → 12:52:25  "Возможно, вы уже успели включить инструмент — проверю ещё раз."
     tool bash running {"command":"curl -s --max-time 15 https://example.com","timeout":30000}
```

and one turn earlier, the same session, message `[33]`:

> «Да» само по себе меня не разблокирует — … **у меня нет ни одного инструмента, который дотягивается до сети**.
> - `Websearch` / `Webfetch` / `Task` — в наборе инструментов нет
> - `bash` — запрещён целиком, включая `curl` и `wget`

Measured against this server's own catalogue: `webfetch` **allow**,
`websearch` **allow**, `task` **ask**, `bash` **ask** for `r2d2-agent`.

**Positive control, same session, before the refusal:** message `[20]` —
`tool bash status=completed`, `{"command":"ls -la /home/koluchiy/r2d2-workspace"}`,
followed by a 1768-character report the user received. The agent used a tool
successfully in this session. The refusal is the discriminating variable.

**Negative result, same session, after the refusal:** the agent states it has no
tools, and when it does act it reaches for `bash` — the one path that needs a
human — rather than `webfetch`, which needs none. It had the tool that would
have worked and did not use it.

**D9 is still broken. The prompt rule did not prevent it on
`opencode/space-bunny-free`.** The two agents' *own* denials are still read
correctly (`[35]`: "`webfetch` у меня нет" is **true** for `r2d2-voice`); it is
the cross-agent reading that the rule fails to stop.

---

## Session reuse — the invariant, and the counts

### Exactly one session per `application_id`

| `application_id` | `session_id` | sessions with that title |
|---|---|---|
| `t21-v3` (all six scenarios) | **`ses_f235aebaeffeTZp7uX1Mp7FNe3`** | **1** |
| `t21-v3b` (D10 probe, scenario 6 only) | `ses_f2352390dffe0vWH4DiDYI6xTJ` | 1 |
| `t21-v3c` (D10 probe, scenario 5 only) | `ses_f234f4446ffeeQin8GAOllNEpT` | 1 |

`POST /session` issued by R2D2, whole run: **3** — one per id, at 12:36:41,
12:46:11, 12:49:25, and never since, including across the `opencode serve`
restart. `db/sessions.db` gained one row per id and rewrote none.

### The count delta

Measured at the end of the six-scenario run, before any probe:

| | before | after | delta |
|---|---|---|---|
| `GET /session?limit=1000` (all projects) | **200** | **201** | **+1** |
| `GET /session?directory=/home/koluchiy/r2d2-workspace&limit=1000` | **9** | **10** | **+1** |
| sessions with an `r2d2:` title | 17 | 18 | **+1** |
| **`POST /session` issued by R2D2** | 0 | **1** | **+1** |
| the one new id | — | `ses_f235aebaeffeTZp7uX1Mp7FNe3` | — |

**The plan's `delta == 1` is met on every attributable measure.** At the end of
the whole run, including the two disambiguation probes, all-projects is 203
(+3) and workspace 12 (+3) — one session per id, no id with two sessions, and
`GET /session` filtered by title `r2d2:alice:t21-v3` yields **exactly one**
session.

**The all-projects count did not move for any reason R2D2 caused.** No
pre-existing session disappeared, was retitled or was reparented: of the 200
sessions in the "before" snapshot, **0** changed `title`, `directory` or
`parentID`. Exactly **one** had its `time.updated` move:

```
ses_f235d7f0fffeFLs3vVhB1W62BM | "T21 attempt 3 final live proof (@Sisyphus-Junior subagent)"
    directory /home/koluchiy/Documents/R2_yandex_station   parentID ses_03299aaaaffewQIF7Gts3N7aPY
    updated 1790408140629 -> 1790408737458
```

That is **this conversation** — the subagent session your other
`opencode serve --port 45512` (pid 235191) is running for this very task. It was
already in the "before" snapshot, it lives in the repository directory rather
than the R2D2 workspace, and R2D2 issues no request that could touch it: the only
routes R2D2 used all the run were `GET /global/health`,
`GET /session?directory=…` (list), `GET /session/:id/message` and
`POST`/`DELETE` on **its own** session, `GET /event`, and
`POST /session/:id/permissions/…` for its own asks. Quoting the number without
this would have been the mistake attempt 2 also had to make.

### The user's pre-existing sessions

| check | result |
|---|---|
| pre-existing sessions that disappeared | **0** of 200 |
| pre-existing sessions whose **title** changed | **0** |
| pre-existing sessions whose **directory** changed (reparented) | **0** |
| pre-existing sessions whose **parentID** changed | **0** |
| pre-existing sessions whose `time.updated` moved | **1** — this agent's own session, above |
| pre-existing sessions read, written or deleted by R2D2 | **0** |
| the 8 attempt-1 `r2d2:alice:t21-live-proof*` / `r2d2:alice:tg:1087136471` sessions | all 8 still present, untouched, reattached as event readers at startup and not written to |
| attempt 2's `t21-final` | still present, `ses_f23bb5dbeffePCgTNtG36cby0v`, untouched; its `db/sessions.db` row untouched |

---

## D1–D11, re-tested

| # | claim | verdict now | the evidence |
|---|---|---|---|
| **D1** | the SSE reader's read timeout is the 3.2 s voice deadline, blind ~51.9 % of the time, 0 of 7 asks caught | **demonstrably fixed, measured again in this run** | R2D2's own readers recorded **0** `stream … failed` lines and **0** reconnects across the 16 minutes the server was up (12:36:09 → 12:52:xx), and caught **5 of 5** `permission.asked` events — the same 5 my independent tap counted. All 63 `stream … failed` lines fall in the 18 seconds I deliberately kept the server down (12:53:15–12:53:33), climbing the 1→2→4→5 s ladder, which is the correct behaviour for a down server. `deadline=3.2s event_read_timeout=30.0s` in the startup line |
| **D2** | both escalation branches enqueue no collector, so the answer is never delivered | **the enqueue is now on all three branches, but it is not unconditional** | 12 `opencode_reply` jobs for this run's ids, every escalating turn armed one, and every branch that had none is represented: cold/C8 `487643e12927` (17.5 s), deadline `05ec4ad87b5b` (6.5 s) and `3e1b1eec4ba5` (19.2 s), sentinel `44e03d5b5c13` (71.2 s). 22 `sendMessage` 200s, each within a second of a job's `updated_at`. **But D15: a 0.5 s marker-read timeout skips the enqueue entirely** |
| **D3** | `/tg/webhook` derives `tg:<chat_id>`, so `да` cannot answer and a second session is minted | **demonstrably fixed, both halves** | `да` in chat 1087136471 → `POST /session/ses_f235aebaeffeTZp7uX1Mp7FNe3/permissions/per_0dca91dcd0017YE2YM5bRcTJ6X` → 200 → `permission.replied … "reply":"once"` (twice, §4b and §4c). The pending row keyed by the Alice `application_id` was found and cleared. `POST /session` for the whole run: 3, one per id; **no** `tg:1087136471` session minted; the stale row from attempt 1 was never used |
| **D4** | the raw `[[NEEDS_AGENT]]` token reached Telegram 11 times | **demonstrably fixed** | Of the 12 job results this run produced, **1** contains the raw token (`e0974a5334ac`, the D6 case in §3). Running the shipped `routing.for_human` over all 12 gives **0** bodies containing it; that one sends `'Написать развёрнутый технический обзор…'` with the token line dropped. A stored assistant message can only reach Telegram through that one function, and it is the only path that carries session text: the other 12 of the 22 messages are the broker's own questions and notices (fixed strings plus the ask's `command`, and none of the four ask titles contains the token) and the replies `/tg/webhook` sends back into the chat. 0 of the 17 captured Alice `text`/`tts` values contain it either |
| **D5** | a voice turn overrunning 3.2 s is acknowledged but never submitted | **the dropped-request half is fixed; the "voice agent never calls the shim" half is still broken** | Every `deadline` turn in this run submitted its work: 2c, scenario 3, scenario 4, scenario 5, scenario 6, the D9 probe, and the post-restart turn — 7 of 7, each with a job. Scenario 5: the shim was still never invoked, in either the plan order or the clean session |
| **D6** | one `[[NEEDS_AGENT]]` poisons the shared session for ever | **still broken on the `path=deadline` branch; the harm has now materialised** | §3: the deadline branch's sweep ran while the voice turn was still in flight, found nothing to delete, anchored on the in-flight marker, and `r2d2-agent` then emitted the token itself — the first message after the anchor, and therefore what the collector shipped. Recovery: a later sentinel-branch sweep deleted all three (`message.removed` ×3) and the agent did real work in the very next turn, so the session heals — but the turn that creates the poison never heals it |
| **D7** | plugin/MCP tools run with no human in the loop; the real hole was `r2d2-agent`'s `"*": "ask"` catch-all | **the fix is installed and served; the known MCP-resource item reproduced again, one step further out** | `/diagnostics/providers` on the live server resolves `r2d2-agent` to `… {"permission":"*","action":"ask"} → {"permission":"*_*","action":"deny"} → {"permission":"todowrite","action":"deny"} …`, and the prompt lists no `call_omo_agent` / `interactive_bash` / `todowrite` tool. **However** message `[23]` of this run is `assistant r2d2-voice` with `tool list_mcp_resources status=completed` **and** `tool list_mcp_resource_templates status=completed`, no ask, under a matrix that denies `read` as well as `"*"`. This is the known open item (MCP-resource tools bucketed as `read` in 1.18.32) and not a new defect, but the known item's stated scope is "reachable by any agent whose `read` is **allowed**", and it fired under `read: **denied**`. Reported as a datum on the open item, for the owner decision that is already pending |
| **D8** | the fallback chain has no working member | **a member exists, is called, and is rate-limited today** | With opencode down the chain called `openrouter` twice (one retry) and got `HTTP 429 … "Rate limit exceeded: free-models-per-day…","X-RateLimit-Limit":"50","X-RateLimit-Remaining":"0","X-RateLimit-Reset":"1790467200"` — the free daily cap, reset 2026-09-27T00:00:00Z. The plan's failure-case acceptance (graceful `ERROR_TEXT`, process up) is met; see `live-run-fail.md` |
| **D9** | one tool denial disables every tool, for both agents, for ever | **still broken; the prompt-rule mitigation measured and ineffective** | §D9 above: refusal planted at `[27]`, then `[33]`/`[37]` — the agent states it has no tools and reaches for `bash` (needs a human) instead of `webfetch` (needs none), both of which its own matrix allows. Positive control in the same session before the refusal: `[20]`, `tool bash completed`. The rule is in both prompts, served live, and did not prevent it |
| **D10** | the plan's own scenario order is unsafe: scenario 4 before 5 and 6 makes both fail | **a plan-ordering hazard, not a code defect — and not reproduced as a cause in this run** | §D10 above: both scenarios re-run first in clean sessions. Scenario 6 got *further* when it went first (two real `curl` asks against `export.arxiv.org` in its first four seconds) and failed for D9 + D13 + D14; scenario 5 got further too (it reached for the terminal) and failed because the ask was unanswerable. Neither failed because it ran after the permission scenario |
| **D11** | the SSE decoder reads only the `event:` field, so every real frame decodes as `message` and the broker is never invoked | **demonstrably fixed, through the full R2D2 stack** | §4: a real `permission.asked` was decoded, logged by R2D2's own reader (`12:41:05,874 opencode sse: permission.asked ls -la /home/koluchiy/r2d2-workspace`), turned into a Telegram question, answered with `да` from `/tg/webhook`, and answered on the wire with `{"response":"once"}` — twice. My independent tap counted the same 5 asks R2D2 caught 5 of. Independently reconfirmed on the wire: **1044 `data:` lines, 0 `event:` lines** in 17 minutes of `GET /event` on this server |
| **D12** | the first question after an `opencode serve` restart raises a bare `httpx.ReadTimeout` and returns `ERROR_TEXT` against a healthy server | **still open — recurred identically** | `live-run-fail.md` §3: `GET /global/health` 200 at 12:53:40.686, then `12:53:43,901 WARNING r2d2 opencode 'opencode' could not answer t21-v3; the turn falls back to the chain: ReadTimeout('')` and `turn route= path=error … total_ms=4217` |

## New findings — D13 … D15

Nothing below was fixed. T21 is a proof, so the code, the config and the tests
are exactly as they were. Each is a reproducible observation with the evidence
that produced it and the reproduction to hand.

### D13 (new, high) — the collector ships the agent's *first sentence* and drops its result

`core/backends/opencode_session.py:collect_reply` ends on "no new assistant text
for one `r2d2_event_poll_interval`", which is 2.0 s. An agent that narrates its
plan and *then* reaches for a tool produces text, goes quiet while it waits for a
permission answer, and the collector reads that silence as "finished".

Measured, `t21-v3b`, scenario 6 run first in a clean session:

```
job a20dc44588b8  opencode_reply  done  12:46:11 → 12:46:19  8.2s
  result: "Сделаю сводку. Сначала соберу реальные данные с arXiv API за последнюю неделю."
```

The agent went on to raise two asks and never finished the digest. Even a
successful digest would not have been delivered, because the collector had
already closed on the narration. The same shape cost the `curl example.com` turn
its result: job `3d2c27a679e7` closed at 12:51:12 on "Возможно, вы уже успели
включить инструмент — проверю ещё раз." while the real answer
(`[38]`, "## Готово — страницу скачал ✅ … Example Domain") was written at
12:52:34 and **never sent to anybody**.

The distinction the heuristic cannot make is *finished* versus *blocked on a
permission question R2D2 itself raised* — the one situation the broker exists to
create.

### D14 (new, high) — a second ask refuses the first, so a two-confirmation turn cannot complete

`core/permissions.py` refuses an outstanding ask as soon as a new one arrives
("the new ask replaces it"). Measured, `t21-v3b`:

```
12:46:15.785  t21-v3b must confirm 'curl -s --max-time 60 "http://export.arxiv.org/api/query?search_query=all:…'
12:46:16.339  WARNING … t21-v3b still has the unanswered ask per_0dcadd85100168ZUMBBR0Bah9w …; refusing it, because the new ask replaces it
12:46:16.359  POST …/permissions/per_0dcadd85100168ZUMBBR0Bah9w → 200
12:46:16.380  t21-v3b must confirm 'curl -s --max-time 60 "http://export.arxiv.org/api/query?search_query=abs:…'
tap:         permission.replied per_0dcadd85100168ZUMBBR0Bah9w -> 'reject'
              permission.replied per_0dcadd972001lrMQfAzqZB68Lu -> 'reject'   (the 300 s sweep)
```

**0.55 s** between the ask and its refusal. A user who was reading the first
Telegram question when the second arrived was asked to confirm something that had
already been rejected, and the command they were being asked about never ran. Any
real task that needs two confirmations is affected; the arxiv digest is the
smallest example.

### D15 (new, medium) — a 0.5 s read failure skips the collector enqueue, and the answer is lost with a blank reason

`SessionCollector._collect_later` reads the transcript under
`MARKER_TIMEOUT_S = 0.5` and, on failure, `return`s — **before**
`self.worker.enqueue(...)`. So the branch that was fixed to always enqueue does
not enqueue when the server is busy, and the turn is acknowledged with nothing
armed to collect it. Measured, `t21-v3`, scenario 2b, while the server was still
busy with 2a's cold agent turn:

```
12:36:45.895  GET /session?directory=…            (store re-verification)
12:36:49.631  WARNING r2d2 opencode 'opencode': the turn in session ses_f235aebaeffeTZp7uX1Mp7FNe3 of t21-v3
              keeps running but cannot be collected: 
12:36:50.228  POST …/prompt_async → 204
12:36:50.231  turn route=opencode path=deadline … total_ms=4474
```

No `opencode_reply` row exists for that turn. The voice agent's answer
`Париж.` was written to the session at 12:36:59 and **never delivered**. The
warning's reason is an empty `TimeoutError`, so the log names a lost answer and
does not say why.

This is the documented degradation ("a server that will not answer here loses
the answer, never the acknowledgement") doing exactly what it says — recorded
here because in a run whose whole point is that long results reach Telegram, it
is the mechanism by which one does not.

Reproduction for all three, without touching a line of the product:
`/tmp/r2d2-qa/t21c/` holds every artefact (see below).

---

## Reproduction

```bash
cd /home/koluchiy/Documents/R2_yandex_station
set -a; . ./.env; . ./.env.oc; set +a
export R2D2_TG_APPLICATION_ID='1087136471=t21-v3'    # declared, never derived
bash scripts/opencode_serve.sh &                       # 127.0.0.1:4599, fresh
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8099 &
curl -s localhost:8099/health
curl -s localhost:8099/webhook -H 'Content-Type: application/json' -d '{
  "meta":{"interfaces":[{"type":"Voice"}]},
  "request":{"type":"SimpleUtterance","command":"Назови столицу Франции. Одно слово."},
  "session":{"new":false,"skill_id":"","application":{"application_id":"t21-v3"},"user":{"user_id":""}},
  "version":"1.0"}'
```

`/tmp/r2d2-qa/t21c/ask.sh` is the timed one-turn helper this run used; it prints
the request, the response and the wall clock and appends a line to `turns.tsv`.

Raw artefacts for this run stay in `/tmp/r2d2-qa/t21c/` and are **not**
committed: `uvicorn.log` (contains the Telegram bot token in httpx URLs — quote
it only as `bot<REDACTED>`), `opencode.log` / `opencode2.log`, `sse-tap.log`
(17 minutes of the global event stream, 1044 frames, 0 `event:` lines),
`sessions-before.json` / `sessions-after6.json` / `sessions-pre-fail.json` /
`sessions-final.json` and `fingerprint-before.json` (the owner's own session
titles), `transcript-*.json`, `job-*.txt`, `turns.tsv`, `providers.json`,
`sessions.db.bak`, and `ask.sh` / `show.py`. The `curlrc` that held the server
password was written 0600 and **deleted** at the end of the run.

The full suite, run with both servers stopped, as `D12`'s note requires:

```
$ .venv/bin/python -m pytest tests/ -q
932 passed, 1 skipped in 73.28s (0:01:13)
```
