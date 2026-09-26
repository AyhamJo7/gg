# Development

## Layout

```
backend/            FastAPI orchestration engine (Python 3.12, uv)
  migrations/       numbered SQL migrations
  src/orchestrator/ engine, providers, api, git, verify, …
  tests/            pytest + pytest-asyncio, including lifecycle/security/recovery
frontend/           Mission Control UI (React 18 + TS + Vite)
  src/pages/        MissionControl, NewMission, Lifecycle, Projects, Providers, Priority, Analytics
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

`orchestrator.server:main` is a CLI entrypoint, not an ASGI application factory;
do not pass it to uvicorn as an app. Use `make backend` and a controlled restart.
Active missions have restart recovery; this does not imply exactly-once provider
execution or complete recovery for the separate product-planning path.

With these commands, SQLite and the auth token live in `backend/.orchestrator/`.
Other launch directories change the relative state path. Run one backend per
state directory. `make frontend` initializes the token first; plain `npm run dev`
requires that token to exist already. Vite embeds it, including in builds.

## Testing

```bash
make test-backend     # pytest: unit + state machine + failover + recovery + API
make test-frontend    # vitest: components + helpers
make e2e              # Playwright: verdict, relay, palette, activity (isolated, fake providers)
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
The 2026-09-10 audit did not verify desktop packaging. The shell inherits its cwd
for backend state, probes port 8787, and still loads after a failed health wait;
it is not a guarantee of backend readiness. Browser development is the canonical
startup documented above.
