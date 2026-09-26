#!/bin/bash
# Browser e2e for operator surfaces. Isolated state dir, fake adapters only:
# never touches backend/.orchestrator, a running dev instance, or providers.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_PORT="${GG_E2E_BACKEND_PORT:-8799}"
FRONTEND_PORT="${GG_E2E_FRONTEND_PORT:-5174}"
STATE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/gg-ui-e2e.XXXXXX")"
PIDS=()
cleanup() {
  # Each server runs in its own process group (setsid): stop the whole group,
  # since uv/npx do not forward termination to the server they spawned.
  for pid in "${PIDS[@]}"; do kill -- "-$pid" 2>/dev/null || true; done
  rm -rf "$STATE_DIR"
}
trap cleanup EXIT

wait_for() {
  for _ in $(seq 1 60); do
    curl -s -m 1 -o /dev/null "$1" && return 0
    sleep 0.5
  done
  echo "timed out waiting for $1" >&2
  return 1
}

setsid bash -c 'cd "$1/backend" && PYTHONPATH=src exec uv run python tests/helpers/e2e_ui_server.py "$2" "$3"' \
  _ "$REPO_ROOT" "$STATE_DIR" "$BACKEND_PORT" &
PIDS+=($!)
wait_for "http://127.0.0.1:$BACKEND_PORT/api/health"

GG_E2E=1 GG_AUTH_TOKEN_FILE="$STATE_DIR/auth_token" GG_BACKEND_HOST="127.0.0.1:$BACKEND_PORT" GG_FRONTEND_PORT="$FRONTEND_PORT" \
  setsid bash -c 'cd "$1/frontend" && exec npx vite --logLevel warn' _ "$REPO_ROOT" &
PIDS+=($!)
wait_for "http://127.0.0.1:$FRONTEND_PORT/"

cd "$REPO_ROOT/frontend"
GG_E2E_URL="http://127.0.0.1:$FRONTEND_PORT" node e2e/operator.e2e.mjs
