# Current architecture

Source baseline: `6ebcc7d`, inspected 2026-09-10. Proposed changes are separately
documented in [the audit/blueprint](docs/ARCHITECTURE.md).

```text
React / HashRouter ── REST /api/* and mission WebSocket
        │
FastAPI api/app.py + shared local bearer token
        │
Orchestrator ── registry, scheduler, recovery, workspace ownership
        ├── ProjectCoordinator ── product plans, phases, gates, acceptance
        │       ├── direct product-planning adapter call (special path)
        │       └── one sequential mission per roadmap phase
        ├── MissionEngine ── plan → implement → test → review/repair → verify
        └── ParallelMissionEngine ── DAG → worktrees → integrate → review → verify
                     │
       ProviderAdapter → process.run_process → CLI
                     │
       Git checkpoints + SQLite + bounded events + raw local logs

Objective checks: workspace/criterion → sandbox → toolchain
Product acceptance: criteria + finding/gate checks + fresh-checkout toolchain
```

## Components and state

Backend modules are under `backend/src/orchestrator/`.

| Component | Responsibility |
|---|---|
| `server.py`, `api/app.py`, `api/auth.py` | Startup, REST, bearer authentication, WebSockets |
| `orchestrator.py` | Engine ownership, scheduler, restart reconciliation, mission actions, basic analytics |
| `project_engine.py`, `product_plan.py` | Product plans/revisions, roadmap phases, prerequisites, acceptance/delivery |
| `engine.py`, `handoff.py` | Sequential phases, summaries, checkpoints, failover |
| `parallel_engine.py`, `dag.py`, `readiness.py` | DAG validation, readiness and task execution |
| `reservations.py`, `task_locks.py`, `locks.py` | Task reservations/scope locks and in-process locks |
| `task_worktree.py`, `integration.py`, `git_ops.py` | Worktrees, final integration, checkpoint ledger and secret scanning |
| `providers/*`, `process.py`, `_spawn_gate.py`, `orphans.py` | CLI argv/output, cooldowns, process groups, identity handshake, owned-process recovery |
| `review.py` | Finding parsing, fingerprints, explicit repair verification |
| `workspace.py`, `verify.py`, `criterion.py`, `sandbox.py` | Root-manifest toolchain detection and confined objective commands |
| `events.py`, `security.py` | Durable events, transient output, redaction and path boundaries |

`projects` registers repositories. `product_projects` is the product lifecycle.
Products own `plan_revisions`, `project_phases`, `project_gates`,
`requirement_evidence`, `criterion_results`, and `acceptance_waivers`.
Each phase currently points to its latest mission; retries replace that link.
Missions own tasks, runs, handoffs, findings, reviews, and mission gates.
DAG edges, reservations, locks, branches, and integrations support parallel work.
`provider_profiles` exists in the schema but is not used by routing.

`db.py` uses one SQLite connection, WAL, foreign keys and a thread lock. Nine
numbered migrations are applied at startup. Individual database calls usually
commit separately; multi-step lifecycle mutations are not all atomic transactions.
Run one backend owner per state directory; multi-process scheduling is unsupported.

## Execution semantics

```text
CREATED → ANALYZING → PLANNING → IMPLEMENTING → TESTING → REVIEWING
        → review/repair loop → FINAL_VALIDATION → COMPLETED | UNVERIFIED
Exceptional: PAUSED, WAITING_FOR_PROVIDER/WORKSPACE/HUMAN, FAILED, CANCELLED
Restart: active missions → RECOVERING → persisted phase
```

The sequential runner retries provider failures, checkpoints before switches, and
uses bounded repair cycles. Final-validation repairs rerun verification but do not
re-enter independent review. Environment signatures such as DNS failure skip this
code-repair path. Parallel final validation reports failure without that same loop.

Parallel tasks wait for dependency completion but their worktrees currently start
from repository HEAD; dependency branches merge only in final integration.
Dependent tasks must not be assumed to see upstream code. Task scopes are scheduling
hints, not filesystem confinement. Several parallel output callbacks suppress the
mission live stream; raw task logs remain available separately. Automatic dynamic
DAG replanning is not implemented.

Product planning directly calls an adapter from a temporary-directory working
directory. It lacks mission-style run persistence and process ownership tracking.
Product phases use sequential missions, even when plans contain scope/provider
suggestions. Product `REVIEWING` advances to acceptance; it does not invoke a
separate product-wide reviewer.

Acceptance caches criterion results by criterion ID, command and SHA, including
failures. It checks selected unresolved finding categories, gates, working-tree
toolchain results, and a fresh clone's toolchain. Fresh-checkout criterion replay,
durable acceptance attempts, and post-check SHA binding need strengthening; see
the audit for evidence and proposals.

## Prompts, metrics, and security

Prompts are Python string builders. There is no context compiler, token budget,
usage parser, prompt version record or context manifest. Product phase prompts
include tasks/criteria but omit the plan's architecture and mapped requirement
definitions. Sequential handoffs repeat mission text and short summaries. Parallel
task prompts contain title/description and generic instructions only. CLI-native
instructions/tools add context outside these strings.

Structured events persist in SQLite; provider output is transient with a 500-line
per-mission replay buffer. Raw stdout/stderr lives below target
`.orchestrator/logs/`. Existing Analytics aggregates mission status, finding severity,
provider runs/failures/duration, not tokens or product effectiveness.

Under `make dev`, database/token files live in `backend/.orchestrator/`. Paths are
cwd-relative; desktop launch can use a different state directory. Verification
uses Linux bubblewrap and fails closed; dependency installation enables network
in the otherwise confined sandbox. Provider CLIs retain their permission behavior.
See [SECURITY.md](SECURITY.md) before operating on unfamiliar repositories.
