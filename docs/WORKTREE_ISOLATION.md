# Worktree Isolation

## Decision

Use **Git worktrees + namespaced task branches** for parallel task isolation.

## Context

Two approaches were evaluated:

1. **Shared working tree + file locks**
   - Simpler infrastructure
   - Higher risk of collision if scopes overlap unexpectedly
   - Harder to recover from a bad write
   - Git history is mixed

2. **Git worktrees + task branches**
   - Each agent operates in a completely isolated directory
   - Git auditability: every task has its own branch with clean history
   - Merge conflicts are detected at integration time, not during write
   - Easy to discard or inspect individual task work
   - Slightly more disk usage

## Decision Rationale

For valuable repositories, **safety > convenience**.  Worktrees maximize:

| Criterion | Worktrees | Shared Tree |
|-----------|-----------|-------------|
| Safety | High — no direct collision possible | Medium — relies on scope parsing |
| Recoverability | High — discard/inspect any branch | Low — mixed history |
| Git auditability | High — per-task branch | Low — single branch |
| Merge isolation | High — merge happens centrally | N/A |
| Restart recovery | High — re-attach worktrees | Medium — re-acquire locks |

## Implementation

### Branch Naming

```
gg/<mission-id-short>/<task-id-short>
```

Example: `gg/a1b2c3d4/e5f6a7b8`

If a user branch with the same name already exists, a short random suffix is
appended.  **Never overwrite existing user branches.**

### Worktree Layout

```
<project>/.orchestrator/worktrees/<mission-short>/<task-short>/
```

### Lifecycle

1. **Create**: on task launch, create worktree from base commit
2. **Checkpoint**: provider checkpoints inside the worktree
3. **Complete**: worktree stays until integration
4. **Integration**: branches are merged into the main branch
5. **Retention**: cleanup helpers exist, but integration does not automatically
   remove every task worktree/branch. Inspect ownership and evidence needs before
   cleanup. Task worktrees currently start from repository HEAD; dependency branch
   content is not automatically included before a consumer task runs.

### Safety Rules

- Never `git checkout -f` (no force checkout)
- Never `git push --force`
- Never delete user-created worktrees (only `gg/*` branches)
- Only clean up artifacts proven to belong to GG

## Security

Each worktree/task branch uses the **same security pipeline** as the original
workspace:

- `.orchestrator/` directory is excluded from commits
- Sensitive files are reset before checkpoint
- Content-based secret scanning on staged diffs
- Large-file protection
- Same workspace containment rules

## Evidence

Deterministic tests verify:
- Worktree creation succeeds
- Existing user branches are never overwritten
- Task branches are namespaced under `gg/`
- Removed worktrees clean up their branches
- Concurrent writers in different worktrees cannot collide
