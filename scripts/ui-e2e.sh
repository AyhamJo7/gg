#!/bin/bash
# Browser e2e for operator surfaces. Isolated state dir, fake adapters only:
# never touches backend/.orchestrator, a running dev instance, or providers.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKEND_PORT="${GG_E2E_BACKEND_PORT:-8799}"
FRONTEND_PORT="${GG_E2E_FRONTEND_PORT:-5174}"

# A foreign listener (a leftover run, or a dev instance on 8787/5173) would
# satisfy wait_for and the browser checks would run against the wrong server.
for port in "$BACKEND_PORT" "$FRONTEND_PORT"; do
  if curl -s -m 1 -o /dev/null "http://127.0.0.1:$port/"; then
    echo "port $port is already in use; refusing to run e2e against it" >&2
    exit 1
  fi
done

STATE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/gg-ui-e2e.XXXXXX")"
PIDS=()
cleanup() {
  # Each server runs in its own process group (setsid): stop the whole group,
  # since uv/npx do not forward termination to the server they spawned.
  for pid in "${PIDS[@]}"; do kill -- "-$pid" 2>/dev/null || true; done
  # Let the servers exit before their state directory disappears; force it if
  # one has not exited after 10 seconds.
  for pid in "${PIDS[@]}"; do
    for _ in $(seq 1 20); do kill -0 "$pid" 2>/dev/null || break; sleep 0.5; done
    if kill -0 "$pid" 2>/dev/null; then kill -KILL -- "-$pid" 2>/dev/null || true; fi
  done
  rm -rf "$STATE_DIR"
}
trap cleanup EXIT

# wait_for URL PID: succeed once URL answers; fail early if PID has died.
wait_for() {
  for _ in $(seq 1 60); do
    kill -0 "$2" 2>/dev/null || { echo "server for $1 exited before it was ready" >&2; return 1; }
    curl -s -m 1 -o /dev/null "$1" && return 0
    sleep 0.5
  done
  echo "timed out waiting for $1" >&2
  return 1
}

setsid bash -c 'cd "$1/backend" && PYTHONPATH=src exec uv run python tests/helpers/e2e_ui_server.py "$2" "$3"' \
  _ "$REPO_ROOT" "$STATE_DIR" "$BACKEND_PORT" &
PIDS+=($!)
wait_for "http://127.0.0.1:$BACKEND_PORT/api/health" "${PIDS[-1]}"

GG_E2E=1 GG_AUTH_TOKEN_FILE="$STATE_DIR/auth_token" GG_BACKEND_HOST="127.0.0.1:$BACKEND_PORT" GG_FRONTEND_PORT="$FRONTEND_PORT" \
  setsid bash -c 'cd "$1/frontend" && exec npx --no-install vite --logLevel warn' _ "$REPO_ROOT" &
PIDS+=($!)
wait_for "http://127.0.0.1:$FRONTEND_PORT/" "${PIDS[-1]}"

cd "$REPO_ROOT/frontend"
GG_E2E_URL="http://127.0.0.1:$FRONTEND_PORT" node e2e/operator.e2e.mjs
