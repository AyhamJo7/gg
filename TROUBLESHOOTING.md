# Troubleshooting

## Backend won't start

- `uv sync` in `backend/` first. Port conflict: change `server.port` in
  `config/orchestrator.yaml`.
- State lives in `backend/.orchestrator/orchestrator.db` under Makefile startup;
  other launch directories change the relative path. Back up SQLite consistently
  (including its WAL state) before recovery work. Do not discard the database as
  a first diagnostic step: it contains mission, acceptance, and provider evidence.

## A provider shows UNAVAILABLE

- `which claude codex agy opencode` — the binary must be on PATH for the backend
  process. Providers → Test reports installation/version, but does not persist
  refreshed health or prove authenticated access. A controlled backend restart
  repeats detection.
- Check `last_error` on the Providers page; hover the state chip.

## Provider stuck in RATE_LIMITED / COOLDOWN

Reliability cooldowns use exponential backoff (base 60s, multiplier 2, cap 1h);
classified quota exhaustion has a configurable four-hour minimum. These are local
retry policies, not the provider's confirmed quota-reset time. They expire through
eligibility checks/scheduler reconciliation. Disable/Enable changes state but does
not clear an existing cooldown timestamp.

## opencode hangs with no output

Its interactive default model may not respond headless. This is exactly why the
adapter pins `providers.opencode.model` (currently
`opencode-go/muse-spark-1.3-contributor`, with `variant: xhigh`).
Run `opencode models` and pick another responsive `opencode-go/*` model if needed.

## codex immediately fails with RATE_LIMIT

Inspect the recorded failure evidence: GG inferred a usage/rate limit from CLI
output. It does not know your account's remaining allowance. Sequential mission
execution supports failover; product planning is a separate, less complete path.

## Mission shows WAITING_FOR_HUMAN

Open Mission Control — a yellow gate card explains what is needed, the choices,
and the recommended one. Resolve it there; the mission resumes itself.

## Mission ends UNVERIFIED

The Definition of Done was not proven. Causes include implementation defects,
unresolved review, missing toolchains, unavailable sandboxing, and environment
failures. Read `blocking_issue` and the relevant run logs. Current code skips
final-validation code repair for recognized environment failures; fix provisioning
before retrying. Product Run Acceptance currently reuses failed criteria at the
same command/SHA, so clicking it again does not necessarily rerun a failed check.

## Backend was killed mid-mission

Start the same backend/state directory again. Active missions are reconciled and
resume at their persisted phase; recorded provider process ownership is checked
before orphan cleanup. Never use a broad provider-name process kill: other CLI
sessions may belong to unrelated work. Product planning currently lacks this
mission-style run ownership, and may remain PLANNING after interruption.

## Frontend can't reach the backend

Dev mode proxies :5173 → :8787. If you changed the backend port, update
`frontend/vite.config.ts`. In the Tauri shell the backend is spawned
automatically; check stderr for `gg-backend spawn failed`.
