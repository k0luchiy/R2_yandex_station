import asyncio
import logging
from dataclasses import dataclass

from app.config import Config
from core.memory import Memory


@dataclass
class ToolContext:
    cfg: Config
    memory: Memory
    application_id: str
    logger: logging.Logger
    approved: bool = False


@dataclass
class ToolResult:
    text: str = ""
    ok: bool = True
    needs_confirm: bool = False
    pending: dict | None = None
    is_async: bool = False
    job: dict | None = None
    tg_send: str | None = None
    error: str | None = None


async def run_shell_process(command: str, timeout: float = 10.0) -> tuple[str, int]:
    proc = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        stdin=asyncio.subprocess.DEVNULL,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return "timeout", -1
    return out.decode("utf-8", errors="replace"), proc.returncode or 0
