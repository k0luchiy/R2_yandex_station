#!/usr/bin/env bash
set -euo pipefail
TUNNEL_NAME="${1:-r2d2}"
exec cloudflared tunnel run "$TUNNEL_NAME"
