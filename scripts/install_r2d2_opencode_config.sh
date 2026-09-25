#!/usr/bin/env bash
# Installs R2D2's own opencode config.
#
# Destination: ~/.r2d2/opencode, the directory the U1 ADOPTED line in
# docs/11-opencode-contract.md chose for OPENCODE_CONFIG_DIR.  Per that line it
# is a layer ON TOP of the owner's global opencode.json, not an isolation of
# it -- see config/opencode/README.md for what that implies for the agents.
#
# This script only copies files.  It never launches, signals or supervises the
# opencode process: that is a separate systemd user unit, and R2D2 must never
# manage it.  Enabling that unit is a deliberate, manual operator step.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG_SRC="$REPO_ROOT/config/opencode/r2d2.opencode.json"
DEST="${HOME}/.r2d2/opencode"

usage() {
  cat <<'EOF'
usage: install_r2d2_opencode_config.sh [--dest DIR]

Copies config/opencode/r2d2.opencode.json to DIR/opencode.json (default
~/.r2d2/opencode), creating DIR 0700 and the config 0600.  Also installs the
r2d2_do shim to ~/.r2d2/r2d2_do.py when its source is present.  Starts nothing.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --dest)
      [ "$#" -ge 2 ] || { echo "--dest requires a directory" >&2; exit 2; }
      DEST="$2"
      shift 2
      ;;
    --dest=*)
      DEST="${1#--dest=}"
      shift
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [ ! -f "$CONFIG_SRC" ]; then
  echo "missing source config: $CONFIG_SRC" >&2
  exit 1
fi

mkdir -p "$DEST"
chmod 700 "$DEST"
install -m 600 "$CONFIG_SRC" "$DEST/opencode.json"
echo "installed $DEST/opencode.json"

# The shim is installed next to the config because the config's bash allowlist
# names that exact path.  It arrives in a later step, so its absence is normal.
CLI_SRC="$REPO_ROOT/opencode/r2d2_cli/r2d2_do.py"
if [ -f "$CLI_SRC" ]; then
  mkdir -p "$HOME/.r2d2"
  chmod 700 "$HOME/.r2d2"
  install -m 755 "$CLI_SRC" "$HOME/.r2d2/r2d2_do.py"
  echo "installed $HOME/.r2d2/r2d2_do.py"
else
  echo "note: $CLI_SRC not present, shim not installed"
fi

echo "point the opencode process at it with: OPENCODE_CONFIG_DIR=$DEST"
