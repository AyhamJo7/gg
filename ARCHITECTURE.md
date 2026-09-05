# Architecture

## Big picture

```
┌────────────────────────────────────────────────────────────┐
│  React Mission Control (Vite)  ── Tauri shell (optional)   │
│  REST /api/*  +  WebSocket /ws/missions/{id}               │
└───────────────────────────┬────────────────────────────────┘
                            │
┌───────────────────────────▼────────────────────────────────┐
│  FastAPI backend (orchestrator.api.app)                    │
│                                                            │
│  Orchestrator (scheduler, recovery, lifecycle)             │
│    └── MissionEngine per active mission                    │
│          ├── durable state machine (SQLite-persisted)      │
│          ├── provider selection (priority matrix + duties) │
│          ├── failover + cooldown (ProviderRegistry)        │
│          ├── handoff generator (context compression)       │
│          ├── review engine (structured findings)           │
│          └── verification engine (real toolchain runs)     │
│                                                            │
│  ProviderRegistry                                          │
│    ├── ClaudeAdapter / CodexAdapter / AgyAdapter /         │
│    │   OpencodeAdapter  (real CLIs, argv-only exec)        │
│    └── FakeAdapter family (deterministic simulation)       │
│                                                            │
│  GitOps (checkpoints, secret-safe commits)                 │
│  EventBus (SQLite-persisted + WebSocket broadcast)         │
└───────────────────────────┬────────────────────────────────┘
                            │ argv, no shell
┌───────────────────────────▼────────────────────────────────┐
│  Local provider CLIs (claude, codex, agy, opencode)        │
│  Authenticated by their own subscriptions — never touched  │
└────────────────────────────────────────────────────────────┘
```

## Module map (backend/src/orchestrator)

| Module | Responsibility |
|---|---|
| `models.py` | Domain entities, enums (MissionStatus, ProviderState, FailureClass, Role) |
| `db.py` | SQLite handle + file migrations (migrations/0001_init.sql) |
| `config.py` | YAML config, priority matrix, per-provider settings |
| `engine.py` | MissionEngine: the durable state machine driving one mission |
| `orchestrator.py` | Scheduler loop, recovery, mission lifecycle API surface |
| `providers/base.py` | ProviderAdapter ABC, ExecutionRequest/Result |
| `providers/classify.py` | Output → FailureClass translation (success-aware) |
| `providers/registry.py` | Detection, health, cooldown/backoff, eligibility |
| `providers/{claude,codex,agy,opencode}.py` | Real CLI adapters |
| `providers/fake.py` | Simulation providers for deterministic tests |
| `process.py` | Subprocess runner: streaming, timeout, SIGTERM→SIGKILL, process-group kill |
| `git_ops.py` | Status/diff/checkpoint; sensitive-file exclusion |
| `workspace.py` | Toolchain detection (test/build/lint/typecheck commands) |
| `handoff.py` | Structured markdown handoff generation + persistence |
| `review.py` | Review instructions, finding parsing, blocker tracking |
| `verify.py` | Evidence-based completion (runs detected commands) |
| `locks.py` | Workspace/git/file/task locks (parallel-ready interfaces) |
| `events.py` | EventBus: persist + broadcast, redacted payloads |
| `security.py` | Path validation, secret redaction, gitignore protection |
| `api/app.py` | REST + WebSocket surface |
| `server.py` | Wiring + uvicorn entrypoint (`gg-backend`) |
| `smoke.py` | Real provider smoke test |

## Mission state machine

```
CREATED → ANALYZING → PLANNING → IMPLEMENTING → TESTING → REVIEWING
        → (REPAIRING ⇄ REVIEWING loop, max N cycles) → FINAL_VALIDATION
        → COMPLETED | UNVERIFIED

Exceptional: PAUSED · WAITING_FOR_PROVIDER · WAITING_FOR_HUMAN ·
             RATE_LIMITED · FAILED · CANCELLED · RECOVERING
```

Key invariants:

- Every transition is persisted to SQLite **before** the action it describes.
- `current_phase` always holds a member of the forward phase sequence, so
  recovery re-enters the correct loop (REPAIRING is a sub-state of REVIEWING).
- Provider phases are idempotent: providers are stateless per run and always
  receive a fresh structured handoff; checkpoints only commit actual changes.
- COMPLETED requires: review passed (no open BLOCKER/HIGH) **and** the real
  toolchain passed. If no toolchain is detectable → UNVERIFIED, never "success".

## Provider selection & failover

1. Role-specific priority list (config/YAML or saved profile).
2. Filter by eligibility: installed, not DISABLED, state == AVAILABLE, cooldown expired.
3. Separation of duties: REVIEW prefers ≠ implementer; REPAIR prefers ≠ reviewer.
4. On failure: classify → record cooldown (exponential backoff, configurable) →
   checkpoint ("orchestrator: checkpoint before provider switch") → write handoff →
   next eligible provider. Human-input/auth failures open a HumanGate instead.

## Data flow for one provider run

```
engine builds prompt (handoff + role instructions)
  → ProviderRegistry.mark_busy
  → adapter.build_command (argv array)
  → process.run_process (stream redacted lines → EventBus → WebSocket)
  → classify_output (exit code + output tail, success-aware)
  → record provider_run row (argv, timings, git before/after)
  → success: record_success + git checkpoint + task completed
    failure: record_failure (cooldown) + failover
```

## Persistence

SQLite at `.orchestrator/orchestrator.db` (repo root, gitignored) with tables:
projects, missions, tasks, providers, provider_runs, events, handoffs,
checkpoints, human_gates, review_findings, settings. Migrations are numbered
SQL files applied at startup. Per-project runtime artifacts live in
`<project>/.orchestrator/{handoffs,logs}/`.

## Optional layers

- **LiteLLM** (`docker-compose.yml`, profile `litellm`): normalization only;
  never owns orchestration decisions.
- **Tauri shell** (`frontend/src-tauri`): spawns `gg-backend`, waits on
  `/api/health`, loads the UI, kills the backend on exit.
