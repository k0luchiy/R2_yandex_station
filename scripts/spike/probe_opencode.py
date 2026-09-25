#!/usr/bin/env python3
"""Real-server probe for the six undocumented opencode behaviours U1-U6.

This module starts and stops nothing. The operator starts `opencode serve`
(see scripts/spike/README.md) and points this probe at it:

    .venv/bin/python scripts/spike/probe_opencode.py \
        --base-url http://127.0.0.1:4598 --out /tmp/r2d2-qa/spike.json

Every request carries a hard timeout so a hung server can never wedge the
probe. If the server is not reachable the probe fails loudly on stderr and
exits non-zero instead of writing a file full of zeros.

# allow: SIZE_OK — spike harness. Six independent measurements, each of which
# must keep its raw server payload verbatim; splitting the file would scatter
# the evidence that docs/11-opencode-contract.md cites line by line.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any, Final

import httpx

# allow: SIZE_OK (see module docstring)
FAST_MODEL: Final = "space-bunny-free"
STRONG_MODEL: Final = "muse-spark-1.3-contributor-free"
STRONG_CANDIDATES: Final = ("muse-spark-1.3", "gpt-5.4-nano", "qwen3.8-flash",
                            "gemini-3.5-flash-lite", "nemotron-3.5-lightning")
PROVIDER: Final = "opencode"
DENY_MARKER: Final = "R2D2SPIKEDENY7Q"
RUN_MARKER: Final = "R2D2SPIKERUN3K"

DEFAULT_SLOW_TIMEOUT: Final = 60.0
DEFAULT_FAST_TIMEOUT: Final = 20.0
MAX_STREAMED_EVENTS: Final = 600


class ProbeError(RuntimeError):
    """A probe could not complete; the reason is recorded, never swallowed."""


def _p50(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def _p95(values: list[float]) -> float | None:
    """Nearest-rank p95: the smallest sample at or above the 95th percentile."""
    if not values:
        return None
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, -(-95 * len(ordered) // 100) - 1))
    return ordered[idx]


def summarise(samples: list[float]) -> dict[str, float | int | None]:
    return {
        "n": len(samples),
        "min": round(min(samples), 3) if samples else None,
        "p50": round(_p50(samples) or 0.0, 3) if samples else None,
        "p95": round(_p95(samples) or 0.0, 3) if samples else None,
        "max": round(max(samples), 3) if samples else None,
    }


def assistant_text(payload: dict[str, Any]) -> str:
    """Concatenate type=="text" parts in order, ignoring reasoning/tool parts."""
    return "".join(
        str(p.get("text", ""))
        for p in payload.get("parts", [])
        if isinstance(p, dict) and p.get("type") == "text"
    )


def message_rows(raw: Any) -> list[dict[str, Any]]:
    """GET /session/:id/message yields [{info, parts}], NOT a flat message list."""
    if not isinstance(raw, list):
        return []
    return [r for r in raw if isinstance(r, dict) and isinstance(r.get("info"), dict)]


def tool_output(rows: list[dict[str, Any]]) -> str:
    """Concatenated stdout of every tool part - proof a command actually ran."""
    return "\n".join(
        str((p.get("state") or {}).get("output", ""))
        for r in rows
        for p in r.get("parts", [])
        if isinstance(p, dict) and p.get("type") == "tool"
    )


def last_rule(ruleset: list[dict[str, Any]], permission: str) -> dict[str, Any] | None:
    """opencode permission semantics: last matching rule wins."""
    hit: dict[str, Any] | None = None
    for rule in ruleset:
        if rule.get("permission") == permission:
            hit = rule
        elif rule.get("permission") == "*":
            hit = rule
    return hit


class Opencode:
    """Thin synchronous client. One instance owns one httpx.Client."""

    def __init__(self, base_url: str, directory: str) -> None:
        self.base = base_url.rstrip("/")
        self.dir = directory
        self.http = httpx.Client(
            base_url=self.base,
            timeout=httpx.Timeout(DEFAULT_FAST_TIMEOUT),
            follow_redirects=False,
        )

    def close(self) -> None:
        self.http.close()

    def q(self, path: str) -> dict[str, str]:
        return {"directory": self.dir}

    def get(self, path: str, *, slow: bool = False, **kw: Any) -> httpx.Response:
        return self.http.get(
            path, params=kw.pop("params", None) or self.q(path),
            timeout=kw.pop("timeout", DEFAULT_SLOW_TIMEOUT if slow else DEFAULT_FAST_TIMEOUT), **kw)

    def post(self, path: str, body: Any, *, slow: bool = False,
             expect: int | None = None) -> httpx.Response:
        r = self.http.post(
            path, json=body, params=self.q(path),
            timeout=DEFAULT_SLOW_TIMEOUT if slow else DEFAULT_FAST_TIMEOUT)
        if expect is not None and r.status_code != expect:
            raise ProbeError(f"{path} -> HTTP {r.status_code} (expected {expect}): {r.text[:400]}")
        return r

    # -- session / message helpers -------------------------------------------------

    def new_session(self, title: str, *, directory: str | None = None) -> dict[str, Any]:
        params = {"directory": directory} if directory is not None else self.q("/session")
        r = self.http.post("/session", json={"title": title}, params=params,
                           timeout=DEFAULT_FAST_TIMEOUT)
        if r.status_code != 200:
            raise ProbeError(f"POST /session -> HTTP {r.status_code}: {r.text[:400]}")
        return r.json()

    def message(self, sid: str, text: str, *, agent: str, model: str = FAST_MODEL,
                system: str | None = None, tools: dict[str, bool] | None = None,
                slow: bool = True) -> httpx.Response:
        body: dict[str, Any] = {
            "model": {"providerID": PROVIDER, "modelID": model},
            "agent": agent,
            "parts": [{"type": "text", "text": text}],
        }
        if system is not None:
            body["system"] = system
        if tools is not None:
            body["tools"] = tools
        return self.post(f"/session/{sid}/message", body, slow=slow)

    def messages(self, sid: str) -> list[dict[str, Any]]:
        return message_rows(self.get(f"/session/{sid}/message").json())

    # -- SSE -----------------------------------------------------------------------

    def stream_events(self, sink: list[dict[str, Any]], stop: threading.Event) -> None:
        """Read GET /event until `stop`; hand-rolled SSE parse, no dependency."""
        try:
            with httpx.Client(base_url=self.base, timeout=None) as c:
                with c.stream("GET", "/event", params=self.q("/event")) as resp:
                    if resp.status_code != 200:
                        sink.append({"_stream_error": f"HTTP {resp.status_code}"})
                        return
                    ev: dict[str, Any] = {}
                    data: list[str] = []
                    for line in resp.iter_lines():
                        if stop.is_set():
                            return
                        if line == "":
                            if data:
                                try:
                                    ev["data"] = json.loads("\n".join(data))
                                except json.JSONDecodeError:
                                    ev["data"] = "\n".join(data)
                                sink.append(ev)
                                if len(sink) >= MAX_STREAMED_EVENTS:
                                    stop.set()
                                    return
                            ev, data = {}, []
                            continue
                        if line.startswith(":"):
                            continue
                        field, _, value = line.partition(":")
                        value = value[1:] if value.startswith(" ") else value
                        if field == "event":
                            ev["event"] = value
                        elif field == "data":
                            data.append(value)
        except Exception as exc:  # stream must never take the probe down
            sink.append({"_stream_error": f"{type(exc).__name__}: {exc}"})


# ---------------------------------------------------------------------------------
# U1 - does OPENCODE_CONFIG_DIR isolate agents + permissions without breaking auth?
# ---------------------------------------------------------------------------------

def probe_u1(oc: Opencode, out: dict[str, Any]) -> None:
    agents = oc.get("/agent", slow=True).json()
    names = sorted(str(a.get("name")) for a in agents)
    scratch = [n for n in names if n.startswith("spike-")]
    # opencode's own built-ins prove nothing about config isolation; everything
    # else that is not a spike agent came from the user's global setup.
    builtins = {"build", "plan", "general", "explore", "compaction", "summary", "title"}
    foreign = [n for n in names if not n.startswith("spike-") and n not in builtins]
    by_name = {str(a.get("name")): a for a in agents}
    providers = oc.get("/config/providers", slow=True).json()
    prov_list = providers.get("providers", providers) if isinstance(providers, dict) else providers
    if isinstance(prov_list, dict):
        prov_list = [dict(v, _id=k) for k, v in prov_list.items()]
    prov_ids = sorted(str(p.get("_id") or p.get("id")) for p in prov_list)

    # behavioural proof: a deny-all agent must not be able to run bash
    sid = oc.new_session("u1-deny")["id"]
    ask = (f"Use the bash tool to run exactly this command: echo {DENY_MARKER}. "
           f"Then report the output verbatim. If you have no bash tool, say NO_BASH_TOOL.")
    r = oc.message(sid, ask, agent="spike-deny", slow=True)
    text = assistant_text(r.json()) if r.status_code == 200 else f"HTTP {r.status_code}: {r.text[:200]}"
    info = r.json().get("info", {}) if r.status_code == 200 else {}
    bash_rule = last_rule(by_name.get("spike-deny", {}).get("permission") or [], "bash")

    out["u1_config_isolation"] = {
        "question": "Does OPENCODE_CONFIG_DIR isolate agent definitions and permission rules, "
                    "and does the Zen credential (in ~/.local/share/opencode/auth.json) still work?",
        "scratch_agents_visible": scratch,
        "foreign_agents_still_visible": foreign,
        "foreign_agent_count": len(foreign),
        "global_config_merged": bool(foreign),
        "provider_ids_visible": prov_ids,
        "global_providers_visible": [p for p in prov_ids if p != "opencode"],
        "spike_deny_bash_effective_rule": bash_rule,
        "spike_deny_bash_rule_action": (bash_rule or {}).get("action"),
        "deny_turn_status": r.status_code,
        "deny_turn_text": text.strip()[:600],
        "deny_tool_parts": len([p for p in (r.json().get("parts", []) if r.status_code == 200 else [])
                                if p.get("type") == "tool"]),
        "deny_marker_executed": DENY_MARKER in tool_output(oc.messages(sid)),
        "deny_turn_mode": info.get("mode"),
        "deny_turn_model": f"{info.get('providerID')}/{info.get('modelID')}",
        "deny_turn_finish": info.get("finish"),
        "deny_turn_input_tokens": (info.get("tokens") or {}).get("input"),
        "credential_works": r.status_code == 200 and info.get("finish") == "stop"
                            and (info.get("tokens") or {}).get("input", 0) > 0,
    }


# ---------------------------------------------------------------------------------
# U2 - is the message-body `system` per-message or session-persistent?
# ---------------------------------------------------------------------------------

def probe_u2(oc: Opencode, out: dict[str, Any]) -> None:
    sid = oc.new_session("u2-system")["id"]
    # Instruction-following, not self-report: asking "which system are you under"
    # made the 1-word free model parrot a token from its own history, so the test
    # was flaky. A tag prefix is an observable behaviour change.
    first = oc.message(sid, "hello", agent="spike-voice",
                       system="Begin every reply with the tag [A].", slow=True)
    second = oc.message(sid, "hello again", agent="spike-voice",
                        system="Begin every reply with the tag [B].", slow=True)
    rows = oc.messages(sid)
    users = [r["info"] for r in rows if r["info"].get("role") == "user"]
    first_text = assistant_text(first.json()).strip()
    second_text = assistant_text(second.json()).strip()
    out["u2_system_param"] = {
        "question": "Is the message-body `system` parameter per-message or session-persistent?",
        "sent_systems": ["Begin every reply with the tag [A].", "Begin every reply with the tag [B]."],
        "first_reply": first_text[:200],
        "second_reply": second_text[:200],
        "first_reply_used_own_system": "[A]" in first_text,
        "second_reply_used_own_system": "[B]" in second_text,
        "second_reply_reused_first_system": "[A]" in second_text and "[B]" not in second_text,
        "stored_user_systems": [u.get("system") for u in users],
        "stored_user_message_ids": [u.get("id") for u in users],
        "messages_endpoint_shape": "[{info:{role,system,agent,model}, parts:[...]}]",
        "protocol_ground_truth": ("each user message persists its own `system` field verbatim; "
                                  "the stored list proves the second turn was not overwritten"),
    }


# ---------------------------------------------------------------------------------
# U3 - what shape does the message-body `tools` parameter accept?
# ---------------------------------------------------------------------------------

def _await_event(events: list[dict[str, Any]], sid: str, want: str, timeout: float,
                 since: int = 0) -> dict[str, Any] | None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        for e in events[since:]:
            data = e.get("data")
            if isinstance(data, dict) and data.get("type") == want \
                    and (data.get("properties") or {}).get("sessionID", sid) == sid:
                return data
        time.sleep(0.2)
    return None


def probe_u3(oc: Opencode, out: dict[str, Any]) -> None:
    doc = oc.get("/doc", slow=True).json()
    shape = doc["paths"]["/session/{sessionID}/message"]["post"]["requestBody"] \
        ["content"]["application/json"]["schema"]["properties"]["tools"]

    # Empirical A/B on ONE agent whose only reachable tool is bash. Without this the
    # server's ambient tool set answers instead and the measurement means nothing.
    # Both cases go through prompt_async: a blocking call would sit forever on `ask`.
    events: list[dict[str, Any]] = []
    stop = threading.Event()
    t = threading.Thread(target=oc.stream_events, args=(events, stop), daemon=True)
    t.start()
    time.sleep(1.0)

    def one_case(title: str, tools: dict[str, bool] | None) -> dict[str, Any]:
        sid = oc.new_session(title)["id"]
        mark = len(events)
        body: dict[str, Any] = {
            "model": {"providerID": PROVIDER, "modelID": FAST_MODEL},
            "agent": "spike-bashonly",
            "parts": [{"type": "text", "text": "Use the bash tool to run exactly: "
                                               "echo R2D2SPIKETOOLS. Report the raw output."}],
        }
        if tools is not None:
            body["tools"] = tools
        r = oc.post(f"/session/{sid}/prompt_async", body, expect=204)
        asked = _await_event(events, sid, "permission.asked", 120, mark)
        entry: dict[str, Any] = {
            "tools_sent": tools,
            "prompt_async_status": r.status_code,
            "raised_permission_asked": asked is not None,
            "permission_verbatim": asked,
        }
        if asked is not None:
            oc.post(f"/session/{sid}/permissions/{asked['properties']['id']}",
                    {"response": "reject"}, expect=200)
        _await_event(events, sid, "session.idle", 120, mark)
        rows = oc.messages(sid)
        entry["event_types"] = sorted({str(e["data"]["type"]) for e in events[mark:]
                                       if isinstance(e.get("data"), dict) and "type" in e["data"]})
        entry["tool_parts"] = sum(1 for rr in rows for p in rr.get("parts", [])
                                  if isinstance(p, dict) and p.get("type") == "tool")
        entry["text"] = assistant_text({"parts": [p for rr in rows for p in rr.get("parts", [])]
                                        }).strip()[:300]
        return entry

    without = one_case("u3-no-tools-key", None)
    with_false = one_case("u3-tools-bash-false", {"bash": False})
    stop.set()
    t.join(timeout=5)

    out["u3_tools_param"] = {
        "question": "What shape does the message-body `tools` parameter accept?",
        "openapi_schema_verbatim": shape,
        "agent_used": "spike-bashonly (permission: {\"*\":\"deny\",\"bash\":\"ask\"})",
        "case_no_tools_key": without,
        "case_tools_bash_false": with_false,
        "tools_key_changes_behaviour": (without["raised_permission_asked"]
                                        != with_false["raised_permission_asked"]),
    }


# ---------------------------------------------------------------------------------
# U4 - exact SSE event.type strings for a permission request and turn completion
# ---------------------------------------------------------------------------------

def probe_u4(oc: Opencode, out: dict[str, Any]) -> None:
    events: list[dict[str, Any]] = []
    stop = threading.Event()
    sid = oc.new_session("u4-events")["id"]
    t = threading.Thread(target=oc.stream_events, args=(events, stop), daemon=True)
    t.start()
    time.sleep(1.0)

    r = oc.post(f"/session/{sid}/prompt_async",
                {"model": {"providerID": PROVIDER, "modelID": FAST_MODEL},
                 "agent": "spike-ask",
                 "parts": [{"type": "text",
                            "text": f"Use the bash tool to run exactly: echo {RUN_MARKER}. "
                                    f"Then report the output verbatim."}]},
                expect=204)
    perm_raw = _await_event(events, sid, "permission.asked", 120)
    if perm_raw is None:
        stop.set()
        t.join(timeout=5)
        raise ProbeError("no permission.asked event within 120s")

    pid = str(perm_raw["properties"]["id"])
    reply = oc.post(f"/session/{sid}/permissions/{pid}", {"response": "once"}, expect=200)
    _await_event(events, sid, "session.idle", 120)
    time.sleep(0.5)
    stop.set()
    t.join(timeout=5)

    # A second round replies to a REAL permission with `remember` in the body. The
    # plan assumes {response, remember?} is accepted; the spec declares only
    # {response}, and unknown keys are not rejected elsewhere, so only a real
    # permission can settle it.
    events2: list[dict[str, Any]] = []
    stop2 = threading.Event()
    t2 = threading.Thread(target=oc.stream_events, args=(events2, stop2), daemon=True)
    t2.start()
    time.sleep(1.0)
    oc.post(f"/session/{sid}/prompt_async",
            {"model": {"providerID": PROVIDER, "modelID": FAST_MODEL},
             "agent": "spike-ask",
             "parts": [{"type": "text", "text": "Use the bash tool to run: echo R2D2SPIKEREMEMBER."}]},
            expect=204)
    perm2 = _await_event(events2, sid, "permission.asked", 120)
    if perm2 is not None:
        with_remember = oc.post(f"/session/{sid}/permissions/{perm2['properties']['id']}",
                                {"response": "reject", "remember": True})
        remember_result = {"status": with_remember.status_code, "body": with_remember.text[:300],
                           "accepted": with_remember.status_code < 400}
    else:
        remember_result = {"status": None, "body": "no second permission was raised",
                           "accepted": None}
    stop2.set()
    t2.join(timeout=5)

    seq = [str(e["data"]["type"]) for e in events
           if isinstance(e.get("data"), dict) and "type" in e["data"]]
    reply_events = [e for e in events if isinstance(e.get("data"), dict)
                    and e["data"].get("type") in ("permission.replied", "permission.v2.replied")]
    rows = oc.messages(sid)
    text = assistant_text({"parts": [p for r in rows for p in r.get("parts", [])]})
    out["u4_sse_event_types"] = {
        "question": "What are the exact SSE event.type strings for a permission request and for "
                    "turn completion?",
        "first_event": seq[0] if seq else None,
        "distinct_types_in_order": list(dict.fromkeys(seq)),
        "counts": dict(Counter(seq)),
        "permission_event_verbatim": perm_raw,
        "permission_reply_status": reply.status_code,
        "permission_reply_body": reply.text[:300],
        "permission_replied_event_verbatim": reply_events[0]["data"] if reply_events else None,
        "permission_body_with_remember_on_real_permission": remember_result,
        "tool_marker_executed_after_once": RUN_MARKER in tool_output(rows),
        "tail_after_permission": seq[seq.index("permission.asked"):] if "permission.asked" in seq else [],
    }


# ---------------------------------------------------------------------------------
# U5 - can POST /session set the working directory?
# ---------------------------------------------------------------------------------

def probe_u5(oc: Opencode, out: dict[str, Any]) -> None:
    alt = "/tmp/r2d2-qa/other"
    Path(alt).mkdir(parents=True, exist_ok=True)
    no_param = oc.new_session("u5-no-directory")
    via_query = oc.new_session("u5-directory-query", directory=alt)
    body_key = oc.http.post("/session", json={"title": "u5-body-key", "directory": alt},
                            params=oc.q("/session"), timeout=DEFAULT_FAST_TIMEOUT)
    proj_with = oc.get("/project/current", params={"directory": oc.dir}).json()
    proj_without = oc.http.get("/project/current", timeout=DEFAULT_FAST_TIMEOUT).json()
    listed = oc.get("/session", params={"directory": alt}).json()
    out["u5_session_directory"] = {
        "question": "Can POST /session set the session working directory, or is it fixed by the "
                    "server's cwd?",
        "server_cwd_at_start": oc.dir,
        "no_directory_param": {"directory": no_param.get("directory"),
                               "path": no_param.get("path"),
                               "projectID": no_param.get("projectID")},
        "directory_query_param": {"sent": alt, "directory": via_query.get("directory"),
                                  "path": via_query.get("path")},
        "directory_body_key": {"status": body_key.status_code,
                               "sent": alt,
                               "directory": body_key.json().get("directory") if
                               body_key.status_code == 200 else body_key.text[:200]},
        "directory_query_honoured": via_query.get("directory") == alt,
        "project_current_with_directory_param": proj_with,
        "project_current_without_directory_param": proj_without,
        "get_session_filtered_by_directory": [(s.get("title"), s.get("directory")) for s in listed],
    }


# ---------------------------------------------------------------------------------
# U6 - real wall-clock latency
# ---------------------------------------------------------------------------------

def _opencode_models(oc: Opencode) -> list[str]:
    payload = oc.get("/config/providers", slow=True).json()
    provs = payload.get("providers", payload) if isinstance(payload, dict) else payload
    if isinstance(provs, dict):
        provs = [dict(v, _id=k) for k, v in provs.items()]
    for p in provs:
        if (p.get("_id") or p.get("id")) == PROVIDER:
            return sorted((p.get("models") or {}).keys())
    return []


def _latency_run(oc: Opencode, model: str, count: int) -> dict[str, Any]:
    sid = oc.new_session(f"u6-{model}")["id"]
    samples: list[float] = []
    rows: list[dict[str, Any]] = []
    for i in range(count):
        row = _one_turn(oc, sid, model, DEFAULT_SLOW_TIMEOUT)
        elapsed = float(row["elapsed_s"])
        text = str(row.get("text", ""))
        row["i"] = i
        if row.get("usable"):
            samples.append(elapsed)
        rows.append(row)
        print(f"  [{model}] sample {i + 1}/{count}: {row['elapsed_s']}s "
              f"status={row.get('status')} usable={row.get('usable')} {text[:40]}", flush=True)
    result = summarise(samples)
    result["model"] = model
    result["samples"] = rows
    result["failed"] = len(rows) - len(samples)
    return result


def _api_error(payload: dict[str, Any]) -> dict[str, Any] | None:
    """A 200 can still be a failure: opencode puts the error in info.error."""
    err = (payload.get("info") or {}).get("error")
    return err if isinstance(err, dict) else None


def _one_turn(oc: Opencode, sid: str, model: str, cap: float) -> dict[str, Any]:
    started = time.monotonic()
    row: dict[str, Any] = {"model": model, "cap_s": cap}
    try:
        r = oc.message(sid, "Reply with exactly: PONG", agent="spike-voice", model=model, slow=True)
    except httpx.HTTPError as exc:
        row.update({"status": None, "elapsed_s": round(time.monotonic() - started, 3),
                    "transport_error": f"{type(exc).__name__}: {exc}", "text": ""})
        return row
    elapsed = time.monotonic() - started
    row["elapsed_s"] = round(elapsed, 3)
    row["status"] = r.status_code
    if r.status_code != 200:
        row.update({"text": "", "error": r.text[:200]})
        return row
    payload = r.json()
    text = assistant_text(payload).strip()
    err = _api_error(payload)
    row.update({"text": text[:120], "http_status": 200,
                "error": err if err else None,
                "usable": bool(text) and err is None})
    if err is None:
        info = payload.get("info") or {}
        row["input_tokens"] = (info.get("tokens") or {}).get("input")
        row["cache_read"] = ((info.get("tokens") or {}).get("cache") or {}).get("read")
        row["finish"] = info.get("finish")
    return row


def _sweep(oc: Opencode, models: list[str]) -> list[dict[str, Any]]:
    """A 200 is not success. opencode reports upstream refusal as HTTP 200 +
    info.error, so every candidate is judged on its text, never on its status."""
    sid = oc.new_session("u6-model-sweep")["id"]
    rows = []
    for m in models:
        row = _one_turn(oc, sid, m, 40.0)
        rows.append(row)
        print(f"  [sweep] {m}: {row.get('elapsed_s')}s usable={row.get('usable')} "
              f"error={(row.get('error') or {}).get('data', {}).get('message') if row.get('error') else None}",
              flush=True)
    return rows


def probe_u6(oc: Opencode, out: dict[str, Any]) -> None:
    catalogue = _opencode_models(oc)
    free = sorted(m for m in catalogue if m.endswith("-free"))
    paid = [m for m in STRONG_CANDIDATES if m in catalogue]
    print(f"  U6: sweeping {len(free)} free + {len(paid)} paid opencode models ...", flush=True)
    sweep = _sweep(oc, free + paid)
    usable = [r["model"] for r in sweep if r.get("usable") and r["model"] != FAST_MODEL]

    print("  U6: 10 samples of space-bunny-free ...", flush=True)
    fast = _latency_run(oc, FAST_MODEL, 10)

    if usable:
        strong_id = usable[0]
        print(f"  U6: 3 samples of {strong_id} (first usable free alternative) ...", flush=True)
        strong = _latency_run(oc, strong_id, 3)
    else:
        strong_id = STRONG_MODEL
        strong = {"model": STRONG_MODEL, "n": 0, "p50": None, "p95": None, "min": None,
                  "max": None, "failed": 0, "samples": [],
                  "note": "no free alternative is usable from `opencode serve`; see model_sweep"}

    out["u6_latency"] = {
        "question": "What is the real wall-clock latency of POST /session/:id/message?",
        "latency_space_bunny_free": fast,
        "latency_muse_spark_1_3_contributor_free": {
            **strong, "requested_model": STRONG_MODEL,
            "requested_model_usable": STRONG_MODEL in usable,
        },
        "latency_measured_alternative": strong_id if usable else None,
        "usable_models": [FAST_MODEL] + usable,
        "model_sweep": sweep,
        "session_reused_across_samples": True,
        "timeout_cap_s": DEFAULT_SLOW_TIMEOUT,
        "judged_on": "text content, never on HTTP status (200 + info.error is a failure)",
    }


# ---------------------------------------------------------------------------------
# adversarial classes
# ---------------------------------------------------------------------------------

def probe_adversarial(oc: Opencode, out: dict[str, Any]) -> None:
    sid = oc.new_session("adversarial")["id"]
    base = {"model": {"providerID": PROVIDER, "modelID": FAST_MODEL},
            "agent": "spike-voice",
            "parts": [{"type": "text", "text": "hi"}]}

    def mutate(**over: Any) -> dict[str, Any]:
        return {**base, **over}

    cases: dict[str, Any] = {
        "bogus_agent_and_bogus_model": mutate(agent="no-such-agent-zzz",
                                              model={"providerID": "no-such-provider-zzz",
                                                     "modelID": "no-such-model-zzz"}),
        "bogus_agent_only": mutate(agent="no-such-agent-zzz"),
        "bogus_model_only": mutate(model={"providerID": PROVIDER, "modelID": "no-such-model-zzz"}),
        "missing_parts": {"model": {"providerID": PROVIDER, "modelID": FAST_MODEL},
                          "agent": "spike-voice"},
        "tools_as_list_wrong_type": mutate(tools=["bash"]),
        "unknown_body_key": mutate(nonsenseKey=123),
    }
    results: dict[str, Any] = {}
    for name, body in cases.items():
        entry: dict[str, Any] = {"sent": body}
        try:
            r = oc.http.post(f"/session/{sid}/message", json=body, params=oc.q("/session"),
                             timeout=20.0)
            entry.update({"status": r.status_code, "body": r.text[:400],
                          "looks_like_error": _looks_like_error(r),
                          "elapsed_cap_s": 20})
        except httpx.HTTPError as exc:
            entry.update({"status": None, "error": f"{type(exc).__name__}: {exc}",
                          "looks_like_error": True, "elapsed_cap_s": 20,
                          "note": "the call did not answer inside the 20s cap - a hang is the finding"})
        results[name] = entry
    # bogus session id + bogus permission body
    bad_sid = oc.http.get("/session/ses_doesnotexist0000000/message", params=oc.q("/x"),
                          timeout=DEFAULT_FAST_TIMEOUT)
    remember = oc.http.post(f"/session/{sid}/permissions/per_doesnotexist0000",
                            json={"response": "once", "remember": True},
                            params=oc.q("/x"), timeout=DEFAULT_FAST_TIMEOUT)
    bad_response = oc.http.post(f"/session/{sid}/permissions/per_doesnotexist0000",
                                json={"response": "maybe"},
                                params=oc.q("/x"), timeout=DEFAULT_FAST_TIMEOUT)
    bad_path_param = oc.http.get("/session/notasessionid/message", params=oc.q("/x"),
                                 timeout=DEFAULT_FAST_TIMEOUT)
    out["adversarial"] = {
        "message_bodies": results,
        "bogus_session_id_get_messages": {"status": bad_sid.status_code,
                                          "body": bad_sid.text[:300]},
        "permission_body_with_remember": {"status": remember.status_code,
                                          "body": remember.text[:300]},
        "permission_body_bad_enum_value": {"status": bad_response.status_code,
                                           "body": bad_response.text[:300]},
        "path_param_fails_pattern": {"request": "GET /session/notasessionid/message",
                                     "status": bad_path_param.status_code,
                                     "body": bad_path_param.text[:300]},
        "note": "a 2xx is NOT treated as success: every case records the body too",
    }


def _looks_like_error(r: httpx.Response) -> bool:
    if r.status_code >= 400:
        return True
    try:
        data = r.json()
    except ValueError:
        return True
    if not isinstance(data, dict):
        return True
    info = data.get("info") or {}
    return bool(info.get("error")) or not any(
        p.get("type") == "text" and str(p.get("text", "")).strip()
        for p in data.get("parts", []) if isinstance(p, dict))


PROBES = {
    "u1": probe_u1, "u2": probe_u2, "u3": probe_u3,
    "u4": probe_u4, "u5": probe_u5, "u6": probe_u6, "adversarial": probe_adversarial,
}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--directory", default="/tmp/r2d2-qa/workspace",
                    help="absolute path sent as the ?directory= query parameter")
    ap.add_argument("--only", default="", help="comma-separated subset of: " + ",".join(PROBES))
    args = ap.parse_args(argv)

    oc = Opencode(args.base_url, args.directory)
    try:
        try:
            health = oc.http.get("/global/health", timeout=DEFAULT_FAST_TIMEOUT)
            health.raise_for_status()
            health_body = health.json()
        except (httpx.HTTPError, ValueError) as exc:
            print(f"opencode unreachable at {args.base_url}: {exc}", file=sys.stderr)
            return 2
        if not health_body.get("healthy"):
            print(f"opencode unreachable at {args.base_url}: health={health_body}", file=sys.stderr)
            return 2

        selected = [p.strip() for p in args.only.split(",") if p.strip()] or list(PROBES)
        results: dict[str, Any] = {
            "base_url": args.base_url,
            "directory_query_param": args.directory,
            "server_health": health_body,
            "errors": {},
        }
        for name in selected:
            print(f"  running probe {name} ...", flush=True)
            try:
                PROBES[name](oc, results)
            except Exception as exc:
                results["errors"][name] = f"{type(exc).__name__}: {exc}"
                print(f"  probe {name} FAILED: {exc}", file=sys.stderr)
        results["summary"] = _summarise(results)
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
        print(json.dumps(results["summary"], indent=2), flush=True)
        return 0 if results["summary"]["ok"] else 1
    finally:
        oc.close()


def _summarise(r: dict[str, Any]) -> dict[str, Any]:
    lat = r.get("u6_latency") or {}
    fast_p50 = ((lat.get("latency_space_bunny_free") or {}).get("p50"))
    strong_p50 = ((lat.get("latency_muse_spark_1_3_contributor_free") or {}).get("p50"))
    ran = sorted(k for k in r if isinstance(r[k], dict) and "question" in r[k])
    return {
        "ok": fast_p50 is not None and not r.get("errors"),
        "probes_run": ran,
        "probes_failed": sorted(r.get("errors", {})),
        "latency_space_bunny_free_p50": fast_p50,
        "latency_strong_p50": strong_p50,
    }


if __name__ == "__main__":
    raise SystemExit(main())
