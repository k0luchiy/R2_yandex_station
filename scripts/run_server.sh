#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
source .venv/bin/activate
exec uvicorn app.main:app --host "${SERVER_HOST:-127.0.0.1}" --port "${SERVER_PORT:-8080}" "$@"
