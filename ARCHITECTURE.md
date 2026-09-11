# Current architecture

Source baseline: invocation-integrity increment on top of `c06079e`
(2026-09-10 audit baseline `6ebcc7d`). Proposals remain in
[the audit/blueprint](docs/ARCHITECTURE.md); this file describes implemented
behavior.

```text
React / HashRouter ── REST /api/* (+ /api/runs, /api/analytics/usage) and mission WebSocket
        │
FastAPI api/app.py + shared local bearer token
        │
Orchestrator ── registry, scheduler, recovery, workspace ownership
        ├── ProjectCoordinator ── product plans (durable PLAN operations), phases, gates, acceptance
        │       ├── InvocationService product-planning attempts (attributed runs)
        │       └── one sequential mission per roadmap phase
        ├── MissionEngine ── plan → implement → test → review/repair → verify
        └── ParallelMissionEngine ── DAG → worktrees → integrate → review → verify
                     │
        InvocationService → ProviderAdapter → process.run_process → CLI
           ├── durable run + stage/owner + prompt manifest + usage + lease
           ├── terminal precedence + exactly-once health + owned cancellation
           └── restart recovery (all owners, PID-identity verified)
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
| `invocations.py`, `usage.py`, `context_manifest.py`, `operations.py` | Shared invocation boundary, usage parsers, prompt manifests, durable operations |
| `context_compiler.py` | Deterministic role-specific context compiler (`compiled-v2` / `context-policy-v2`) |
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

`db.py` uses one SQLite connection, WAL, foreign keys and a thread lock. Eleven
numbered migrations are applied at startup (0010: run attribution/stage/status/
model columns, `run_context_manifests`, `run_usage`, `invocation_leases`,
`orchestration_operations`; 0011: compiler manifest columns — budget/used/
remaining estimates, repeated-context ratio, warnings, plan revision). Individual database
calls usually commit separately; multi-step lifecycle mutations are not all atomic
transactions. Run one backend owner per state directory; multi-process scheduling
is unsupported. Historical runs keep NULL/UNKNOWN telemetry; never zero-filled.

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

Product planning runs through the same invocation boundary with a durable PLAN
operation (one active per product, stale-result and double-click protected) and
an isolated per-run working directory. Each attempt is an attributed run with
terminal outcome, usage provenance, and lease. Product phases use sequential
missions, even when plans contain scope/provider suggestions. Product `REVIEWING`
advances to acceptance; it does not invoke a separate product-wide reviewer.

Acceptance caches criterion results by criterion ID, command and SHA, including
failures. It checks selected unresolved finding categories, gates, working-tree
toolchain results, and a fresh clone's toolchain. Fresh-checkout criterion replay,
durable acceptance attempts, and post-check SHA binding need strengthening; see
the audit for evidence and proposals.

## Prompts, metrics, and security

All provider prompts go through the deterministic role-specific context
compiler (`backend/src/orchestrator/context_compiler.py`, no provider calls).
Per role (planner / implementer / reviewer / repairer / testing) it projects
only mapped requirements, acceptance criteria, relevant architecture, explicit
dependency handoffs, bounded failure evidence, and retrieval hints into a
budgeted prompt (`compiled-v2`, policy `context-policy-v2`), with per-block
include/compact/omit decisions persisted as manifest metadata (no raw prompt
or secret text). Reviewer prompts exclude implementer self-assessment;
repairer prompts carry only the current defect contract. Mandatory blocks
(requirements, acceptance, safety) can never be silently dropped — overflow
fails instead. Mode contract (`context.mode`): `compiled` (default) is
strict — compilation failure blocks provider execution and the owning
mission/task/operation records `CONTEXT_COMPILATION_FAILED` (no fake run, no
lease, no health change); `legacy` executes `legacy-v1` without compiling;
`shadow` measures the compiled candidate but always executes legacy exactly
once, with `SHADOW_COMPILATION_FAILED` recorded observably on failure.
`backend/scripts/compare_context.py` replays representative roles without
providers. Honest sizing: compiled-v2 bounds growing/repetitive context and
restores missing task contracts — small under-specified legacy prompts may
become larger (required correctness context added), while long-history repair
prompts shrink; there is no universal savings percentage.
GG prompt estimates and provider-observed usage are separate metrics; quota and
cost are not inferred.

Structured events persist in SQLite; provider output is transient with a 500-line
per-mission replay buffer. Raw stdout/stderr lives below target
`.orchestrator/logs/` (planning logs under state `logs/`). Analytics keeps its
mission/finding/provider counts and adds `/api/analytics/usage` coverage,
`/api/analytics/context` efficiency (avg estimated tokens by policy/role,
repeated-context ratio, compilation warnings; legacy vs compiled), plus
`/api/runs` inspection with a read-only Context view (blocks, budget, omissions,
warnings — never raw prompts); unknown telemetry is shown as unknown, never zero.

## Artifact evidence and writer provenance

Every code-affecting checkpoint is linked to its producer in
`write_provenance` (provider run + base/result SHA, changed paths, or
HUMAN_OPERATOR / SYSTEM / UNKNOWN_EXTERNAL). Writer membership follows
actual committed contribution in the evaluated range — never invocation
role: a no-change run does not taint reviewer independence, while a
testing/planning run that commits code does. Provider runs carry
`retry_of_run_id` chains; roadmap phases keep immutable
`project_phase_attempts` while `project_phases` holds the current pointer.
Each review attempt is bound to its exact reviewed range
(`reviews.reviewed_base/head_sha` + writer set/detail) and repository;
findings keep origin and verified-resolution lineage as their status evolves.
Reviewer independence means reviewer outside the contributing writer set of
the exact reviewed artifact; otherwise review is recorded degraded, never
certified. All evidence (writes, reviews, verification, criteria,
fresh checkouts) is scoped by repository identity + SHA — same hash text in
another repository certifies nothing; keyless historical rows stay visible
but cannot certify current artifacts. Verification, criterion, and
fresh-checkout executions persist as immutable SHA-bound attempts
(`verification_attempts`, `criterion_attempts`, `fresh_checkout_attempts`);
criteria are additionally bound to plan revision, with explicit RECHECK
creating new attempts instead of reusing cache.
`GET /api/product-projects/{id}/evidence` evaluates one candidate SHA
(review/verification/criteria/fresh/writers) and product delivery requires all
of them to cover exactly the delivery SHA — any post-evidence change makes
prior evidence stale and blocks delivery; the final transition re-reads HEAD
and cleanliness under the shared repo mutation lock and re-evaluates the
current artifact, so a candidate that moves mid-acceptance can never deliver
as its superseded SHA. Dirty workspaces block code-writing runs before
execution, and mission start never stages, commits, or auto-labels dirt:
pre-existing changes wait on an explicit operator decision (adopt via
`POST /api/missions/{id}/adopt-changes`, which alone creates HUMAN_OPERATOR —
unknown dirt is never converted to human to force completeness).

Under `make dev`, database/token files live in `backend/.orchestrator/`. Paths are
cwd-relative; desktop launch can use a different state directory. Verification
uses Linux bubblewrap and fails closed; dependency installation enables network
in the otherwise confined sandbox. Provider CLIs retain their permission behavior.
See [SECURITY.md](SECURITY.md) before operating on unfamiliar repositories.
