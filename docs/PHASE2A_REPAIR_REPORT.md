# Phase 2A Repair — Engineering Report

## 1. Commit & Repository State

| Item | Value |
|------|-------|
| Branch | `phase2/task-dag-parallelism` |
| HEAD | `ec16ca6` |
| Status | clean (nothing to commit, working tree clean) |
| Certified v1 tag | `v1.0-core-certified` @ `c74347de3f1537bfec7f558bb5062cd98e992001` (untouched) |

## 2. Provider Authentication & Availability

Pre-flight CLI health checks (non-interactive `say hello` prompt):

| Provider | Installed | Authenticated | Quota State |
|----------|-----------|---------------|-------------|
| `claude` | 2.1.263 | yes | **EXHAUSTED** — weekly limit, resets Sep 8 |
| `codex`  | 0.153.4 | yes | **EXHAUSTED** — usage limit, resets Sep 8 |
| `agy`    | 1.1.27  | yes | **AVAILABLE** — responded successfully |
| `opencode`| 1.17.13 | yes | **AVAILABLE** — responded successfully |

For the dogfood run `claude` and `codex` were explicitly disabled in config to avoid burning remaining quota. The mission therefore exercised **two distinct live providers** (`agy`, `opencode`).

## 3. Real Dogfood Mission — Evidence

### 3.1 Setup
- **Disposable repo**: `/tmp/gg-dogfood-Ev28Dq`
- **Project ID**: `dogfood-proj`
- **Mission ID**: `dogfood-mission`
- **Scheduling mode**: `PARALLEL_SAFE`
- **DAG**: two independent `implementation` tasks (no dependencies)

### 3.2 Task Assignments & Overlapping Execution

| Task | Provider | Run ID | Started (UTC) | Finished (UTC) | Duration |
|------|----------|--------|---------------|----------------|----------|
| `task-a` (greeting module) | `agy` | `run-1788778653393831` | 10:57:33.396 | 10:58:48.367 | ~75 s |
| `task-b` (farewell module) | `opencode` | `run-1788778653429128` | 10:57:33.431 | 10:58:02.123 | ~29 s |

**Overlap proved**: `task-b` started 35 ms after `task-a` and ran in parallel for ~28.7 seconds (`overlapped: true`).

### 3.3 Worktrees & Branches

| Task | Worktree Path | Branch | Checkpoint Commit |
|------|---------------|--------|-------------------|
| `task-a` | `…/worktrees/dogfood-miss/task-a` | `gg/dogfood-miss/task-a` | `20df3a0296fdd746d0d40482e895cfdb5a267bce` |
| `task-b` | `…/worktrees/dogfood-miss/task-b` | `gg/dogfood-miss/task-b` | `455541841faea95e6b15ab414ee0290a28397dc3` |

Each task ran in its own isolated worktree created from the identical base commit `cb0dade…`.

### 3.4 Integration

- **Integration ID**: `int-1788778728421012`
- **Status**: `COMPLETED`
- **Merged commit**: `cf0d24edecec834f4a4fceceef75474811723f53`
- **Conflict files**: `[]` (none)
- **Summary**: `integrated gg/dogfood-miss/task-b; integrated gg/dogfood-miss/task-a`

Both checkpointed branches were merged into `main` without conflicts; no work was discarded.

### 3.5 Structured Review Pipeline

- **Review run ID**: `run-1788778728468567`
- **Provider**: `agy`
- **Role**: `review`
- **Started**: 10:58:48.484
- **Finished**: 11:01:36.673
- **Output contract**: `REVIEW_FINDINGS_JSON: []`
- **Blockers / HIGH findings**: 0
- **Auto-fix applied**: reviewer fixed an import-order issue in `tests/test_dogfood.py` and committed the fix (`git_commit_after` moved from `cf0d24…` → `29009e1…`).

The review pipeline executed independently, produced parseable structured output, found no blockers, and the mission proceeded to verification.

### 3.6 Final Verification

| Command | Exit | Duration | Tail |
|---------|------|----------|------|
| `uv run pytest -q` | 0 | 0.139 s | `2 passed in 0.00s` |
| `uv run ruff check .` | 0 | 0.022 s | `All checks passed!` |
| `uv run mypy .` | 0 | 0.099 s | `Success: no issues found in 4 source files` |

- `attempted`: `true`
- `all_passed`: `true`

### 3.7 Final Mission State

- **Status**: `COMPLETED`
- **Final git head**: `29009e1f44e806d7ccf01210419c12cb765c709e`
- **Repair cycles**: 0
- **Blocking issue**: `null`

COMPLETED was reached only after all gates passed (tasks → integration → review → verification).

## 4. SIGKILL Recovery Regression

Test: `tests/test_phase2_repair.py::test_sigkill_recovery_reconciles_running_tasks`

- **Result**: PASSED
- Simulated two `RUNNING` tasks with active reservations and locks.
- `_reconcile_running_tasks()` correctly:
  - Reaped dead PIDs via `/proc/{pid}` checks
  - Released provider reservations
  - Released task locks
  - Reset tasks to `PENDING`/`FAILED`
- No duplicate writers or stale reservations remained.

## 5. Full Verification Suite

### Backend
```
mypy --strict src      → Success: no issues found in 36 source files
ruff check src tests   → All checks passed!
pytest tests/          → 211 passed, 2 warnings
```

### Frontend
```
npm run typecheck      → clean (tsc -b --noEmit)
npm run lint           → 1 warning (react-refresh/only-export-components), exit 0
npm run test -- --run  → 12 passed, 5 test files
```

## 6. Remaining Limitations

1. **Provider diversity ceiling**: `claude` and `codex` were quota-exhausted before the dogfood run. Real failover across four providers was not exercised; only `agy` + `opencode` were available.
2. **Controlled parallelism**: The dogfood mission used narrow, non-overlapping workspace scopes (`src/greeting.py`, `src/farewell.py`) and explicit preferred-provider assignments to guarantee parallel execution. Missions with broader scopes or implicit provider selection may still serialize when lock contention or capacity limits arise.
3. **No human gate exercised**: The mission did not hit a `WAITING_FOR_HUMAN` state, so the gate-resolution path was not dogfooded live.
4. **Repair cycle untested live**: Review returned zero blockers, so the repair → re-review loop was not triggered during the real run. It is covered deterministically by unit tests (`test_phase2_repair.py`).
5. **Scale**: The workload was intentionally trivial (two single-file modules). Behavior under larger DAGs (≥10 tasks) remains theoretical pending further load testing.

## 7. Audit Handoff

**This commit is a repair candidate, not an independently certified release.**

- Certified v1 core remains recoverable at tag `v1.0-core-certified`.
- All 8 audited defects have been addressed in the source and are guarded by 11 repair regression tests.
- The parallel scheduler is gated behind `scheduling_mode == PARALLEL_SAFE`; sequential v1 is still the default.
- **Recommended next step**: AGY / Opus 4.6 should audit `1dc84e5` on `phase2/task-dag-parallelism` for:
  - Concurrency safety under broader workspace scopes
  - Provider failover behavior with >2 available providers
  - Repair-cycle correctness with real blockers
  - Large-DAG performance and deadlock detection
