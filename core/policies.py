import re

SAFE_PREFIXES = (
    "ls", "pwd", "df", "ps", "cat", "echo", "date", "uptime", "whoami",
    "free", "du", "uname", "hostname", "git status", "git diff",
)

RISKY_KEYWORDS = (
    "rm ", "rm -", "rmdir", "mkfs", "shutdown", "sudo", "reboot", "kill",
    "pkill", "dd ", "chmod 777", "chmod +x", "chown", "mount", "fdisk",
    "mke2fs", "format", "wipe", "> /dev", "crontab",
)

CAUTION_KEYWORDS = (
    "systemctl", "service ", "apt ", "dnf ", "pacman", "pip install",
    "git pull", "npm install", "killall",
)

AFFIRM_WORDS = (
    "да", "подтверждаю", "подтверди", "выполняй", "валяй", "давай", "ок",
    "окей", "согласен", "конечно", "точно", "выполнить", "выполняем", "ага",
    "да да", "угу",
)

DENY_WORDS = (
    "нет", "не надо", "отмена", "отмени", "отменить", "стоп", "хватит",
    "не", "неа", "остановись",
)


def risk_level(command: str) -> str:
    low = command.strip().lower()
    low = re.sub(r"\s+", " ", low)
    if not low:
        return "safe"
    if any(k in low for k in RISKY_KEYWORDS):
        return "dangerous"
    if any(k in low for k in CAUTION_KEYWORDS):
        return "caution"
    return "safe"


def confirmation_verdict(text: str | None) -> str | None:
    if not text:
        return None
    low = text.strip().lower()
    low = re.sub(r"\s+", " ", low)
    if any(w in low for w in DENY_WORDS):
        return "no"
    if any(w in low for w in AFFIRM_WORDS):
        return "yes"
    return None
