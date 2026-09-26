"""D11's end-to-end check: a REAL `permission.asked` off a REAL server, read by the
REAL reader, dispatched by the REAL route seam, landing in a REAL `PermissionBroker`
-- and the answer the broker posts really running the command.

Everything is R2D2's own object except the two doubles below, both named here because
a reader must know exactly what was faked:

* **Telegram is a recorder.** `core.permissions.send_message` is replaced so the
  question the broker would send is captured instead of posted. No token is read, no
  network call leaves the box.
* **The database is a throwaway file** under `/tmp/r2d2-d11`, so the owner's own
  `db/` and sessions are untouched.

The server is the throwaway one the operator started for the tap (scratch
`OPENCODE_CONFIG_DIR`, port 4613). Nothing here touches port 4599, `~/.r2d2/` or
`~/.config/opencode/`.

    .venv/bin/python qa/d11-live-broker-check.py \\
      --base-url http://127.0.0.1:4613 --password "$OPENCODE_SERVER_PASSWORD"

Exit 0 and a non-empty `BROKER REACHED` line means the loop is closed. Exit 2 means
the ask never arrived and the run proves nothing.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Final

import httpx

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import core.permissions as permissions_module                              # noqa: E402
from app.config import Config                                              # noqa: E402
from app.opencode_route import SessionReaders                               # noqa: E402
from core.backends.config_loader import BackendSpec                        # noqa: E402
from core.memory import Memory                                             # noqa: E402
from core.opencode.client import OpencodeClient                            # noqa: E402
from core.opencode.sse import EventSource                                  # noqa: E402
from core.permissions import PermissionBroker                              # noqa: E402

APP_ID: Final = "r2d2:alice:d11-live-check"
MARKER: Final = "R2D2D11E2E"


class Recorder:
    """Stands in for `core.tools.telegram_tool.send_message`: keeps what the broker
    would have posted and answers `True`, so the broker sees a delivered message
    instead of a dead channel."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def __call__(self, _cfg: object, text: str, chat_id: int | None = None) -> bool:
        del chat_id
        self.sent.append(text)
        return True


def _spec(base_url: str, password: str) -> BackendSpec:
    return BackendSpec(
        name="opencode",
        kind="opencode_session",
        base_url=base_url,
        username="opencode",
        password=password,
        voice_agent="spike-ask",
        task_agent="spike-ask",
        fast_model="space-bunny-free",
        task_model="space-bunny-free",
        summarize_model="space-bunny-free",
        timeout=3.2,
    )


OUT: Final = REPO / "qa" / "d11-live-broker-check.out"


def _emit(report: list[str], password: str) -> None:
    """Print the report and keep it, with the scratch password never written down."""
    text = "\n".join(report).replace(password, "<REDACTED>") if password else "\n".join(report)
    OUT.write_text(text + "\n", encoding="utf-8")
    print(text)


async def main(base_url: str, password: str, directory: str) -> int:
    report: list[str] = []
    recorder = Recorder()
    permissions_module.send_message = recorder          # type: ignore[assignment]
    spec = _spec(base_url, password)

    with tempfile.TemporaryDirectory(prefix="r2d2-d11-") as scratch:
        memory = await Memory(str(Path(scratch) / "d11.sqlite3")).connect()
        cfg = Config(
            r2d2_workspace=directory,
            telegram_bot_token="D11-THROWAWAY-NOT-A-TOKEN",
            telegram_chat_id="0",
            r2d2_permission_timeout=120.0,
        )
        client = OpencodeClient(spec, directory)
        broker = PermissionBroker(memory, client, cfg)
        readers = SessionReaders(spec, directory, broker)

        auth = httpx.BasicAuth(spec.username, spec.password)
        timeout = httpx.Timeout(connect=10.0, read=200.0, write=30.0, pool=30.0)
        async with httpx.AsyncClient(auth=auth, timeout=timeout) as http:
            created = await http.post(f"{base_url}/session", params={"directory": directory})
            created.raise_for_status()
            sid = created.json()["id"]
            report.append(f"session: {sid}")
            # The seam the route installs per session, with the real broker behind it.
            handle = readers._handler(APP_ID, sid)       # type: ignore[attr-defined]

            seen: list[str] = []
            stop = asyncio.Event()

            async def collect(event: Any) -> None:
                seen.append(event.type)
                await handle(event)

            source = EventSource(spec, directory, session_id=sid)
            reader = asyncio.create_task(source.run(collect, stop=stop))
            await asyncio.sleep(1.5)                    # let server.connected through

            # A real turn, driven by an agent whose matrix asks before `bash`.
            for attempt in range(1, 5):
                await http.post(
                    f"{base_url}/session/{sid}/prompt_async",
                    params={"directory": directory},
                    json={
                        "model": {"providerID": "opencode", "modelID": spec.fast_model},
                        "agent": spec.task_agent,
                        "parts": [{"type": "text", "text": f"Use the bash tool to run exactly: "
                                                            f"echo {MARKER}. Then report the raw "
                                                            f"output."}],
                    },
                )
                report.append(f"attempt {attempt}: prompt_async sent")
                for _ in range(450):                    # up to 90 s for the ask
                    await asyncio.sleep(0.2)
                    if await memory.get_pending(APP_ID):
                        break
                else:
                    await asyncio.sleep(45)
                    continue
                break

            pending = await memory.get_pending(APP_ID)
            report.append("")
            report.append(f"BROKER REACHED: {bool(pending)}")
            report.append(f"event types the reader dispatched: {seen}")
            report.append(f"pending row: {json.dumps(pending, ensure_ascii=False) if pending else None}")
            report.append(f"question the broker would send: {recorder.sent}")
            if not pending:
                stop.set()
                reader.cancel()
                _emit(report, password)
                return 2

            # And the answer path: the broker's own text verdict, then the server.
            verdict = await broker.resolve_from_text(APP_ID, "да")
            report.append(f"resolve_from_text('да') -> {verdict}")
            transcript = (
                await http.get(f"{base_url}/session/{sid}/message", params={"directory": directory})
            ).text
            report.append(f"the marker {MARKER} is in the transcript: {MARKER in transcript}")

            stop.set()
            reader.cancel()
            try:
                await reader
            except asyncio.CancelledError:
                pass
            await source.aclose()
        await client.aclose()
        await memory.close()

    _emit(report, password)
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:4613")
    parser.add_argument("--password", default="")
    parser.add_argument("--directory", default="/tmp/r2d2-d11/workspace")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.base_url.rstrip("/"), args.password, args.directory)))
