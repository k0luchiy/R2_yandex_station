#!/usr/bin/env bash
# Start the gateway on SERVER_HOST:SERVER_PORT.
#
# app/config.py no longer calls load_dotenv() at import time, so nothing reads
# .env unless this script does it. That is deliberate -- a process should not
# acquire credentials merely by being imported -- but it means a bare
# `uvicorn app.main:app` starts a server with no configuration at all: no
# ALICE_SKILL_ID, no TELEGRAM_BOT_TOKEN, no LLM provider, and every /webhook
# request refused with "Доступ запрещён." The server looks healthy while being
# entirely inert, which is a bad way to discover the problem.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -f .venv/bin/activate ]; then
  echo "run_server.sh: no .venv here. The venv must live at <checkout>/.venv --" >&2
  echo "  see README 'Install'. Not falling back to a system interpreter." >&2
  exit 1
fi
# shellcheck source=/dev/null
source .venv/bin/activate

# .env fills in only what the environment does not already define, so an
# explicitly exported variable wins. This is the convention a caller expects,
# and it is the safer order: a test harness that exports fake credentials must
# not have the real ones from .env silently override them.
#
# Only plain KEY=VALUE lines are handled, which is all .env.example documents
# and all the shipped files use. A value containing quotes would keep them
# literally, so quotes stay unsupported rather than half-supported.
if [ -f .env ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      ''|'#'*) continue ;;
      *=*) ;;
      *) continue ;;
    esac
    name=${line%%=*}
    case "$name" in
      [A-Za-z_]*) ;;
      *) continue ;;
    esac
    if [ -z "${!name+x}" ]; then
      export "$line"
    fi
  done < .env
  echo "run_server.sh: configuration read from .env (already-set variables win)" >&2
else
  echo "run_server.sh: no .env here. Starting with the environment only." >&2
  echo "  Without ALICE_SKILL_ID and ALICE_USER_ID every /webhook request is" >&2
  echo "  refused on purpose, so the server will look healthy and be inert." >&2
  echo "  cp .env.example .env && chmod 0600 .env" >&2
fi

exec uvicorn app.main:app --host "${SERVER_HOST:-127.0.0.1}" --port "${SERVER_PORT:-8080}" "$@"
