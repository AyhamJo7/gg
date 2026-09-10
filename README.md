# GG Orchestrator

GG is a local software-building orchestrator for authenticated CLI subscriptions.
Its React browser UI drives a Python/FastAPI backend, SQLite state, Git checkpoints,
and installed `claude`, `codex`, `agy`, and `opencode` adapters. It does not require
a paid API-key gateway. Authentication and account access belong to the CLIs;
installation alone does not prove available quota.

## Start locally

Requires Python 3.12+, `uv`, Node/npm, Git, and authenticated provider CLIs on PATH.
Objective verification additionally requires Linux `bubblewrap` (`bwrap`) with
working unprivileged user namespaces. It fails closed when unavailable.

```bash
make install
make dev
```

Open **http://localhost:5173**. The backend listens on **127.0.0.1:8787**.
`make dev` creates the local auth token before starting Vite. Under the Makefile,
state and token live in `backend/.orchestrator/`; the configured database path is
relative to the backend's working directory. Do not publish the built frontend:
Vite embeds this checkout's bearer token.

## Two workflows

- **Idea → Product:** create a product idea, generate a structured plan, inspect
  requirements/architecture/roadmap, then select **Start Project**. Each roadmap
  phase becomes a sequential mission. External prerequisites appear as Human
  Gates. Final acceptance checks requirements and replays the detected toolchain
  in a fresh checkout before recording a delivery SHA and report.
- **Projects → New Mission:** register an existing repository and request work.
  Choose sequential execution or **Parallel Safe**, optionally supplying a task
  DAG. Parallel tasks use separate Git worktrees and a later integration step.

These are distinct entities: “Projects” registers repositories; “Idea → Product”
manages product lifecycles. Product creation and plan generation are separate
steps. Use the explicit Start action: the current auto-execute/approval fields do
not provide a reliable unattended approval workflow.

## What completion means

Missions include planning, implementation, testing, review/repair, and deterministic
toolchain verification. `COMPLETED` is a mission verdict; `DELIVERED` is a separate
product verdict. Review independence currently compares provider names and can
degrade to disclosed self-review. Unavailable or failed verification can produce
`UNVERIFIED`; product acceptance can become `BLOCKED`.

Requirement checks run allowlisted commands in a sandbox. Human Gates support
external setup and variable-name checks; configure secret values in your own
editor, never in plan text or resolution notes. Waivers are explicit exceptions,
not proof that a requirement passed. Fresh-checkout verification currently repeats
the detected toolchain, **not every requirement-specific criterion**.

## Providers and safety

Configure providers in [config/orchestrator.yaml](config/orchestrator.yaml).
All four adapters are enabled there; the current OpenCode default is
`opencode-go/muse-spark-1.3-contributor` with `variant: xhigh`. Other adapters inherit
their CLI model defaults. Role priorities and saved profiles are editable in the
browser; profile behavior differs between execution paths.

Provider processes have substantial local authority. GG's verification sandbox
does not uniformly sandbox provider CLIs. Raw provider logs are sensitive local
files; streamed output is redacted heuristically. Git checkpoints exclude internal
runtime files and recognized secrets. See [SECURITY.md](SECURITY.md) for the actual
boundaries, including network-enabled dependency installation.

## Invocation observability

Every provider execution is one durable invocation with owner/stage, terminal
outcome, prompt manifest (`~N estimated` via `char4-v1`), usage provenance
(`PROVIDER_REPORTED`/`CLI_REPORTED`/`UNKNOWN` with `COMPLETE`/`PARTIAL`/`UNKNOWN`),
and lease-owned cancellation/recovery. Inspect runs at `/api/runs`,
`/api/runs/{id}`, `/api/runs/{id}/context`, and `/api/analytics/usage`; the UI
Run Inspector shows the same without ever rendering unknown as zero.
Artifact evidence (writers, exact-SHA review/verification/criteria/fresh
status, delivery readiness) is read-only at
`/api/product-projects/{id}/evidence` and in the Lifecycle delivery tab.

## Development and documentation

`make test`, `make lint`, and `make typecheck` are deterministic development checks.
`make smoke` invokes real providers and consumes subscription capacity.

- [ARCHITECTURE.md](ARCHITECTURE.md): current components, state, and limitations.
- [DEVELOPMENT.md](DEVELOPMENT.md): commands, tests, migrations, desktop shell.
- [PROVIDERS.md](PROVIDERS.md): commands, output formats, usage visibility.
- [TROUBLESHOOTING.md](TROUBLESHOOTING.md): operator recovery and diagnostics.
- [AGENTS.md](AGENTS.md): concise contributor instructions.
- [Architecture audit and proposed blueprint](docs/ARCHITECTURE.md): assessment
  dated 2026-09-10; proposals are not implemented features.

Historical release reports and ADRs live under `docs/`. Their verification claims
apply to their recorded versions; current source takes precedence.
