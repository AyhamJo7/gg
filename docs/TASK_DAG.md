# Task DAG

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

If the planner output is malformed:
1. Retry with explicit JSON instructions
2. Fallback planner
3. `UNVERIFIED` / Human Gate

## Dynamic Re-Planning

Bounded re-planning is supported.  If a task permanently fails, the planner may
replace that task, but completed tasks are immutable unless explicitly
invalidated.  Every graph mutation creates a new `dag_revisions` record
recording who changed the graph, why, and which tasks were added/removed.

## Compatibility

Existing sequential missions continue to use the v1 `MissionEngine`.
Parallel-safe missions use `ParallelMissionEngine`.
Both engines share the same provider registry, database, and event bus.
