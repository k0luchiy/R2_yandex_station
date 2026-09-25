import asyncio
import time

import psutil

from core.tools.base import ToolContext, ToolResult

SCHEMA = {
    "type": "function",
    "function": {
        "name": "system_status",
        "description": (
            "Узнать статус ноутбука: заряд батареи, нагрузку процессора, занятость памяти, "
            "аптайм. Используй когда спрашивают «какой статус», «сколько заряда», «что грузит CPU»."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "spoken_reply": {
                    "type": "string",
                    "description": "Короткая фраза, которую услышит пользователь сразу",
                },
            },
            "required": ["spoken_reply"],
        },
    },
}


def _status_text() -> str:
    parts = []
    try:
        bat = psutil.sensors_battery()
        if bat is not None:
            charging = " заряжается" if bat.power_plugged else ""
            parts.append(f"заряд {int(bat.percent)} процентов{charging}")
    except Exception:
        pass
    try:
        parts.append(f"процессор {int(psutil.cpu_percent(interval=0.3))} процентов")
    except Exception:
        pass
    try:
        mem = psutil.virtual_memory()
        parts.append(f"память занята на {int(mem.percent)} процентов")
    except Exception:
        pass
    try:
        uptime_s = int(time.time() - psutil.boot_time())
        parts.append(f"аптайм {uptime_s // 3600} часов {uptime_s % 3600 // 60} минут")
    except Exception:
        pass
    return "Статус: " + ", ".join(parts) if parts else "Не смог узнать статус."


async def handler(ctx: ToolContext, args: dict) -> ToolResult:
    return ToolResult(text=_status_text())


APPS = {
    "браузер": "xdg-open https://ya.ru",
    "browser": "xdg-open https://ya.ru",
    "интернет": "xdg-open https://ya.ru",
    "firefox": "firefox",
    "файрфокс": "firefox",
    "chrom": "google-chrome",
    "хром": "google-chrome",
    "код": "code",
    "code": "code",
    "vscode": "code",
    "терминал": "gnome-terminal",
    "terminal": "gnome-terminal",
    "консоль": "gnome-terminal",
    "телеграм": "telegram-desktop",
    "телеграмм": "telegram-desktop",
    "telegram": "telegram-desktop",
    "спотифай": "spotify",
    "spotify": "spotify",
    "файлы": "nautilus",
    "files": "nautilus",
    "проводник": "nautilus",
    "видео": "vlc",
    "video": "vlc",
    "vlc": "vlc",
    "музыка": "rhythmbox",
    "music": "rhythmbox",
    "калькулятор": "qalculate-gtk",
    "calculator": "qalculate-gtk",
    "почта": "thunderbird",
    "mail": "thunderbird",
}


def resolve_app(app: str) -> str | None:
    key = (app or "").strip().lower()
    if not key:
        return None
    if key in APPS:
        return APPS[key]
    for name, cmd in APPS.items():
        if name in key or key in name:
            return cmd
    return None


async def open_app(ctx: ToolContext, args: dict) -> ToolResult:
    app = args.get("app") or ""
    cmd = resolve_app(app)
    if not cmd:
        return ToolResult(f"Не знаю приложения «{app}».", ok=False)
    proc = await asyncio.create_subprocess_shell(
        f"nohup {cmd} >/dev/null 2>&1 &",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await asyncio.wait_for(proc.communicate(), timeout=5.0)
    spoken = args.get("spoken_reply") or f"Открываю {app}."
    return ToolResult(text=spoken)


APP_SCHEMA = {
    "type": "function",
    "function": {
        "name": "open_app",
        "description": "Запустить приложение на ноутбуке. Примеры: браузер, код, терминал, телеграм, спотифай, файлы.",
        "parameters": {
            "type": "object",
            "properties": {
                "app": {"type": "string", "description": "Название приложения"},
                "spoken_reply": {"type": "string", "description": "Короткая фраза, которую услышит пользователь"},
            },
            "required": ["app", "spoken_reply"],
        },
    },
}
