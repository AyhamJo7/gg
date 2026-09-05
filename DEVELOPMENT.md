# Development

## Layout

```
backend/            FastAPI orchestration engine (Python 3.12, uv)
  migrations/       numbered SQL migrations
  src/orchestrator/ engine, providers, api, git, verify, …
  tests/            46 tests (pytest + pytest-asyncio)
frontend/           Mission Control UI (React 18 + TS + Vite)
  src/pages/        MissionControl, NewMission, Projects, Providers, Priority, Analytics
  src/components/   Terminal, GitPanel, ProviderStrip, WorkflowTimeline, GateCard, …
  src-tauri/        Tauri 2 shell (spawns backend, health-gated)
config/             orchestrator.yaml — providers, priorities, cooldowns
docs/adr/           architecture decision records
docker-compose.yml  optional LiteLLM (profile: litellm)
Makefile            dev / test / lint / typecheck / build / smoke
```

## Daily workflow

```bash
make install          # uv sync + npm install
make dev              # backend :8787 + frontend :5173 (Ctrl-C stops both)
make backend          # backend only
make frontend         # frontend only
```

Backend hot-reload: run `uv run uvicorn orchestrator.server:main --reload` in
`backend/` (or just restart `make backend`). The backend recovers in-flight
missions from SQLite on boot — killing it mid-mission is safe.

## Testing

```bash
make test-backend     # pytest: unit + state machine + failover + recovery + API
make test-frontend    # vitest: components + helpers
cd backend && uv run pytest tests/test_engine_e2e.py -q   # E2E with fake providers
```

Testing philosophy: fake adapters perform *real* filesystem work so git
checkpoints, handoffs and verification exercise genuine behavior without
burning subscription quota. Real providers are exercised by `make smoke`
and manual dogfood missions, not by CI.

## Code standards

- Python: `ruff` (lint+format config in backend/pyproject.toml), `mypy --strict`.
  Type hints everywhere; Pydantic at boundaries.
- TypeScript: strict mode, no `any`, named exports.
- argv arrays for every subprocess — `shell=True` is banned (ruff S-rules enforce).
- Never commit secrets; `security.py` redaction covers logs/events.

## Adding a provider

1. Create `backend/src/orchestrator/providers/<name>.py` implementing
   `ProviderAdapter` (see PROVIDERS.md for the contract).
2. Register it in `providers/registry.py::build_real_adapters`.
3. Add config defaults under `providers.<name>` in `config/orchestrator.yaml`
   and to the priority matrix.
4. Add adapter tests (command shape + output normalization) — no real execution.

## Migrations

Add `backend/migrations/NNNN_description.sql` (highest number + 1). Applied at
startup; tracked in `schema_migrations`. Never edit applied migrations.

## Tauri build

Requires Rust (`curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh`)
and `npm i -D @tauri-apps/cli`, then `npx tauri build` in `frontend/`. The shell
expects `gg-backend` on PATH (`uv tool install ./backend` or the repo venv).
Tauri build is currently UNVERIFIED on this machine (no Rust toolchain).
