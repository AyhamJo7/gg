# Troubleshooting

## Backend won't start

- `uv sync` in `backend/` first. Port conflict: change `server.port` in
  `config/orchestrator.yaml`.
- Corrupt DB: state lives in `.orchestrator/orchestrator.db` (repo root).
  Stop the backend, move the file aside, restart — missions are lost but the
  schema rebuilds. (Projects' git history is never affected.)

## A provider shows UNAVAILABLE

- `which claude codex agy opencode` — the binary must be on PATH for the backend
  process. If you installed a CLI after starting the backend, restart it or use
  Providers → Test (detection re-runs).
- Check `last_error` on the Providers page; hover the state chip.

## Provider stuck in RATE_LIMITED / COOLDOWN

Cooldowns use exponential backoff from real failures (base 60s × 2^n, max 1h).
They expire automatically. To force-clear: Providers → Disable → Enable.
Do not restart the backend for this — the scheduler tick reconciles cooldowns.

## opencode hangs with no output

Its interactive default model may not respond headless. This is exactly why the
adapter pins `providers.opencode.model` (default `opencode-go/kimi-k2.7-code`).
Run `opencode models` and pick another responsive `opencode-go/*` model if needed.

## codex immediately fails with RATE_LIMIT

Your ChatGPT Plus usage window is exhausted. This is normal operation — the
orchestrator failed over automatically; check the mission's provider strip and
git log. The entry clears after cooldown.

## Mission shows WAITING_FOR_HUMAN

Open Mission Control — a yellow gate card explains what is needed, the choices,
and the recommended one. Resolve it there; the mission resumes itself.

## Mission ends UNVERIFIED

Not a failure of the software — it means the Definition of Done could not be
proven: either no toolchain was detectable in the project (add `test`/`build`
scripts to package.json or pytest/pyproject.toml), or BLOCKER/HIGH review
findings survived all repair cycles. Read `blocking_issue` on the mission.

## Backend was killed mid-mission

Nothing to do. Start it again — missions in active states are marked RECOVERING
and resume at their persisted phase. Providers stuck BUSY are reset to AVAILABLE.
The currently-running CLI at kill time may need a Ctrl-C if it outlived the
backend (we kill process groups, so SIGKILL of the backend alone can orphan a
child — `pkill -f 'claude -p'` etc. if needed).

## Frontend can't reach the backend

Dev mode proxies :5173 → :8787. If you changed the backend port, update
`frontend/vite.config.ts`. In the Tauri shell the backend is spawned
automatically; check stderr for `gg-backend spawn failed`.
