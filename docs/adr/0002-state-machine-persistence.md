# ADR 0002: State Machine Persistence

## Status
Accepted (implemented, restart-tested)

## Context
Missions run for minutes to hours. Backend restarts, crashes, and machine
reboots must not lose orchestration state or double-execute work.

## Decision
SQLite (WAL) as canonical state, via file-based migrations. Every status
transition is persisted **before** the action it describes. `missions.current_phase`
always stores a member of the forward phase sequence; REPAIRING is modeled as a
sub-state (status only), so recovery re-enters the enclosing loop correctly.
On startup, missions in active statuses are marked RECOVERING and relaunched at
`current_phase`; providers stuck BUSY are reset to AVAILABLE (no runs can be in
flight at boot).

Idempotency rules: providers are stateless per run and always receive a fresh
structured handoff; git checkpoints only commit actual changes, so re-running a
phase never double-commits.

## Consequences
- Verified: SIGKILL mid-PLANNING → restart → mission resumed at PLANNING → COMPLETED.
- Restarting mid-phase may re-run that phase's provider call (cost: a little quota;
  correctness: preserved, since prompts are handoff-based, not history-based).
- PostgreSQL swap remains possible behind `db.py`'s repository-style surface.
