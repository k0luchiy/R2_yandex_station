#!/usr/bin/env bash
# The one command that installs an R2D2 deployment. Everything here is
# idempotent: run it twice and the second run changes nothing, so it is also the
# way to repair a machine whose paths moved.
#
# What it writes, and why each piece is here rather than in another script:
#   ~/.r2d2/opencode/opencode.json  the permission matrix  (config + shim)
#   ~/.r2d2/r2d2_do.py              the agent's only tool  (config + shim)
#   ~/.config/systemd/user/r2d2-opencode.service           (this script)
#   $WORKSPACE                       the server's cwd, 0700 (this script)
#   .env.oc                          from .env.oc.example, 0600 (this script)
#
# What it deliberately does NOT do: it never writes a password, never starts or
# stops anything, and never enables the unit. opencode's server auth is HTTP
# basic and an empty password means every local process gets an unrestricted
# agent runtime, so the credential is the operator's to generate, and enabling a
# unit is a deliberate act. Both are printed at the end as the next steps.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORKSPACE="${HOME}/r2d2-workspace"
PREFIX=""
ENV_OC="$REPO_ROOT/.env.oc"

usage() {
  cat <<'EOF'
usage: scripts/install.sh [--workspace DIR] [--prefix DIR]

  --workspace DIR   directory the opencode server runs in, and therefore the
                    directory every session it creates points at (default
                    $HOME/r2d2-workspace). Must be creatable; the server's cwd
                    is never created for you.
  --prefix DIR      where to install the opencode config and the shim (default
                    $HOME/.r2d2).

Prints what it did and what is still yours to do. Safe to run again.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --workspace)
      [ "$#" -ge 2 ] || { echo "--workspace requires a directory" >&2; exit 2; }
      WORKSPACE="$2"
      shift 2
      ;;
    --workspace=*)
      WORKSPACE="${1#--workspace=}"
      shift
      ;;
    --prefix)
      [ "$#" -ge 2 ] || { echo "--prefix requires a directory" >&2; exit 2; }
      PREFIX="$2"
      shift 2
      ;;
    --prefix=*)
      PREFIX="${1#--prefix=}"
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

if [ ! -x "$REPO_ROOT/.venv/bin/python" ]; then
  echo "install.sh: $REPO_ROOT/.venv/bin/python is missing." >&2
  echo "  The shim re-execs into it, so install it first:" >&2
  echo "    python3 -m venv .venv && .venv/bin/pip install -e \".[dev]\"" >&2
  exit 1
fi

set -- --unit --workspace "$WORKSPACE"
if [ -n "$PREFIX" ]; then
  set -- "$@" --dest "$PREFIX/opencode"
fi
bash "$REPO_ROOT/scripts/install_r2d2_opencode_config.sh" "$@"

mkdir -p "$WORKSPACE"
chmod 700 "$WORKSPACE"
echo "workspace $WORKSPACE ready"

if [ -n "$PREFIX" ] && [ "$PREFIX" != "$HOME/.r2d2" ]; then
  # The launcher's own default is $HOME/.r2d2/opencode, and a config dir it cannot
  # see is a deployment whose permission matrix is not installed (C2).
  echo "note: put R2D2_OC_CONFIG_DIR=$PREFIX/opencode in $ENV_OC, otherwise the"
  echo "      launcher looks in $HOME/.r2d2/opencode and warns that R2D2's agents are not defined"
fi

# .env.oc holds the password, so it is created once and then never touched: a
# second run must not be able to overwrite a credential.
if [ -e "$ENV_OC" ]; then
  echo "kept $ENV_OC (never overwritten)"
else
  install -m 600 "$REPO_ROOT/.env.oc.example" "$ENV_OC"
  echo "created $ENV_OC from the example -- put a password in it (openssl rand -base64 24)"
fi

cat <<EOF

installed. Still yours to do, in this order:

  1. a password for the opencode server:
       \$EDITOR $ENV_OC      # R2D2_OC_PASSWORD=  (and the same value in $REPO_ROOT/.env)
  2. nothing: Config's own default for R2D2_WORKSPACE is the same
     ~/r2d2-workspace this script creates, and it expands to the directory every
     session is scoped to. Set R2D2_WORKSPACE in .env only if you passed
     --workspace somewhere else.
  3. check what this opencode actually offers -- the model ids in
     config/backends.json are a snapshot of one machine (docs/08-deployment.md 2.5)
  4. enable the unit (a deliberate act, not this script's):
       systemctl --user daemon-reload
       systemctl --user enable --now r2d2-opencode
  5. register the Alice skill and bind Telegram (docs/08-deployment.md 6.1, 7)
EOF
