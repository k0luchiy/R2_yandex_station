"""D11's evidence: what opencode 1.18.32's `GET /event` frame really is, captured
from a throwaway server, and what the shipped decoder makes of each body.

Read-only with respect to the user's own setup. It talks ONLY to the scratch server
the operator started for it (throwaway `OPENCODE_CONFIG_DIR` under /tmp, port 4613),
and it drives one permission ask of its own so the three frames R2D2 dispatches on
are captured live rather than reconstructed.

    cd /tmp/r2d2-d11/workspace
    OPENCODE_CONFIG_DIR=/tmp/r2d2-d11/config \
    OPENCODE_LOG_LEVEL=warn OPENCODE_SERVER_PASSWORD=<throwaway> \
      /home/koluchiy/.opencode/bin/opencode serve --hostname 127.0.0.1 --port 4613 &
    .venv/bin/python qa/d11-wire-tap.py --base-url http://127.0.0.1:4613 \
      --password "$OPENCODE_SERVER_PASSWORD"

Prints the report AND writes it to `qa/d11-wire-tap.out`. The frames the test
suite pins are the ones in the VERBATIM section, character for character as the
server sent them.
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import json
import sys
import time
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from core.opencode.sse_frames import decode_frame, frames  # noqa: E402

AGENT = "spike-ask"           # `{"*": "allow", "bash": "ask"}` in the throwaway config
PROVIDER = "opencode"
#: `modelID` alone, NOT the `provider/modelID` this repo's config spells: the server
#: resolves the pair and answers `session.error` "Model not found: opencode/opencode/
#: space-bunny-free" when the prefix is repeated (C1's silent-substitute cousin).
MODEL = "space-bunny-free"
MARKER = "R2D2D11TAP"
OUT = REPO / "qa" / "d11-wire-tap.out"

#: The frames R2D2 dispatches on, and the deadline each gets. `message.part.delta`
#: is only emitted while a text part streams, so it is wanted from the very start.
WANTED = (
    "server.connected",
    "permission.asked",
    "permission.replied",
    "session.idle",
    "message.part.delta",
    "server.heartbeat",
)


class Tap:
    """One `GET /event` connection, recording raw lines and decoded events."""

    def __init__(self, client: httpx.AsyncClient, base: str, auth: httpx.Auth, directory: str):
        self._client, self._base, self._auth, self._dir = client, base, auth, directory
        self.raw_lines: list[str] = []
        self.bodies: list[str] = []
        self.event_lines: list[str] = []
        self.types: list[str | None] = []

    async def run(self) -> None:
        timeout = httpx.Timeout(connect=5.0, read=90.0, write=5.0, pool=5.0)
        async with self._client.stream(
            "GET", f"{self._base}/event", params={"directory": self._dir},
            auth=self._auth, timeout=timeout,
        ) as response:
            if response.status_code != 200:
                raise SystemExit(f"GET /event -> HTTP {response.status_code}")
            data: list[str] = []
            event_line = ""
            async for line in response.aiter_lines():
                self.raw_lines.append(line)
                if line == "":
                    if data:
                        body = "\n".join(data)
                        self.bodies.append(body)
                        self.event_lines.append(event_line)
                        got = decode_frame(event_line, data)
                        self.types.append(got.type if got else None)
                    event_line, data = "", []
                elif line.startswith(":"):
                    continue
                else:
                    field, _, value = line.partition(":")
                    if value.startswith(" "):
                        value = value[1:]
                    if field == "event":
                        event_line = value
                    elif field == "data":
                        data.append(value)

    def of_type(self, want: str) -> str | None:
        """The first recorded body whose own `type` is `want`, verbatim."""
        for body in self.bodies:
            try:
                payload = json.loads(body)
            except ValueError:
                continue
            if isinstance(payload, dict) and payload.get("type") == want:
                return body
        return None


async def main(base: str, password: str, directory: str) -> list[str]:
    auth = httpx.BasicAuth("opencode", password)
    report: list[str] = [
        f"D11 wire tap -- opencode 1.18.32, GET /event at {base}, directory {directory}",
        "",
    ]
    # POST /session answered in 7.9 s cold on this build, so every non-stream request
    # gets a generous bound; the stream's own bound is 90 s, set in Tap.run().
    plain = httpx.Timeout(connect=10.0, read=180.0, write=30.0, pool=30.0)
    async with httpx.AsyncClient(auth=auth, timeout=plain) as client:
        tap = Tap(client, base, auth, directory)
        reader = asyncio.create_task(tap.run())
        await asyncio.sleep(1.5)                      # let server.connected land

        created = await client.post(f"{base}/session", params={"directory": directory})
        created.raise_for_status()
        sid = created.json()["id"]
        report.append(f"session: {sid}")

        # The one usable free model 429s and the turn then ends in `session.error`
        # (README: free models give 429), which costs an attempt without raising an
        # ask. So a turn that only errored is retried rather than reported as "the
        # server never asks" -- two of four runs on this build needed the retry.
        asked: str | None = None
        for attempt in range(1, 5):
            prompt = await client.post(
                f"{base}/session/{sid}/prompt_async",
                params={"directory": directory},
                json={
                    "model": {"providerID": PROVIDER, "modelID": MODEL},
                    "agent": AGENT,
                    "parts": [{"type": "text", "text": f"Use the bash tool to run exactly: "
                                                        f"echo {MARKER}. Then report the raw output."}],
                },
            )
            report.append(f"attempt {attempt}: POST /session/{{id}}/prompt_async "
                          f"-> HTTP {prompt.status_code}")
            asked = await _wait_for_body(tap, "permission.asked", 90)
            if asked is not None:
                break
            await asyncio.sleep(45)                   # the throttle is per minute
        if asked is None:
            report.append("NO permission.asked WAS RAISED -- the rest of the run is unproven.")
        else:
            per_id = json.loads(asked)["properties"]["id"]
            answered = await client.post(
                f"{base}/session/{sid}/permissions/{per_id}",
                params={"directory": directory}, json={"response": "once"},
            )
            report.append(f"POST /session/{{id}}/permissions/{{per_id}} -> "
                          f"HTTP {answered.status_code} body {answered.text[:20]!r}")
        await _wait_for_body(tap, "permission.replied", 90)
        await _wait_for_body(tap, "session.idle", 150)
        if tap.of_type("message.part.delta") is None:
            # A tool-only turn streams no text, so ask a second, tool-free question:
            # `message.part.delta` only exists while a text part is being written.
            await client.post(
                f"{base}/session/{sid}/prompt_async",
                params={"directory": directory},
                json={
                    "model": {"providerID": PROVIDER, "modelID": MODEL},
                    "agent": AGENT,
                    "parts": [{"type": "text", "text": "Reply with the single word: ok"}],
                },
            )
            await _wait_for_body(tap, "message.part.delta", 150)
            await _wait_for_body(tap, "session.idle", 120)
        await asyncio.sleep(11)                       # one more heartbeat gap
        reader.cancel()
        try:
            await reader
        except asyncio.CancelledError:
            pass

    # -- what the wire showed -------------------------------------------------------
    field_lines = [line for line in tap.raw_lines if not line.startswith(":")]
    report += [
        "",
        "== THE WIRE ==",
        f"lines read:                       {len(tap.raw_lines)}",
        f"lines starting with 'data:':      {sum(1 for l in field_lines if l.startswith('data:'))}",
        f"lines starting with 'event:':     {sum(1 for l in field_lines if l.startswith('event:'))}",
        f"lines starting with ':' (comment):{sum(1 for l in tap.raw_lines if l.startswith(':'))}",
        f"blank dispatch lines:             {sum(1 for l in tap.raw_lines if l == '')}",
        f"bodies recorded:                  {len(tap.bodies)}",
        f"first three raw lines:            {[l[:60] for l in tap.raw_lines[:3]]}",
    ]
    named = collections.Counter(json.loads(b)["type"] for b in tap.bodies
                                if b.startswith('{"') and '"type"' in b)
    report.append(f"bodies by their own \"type\":     {dict(named)}")
    report.append(f"decode_frame() types, as shipped: {dict(collections.Counter(tap.types))}")

    # -- what the shipped decoder makes of each named frame -------------------------
    report += ["", "== VERBATIM BODIES, AS THE SERVER SENT THEM =="]
    for want in WANTED:
        body = tap.of_type(want)
        if body is None:
            report += [f"--- {want}: NOT SEEN ON THIS RUN ---"]
            continue
        report += [f"--- {want}", f"data: {body}"]
    for body in tap.bodies:
        if '"type":"session.error"' in body:
            report += ["--- session.error (why a turn produced no ask)", f"data: {body}"]
            break
    return report


async def _wait_for_body(tap: Tap, want: str, timeout: float) -> str | None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        body = tap.of_type(want)
        if body is not None:
            return body
        await asyncio.sleep(0.2)
    return None


def _sanitize(text: list[str], password: str) -> None:
    for i, line in enumerate(text):
        text[i] = line.replace(password, "<REDACTED>") if password else line


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:4613")
    parser.add_argument("--password", default="", help="OPENCODE_SERVER_PASSWORD of the scratch server")
    parser.add_argument("--directory", default="/tmp/r2d2-d11/workspace")
    args = parser.parse_args()
    lines = asyncio.run(main(args.base_url.rstrip("/"), args.password, args.directory))
    _sanitize(lines, args.password)
    text = "\n".join(lines)
    OUT.write_text(text + "\n", encoding="utf-8")
    print(text)
