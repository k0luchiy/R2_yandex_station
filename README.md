# R2D2 — a Yandex Station (Alice) gateway in front of your own opencode agent

A thin voice and text gateway that puts **your** [opencode](https://opencode.ai)
agent behind a private Yandex Station skill. Every question becomes one persistent
opencode session; short answers come back as speech inside Alice's 4.5-second
budget, and anything long or slow arrives later in Telegram — because Alice cannot
send you a message after the fact.

```
"Alice, run skill R2D2"
"R2, what are quantum dots"                     → spoken, when it fits the 3.3 s budget
"R2, find papers about RAG and summarise them"   → "Checking, I'll send it to Telegram"
"R2, send me the arxiv digest to Telegram"      → arrives minutes later, as a message
```

Everything above the [О проекте](#о-проекте-на-русском) heading is English.
The original Russian README is preserved at the bottom of this file, and
everything in `docs/` is Russian.

---

## Read this before you clone anything

**This is not a library, not a CLI, and not a product you can install and use.**
It is one person's private custom skill. Concretely:

- **You cannot just run it.** An Alice skill is registered by hand in the
  [Yandex Dialogs console](https://dialogs.yandex.ru/), privately, inside one
  Yandex account. The `skill_id` and `application_id` it issues exist only
  inside that account, and private skills go through that account's moderation
  before they work. There is no marketplace entry, no API token, and nothing to
  `pip install` that would hand you a working voice assistant.
- **So "clone and it works" is false, and this README will not pretend
  otherwise.** What *is* true, and is the reason the code is worth reading: the
  gateway itself holds no identity. `core/brain.py` compares the incoming
  `skill_id` and `user_id` against configured values **only when both are
  non-empty**, so a second person can register their own skill and point their
  own ids at this code. Nothing in `app/`, `core/` or `config/` is hard-coded to
  one account, one model, or one machine.
- **Therefore the promise is: *clone it and make it your own skill.*** That is
  four manual steps you do once, listed below. They are not automated on
  purpose: the console step is a web form, the systemd step is a deliberate act,
  and automating either would be automating somebody else's account.
- **As of the most recent change, an undeclared identity refuses every request
  rather than serving everyone.** If `ALICE_SKILL_ID` / `ALICE_USER_ID` are
  empty, `/webhook` answers `Доступ запрещён.` to everything, on purpose — a
  deployment that has not decided who it is for is closed, not open. There is a
  documented development escape hatch (`R2D2_ALLOW_UNAUTHENTICATED=1`, loopback
  only) and the listener's code default is `127.0.0.1`.

If you wanted a pip-installable assistant you can `pip install`, this is the
wrong repository. If you wanted to read a voice gateway that treats a
4.5-second platform timeout as a hard architectural constraint — and a
Yandex-specific integration that nonetheless refuses to hard-code its owner's
identity — it is the right one.

---

## Install

Four commands and two file copies. The venv **must** be at `./.venv` inside the
checkout — not a system Python, not a venv elsewhere. `tests/test_r2d2_do_cli.py`
runs the `r2d2_do` shim as a subprocess through `<checkout>/.venv/bin/python`,
and asserts that path is the interpreter running the tests.

```bash
git clone <this repository> r2d2
cd r2d2

python3.12 -m venv .venv          # 3.12 or 3.13; see "Python versions" below
.venv/bin/pip install -e ".[dev]" # runtime deps + pytest/ruff/mypy/coverage

cp .env.example  .env  && chmod 0600 .env
cp .env.oc.example .env.oc && chmod 0600 .env.oc
```

Verify the install before you configure anything:

```bash
.venv/bin/python -m pytest -q     # 1068 passed, 3 skipped
```

Both `.env` files are git-ignored and both hold live credentials once filled in.
`.env` is the gateway's own configuration (and, for the opencode client, the same
`R2D2_OC_PASSWORD` the server uses). `.env.oc` is the `opencode serve` unit's
environment. The committed `*.example` files document every field;
`tests/test_no_secrets_tracked.py` fails if `Config` grows a field that
`.env.example` does not mention.

`pip install -e .` is verified in CI from a clean interpreter, including that
`app.main`, `core.brain`, `core.opencode.client` and `core.tools.registry` import
from outside the source tree. There is deliberately no console-script entry
point: R2D2 is an application, and you start it the way the documents say —
`uvicorn app.main:app --host 127.0.0.1 --port 8080`, or
`bash scripts/run_server.sh`.

### Python versions

`pyproject.toml` declares `requires-python = ">=3.11"`, which is what
`docs/01-overview.md` and `docs/08-deployment.md` have always said and is true
of the **runtime**. It is *not* true of the test suite, and this was measured
rather than assumed:

| interpreter | result |
|---|---|
| 3.12 | 1069 passed, 2 skipped — measured by CI |
| 3.13 | 1068 passed, 3 skipped locally; 1069/2 in CI, which seeds the three credential guards that skip without a `.env` |
| 3.11 | not re-measured since the 986-test revision; the two failures described below still apply |

The two 3.11 failures are `tests/test_backend_base.py:121` and `:131`, which read
`Backend.__protocol_attrs__` — an attribute CPython only sets on a
`@runtime_checkable` `Protocol` from 3.12. `core/backends/base.py` itself runs
fine on 3.11, so this is a property of the two assertions and not of the runtime.
CI therefore runs 3.12 and 3.13; add 3.11 once those two assertions derive the
protocol members in a version-independent way. 3.14 is absent because
uvicorn/psutil wheels for it were not available when this was written, and a
matrix entry that fails for a packaging reason teaches people to ignore red.

### Dependencies

Declared in `pyproject.toml` after auditing what `app/`, `core/` and
`opencode/` actually import — not after reading a requirements file:

| distribution | imported by | note |
|---|---|---|
| `fastapi` | `app/main.py`, `app/http.py`, `app/diagnostics.py` | |
| `httpx` | 16 modules | the opencode client, the SSE reader, the backends, the tools |
| `aiosqlite` | `core/memory.py` | |
| `psutil` | `core/tools/laptop_tool.py` | battery/cpu/memory probes for the `laptop` tool |
| `python-dotenv` | `app/config.py` | inside a `try`/`except`; a missing loader is a warning, not a crash |
| `uvicorn[standard]` | **no module imports it** | the ASGI server. `scripts/run_server.sh` execs it, `tests/test_e2e_stack.py` imports it to bind a real socket, and every document tells you to run it. Listed because it is required, not because it is imported — the one honest exception to the rule above. |

Dev-only, in the `dev` extra: `pytest`, `pytest-asyncio`, `respx`, `pytest-cov`,
`ruff`, `mypy`, `types-psutil`. Nothing under `app/` or `core/` imports any of
them.

There is one non-Python dependency that matters more than all of the above: the
`opencode` binary (1.18.32) on `PATH`, with a working Zen subscription. It is the
brain; R2D2 is the voice.

---

## The four manual steps

Nothing below is automated, and that is the design rather than an omission. In
order:

**1. Register a private Alice skill.**
[dialogs.yandex.ru](https://dialogs.yandex.ru/) → create a dialog → type
"Alice skill" → set the backend URL to your tunnel (step 4) → access type
**Private**. Copy the issued `skill_id` and your `user_id` into `.env` as
`ALICE_SKILL_ID` and `ALICE_USER_ID`. Until both are set the webhook refuses
every request, and the startup log says so by name.

**2. Install the opencode config and the tool shim.**

```bash
bash scripts/install_r2d2_opencode_config.sh    # -> ~/.r2d2/opencode, ~/.r2d2/r2d2_do.py
```

This matters more than it looks: `OPENCODE_CONFIG_DIR` **merges with** your
global `opencode.json` rather than isolating it, so every foreign agent and MCP
server your own config declares stays loaded and visible inside R2D2's process.
R2D2's permission block is therefore its only defence, and it only works if it
is installed. The reasoning is in
[`config/opencode/README.md`](config/opencode/README.md) — the one document in
this repository written in English, and the one a contributor should read first.

**3. Run the opencode server.** A systemd *user* unit
(`scripts/r2d2-opencode.service`) owns the process; R2D2 is only its client and
never manages it. `scripts/opencode_serve.sh` refuses to start on a blank
`R2D2_OC_PASSWORD`, because opencode's server auth is HTTP basic and without it
the server answers every route to every local process. Enabling the unit is
deliberately manual:

```bash
systemctl --user daemon-reload
systemctl --user enable --now r2d2-opencode
```

**4. Expose `/webhook` over HTTPS.** Alice will not call a plain-HTTP or
loopback URL from the cloud. `scripts/tunnel.sh` runs `cloudflared`; the quick
tunnel URL it prints goes into the skill's backend URL in step 1. The gateway's
code default is `127.0.0.1`; `.env.example` ships `SERVER_HOST=0.0.0.0` because a
tunnel needs it, which is the right trade only while the tunnel is the only thing
pointed at the port.

**Optional, and the difference between "answers" and "is useful":**

- **Telegram** — `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, and
  `R2D2_TG_APPLICATION_ID` (the `chat_id=application_id` binding that gives a
  chat an identity; without it, `да` in Telegram does not answer a question you
  asked by voice). Telegram is the only channel that can *deliver*: long results
  and permission questions both go there.
- **A fallback key** — `R2D2_ZEN_KEY` or `OPENROUTER_API_KEY`.
  `config/backends.json` is an ordered chain (`opencode` → `zen` → `yandexgpt` →
  `openrouter`). With no key the chain is short, and a turn that outruns the
  voice budget degrades to an emergency reply. Adding a provider is one JSON
  object; changing a model is one line per slot.

---

## How it works

Four ideas, in the order they matter:

**Two paths, not one model.** Alice gives you 4.5 seconds and 1024 characters.
A tool-less `r2d2-voice` agent answers inside `R2D2_FAST_DEADLINE` (3.3 s) and is
*spoken* — and **whether it fits is measured, not promised**: p50 1.667 s on
opencode 1.18.32, while on 1.18.33 nine of eleven voice turns ran past the 3.2 s
this deadline used to ship, which is why it is now the plan's own
`min(3.6, 4.5 − 1.2)` = 3.3 s — ten of those eleven land inside it. See
[`docs/07-latency-strategy.md`](docs/07-latency-strategy.md) and
[`qa/live-run-v10.md`](qa/live-run-v10.md) § NEW-1. If the request is real work,
that agent emits a `[[NEEDS_AGENT]]` sentinel instead of an
answer, the turn is escalated to a full `r2d2-agent`, and the result is
delivered to Telegram. The measured cost of the sentinel path is that the ack
still has to beat the clock, so it ships a fixed "checking, I'll send it to
Telegram" and does the work off Alice's budget.

**Context lives in the session, not in the prompt.** One user question is one
opencode session, created once and reused by both Alice and Telegram. SQLite
stores only the `application_id → session_id` mapping — the conversation itself is
the agent's, so a turn that escalates to the full agent keeps the context the
voice agent built instead of starting over.

**The latency budget is a design constraint, not an optimisation.** `4.5 s` is
Alice's timeout; `3.3 s` is `R2D2_FAST_DEADLINE`; the SSE reader's read bound is
looser still, so a 10-second heartbeat cannot be mistaken for a dead connection.
Every number is in `docs/07-latency-strategy.md` with the measurement behind it.

**R2D2 brokers opencode's permissions instead of answering them.** When the agent
asks, the question becomes a Telegram message; `да` comes back as
`{"response": "once"}`. A durable `always` grant is not merely discouraged — it
is unrepresentable in the type and refused on the wire, and an unanswered
question is rejected after `R2D2_PERMISSION_TIMEOUT` (300 s). Two agents rather
than one prompt: `r2d2-voice` may run 12 allowlisted read-only commands and is
forbidden `r2d2_do shell` outright; everything else asks.

---

## What it will not do

- **It will not work without your own Alice skill.** There is no demo mode that
  reaches a speaker.
- **It will not run on 3.11's test suite**, for the reason measured above. The
  runtime does; two assertions about a `Protocol` do not.
- **It will not reach a model through a free tier.** Every free Zen model except
  one is rejected by `opencode serve` with an inner 403; paid models need a
  balance. The measurements are in `docs/11-opencode-contract.md`.
- **It will not let you answer a permission question by saying "yes" to Alice.**
  Permission questions go to Telegram on purpose, because Alice cannot wait.
- **It is not hardened for the public internet as shipped.** The security model is
  private-skill + id allowlist + loopback opencode + `deny`-by-default agents. It
  is documented in `docs/09-security.md` and it is *not* a claim of
  multi-tenancy: one deployment serves one `skill_id` and one `user_id`.
- **It is Linux-only in practice** — the systemd user unit, `cloudflared` and the
  venv path in the launcher all assume it.
- **It will not reformat your global opencode config.** `OPENCODE_CONFIG_DIR`
  merges, so R2D2's rules sit on top of yours; the risk that creates, and the
  two consequences that follow from it, are written down in
  [`config/opencode/README.md`](config/opencode/README.md) rather than papered
  over.

---

## Known findings

Recorded rather than hidden, because a stranger reading a green CI badge should
know what the badge does not cover. All of these are in files that were owned by
a concurrent change while the tooling landed, so none of them were fixed here.

Every finding this list held has been fixed: the `__all__` that raised
`AttributeError` on a star import, the twice-defined `shell_deny_violations`
(the duplicate went when the matrix test was rewritten to derive its probe from
the shipped rules), the `Mapping` that `core/permissions.py` annotated with and
never imported, and the `if False` branch in `tests/test_brain_hybrid.py`. The
mypy ledger in `pyproject.toml` is a different thing and is not going away.

The last of these was the one that mattered most: `core/brain.py` scheduled a
Telegram notification with `asyncio.create_task` and kept no reference to it,
which is the documented way to lose work — the loop holds only a weak reference,
so the task can be collected before it runs. The action happened and the user was
told nothing, with nothing in the logs to say why. Every other `create_task` in
the project kept its task; this was the one that did not, and it now does.

**The type checker is advisory, and says so.** `mypy` reports **41** findings
over `app/`, `core/` and `opencode/`: 23 `union-attr` on a `Connection | None`
in `core/memory.py` that no annotation narrows, 6 `valid-type` on
dataclass-shaped callables used as types, 6 `arg-type` and 3 `assignment` from
the two config loaders coercing env strings, 4 `misc`, 1 `name-defined` (the
un-imported `Mapping` named above) and 1 `func-returns-value`. Every one of them
is in a file this change was not allowed to edit, so the `mypy` step itself is
`continue-on-error` and prints its breakdown on every run. A permanently red
badge is worse than no badge, because people learn to ignore red — so the
findings do not fail the build, but a **separate blocking step does compare the
count against a baseline of 41 and fails if it grew.** That is what "cannot
quietly grow" has to mean to be worth saying: an advisory number nobody acts on
is a number that can rot, and this one already had. Promote `mypy` to blocking
when the ledger is empty.

**mypy needed an interpreter, not just an environment.** The first run reported
`Cannot find implementation or library stub for module named "httpx"` for every
module in the project. That was the tooling, not the code: mypy is now run with
`--python-executable` pointing at an interpreter that has the dependencies. The
46 above are the real number.

**Coverage measures `app/` and `core/` only.** `opencode/r2d2_cli/r2d2_do.py` is
excluded on purpose: `tests/test_r2d2_do_cli.py` exercises it as a *subprocess*,
which in-process coverage cannot see, so including it would report a misleading
0% for a file the suite runs on every pass. The two weakest in-process files are
`core/tools/laptop_tool.py` (17%) and `core/tools/tg_tool.py` (27%) — machine
probes and the Telegram read/send tool, both reachable only with real hardware or
a real network. `core/tools/arxiv_tool.py` is 57% for the same reason.

**`ruff format` is not run.** The repository is not formatted to ruff's style;
enforcing it would be a ~300-file diff that buries the change that matters.
`E501` is not selected either: 1326 lines exceed 88 columns, most of them prose
inside docstrings explaining a measured defect. `RUF001`/`RUF002` are off because
132 of their hits are Russian text with typographic quotes and em dashes, which
is correct Russian typography rather than homoglyph confusion.

---

## Development

```bash
.venv/bin/python -m pytest -q                          # the whole suite, ~2 min
.venv/bin/python -m pytest tests/test_docs.py -q       # docs vs code, ~1 s
.venv/bin/python -m pytest --cov --cov-report=term-missing
.venv/bin/ruff check .
.venv/bin/mypy
pre-commit install && pre-commit run                  # the fast subset
```

`tests/test_docs.py` is unusual and worth knowing about: it cross-checks the
documentation *against the code*, in both directions. Every endpoint the
`opencode` client sends must appear in `docs/11-opencode-backend.md`'s table, and
every route that table claims must exist in `core/opencode/client.py`; every
model id in the document must be either configured in `config/backends.json` or
recorded as a measured refusal; every permission value in `docs/09-security.md`
must equal the one in `config/opencode/r2d2.opencode.json`; every module under
`core/` must appear in the module table; and the README's doc table must link
every file in `docs/`. Each checker has a mutation test proving it can fail.

`tests/test_no_secrets_tracked.py` is the credential guard, and the reason this
repository is safe to publish. Four layers: eight credential *shapes* scanned
across every tracked file, so a key nobody remembered to list is still caught;
the live `.env`/`.env.oc` values read at test time and searched for in every
tracked file; `.env.example` completeness reflected over the `Config` dataclass;
and ignore rules proven with `git check-ignore -v` rather than by reading
`.gitignore`. It scans its own source, and a hit is suppressed only when the file
is in a reviewed allowlist *and* the matched text carries a canary marker.

Three of its tests skip themselves when the operator's own credentials are
absent, which is exactly the case on a fresh clone. CI therefore seeds a
synthetic `TELEGRAM_BOT_TOKEN`, `R2D2_OC_PASSWORD` and `R2D2_CI_SENTINEL_TOKEN`
— generated one second earlier, never printed, git-ignored, and with not one line
of either test file changed — so that in CI the strongest guards in the suite
actually run rather than skipping. One skip remains and is deliberate:
`test_arxiv_prints_a_digest_or_an_explicit_nothing_found` opens a TLS connection
to `export.arxiv.org` to prove the digest tool really fetches, and CI sets
`R2D2_CLI_TEST_OFFLINE=1`. It is the only networked test. Everything else is
hermetic by construction: `tests/_hermetic/sitecustomize.py` makes the `r2d2_do`
subprocess refuse `connect()` *and* `getaddrinfo()` outright, and
`test_hermetic_socket_blocker_is_real` fails if that blocker ever stops working.

---

## Security model

Private Alice skill + `skill_id`/`user_id` allowlist; the opencode server on
loopback with a mandatory HTTP basic password; opencode agents `deny` by default
and ask for everything that changes state; `always` is impossible to send even at
the type level; an unanswered permission question is rejected; the global
`~/.config/opencode/opencode.json` is never modified. Full detail, and the
measurements behind each claim, in [`docs/09-security.md`](docs/09-security.md).

One caveat that the docs state and this README repeats because it is the easiest
thing to get wrong: `OPENCODE_CONFIG_DIR` merges with the global config instead of
isolating it, so "a missing rule is not a denial, it is an inheritance". R2D2's
permission block is a complete defence only if it is installed *and* names every
key for both agents.

---

## Licence

**MIT** — see [`LICENSE`](LICENSE). A licence is a choice, not a default, so: this
is a single-author project whose realistic contributor set is a handful of
people, which is exactly the situation Apache-2.0's explicit patent grant was
designed for but where it buys very little. The real legal exposure here is
trademark and platform-API use (Alice, opencode, Zen), which no OSI licence
addresses and which this README disclaims above. MIT is the shortest thing a
stranger can read in full, needs no `NOTICE` file and no CLA, and its "AS IS"
warranty disclaimer matters for a project that cannot run without somebody else's
paid subscription. The copyright line names the git-configured author and carries
no email address.

---

## Documentation

`docs/` is Russian. `config/opencode/README.md` is English and is the document a
contributor should read before touching the permission matrix.

| # | Документ | О чём |
|---|---|---|
| 01 | [docs/01-overview.md](docs/01-overview.md) | Цели, сценарии, архитектура |
| 02 | [docs/02-alice-requirements.md](docs/02-alice-requirements.md) | Ограничения платформы Алисы |
| 03 | [docs/03-architecture.md](docs/03-architecture.md) | Компоненты сервера |
| 04 | [docs/04-alice-protocol.md](docs/04-alice-protocol.md) | Протокол запроса/ответа |
| 05 | [docs/05-llm-provider.md](docs/05-llm-provider.md) | Мозг: бэкенды, реестр из конфига, промпт, tools |
| 06 | [docs/06-tools.md](docs/06-tools.md) | Инструменты и шим `r2d2_do` для агента |
| 07 | [docs/07-latency-strategy.md](docs/07-latency-strategy.md) | Два пути и бюджет 4,5 с |
| 08 | [docs/08-deployment.md](docs/08-deployment.md) | Запуск, opencode-сервер, туннель, навык |
| 09 | [docs/09-security.md](docs/09-security.md) | Безопасность и брокер разрешений |
| 10 | [docs/10-roadmap.md](docs/10-roadmap.md) | Этапы реализации |
| 11а | [docs/11-opencode-contract.md](docs/11-opencode-contract.md) | **Измеренный контракт opencode-сервера** |
| 11б | [docs/11-opencode-backend.md](docs/11-opencode-backend.md) | **Бэкенд opencode: агенты, разрешения, маршрутизация** |

Два файла с номером 11 — это не опечатка: первый содержит сырые измерения,
второй — описание того, как код на них опирается.

The live-run evidence, including the defects that measurement found and the fix
for each, is in [`qa/live-run.md`](qa/live-run.md) and its successors.

---

## Status

The code is written against opencode and covered by 1069 tests, including a
full-stack run against a fake opencode server and a live run against a real one
([`qa/live-run.md`](qa/live-run.md)). Coverage of `app/` + `core/` is 93%
(3174 statements, 704 branches; 73 partial branches). The suite runs on every
push and every pull request, on Python 3.12 and 3.13, with no network access.

Deployment is described in [08-deployment.md](docs/08-deployment.md); three steps
remain manual and intentionally so: **enabling the `r2d2-opencode` systemd
unit**, **registering the skill** in the Yandex Dialogs console, and **declaring
the Telegram binding** `R2D2_TG_APPLICATION_ID` as `chat_id=application_id` pairs
— without it a chat has no identity, and `да` in Telegram does not answer a
question asked by voice.

---

<a id="о-проекте-на-русском"></a>

# О проекте (на русском)

Оригинальный README проекта, сохранённый без изменений. English above.

---

# R2D2 — умный навык Алисы для Яндекс Станции

Тонкий голосовой и текстовый шлюз **перед собственным агентом opencode**.
Каждый вопрос попадает в одну постоянную сессию opencode, ответ даёт твоя
подписка opencode, а долгие результаты и подтверждения команд приходят в
Telegram — потому что Алиса не умеет присылать сообщения позже.

```
«Алиса, запусти навык Р2Д2»
«Р2, что такое квантовые точки»            → голосом, если уложился в 3,3 с
«Р2, открой браузер»                       → голосом, через r2d2_do
«Р2, найди статьи про RAG и сделай сводку» → «Проверяю, пришлю в телеграм»
«Р2, отправь в телеграм сводку статей с arxiv»
```

## Ключевые идеи

- **Протокол Алисы:** webhook + JSON, таймаут **4,5 с**, ответ ≤ **1024 символа**.
- **Два пути, а не одна модель.** Голосовой агент без инструментов укладывается
  в `R2D2_FAST_DEADLINE` (3,3 с) и отвечает голосом. **Укладывается ли он — это
  измерено, а не обещано:** на opencode 1.18.32 p50 1,667 с, а на 1.18.33 девять
  голосовых ходов из одиннадцати вышли за прежние 3,2 с, и потому дедлайн равен
  формуле `min(3.6, 4.5 − 1.2)` = 3,3 с, в которую укладываются десять (см.
  [`qa/live-run-v10.md`](qa/live-run-v10.md) § NEW-1). Если задача настоящая,
  агент выдаёт маркер
  `[[NEEDS_AGENT]]`, ход уходит полноценному агенту, а результат — в Telegram.
- **Контекст живёт в сессии opencode.** Один вопрос пользователя — одна сессия,
  созданная один раз и переиспользуемая и Алисой, и Telegram. В SQLite лежит
  только соответствие `application_id → session_id`.
- **Своя подписка вместо бесплатных моделей.** Бесплатные модели OpenRouter
  давали 1–12 с и 429; через opencode все бесплатные Zen-модели, кроме одной,
  отклоняются, а платные — по балансу. Замеры и выводы — в
  [11-opencode-contract.md](docs/11-opencode-contract.md).
- **Два агента вместо одного промпта.** `r2d2-voice` выполняет на машине только
  12 команд белого списка, а всё прочее — как и `r2d2-agent` — спрашивает
  разрешения; `r2d2_do shell` голосовому агенту запрещён вовсе. Решения заданы
  декларативно в `config/opencode/r2d2.opencode.json`.
- **R2D2 брокерит разрешения opencode.** Вопрос уходит в Telegram, ответ
  возвращается текстом, долговременное разрешение (`always`) не отправляется
  никогда, а неотвеченный вопрос отклоняется через 300 с.
- **Инструменты R2D2 доступны агенту** через один CLI-шим с точечным белым
  списком `bash` — без MCP-сервера и без новых зависимостей.
- **Цепочка фолбэков из конфигурации.** Набор и порядок бэкендов задаёт
  `config/backends.json`; добавить провайдера — один объект JSON, кода не
  требуется. Если opencode недоступен, ход уходит дальше по цепочке, и голосовой
  ассистент продолжает работать.
- **Приватный навык:** доступен только тебе, автомодерация.

## Стек

Python 3.11+ · FastAPI · httpx · opencode `serve` (локально, `127.0.0.1:4599`,
HTTP basic) · SQLite · cloudflared · Telegram Bot API · systemd (юнит
пользователя) · Linux

## Безопасность

Приватный навык + whitelist по `skill_id`/`user_id`; opencode-сервер только на
loopback и с обязательным паролем; разрешения opencode по умолчанию в `deny`,
всё изменяющее у агента спрашивает разрешения; `"always"` невозможно отправить
даже на уровне типов; глобальный `~/.config/opencode/opencode.json` никогда не
меняется. Подробно — [09-security.md](docs/09-security.md).

## Документация

Таблица документов — выше, в разделе [Documentation](#documentation).

## Проверка согласованности

`tests/test_docs.py` сверяет документацию с кодом: каждый путь из
[11-opencode-backend.md](docs/11-opencode-backend.md) обязан существовать в
`core/opencode/client.py`, каждая модель — в `config/backends.json`, каждое
значение разрешения — в `config/opencode/r2d2.opencode.json`, каждый блок кода
в новом документе — дословная выдержка из исходника, а новый модуль под `core/`
обязан появиться в карте модулей. У каждой проверки есть тест на мутацию,
доказывающий, что она падает на неверных данных.

```bash
.venv/bin/python -m pytest tests/test_docs.py -q
```

## Статус

Код переписан под opencode и покрыт тестами, включая сквозной прогон против
поддельного opencode-сервера и живой прогон против настоящего
([qa/live-run.md](qa/live-run.md)), чьи восемь дефектов (D1–D8) исправлены.
Развёртывание описано в [08-deployment.md](docs/08-deployment.md); три шага
остаются ручными и намеренно не автоматизированы: **включение systemd-юнита**
`r2d2-opencode`, **регистрация навыка** в консоли Яндекс.Диалогов и
**объявление привязки Telegram** `R2D2_TG_APPLICATION_ID` парами
`chat_id=application_id` — без неё чат не имеет личности, и `да` в Telegram не
ответит на вопрос, заданный голосом.
