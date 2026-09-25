#!/usr/bin/env bash
# Starts the opencode server that R2D2's brain talks to. The systemd user unit
# scripts/r2d2-opencode.service runs this; R2D2 itself never does.
#
# The refusal below is the whole point of this script. opencode's server auth is
# HTTP basic via OPENCODE_SERVER_PASSWORD, and with that variable unset the
# server does not warn -- it answers every route, on a plain HTTP port, to every
# process on this box. OPENCODE_SERVER_PASSWORD is not set anywhere here, so a
# launcher that started without a check would hand out an unauthenticated
# session store, an unrestricted agent runtime and a bash tool. A blank
# R2D2_OC_PASSWORD is no password and is refused the same way.
#
# The port is always passed explicitly: the docs claim a default of 4096 while
# `serve --help` on this build claims 0, and a launcher that trusted either
# would move the port on the next upgrade. The binary is the 1.18.32 absolute
# path because 1.18.21 and 1.18.5 are also installed and a bare `opencode`
# resolves to whichever sorts first.
set -euo pipefail

BIN="${R2D2_OC_BIN:-/home/koluchiy/.opencode/bin/opencode}"
CONFIG_DIR="${R2D2_OC_CONFIG_DIR:-${HOME}/.r2d2/opencode}"
WORKSPACE="${R2D2_OC_WORKSPACE:-/home/koluchiy/r2d2-workspace}"
PORT="${R2D2_OC_PORT:-4599}"
PASSWORD="${R2D2_OC_PASSWORD:-}"

if [ -z "${PASSWORD//[[:space:]]/}" ]; then
  echo "opencode_serve.sh: refusing to start." >&2
  echo "  R2D2_OC_PASSWORD is empty, so OPENCODE_SERVER_PASSWORD would be empty too" >&2
  echo "  and every local process could drive this server with no authentication." >&2
  echo "  Put a password in .env.oc (chmod 0600) and start the unit again." >&2
  exit 1
fi

if [ ! -x "$BIN" ]; then
  echo "opencode_serve.sh: $BIN is missing or not executable" >&2
  exit 1
fi

if [ ! -d "$WORKSPACE" ]; then
  echo "opencode_serve.sh: workspace $WORKSPACE does not exist or is not a directory" >&2
  echo "  the server's cwd is where every session it creates points, so it will not be created" >&2
  echo "  for you: mkdir -p $WORKSPACE" >&2
  exit 1
fi

if [ ! -d "$CONFIG_DIR" ]; then
  echo "opencode_serve.sh: warning: $CONFIG_DIR is absent." >&2
  echo "  R2D2's agents are not defined, and OPENCODE_CONFIG_DIR merges with your global" >&2
  echo "  config rather than isolating it, so your global permission rules would apply." >&2
  echo "  Install R2D2's config with scripts/install_r2d2_opencode_config.sh" >&2
fi

export OPENCODE_SERVER_PASSWORD="$PASSWORD"
export OPENCODE_CONFIG_DIR="$CONFIG_DIR"
export OPENCODE_SERVER_USERNAME="${R2D2_OC_USERNAME:-opencode}"

cd "$WORKSPACE"
exec "$BIN" serve --hostname 127.0.0.1 --port "$PORT"
