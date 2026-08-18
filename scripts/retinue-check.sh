#!/usr/bin/env bash
# Fast Retinue-delta checks — the same pair GitHub Actions runs.
# Does not run the inherited Hermes suite.
# Does not create or replace a virtualenv (uv run would). Use
# scripts/retinue-dev-setup.sh first if pytest is missing.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

if [[ -n "${VIRTUAL_ENV:-}" && -x "${VIRTUAL_ENV}/bin/pytest" ]]; then
  PYTEST="${VIRTUAL_ENV}/bin/pytest"
elif [[ -x "$REPO_ROOT/.venv/bin/pytest" ]]; then
  PYTEST="$REPO_ROOT/.venv/bin/pytest"
elif command -v pytest >/dev/null 2>&1; then
  PYTEST="$(command -v pytest)"
else
  echo "error: pytest not found." >&2
  echo "       Run ./scripts/retinue-dev-setup.sh (or activate the venv it created)." >&2
  exit 1
fi

echo "==> pytest plugins/platforms/retinue_rooms  (using $PYTEST)"
"$PYTEST" plugins/platforms/retinue_rooms -q

echo "==> retinue-web PTT tests + build (tsc + vite)"
if [[ ! -d retinue-web/node_modules ]]; then
  (cd retinue-web && npm ci)
fi
(cd retinue-web && node --experimental-strip-types --test src/voice/ptt.test.ts && npm run build)

echo "==> retinue-check ok"
