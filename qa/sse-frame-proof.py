"""Live proof of what opencode 1.18.32's `GET /event` actually puts on the wire,
and what R2D2's shipped decoder makes of it. Read-only; opens one stream."""
import asyncio
import collections
import sys

import httpx

sys.path.insert(0, "/home/<user>/Documents/R2_yandex_station")

from app.config import Config                                    # noqa: E402
from core.backends.config_loader import load_backend_specs      # noqa: E402
from core.opencode.sse_frames import decode_frame, frames       # noqa: E402

cfg = Config.load()
_chain, specs = load_backend_specs(cfg)
spec = specs["opencode"]
auth = httpx.BasicAuth(spec.username, spec.password)
SESSION = "ses_f23bb5dbeffePCgTNtG36cby0v"
WINDOW = 22.0

report: list[str] = []


async def main() -> None:
    timeout = httpx.Timeout(connect=3.2, read=30.0, write=3.2, pool=3.2)
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream(
            "GET", f"{spec.base_url}/event",
            params={"directory": cfg.r2d2_workspace}, auth=auth,
        ) as response:
            report.append(f"HTTP {response.status_code} from GET /event")
            lines = 0
            first: list[str] = []
            async for line in response.aiter_lines():
                lines += 1
                if len(first) < 3:
                    first.append(line)
                if lines >= 4000:
                    break
            report.append(f"RAW lines read: {lines}")
            report.append(f"lines starting with 'event:': "
                          f"{sum(1 for line in first if line.startswith('event:'))} of the first 3")
            for i, line in enumerate(first):
                report.append(f"  raw line {i}: {line[:100]!r}")


async def decoded() -> None:
    timeout = httpx.Timeout(connect=3.2, read=30.0, write=3.2, pool=3.2)
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream(
            "GET", f"{spec.base_url}/event",
            params={"directory": cfg.r2d2_workspace}, auth=auth,
        ) as response:
            types: collections.Counter[str] = collections.Counter()
            owner: collections.Counter[str] = collections.Counter()
            n = 0
            async for event in frames(response):
                n += 1
                types[event.type] += 1
                sid = event.session_id
                owner["MINE" if sid == SESSION else ("no-sessionID" if sid is None else "other")] += 1
            report.append(f"DECODED frames by the shipped frames(): {n}")
            report.append(f"decoded event.type histogram: {dict(types)}")
            report.append(f"session filter outcome: {dict(owner)}")


async def drive() -> None:
    for coro in (main, decoded):
        try:
            await asyncio.wait_for(coro(), WINDOW)
        except TimeoutError:
            report.append(f"({coro.__name__}: window {WINDOW}s elapsed, stream still open)")
        except Exception as exc:  # noqa: BLE001
            report.append(f"({coro.__name__}: {type(exc).__name__}: {exc})")


# Offline half: what decode_frame makes of a REAL wire line, verbatim.
async def drive_all() -> None:
    await drive()
    sample = (
        'data: {"id":"evt_x","type":"permission.asked","properties":'
        '{"id":"per_x","sessionID":"ses_x","permission":"bash","always":["ls *"]}}'
    )
    body = sample[len("data:"):].strip()
    as_seen = decode_frame("", [body])            # what frames() passes: no `event:` line
    as_expected = decode_frame("permission.asked", [body])
    report.append("")
    report.append("OFFLINE: decode_frame() on a verbatim wire line")
    report.append(f"  wire line has an 'event:' field? {'event:' in sample}")
    report.append(f"  decode_frame(<no event name>, ...) -> type={as_seen.type!r}")
    report.append(f"  decode_frame('permission.asked', ...) -> type={as_expected.type!r}, "
                  f"permission_id={as_expected.permission_id!r}")


asyncio.run(drive_all())
print("\n".join(report))
