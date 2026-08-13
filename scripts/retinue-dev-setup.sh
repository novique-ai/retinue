#!/usr/bin/env bash
# Isolated Retinue contributor bootstrap.
# Installs the editable package + test extras and builds retinue-web/.
# Does not run an interactive hermes setup and does not touch ~/.hermes
# unless HERMES_HOME is already pointed there.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

HERMES_HOME="${HERMES_HOME:-$HOME/.retinue}"
# Keep the venv out of the clone so an agent `rm -rf .venv` cannot
# destroy the runtime. Reuse an existing in-tree .venv if the operator
# already created one.
if [[ -x "$REPO_ROOT/.venv/bin/python" ]]; then
  VENV="$REPO_ROOT/.venv"
else
  VENV="$HERMES_HOME/venvs/retinue-dev"
fi

need() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "error: '$1' is required but not on PATH." >&2
    echo "       $2" >&2
    exit 1
  fi
}

need git "Install git (and git-lfs if this clone uses LFS pointers)."
need uv "Install uv from https://docs.astral.sh/uv/"
need node "Install Node.js 20+ (https://nodejs.org/). Used only to build retinue-web/."
need npm "npm ships with Node.js."

echo "==> Retinue dev setup"
echo "    repo:        $REPO_ROOT"
echo "    HERMES_HOME: $HERMES_HOME  (not used for install; printed for the next step)"
echo "    venv:        $VENV"

mkdir -p "$(dirname "$VENV")"
if [[ ! -x "$VENV/bin/python" ]]; then
  echo "==> creating venv"
  uv venv "$VENV" --python 3.12
fi

# shellcheck disable=SC1091
source "$VENV/bin/activate"
echo "==> python $(python --version)"
echo "==> installing hermes-agent (editable) with [dev]"
uv pip install -e ".[dev]"

echo "==> building retinue-web (required — the adapter serves dist/)"
if [[ ! -f retinue-web/package-lock.json ]]; then
  echo "error: retinue-web/package-lock.json is missing; cannot npm ci" >&2
  exit 1
fi
(cd retinue-web && npm ci && npm run build)

cat <<EOF

==> setup ok

Next (new shell or this one):

  export HERMES_HOME="${HERMES_HOME}"
  export RETINUE_ROOMS_ENABLED=1
  export GATEWAY_MULTIPLEX_PROFILES=true
  export PATH="${VENV}/bin:\$PATH"

  # Once per home: put a provider key in \$HERMES_HOME/.env
  # or run:  hermes auth login
  # mkdir -p "\$HERMES_HOME" && cp cli-config.yaml.example "\$HERMES_HOME/config.yaml"

  hermes doctor
  hermes gateway
  # then open http://127.0.0.1:8643

Checks before a PR:

  ./scripts/retinue-check.sh

See docs/development.md if something above failed.
EOF
