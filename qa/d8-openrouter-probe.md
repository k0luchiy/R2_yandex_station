# D8 — the fallback chain had no working member: a live sweep of OpenRouter

Plan: `.omo/plans/opencode-brain.md`, defect **D8** from
[live-run.md](live-run.md). Measured on this machine against the real
OpenRouter API on **2026-09-26, 02:5x–03:4x UTC**, with the operator's own key
read from `$OPENROUTER_API_KEY` by name. **No credential value appears in this
file, in any probe script, or in anything committed.** `.env` and `.env.oc` are
not tracked and were not staged.

## Verdict

**The chain works now, and it is one measured model, not a plausible-looking id.**

| | |
|---|---|
| free ids advertised by `GET /models` | **17** |
| free ids that answered at least one probe | **9** |
| free ids that answered **every** probe | **1** — `inclusionai/ling-3.0-flash-sante:free` |
| the id that was configured before this sweep | `inclusionai/ling-3.0-flash:free` — **HTTP 404**, absent from the catalogue |
| free-tier quota on this key | **50 requests/day** (`X-RateLimit-Limit: 50`), exhausted by this sweep |

The previous configuration named `inclusionai/ling-3.0-flash:free`. It is no
longer served for free; `POST /chat/completions` answers HTTP 404 with
`{"error":{"message":"This model is unavailable for free. The paid version is
available now - use this slug instead", ...}}`, which is what
[live-run.md](live-run.md) §D8 recorded during the run. So the chain was
declared, looked complete, and could not answer: with `zen` dropped
(`${R2D2_ZEN_KEY}` unset) and `yandexgpt` dropped (`YANDEX_API_KEY` empty),
losing `opencode` meant `ERROR_TEXT` and nothing else.

## Method

Three rounds, all against `https://openrouter.ai/api/v1`, `Authorization: Bearer
$OPENROUTER_API_KEY`, one request per (model, case) with ≥0.5 s between them:

1. **catalogue** — `GET /models`; 458 models advertised, 17 with a `:free` id.
2. **sweep** — every one of the 17, one trivial prompt
   (`Reply with exactly one word: PONG`, `max_tokens=32`).
3. **stability** — the ten that returned HTTP 200, three samples of the trivial
   prompt and three of a realistic Russian digest request
   (`max_tokens=1200`, the shape `arxiv_tool.summarize_entries` sends), then the
   two finalists again for five trivial samples, two one-word Russian voice
   samples and two digests.

A free `:free` id was counted as **answering** only with HTTP 200 **and**
non-empty `content`. HTTP 200 with an empty body is counted as a failure, and
that distinction is the whole finding: `parse_choice` turns an empty body into a
`Choice` with `content=""`, the brain speaks that as silence, and a fallback
that returns nothing is not a fallback.

## What answered, and how

`inclusionai/ling-3.0-flash-sante:free` — the configured model after this sweep.

| case | result | latency |
|---|---|---|
| one-word prompt ×5 | 5/5 `PONG`, `finish_reason: stop` | 0.84, 0.90, 1.01, 1.52, 1.85 s |
| one-word Russian ×2 | 2/2 `Париж` | 0.94, 0.95 s |
| Russian digest ×2 | 2/2, correct Russian prose on both arXiv abstracts | 4.04, 4.35 s |

That is 9 of 9. It is also, per `GET /models`, a **health-and-medicine-focused
mixture-of-experts variant** of Ling 3.0 Flash (5.1B active of 124B). The domain
tuning is a real caveat and is recorded in
[docs/05-llm-provider.md](../docs/05-llm-provider.md) §2.1; it is what was
configured anyway, because the alternative on this machine is a chain that
cannot answer at all.

## What did not answer, and how it failed

| id | failure |
|---|---|
| `inclusionai/ling-3.0-flash:free` (was configured) | **HTTP 404** `This model is unavailable for free`; absent from `GET /models` |
| `google/gemma-4-31b-it:free` | **HTTP 429** `Provider returned error`, 6 of 6 probes, 0.15–0.32 s |
| `google/gemma-4-26b-a4b-it:free` | **HTTP 429** `Provider returned error` |
| `qwen/qwen3.8-27b:free` | **HTTP 429** `Provider returned error`, 6 of 6 probes |
| `thinkingmachines/inkling:free`, `…-small:free` | **HTTP 403** `is only available on agentic harnesses. Try plugging it into a coding agent` |
| `liquid/lfm-2.5-2.6b:free` | **HTTP 502** once; then HTTP 200 with **empty content**, 0 of 5 one-word probes — 3/3 on digests, so it looks usable until you ask it for one word |
| `inclusionai/ling-3.0-flash-fin:free` | HTTP 200 with **empty content**, 0 of 5 one-word probes; 3/3 on digests at 1.95–4.70 s |
| `cohere/north-mini-code:free` | HTTP 200 with **empty content**, 0 of 3 one-word probes |
| `poolside/laguna-xs-2.1:free` | HTTP 200 with **empty content** 0 of 3; HTTP 429 on 2 of 6 |
| `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free` | HTTP 200 with **empty content** on 5 of 9; one probe took 57.32 s |
| `poolside/laguna-s-2.1:free` | answers, but **HTTP 429 on 2 of 6** |
| `nvidia/nemotron-3-ultra-550b-a55b:free` | answers 6/6, but one draw took **82.12 s** (`finish_reason: length`) — fatal inside a 3.2 s voice budget |
| `nvidia/nemotron-3.5-lightning:free` | answers, one draw took **55.01 s** |
| `nvidia/nemotron-3-super-120b-a12b:free` | answers with its **reasoning**, not with an answer: `The user says: "Reply with exactly one word: PONG". So we must output…` |
| `nvidia/nemotron-3.5-content-safety:free`, `dots-studio/dots-3-note-preview:free` | HTTP 200 with **empty content** |

Two rejections are worth naming, because both would have looked like a working
fallback in a status check:

* **`liquid/lfm-2.5-2.6b:free` is the best Russian digest model of the sixteen**
  (1.60–2.94 s, correct Russian) **and cannot answer "Париж"** — 0 of 5, every
  answer HTTP 200 with an empty body and the whole `max_tokens` budget spent on
  reasoning. `OpenAICompatibleBackend` has no "empty content means try the next
  backend" rule, so it would have been the configured fallback and it would have
  answered nothing.
* **`nvidia/nemotron-3-ultra-550b-a55b:free` is fast when it is fast** — 0.59 s
  and 0.97 s on two of three trivial probes, 4.17–7.26 s on digests — and 82.12 s
  on the third. A fallback with a 20 s tail is a fallback that sometimes hangs.

## The limit this sweep found by exhausting it

The final verification call through the project's own `build_chain` returned:

```
HTTP 429 ({"error":{"message":"Rate limit exceeded: free-models-per-day.
Add 10 credits to unlock 1000 free model requests per day","code":429,
"metadata":{"headers":{"X-RateLimit-Limit":"50","X-RateLimit-Remaining":"0",
"X-RateLimit-Reset":"1790467200"}}}})
```

`X-RateLimit-Reset` = `1790467200` = **2026-09-27T00:00:00Z**. Fifty free requests
a day is the whole budget, and this sweep spent it. Consequences, stated plainly:

* the post-change call through `config/backends.json` **could not be repeated**
  after the config was edited — the quota was already gone. The 9 successful
  probes above are the live evidence, and they were made against this exact id on
  this exact endpoint;
* what *was* verified after the edit, offline, is the half that is ours: with a
  mocked transport, the registry built from `config/backends.json` sends
  `POST https://openrouter.ai/api/v1/chat/completions` with
  `Authorization: Bearer <redacted>` and body `"model":"inclusionai/ling-3.0-flash-sante:free"`;
* **the free fallback is rate-limited to 50 calls a day on this key.** That is
  the honest size of "it degrades gracefully": it degrades to a working answer
  while the quota lasts, and to HTTP 429 after it, which `build_chain`'s walk
  turns into `ERROR_TEXT`. A funded key is the only way past it.

## What changed because of this file

| | |
|---|---|
| `config/backends.json` | `openrouter.model`: `inclusionai/ling-3.0-flash:free` → `inclusionai/ling-3.0-flash-sante:free` |
| `docs/05-llm-provider.md` | both occurrences of the model id, plus §2.1 recording the sweep and the three caveats |
| `tests/test_backend_registry.py` | the sweep is now a table the suite checks: a configured `openai_compatible` model that the sweep measured failing fails the suite, with a mutation test proving the guard fires |

The chain **order** is unchanged, and no backend was added or removed. `zen` and
`yandexgpt` are still dropped on this machine with a WARNING naming the empty
`api_key`, which is the correct behaviour and not part of this defect.

## Reproducing

```bash
cd /home/koluchiy/Documents/R2_yandex_station
set -a; . ./.env; set +a            # OPENROUTER_API_KEY by name, never echoed
# 1. the catalogue
curl -s -H "Authorization: Bearer $OPENROUTER_API_KEY" \
  https://openrouter.ai/api/v1/models | .venv/bin/python -c \
  'import json,sys; print("\n".join(sorted(m["id"] for m in json.load(sys.stdin)["data"] if m["id"].endswith(":free"))))'
# 2. one probe
curl -s -H "Authorization: Bearer $OPENROUTER_API_KEY" -H 'Content-Type: application/json' \
  -d '{"model":"inclusionai/ling-3.0-flash-sante:free","messages":[{"role":"user","content":"Назови столицу Франции. Одно слово."}],"max_tokens":400}' \
  https://openrouter.ai/api/v1/chat/completions
```

Both commands need quota: as of this file, the daily 50 are spent and the second
one answers HTTP 429 until 2026-09-27T00:00:00Z.
