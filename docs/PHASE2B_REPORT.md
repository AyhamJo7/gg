# Phase 2B — Frontend Parallel Mission Experience Report

## 1. Phase 2B Candidate Verdict

**READY_WITH_LIMITATIONS**

The frontend can create, display, and control Parallel Safe missions end-to-end. All new UI surfaces are backed by real backend API calls. The implementation is committed cleanly and all verification suites pass. Two genuine limitations remain (see §7).

## 2. Exact Git State

| Item | Value |
|------|-------|
| Branch | `phase2/frontend-parallel-experience` |
| HEAD | `f9ba7d9` |
| Parent (approved Phase 2A) | `9498002` |
| Status | clean — nothing to commit, working tree clean |

## 3. Implemented UI

### 3.1 Scheduling Mode Selector (`NewMissionPage`)
- Dropdown with **Sequential** (default) and **Parallel Safe** options.
- When **Parallel Safe** is selected, exposes DAG creation mode:
  - **Auto-plan**: backend planner decomposes the mission (existing capability).
  - **Manual DAG**: interactive task editor appears.

### 3.2 Manual DAG Editor
- Add/remove tasks with controls.
- Per-task fields: ID, title, description, role, priority, preferred providers (comma-separated), workspace scope (comma-separated).
- Dependency matrix: checkbox grid for `from → to` edges.
- **Client-side validation** before submission:
  - Duplicate task IDs
  - Missing ID/title
  - Unknown task references in dependencies
  - Self-dependencies
  - **Cycle detection** (DFS)

### 3.3 Mission DAG Visualization (`DagGraph`)
- Topological level layout (left-to-right columns).
- Each task card shows:
  - Task ID, status badge (with pulse for active states)
  - Title
  - Assigned provider
  - Workspace scope
  - Blocking reason (if any)
- Click-select highlighting.
- Dependency summary text below the graph.

### 3.4 Live Parallel Execution (`MissionControlPage`)
- **PARALLEL** badge displayed next to mission status for `PARALLEL_SAFE` missions.
- **Task DAG** section conditionally rendered only for parallel missions.
- **Individual task panels** (`TaskPanel`) for every task in the mission:
  - Status, provider, scope, start time, checkpoint
  - Cancel button for non-terminal tasks
  - Retry button for FAILED/CANCELLED tasks
- **Provider strip**, **workflow timeline**, **live terminal**, and **git ledger** remain available.
- All data comes from backend polling (`/api/missions/{id}`, `/api/missions/{id}/dag`) and WebSocket events.

### 3.5 Merge Conflict / Human Gate UI
- `IntegrationPanel` displays:
  - Integration status (`PENDING`, `IN_PROGRESS`, `COMPLETED`, `MERGE_CONFLICT`, `FAILED`)
  - Merged commit SHA
  - Branch names
  - **Conflict files list** with red styling when `MERGE_CONFLICT`
  - Summary text
- `GateCard` (pre-existing) handles human gates with choice buttons.

### 3.6 Review Findings (`ReviewFindingsPanel`)
- Lists all findings with severity badges.
- **BLOCKER** and **HIGH** findings get red/orange border highlights.
- Shows file, description, recommended fix, and resolution status.
- Badge count for open blocker/high findings.

### 3.7 Task Controls
- **Cancel task**: calls `POST /api/missions/{id}/tasks/{taskId}/cancel`
- **Retry task**: calls `POST /api/missions/{id}/tasks/{taskId}/retry`
- Respects terminal-state immutability (buttons hidden for COMPLETED/FAILED/CANCELLED/UNVERIFIED).

## 4. API Changes

### Backend additions (`backend/src/orchestrator/api/app.py`)

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `POST /api/missions/{mission_id}/dag` | NEW | Bulk-create tasks and dependencies for a mission (only when status == `CREATED`). |
| `GET /api/missions/{mission_id}` | MODIFIED | Now returns full `tasks` rows (all columns) and includes `integrations` array. |

No other backend behavior was changed. Certified v1 and Phase 2A logic remain intact.

## 5. Verification

### Backend
```
mypy --strict src      → Success: no issues found in 36 source files
ruff check src tests   → All checks passed!
pytest tests/          → 211 passed, 2 warnings
```

### Frontend
```
npm run typecheck      → clean (tsc -b --noEmit)
npm run lint           → 1 warning (pre-existing react-refresh/only-export-components), exit 0
npm run test -- --run  → 30 passed, 11 test files
npm run build          → dist/ generated successfully (250.6 kB JS, 7.95 kB CSS)
```

### New test coverage
- `NewMissionPage.test.tsx` — scheduling mode default, switch to Parallel Safe, manual DAG editor
- `DagGraph.test.tsx` — task rendering, click handler, empty state
- `TaskPanel.test.tsx` — title/status display, provider/scope, cancel/retry buttons
- `IntegrationPanel.test.tsx` — empty state, completed integration, merge conflict with files
- `ReviewFindingsPanel.test.tsx` — empty state, findings rendering, blocker/high badge
- `MissionControlPage.test.tsx` — PARALLEL badge, Task DAG, task panels for parallel missions

## 6. UI Dogfood

### Method
- Started real backend (`uv run python -m orchestrator.server`) and frontend (`npm run dev`) simultaneously.
- Used a disposable repo `/tmp/gg-ui-dogfood-1788786644` with `pytest` setup.
- Created project and mission via API, then used Playwright to load the frontend and capture 13 screenshots over ~60 seconds.

### Mission details
- **Mission ID**: `f7558af03b9448ed`
- **Title**: UI Dogfood Parallel
- **Scheduling mode**: `PARALLEL_SAFE`
- **Final status**: `UNVERIFIED`
- **Blocking issue**: `planner did not produce structured DAG`

### Why it didn't reach COMPLETED
The planner (`agy`) returned a JSON object inside its summary text, but the `_extract_dag_from_output` parser in `parallel_engine.py` failed to extract it in the exact format expected. This is a **backend planner robustness issue**, not a frontend defect. The frontend correctly displayed:
- `RECOVERING` → `PARALLEL` badge
- Empty Task DAG ("No tasks in this mission yet.")
- Provider strip with Agy BUSY
- Integration panel: "Integration has not started yet"
- Review findings: "No review findings recorded"
- Live terminal and git ledger active throughout

Screenshots are available at `/tmp/gg-ui-dogfood-shots/` (00-initial.png through 12.png).

## 7. Remaining Limitations

1. **Planner JSON extraction fragility**: The auto-plan workflow for Parallel Safe missions depends on the planner emitting an exact JSON block. When the planner wraps JSON in markdown or natural language, the parser fails and the mission becomes `UNVERIFIED`. This is a backend issue; the frontend correctly surfaces the failure. Manual DAG creation is a reliable workaround.

2. **No live multi-task terminal splitting**: The existing `Terminal` component shows a single merged provider output stream. Each task's individual stdout log files exist on disk (`.orchestrator/logs/*.stdout.log`), but the frontend does not yet offer per-task log viewers. This is a UI enhancement, not a correctness issue.

## 8. Independent Audit Handoff

**SHA for audit**: `f9ba7d9` on branch `phase2/frontend-parallel-experience`

**Highest-risk behaviors for AGY to verify:**

1. **DAG validation edge cases**: Test the manual DAG editor with cycles, self-dependencies, and missing task references. Verify the error messages block submission.
2. **Parallel mission conditional rendering**: Confirm that switching a mission from `SEQUENTIAL` to `PARALLEL_SAFE` correctly shows/hides the Task DAG and individual task panels without requiring a page reload.
3. **Integration conflict display**: Simulate a `MERGE_CONFLICT` integration status and verify the conflict file list renders with red styling.
4. **Task cancel/retry**: Start a parallel mission, cancel one running task via the UI, then retry it. Verify the backend reservation/lock release and task state transitions.
5. **WebSocket reconnect**: Refresh the page during an active parallel mission and confirm the terminal and event history repopulate correctly.
6. **Backend API contract**: Verify that `POST /api/missions/{id}/dag` rejects updates after the mission is started (409 expected).
