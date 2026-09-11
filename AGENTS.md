# GG contributor guidance

GG is a local, single-operator orchestrator of authenticated provider CLIs.
Read [ARCHITECTURE.md](ARCHITECTURE.md) for current behavior and
[DEVELOPMENT.md](DEVELOPMENT.md) for commands. The redesign in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) is proposed, not implemented.
Current source overrides historical reports.

## Boundaries

- Keep orchestration policy out of adapters. Distinguish repository projects,
  product lifecycles, missions, tasks, and runs.
- Check all execution paths when changing shared behavior: product planning,
  sequential phases, parallel planning, tasks, and integration review/repair.
- Preserve process ownership, cancellation, capacity, workspace exclusion,
  checkpoint safety, and restart behavior. CLI success is not acceptance.
- Preserve reviewer provenance. Repair claims and omitted findings are not
  verified fixes. Objective verification belongs in deterministic sandboxed tools.
- Consult [SECURITY.md](SECURITY.md) before changing execution, paths, logs, auth,
  or Git. Never weaken fail-closed containment or commit secrets/runtime evidence.

## Workflow

Inspect `git status` and local instructions first; preserve user changes.
Use `uv` for Python and the existing npm lockfile/workflow for this frontend.
Checks: `make test-backend`, `make test-frontend`, `make lint`, `make typecheck`.
Run affected tests with fresh temporary fixtures; add meaningful regression
coverage for behavior changes. Never use real providers in routine tests.
`make smoke` and real dogfood scripts consume quota and require explicit scope.

Use Conventional Commits. Never force-push main, reset user work, or kill shared
instances. Migrations are append-only. Review generated build-file changes before
staging; commit only task-related files.

## Context discipline

Read relevant modules after orienting. Keep these instructions concise and link
specifications. Avoid entire plans, logs, or previous reports in task prompts.
Preserve scoped requirements, architecture decisions, failure evidence, and reviewer
independence. Label token estimates and unknown usage honestly; CLI token counts
are not quota remaining or a subscription bill. Never inspect unrelated sessions.
Treat mapped acceptance criteria as authoritative for the assigned task; do not
duplicate long architecture text in reports. Provider prompts are compiled by
`context_compiler.py` (`compiled` default and strict — compilation failure
blocks provider execution with no silent legacy fallback; `legacy`/`shadow`
via `context.mode` for comparison) — change role policies there, not in ad hoc
string builders. Do not claim verification for a SHA different from the
candidate being delivered. After any code-writing repair, independent review
of the new candidate is required. Never attribute pre-existing workspace
changes to the current provider run. Writer membership follows actual
committed contribution, never invocation role. Evidence is scoped by
repository identity + SHA; keyless history cannot certify new artifacts.
Mission start never auto-commits dirt — explicit adoption only; unknown
dirt stays unknown. Preserve failed/retried attempts as history; delivery
evidence is evaluated per exact SHA via `provenance.py`. DAG task execution
is artifact-pinned via `dep_inputs.py`: input SHA before provider capacity,
ancestry-validated results, no silent global-integration consumption.
