#!/usr/bin/env bash
# Installs R2D2's own opencode config, the r2d2_do shim, and -- on request --
# the systemd user unit that runs the server.
#
# Destinations: ~/.r2d2/opencode for the config (the directory the U1 ADOPTED line
# in docs/11-opencode-contract.md chose for OPENCODE_CONFIG_DIR) and
# ~/.r2d2/r2d2_do.py for the shim.  Per that line the config dir is a layer ON
# TOP of the owner's global opencode.json, not an isolation of it -- see
# config/opencode/README.md for what that implies for the agents.
#
# The config is copied byte for byte, because the install test pins the installed
# bytes to the repo bytes: a stale installed copy would keep the OLD permission
# matrix alive.  The shim is the one file that is *rendered* rather than copied:
# its shebang and its VENV_PY both carry an @R2D2_REPO@ template, and a
# committed absolute interpreter path is a path to somebody else's machine --
# `os.execv` on it raised before any handler ran, so the agent got a traceback
# on stderr and nothing on stdout.
#
# This script only writes files.  It never launches, signals or supervises the
# opencode process: that is a separate systemd user unit, and R2D2 must never
# manage it.  Enabling that unit is a deliberate, manual operator step.
#
# `scripts/install.sh` is the single command that does all of this plus the
# workspace, `.env.oc` and the unit; call that unless you are testing.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONFIG_SRC="$REPO_ROOT/config/opencode/r2d2.opencode.json"
CLI_SRC="$REPO_ROOT/opencode/r2d2_cli/r2d2_do.py"
UNIT_SRC="$REPO_ROOT/scripts/r2d2-opencode.service"
DEST="${HOME}/.r2d2/opencode"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
WORKSPACE="${HOME}/r2d2-workspace"
WANT_UNIT=0
#: The checkout placeholder in the shim's shebang and VENV_PY.  One token, two
#: occurrences, and neither is a path: the committed file names no machine.
REPO_TOKEN="@R2D2_REPO@"

usage() {
  cat <<'EOF'
usage: install_r2d2_opencode_config.sh [--dest DIR] [--unit] [--workspace DIR]
                                       [--unit-dir DIR]

Copies config/opencode/r2d2.opencode.json to DIR/opencode.json (default
~/.r2d2/opencode), creating DIR 0700 and the config 0600.  Installs the r2d2_do
shim to ~/.r2d2/r2d2_do.py (0755) with this checkout's paths substituted for the
@R2D2_REPO@ template, so its shebang and its re-exec target are real here.
With --unit, also writes a systemd user unit to UNIT_DIR/r2d2-opencode.service
(default ${XDG_CONFIG_HOME:-~/.config}/systemd/user) with this checkout's
launcher, .env.oc, docs and --workspace as its directories.  Starts nothing and
enables nothing.
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
    --workspace)
      [ "$#" -ge 2 ] || { echo "--workspace requires a directory" >&2; exit 2; }
      WORKSPACE="$2"
      shift 2
      ;;
    --workspace=*)
      WORKSPACE="${1#--workspace=}"
      shift
      ;;
    --unit-dir)
      [ "$#" -ge 2 ] || { echo "--unit-dir requires a directory" >&2; exit 2; }
      UNIT_DIR="$2"
      shift 2
      ;;
    --unit)
      WANT_UNIT=1
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
if [ -f "$CLI_SRC" ]; then
  mkdir -p "$HOME/.r2d2"
  chmod 700 "$HOME/.r2d2"
  install -m 755 "$CLI_SRC" "$HOME/.r2d2/r2d2_do.py"
  # Bash substitution, not sed: the replacement is taken literally, so a checkout
  # path containing & or / cannot corrupt the file the way a sed replacement can.
  # Written to a sibling and moved, so a failure here cannot leave a shim whose
  # shebang points nowhere.
  SHIM_BODY="$(cat "$HOME/.r2d2/r2d2_do.py")"
  printf '%s\n' "${SHIM_BODY//$REPO_TOKEN/$REPO_ROOT}" > "$HOME/.r2d2/r2d2_do.py.new"
  chmod 755 "$HOME/.r2d2/r2d2_do.py.new"
  mv "$HOME/.r2d2/r2d2_do.py.new" "$HOME/.r2d2/r2d2_do.py"
  echo "installed $HOME/.r2d2/r2d2_do.py"
else
  echo "note: $CLI_SRC not present, shim not installed"
fi

# The committed unit spells its paths with systemd's own %h plus the checkout it
# was written on, so it is a template: this renders the four path directives and
# copies every other line -- the comments, the start-limit burst, the ordering --
# exactly as written.
if [ "$WANT_UNIT" -eq 1 ]; then
  if [ ! -f "$UNIT_SRC" ]; then
    echo "missing unit template: $UNIT_SRC" >&2
    exit 1
  fi
  mkdir -p "$UNIT_DIR"
  chmod 700 "$UNIT_DIR"
  UNIT_DEST="$UNIT_DIR/r2d2-opencode.service"
  while IFS= read -r line; do
    case "$line" in
      WorkingDirectory=*) line="WorkingDirectory=$WORKSPACE" ;;
      EnvironmentFile=*) line="EnvironmentFile=$REPO_ROOT/.env.oc" ;;
      ExecStart=*) line="ExecStart=$REPO_ROOT/scripts/opencode_serve.sh" ;;
      Documentation=*) line="Documentation=file://$REPO_ROOT/docs/08-deployment.md" ;;
    esac
    printf '%s\n' "$line"
  done < "$UNIT_SRC" > "$UNIT_DEST"
  chmod 644 "$UNIT_DEST"
  echo "installed $UNIT_DEST"
else
  echo "note: no unit written (--unit), so this is not yet a runnable deployment;"
  echo "      'bash scripts/install.sh' writes the unit, the workspace and .env.oc too"
fi

echo "point the opencode process at it with: OPENCODE_CONFIG_DIR=$DEST"
