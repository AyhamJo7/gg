# Task DAG

Current-source clarification (2026-09-10): this describes the DAG data model.
See [current architecture](../ARCHITECTURE.md) and the
[audit](ARCHITECTURE.md) for execution limitations.

## Overview

Phase 2A introduces a first-class **persistent task graph** to the GG Orchestrator.

Every mission in `PARALLEL_SAFE` scheduling mode is decomposed into a directed
acyclic graph (DAG) of executable tasks.  The graph is persisted in SQLite,
survives backend restart, and is validated before execution begins.

## Task Model

Each task contains:

| Field | Description |
|-------|-------------|
| `id` | Unique task identifier |
| `mission_id` | Owning mission |
| `title` | Human-readable title |
| `description` | Detailed description |
| `task_type` | e.g. `implementation`, `testing`, `integration` |
| `role` | Provider role (`planning`, `implementation`, `testing`, `review`, `repair`) |
| `status` | See status lifecycle below |
| `dependencies` | IDs of tasks that must complete first |
| `dependents` | IDs of tasks that depend on this task |
| `preferred_providers` | Suggested providers for this task |
| `assigned_provider` | Provider that claimed the task |
| `workspace_scope` | File/path patterns the task will touch |
| `resource_locks` | Acquired locks at runtime |
| `attempts` / `max_attempts` | Retry counters |
| `priority` | Scheduling priority (higher = earlier) |
| `created_at` / `ready_at` / `started_at` / `finished_at` | Timeline |
| `provider_run_id` | Link to the provider run record |
| `checkpoint_before` / `checkpoint_after` | Git commit SHAs |
| `result` | Structured result payload |
| `blocking_issue` | Why the task is blocked |
| `dag_revision` | Which DAG revision this task belongs to |

## Task Status Lifecycle

```
PENDING → READY → CLAIMED → RUNNING → COMPLETED
   ↓        ↓        ↓         ↓
BLOCKED  WAITING_FOR_PROVIDER  FAILED
   ↓
CANCELLED
```

## DAG Validation

Before any task executes, the graph is validated deterministically:

- **Cycles** → rejected (Kahn's algorithm)
- **Self-dependencies** → rejected
- **Missing dependency IDs** → rejected
- **Duplicate task IDs** → rejected
- **Invalid workspace scopes** → rejected

A malformed planner DAG never begins execution.

## Planner → Structured DAG

The planning provider must produce a JSON payload:

```json
{
  "tasks": [
    {
      "id": "backend-api",
      "title": "Implement backend API",
      "role": "implementation",
      "depends_on": [],
      "workspace_scope": ["backend/**"],
      "preferred_providers": ["opencode", "codex"]
    }
  ]
}
```

Provider execution failures have bounded failover. After a successful CLI call,
missing structured DAG output currently becomes `UNVERIFIED`; a structurally
invalid DAG becomes `FAILED`. There is no implemented format-repair/fallback
planning loop at this parsing stage.

## Dynamic Re-Planning

Automatic dynamic replanning is not implemented. The schema contains
`dag_revisions`; initial planner output is recorded there. The manual DAG API is
restricted to mission setup states. Task retries are separate from graph revision.

Dependency edges currently gate task start, but do not transfer upstream branch
content into the consumer worktree. Tasks start from the main repository's HEAD;
completed task branches are integrated after all tasks finish. Do not assume that
a dependent task can already read its producer's code.

## Compatibility

Existing sequential missions continue to use the v1 `MissionEngine`.
Parallel-safe missions use `ParallelMissionEngine`.
Both engines share the same provider registry, database, and event bus.
