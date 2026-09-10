# Parallel Scheduler

Current-source clarification (2026-09-10): Parallel Safe is an alternative mission
mode, not the executor automatically selected for product roadmap phases. See
[current architecture](../ARCHITECTURE.md) for boundaries shared with the lifecycle.

## Overview

The Phase 2A parallel scheduler replaces the primarily sequential phase executor
with a **dependency-aware task scheduler** capable of safely running independent
engineering tasks in parallel across Claude, Codex, AGY, and OpenCode.

## Architecture

```
Mission
  ↓
DAG (persisted in SQLite)
  ↓
Readiness Engine  ←→  Resource Locks
  ↓
Ready Queue
  ↓
Scheduler
  ├── Provider Arbitration
  ├── Atomic Reservation
  ├── Worktree Creation
  └── Task Runner (async)
  ↓
Integration Engine
  ↓
Review + Verification
```

## Readiness Engine

A task becomes `READY` only when **all** of the following are true:

1. All dependencies are `COMPLETED`
2. No unresolved Human Gate exists
3. Required resources are available
4. Workspace scopes do not conflict with running tasks
5. Mission is not `PAUSED` / `CANCELLED` / `FAILED`

Readiness is recomputed **idempotently** on every scheduler tick from durable
database state.  Restart recovery reconstructs the exact same `READY` set.

## Provider Arbitration

For every `READY` task, the scheduler calculates candidate providers using an
**observable, deterministic, configurable score**:

```
score =
  + role preference (from config priority matrix)
  + historical success rate
  + independence bonus (function parameter; task caller currently leaves it zero)
  - cooldown penalty
  - recent failure penalty
```

The scoring helper returns explanation strings, but the task caller currently
discards them; they are not displayed in the UI. `provider_profiles` is not read
by the scoring path. Historical success is provider-wide CLI success, not verified
implementation quality or role-specific effectiveness.

## Atomic Provider Reservation

Provider acquisition is atomic inside a database transaction:

1. Count active reservations for the provider
2. Check against `max_parallel_per_provider` limit
3. Check against global `max_parallel_tasks` limit
4. Verify provider is still eligible
5. Insert reservation row
6. Update task status to `CLAIMED`

This prevents the race:
```
check available → race → two tasks launch
```

## Resource Locks

Two agents must not edit overlapping resources concurrently.  Locks are
persisted in SQLite so restart recovery can reconstruct them.

Lock types:
- `WORKSPACE_EXCLUSIVE` — whole repository
- `PATH_PREFIX` — directory or file prefix
- `GIT` — serialized Git operations
- `DATABASE` — serialized DB schema changes
- `INTEGRATION` — integration stage

Scope conflict detection is **conservative**:
- Exact file match → conflict
- Directory prefix overlap → conflict
- Wildcard overlap → conflict
- Uncertain → **serialize** (false serialization is acceptable)

## Scheduler Loop

```python
while mission active:
    if paused/cancelled: stop
    if all tasks terminal: run integration + verification

    ready = compute_ready_tasks()
    for task in ready (sorted by priority):
        if at max_parallel: break
        provider = arbitrate_provider(task)
        if try_reserve_provider(task, provider):
            if acquire_locks(task):
                create_worktree(task)
                launch_task_runner(task)
    await next_tick()
```

## Failure Semantics

A single task failure does **not** necessarily kill the mission:

| Failure Type | Action |
|-------------|--------|
| Provider failure | Bounded task retry; selection may use another eligible provider |
| Task implementation failure | Block dependents, retry if attempts remain |
| Final verification failure | `UNVERIFIED`; no sequential-style final-validation repair loop |
| Dependency failure | Block dependents |
| Integration conflict | `WAITING_FOR_HUMAN`, conflict information and manual resume workflow |
| Other integration failure | `FAILED` |

## Mission Pause / Cancel

- **Pause**: stop launching new tasks, gracefully cancel running task runners,
  preserve all state.  Resume continues correctly.
- **Cancel**: stop all active task runners, prevent `READY` tasks from launching,
  preserve branches/worktrees for audit, mark unfinished tasks `CANCELLED`.

## Restart Recovery

On backend restart:
1. Reap orphaned provider processes (PID ownership verified)
2. Recover active missions into `RECOVERING`
3. Reconstruct task states from `tasks` table
4. Reconstruct locks from `task_locks` table
5. Reconstruct provider reservations from `provider_reservations` table
6. Resume scheduling loop

Recovery and already-merged checks aim to avoid duplicate writers/merges, but
provider calls can be re-executed after interruption. Task-level cancel currently
updates rows/releases locks without interrupting the provider; process-lifetime
ownership needs strengthening before claiming uniform cancellation guarantees.
Reservations cover DAG tasks, not every provider invocation. Scope keys also lack
a repository namespace, and configured per-provider concurrency above one is not
consistently honored. These are current limitations, not implemented guarantees.
