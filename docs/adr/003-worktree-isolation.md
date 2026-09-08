# ADR-003: Git Worktree Isolation for Parallel Task Execution

## Status

Accepted — implemented in Phase 2A

## Context

Phase 2A requires safely executing independent engineering tasks in parallel
across multiple AI providers.  The core risk is concurrent destructive writes to
the same repository.

Two architectural options were evaluated:

1. **Shared working tree + fine-grained file locks**
2. **Git worktrees with namespaced branches per task**

## Decision

Adopt **Git worktrees + namespaced branches** as the isolation mechanism for
parallel task execution.

## Consequences

### Positive

- **Zero direct write collisions**: agents operate in completely separate
directories; there is no shared mutable filesystem state during task execution.
- **Deterministic integration**: all merges happen centrally after tasks complete,
so conflicts are discovered at a controlled integration gate rather than during
execution.
- **Full Git auditability**: every task leaves a dedicated branch with clean,
attributed history.
- **Safe discard/inspection**: failed or suspicious task work can be inspected
or discarded without affecting other tasks or the main branch.
- **Simpler restart recovery**: worktrees can be re-discovered from the
`task_branches` table; no fragile lock reconstruction needed.

### Negative

- **Disk usage**: N worktrees consume N times the working tree size (mitigated
by Git's shared object database).
- **Slightly longer setup**: creating a worktree takes ~100-500ms vs. zero for
shared tree.
- **Merge complexity**: integration must handle merge conflicts explicitly.

### Mitigations

- Worktrees are created under `.orchestrator/worktrees/` so they are
self-contained and easy to clean up.
- Merge conflicts trigger a Human Gate rather than auto-resolution.
- Branches use the `gg/` namespace so they are clearly identifiable.

## Alternatives Considered

### Shared Working Tree + File Locks

Rejected because:
- Conservative scope conflict detection serializes many truly independent tasks
  (e.g. `backend/models.py` vs. `backend/views.py` might falsely conflict).
- A scope parser bug or edge case could allow a real collision.
- Recovery after a bad write is difficult because Git history is mixed.
- Restart recovery requires reconstructing all in-memory and persistent locks
  simultaneously, which is error-prone.

## References

- `docs/WORKTREE_ISOLATION.md`
- `docs/PARALLEL_SCHEDULER.md`
- `backend/src/orchestrator/task_worktree.py`
- `backend/src/orchestrator/integration.py`
