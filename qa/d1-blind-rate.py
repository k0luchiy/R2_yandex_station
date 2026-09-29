"""D1 re-measurement, live, using R2D2's OWN shipped EventSource.

Two readers on the same real session against the real `opencode serve`:

  * the shipped bound  -- `event_read_timeout` from config/backends.json (30.0 s)
  * the pre-fix bound  -- `timeout` from the same spec (3.2 s), which is what
    `core/opencode/sse.py` used to hand `httpx.AsyncClient(timeout=...)`

Both are driven through the shipped `EventSource.run()` (same reconnect ladder,
same `_timeouts()`, same session filter). The only thing that differs is
`event_read_timeout`. What is measured, per reader:

  * `connected_s`   -- wall-clock seconds the socket was actually open
  * `blind_s`        -- wall-clock seconds the reader had no connection, i.e.
                        the fraction of every permission ask it could have
                        missed
  * `connects`       -- successful GET /event 200s (server.connected frames)
  * `reconnects`     -- ladder climbs
  * frames by type, and `permission.asked` seen
"""
import asyncio
import os
import sys
import time
from dataclasses import replace

sys.path.insert(0, "/home/<user>/Documents/R2_yandex_station")

from app.config import Config                                    # noqa: E402
from core.backends.config_loader import load_backend_specs      # noqa: E402
from core.opencode.sse import CONNECTED, PERMISSION_ASKED, EventSource  # noqa: E402

SESSION = sys.argv[1]
WINDOW = float(sys.argv[2]) if len(sys.argv) > 2 else 60.0


class Timed(EventSource):
    """`EventSource` with the connection's up/down times measured, nothing else."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.connected_s = 0.0
        self.connects = 0
        self.reconnects = 0
        self.frames: dict[str, int] = {}

    async def events(self):  # type: ignore[override]
        opened = time.monotonic()
        try:
            async for event in super().events():
                if event.type == CONNECTED:
                    self.connects += 1
                    self.connected_s += time.monotonic() - opened
                    opened = time.monotonic()
                yield event
        finally:
            self.connected_s += time.monotonic() - opened


async def measure(spec, read_timeout: float, label: str) -> Timed:
    src = Timed(replace(spec, event_read_timeout=read_timeout), spec.base_url,
                session_id=SESSION)
    seen: dict[str, int] = src.frames
    stop = asyncio.Event()
    t0 = time.monotonic()

    async def handler(event) -> None:
        seen[event.type] = seen.get(event.type, 0) + 1

    task = asyncio.create_task(src.run(handler, stop=stop))
    await asyncio.sleep(WINDOW)
    stop.set()
    await asyncio.sleep(0.2)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await src.aclose()
    wall = time.monotonic() - t0
    blind = max(0.0, wall - src.connected_s)
    return {
        "label": label,
        "read_timeout_s": read_timeout,
        "wall_s": round(wall, 2),
        "connected_s": round(src.connected_s, 2),
        "blind_s": round(blind, 2),
        "blind_pct": round(100.0 * blind / wall, 1),
        "connects": src.connects,
        "permission_asked": seen.get(PERMISSION_ASKED, 0),
        "frames": dict(sorted(seen.items())),
    }


async def main() -> None:
    cfg = Config.load()
    _chain, specs = load_backend_specs(cfg)
    spec = specs["opencode"]
    print(f"# session={SESSION}  window={WINDOW}s")
    print(f"# shipped spec: timeout={spec.timeout}s "
          f"event_read_timeout={spec.event_read_timeout}s")
    results = []
    for label, value in (("PRE-FIX (read=timeout)", spec.timeout),
                         ("SHIPPED (read=event_read_timeout)", spec.event_read_timeout)):
        results.append(await measure(spec, value, label))
    for r in results:
        print()
        for k, v in r.items():
            print(f"{k}: {v}")


asyncio.run(main())
