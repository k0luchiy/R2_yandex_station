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
# would move the port on the next upgrade.
#
# The binary is resolved, never guessed, and the order is fixed:
#   1. R2D2_OC_BIN, verbatim -- the operator's pin, and the only way to choose.
#   2. ~/.opencode/bin/opencode -- where opencode's own installer puts it
#      (docs/08-deployment.md 0.1, variant A), so the recommended channel needs
#      no manual step.
#   3. PATH -- but ONLY if exactly one executable named `opencode` is on it, and
#      never silently: the resolved absolute path is printed to stderr, which
#      systemd keeps in the journal, so the build that owns the port is on the
#      record. Two or more candidates is a refusal, not a coin flip: a machine
#      with a stale copy installed system-wide plus a current one in a user
#      prefix is exactly the case where "whichever sorts first" hands the port to
#      the wrong build, and the operator is the only one who can say which.
# The exec line always uses "$BIN", so whatever was resolved is what runs.
set -euo pipefail

# systemd does not expand ${HOME} in an EnvironmentFile, so a value copied from
# .env.oc.example arrives here as a literal; and an operator may type ~ or an
# absolute path. One normaliser, so all three spellings mean one directory.
expand_home() {
  local value="$1"
  case "$value" in
    '~') value="$HOME" ;;
    '~/'*) value="$HOME/${value#\~/}" ;;
    '$HOME') value="$HOME" ;;
    '$HOME/'*) value="$HOME/${value#\$HOME/}" ;;
    '${HOME}') value="$HOME" ;;
    '${HOME}/'*) value="$HOME/${value#\$\{HOME\}/}" ;;
  esac
  printf '%s\n' "$value"
}

# Every executable named `opencode` on PATH, in PATH order, one line per FILE:
# on a Debian-shaped system /bin is a symlink to /usr/bin, so counting paths
# would report two candidates where there is one binary and refuse a machine
# that is in fact unambiguous. Empty PATH elements are skipped rather than read
# as the current directory: a relative hit is the one a stale checkout plants.
path_opencode_copies() {
  local dir name base key seen=""
  local IFS=:
  for dir in $PATH; do
    if [ -z "$dir" ] || [ ! -x "$dir/opencode" ] || [ -d "$dir/opencode" ]; then
      continue
    fi
    name="${dir##*/}"
    base="$(cd -P "$dir" 2>/dev/null && pwd -P || printf '%s' "$dir")"
    key="$base/$name"
    case "$seen" in
      *"|$key|"*) continue ;;
    esac
    seen="${seen}|$key|"
    printf '%s\n' "$dir/opencode"
  done
}

BIN="$(expand_home "${R2D2_OC_BIN:-${HOME}/.opencode/bin/opencode}")"
CONFIG_DIR="$(expand_home "${R2D2_OC_CONFIG_DIR:-${HOME}/.r2d2/opencode}")"
WORKSPACE="$(expand_home "${R2D2_OC_WORKSPACE:-${HOME}/r2d2-workspace}")"
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
  copies=()
  while IFS= read -r line; do
    if [ -n "$line" ]; then
      copies+=("$line")
    fi
  done < <(path_opencode_copies)
  if [ -n "${R2D2_OC_BIN:-}" ]; then
    echo "opencode_serve.sh: R2D2_OC_BIN=$BIN is missing or not executable." >&2
    echo "  It is your pin, so nothing else is tried. Check the path, or unset the" >&2
    echo "  variable to let this script resolve the binary itself." >&2
    exit 1
  elif [ "${#copies[@]}" -eq 0 ]; then
    echo "opencode_serve.sh: no opencode binary at $BIN and none on PATH." >&2
    echo "  Install the CLI first (docs/08-deployment.md 0.1) or set R2D2_OC_BIN." >&2
    exit 1
  elif [ "${#copies[@]}" -gt 1 ]; then
    echo "opencode_serve.sh: ${#copies[@]} copies named opencode on PATH:" >&2
    printf '  %s\n' "${copies[@]}" >&2
    echo "  Refusing to guess which one should own this port. Set R2D2_OC_BIN to" >&2
    echo "  the build you want (opencode --version) and start the unit again." >&2
    exit 1
  else
    BIN="${copies[0]}"
    echo "opencode_serve.sh: no binary at $HOME/.opencode/bin/opencode; using the single" >&2
    echo "  PATH match $BIN. Set R2D2_OC_BIN to pin it." >&2
  fi
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
  echo "  Install R2D2's config with scripts/install.sh" >&2
fi

export OPENCODE_SERVER_PASSWORD="$PASSWORD"
export OPENCODE_CONFIG_DIR="$CONFIG_DIR"
export OPENCODE_SERVER_USERNAME="${R2D2_OC_USERNAME:-opencode}"

cd "$WORKSPACE"
exec "$BIN" serve --hostname 127.0.0.1 --port "$PORT"
