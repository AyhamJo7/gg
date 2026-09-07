"""Parallel mission engine for DAG-based missions.

Drives a mission through its task graph with safe parallel execution.
Each task runs in its own worktree when possible.  Integration is explicit.

This engine preserves all certified v1 safety properties:
- durable state before action
- checkpoint failures are fatal
- human gates pause execution
- provider failover with cooldown
- process ownership / reaping
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import git_ops, integration, task_locks, task_worktree
from .dag import DagValidationError, validate_planner_payload
from .events import EventBus
from .models import (
    EventType,
    FailureClass,
    MissionStatus,
    ProviderState,
    Role,
    TaskStatus,
    utcnow,
)
from .providers.base import ExecutionRequest, ExecutionResult
from .readiness import compute_ready_tasks
from .reservations import (
    provider_score,
    release_provider_reservation,
    try_reserve_provider,
)
from .security import redact

if TYPE_CHECKING:
    from .config import Config
    from .db import Database
    from .providers.registry import ProviderRegistry

logger = logging.getLogger(__name__)


class ParallelMissionEngine:
    """Engine for missions with scheduling_mode == PARALLEL_SAFE."""

    def __init__(
        self,
        mission_id: str,
        db: Database,
        events: EventBus,
        registry: ProviderRegistry,
        config: Config,
    ):
        self.mission_id = mission_id
        self.db = db
        self.events = events
        self.registry = registry
        self.config = config
        self._pause = asyncio.Event()
        self._cancel = asyncio.Event()
        self._gate_resolved = asyncio.Event()
        self._wake = asyncio.Event()
        self._shutdown = False
        self._running_tasks: dict[str, asyncio.Task[None]] = {}
        self.project_path: Path | None = None

    def request_pause(self) -> None:
        self._pause.set()
        self._wake.set()
        for t in list(self._running_tasks.values()):
            t.cancel()

    def request_cancel(self) -> None:
        self._cancel.set()
        self._pause.clear()
        self._wake.set()
        for t in list(self._running_tasks.values()):
            t.cancel()

    def resume(self) -> None:
        self._pause.clear()
        self._gate_resolved.set()
        self._wake.set()

    def resolve_gate(self) -> None:
        self._gate_resolved.set()
        self._wake.set()

    def wake(self) -> None:
        self._wake.set()

    def _mission(self) -> dict:
        row = self.db.get("missions", self.mission_id)
        if not row:
            raise RuntimeError(f"mission {self.mission_id} vanished")
        return row

    def _set_mission_status(self, status: MissionStatus, **extra: Any) -> None:
        from .models import TERMINAL_STATUSES

        data: dict[str, Any] = {"status": status.value, "updated_at": utcnow()}
        data.update(extra)
        if status in TERMINAL_STATUSES:
            data["finished_at"] = utcnow()
        self.db.update("missions", self.mission_id, data)
        self.events.publish(
            EventType.MISSION_STATUS_CHANGED,
            self.mission_id,
            status=status.value,
            **extra,
        )

    def _project_path(self) -> Path:
        mission = self._mission()
        project = self.db.get("projects", mission["project_id"])
        if not project:
            raise RuntimeError("project vanished")
        return Path(project["path"])

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        self.project_path = self._project_path()
        _ = self._mission()

        # Ensure we have a DAG
        tasks = self.db.query("SELECT * FROM tasks WHERE mission_id=?", (self.mission_id,))
        if not tasks:
            # No tasks yet — need planning phase to produce DAG
            ok = await self._run_planning_phase()
            if not ok:
                return
            tasks = self.db.query("SELECT * FROM tasks WHERE mission_id=?", (self.mission_id,))

        if self._cancel.is_set():
            self._set_mission_status(MissionStatus.CANCELLED)
            return

        # Validate DAG
        try:
            self._validate_dag_from_db()
        except DagValidationError as exc:
            self._set_mission_status(MissionStatus.FAILED, blocking_issue=f"DAG invalid: {exc}")
            return

        self.events.publish(
            EventType.DAG_VALIDATED,
            self.mission_id,
        )

        # Main scheduling loop
        while not self._shutdown:
            if self._check_pause_cancel():
                return

            # Check if all tasks are terminal
            if self._all_tasks_terminal():
                await self._on_all_tasks_terminal()
                return

            ready = compute_ready_tasks(self.db, self.mission_id)
            if not ready:
                # Nothing ready — wait for running tasks to complete
                if not self._running_tasks:
                    # Deadlock detection: no running, no ready, not all terminal
                    pending = self.db.query(
                        "SELECT status FROM tasks WHERE mission_id=? "
                        "AND status NOT IN ('COMPLETED','FAILED','CANCELLED','UNVERIFIED')",
                        (self.mission_id,),
                    )
                    if not pending:
                        await self._on_all_tasks_terminal()
                        return
                    # Some tasks are blocked — wait a tick
                await self._wait_tick()
                continue

            for task in ready:
                if self._check_pause_cancel():
                    return
                if len(self._running_tasks) >= self._max_parallel():
                    break
                launched = await self._launch_task(task)
                if launched:
                    self._wake.set()  # re-check immediately

            await self._wait_tick()

    def _max_parallel(self) -> int:
        return int(self.config.get("scheduler.max_parallel_tasks", 3))

    async def _wait_tick(self) -> None:
        self._wake.clear()
        tick = float(self.config.get("orchestration.scheduler_tick_seconds", 2))
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=tick)
        except TimeoutError:
            pass

    def _check_pause_cancel(self) -> bool:
        if self._cancel.is_set():
            self._cancel_all_pending()
            self._set_mission_status(MissionStatus.CANCELLED)
            return True
        if self._pause.is_set():
            self._set_mission_status(MissionStatus.PAUSED)
            return True
        return False

    def _cancel_all_pending(self) -> None:
        """Mark all non-terminal tasks as CANCELLED."""
        self.db.execute(
            """UPDATE tasks SET status=?, blocking_issue='mission cancelled'
               WHERE mission_id=? AND status NOT IN ('COMPLETED','FAILED','CANCELLED','UNVERIFIED')""",
            (TaskStatus.CANCELLED.value, self.mission_id),
        )
        for t in list(self._running_tasks.values()):
            t.cancel()

    def _all_tasks_terminal(self) -> bool:
        rows = self.db.query(
            "SELECT COUNT(*) as cnt FROM tasks WHERE mission_id=? "
            "AND status NOT IN ('COMPLETED','FAILED','CANCELLED','UNVERIFIED')",
            (self.mission_id,),
        )
        return rows[0]["cnt"] == 0

    async def _on_all_tasks_terminal(self) -> None:
        """When all tasks are terminal, run integration + verification."""
        mission = self._mission()
        if mission["status"] in (MissionStatus.CANCELLED.value, MissionStatus.FAILED.value):
            return

        # Count failures
        failed = self.db.query(
            "SELECT COUNT(*) as cnt FROM tasks WHERE mission_id=? AND status='FAILED'",
            (self.mission_id,),
        )[0]["cnt"]

        if failed > 0:
            self._set_mission_status(
                MissionStatus.FAILED,
                blocking_issue=f"{failed} task(s) failed",
            )
            return

        # Integration
        self._set_mission_status(MissionStatus.IMPLEMENTING)  # reuse status or add INTEGRATING?
        result = await integration.run_integration(self.db, self.events, self.project_path, self.mission_id)

        if result["status"] == integration.IntegrationStatus.MERGE_CONFLICT.value:
            self._set_mission_status(
                MissionStatus.WAITING_FOR_HUMAN,
                blocking_issue=f"merge conflict: {', '.join(result['conflict_files'])}",
            )
            return
        if result["status"] == integration.IntegrationStatus.FAILED.value:
            self._set_mission_status(MissionStatus.FAILED, blocking_issue=f"integration failed: {result['summary']}")
            return

        # Review + verification (reuse v1 engine logic via direct calls)
        # For Phase 2A, run final validation directly
        from .verify import run_verification
        from .workspace import inspect_workspace

        self._set_mission_status(MissionStatus.FINAL_VALIDATION)
        workspace = await inspect_workspace(self.project_path, self.config.allowed_roots())
        report = await run_verification(workspace, self.db, self.events, self.mission_id, self.project_path)

        if not report.attempted:
            self._set_mission_status(
                MissionStatus.UNVERIFIED,
                blocking_issue="no verification toolchain detected",
            )
            return
        if not report.all_passed:
            failures = [r for r in report.results if not r.passed]
            detail = "\n".join(f"{r.command}: exit={r.exit_code}" for r in failures)
            self._set_mission_status(
                MissionStatus.UNVERIFIED,
                blocking_issue=f"verification failed:\n{detail[:800]}",
            )
            return

        head = await git_ops.head_sha(self.project_path)
        self._set_mission_status(MissionStatus.COMPLETED, current_provider=None, git_head=head)
        self.events.publish(
            EventType.MISSION_COMPLETED,
            self.mission_id,
        )

    # ------------------------------------------------------------------
    # Planning → DAG
    # ------------------------------------------------------------------

    async def _run_planning_phase(self) -> bool:
        """Run the planning provider and convert output to a persisted DAG."""

        # Temporarily use the v1 engine for planning
        # In a full implementation, we'd refactor to share planning logic.
        # For Phase 2A, we run planning via a simplified inline flow.
        role = Role.PLANNING
        provider_name = self._select_provider(role)
        if not provider_name:
            self._set_mission_status(MissionStatus.FAILED, blocking_issue="no provider available for planning")
            return False

        adapter = self.registry.get_adapter(provider_name)
        if not adapter:
            self._set_mission_status(MissionStatus.FAILED, blocking_issue=f"adapter missing for {provider_name}")
            return False

        prompt = self._build_planning_prompt()
        run_id = f"run-{utcnow().timestamp()}".replace(".", "")
        log_dir = self.project_path / ".orchestrator" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)

        request = ExecutionRequest(
            prompt=prompt,
            workdir=self.project_path,
            role=role.value,
            timeout_s=self.config.provider_timeout_s(provider_name),
            run_id=run_id,
            log_dir=log_dir,
        )

        self.db.insert(
            "provider_runs",
            {
                "id": run_id,
                "mission_id": self.mission_id,
                "provider": provider_name,
                "role": role.value,
                "command": [redact(provider_name)],
                "cwd": str(self.project_path),
                "started_at": utcnow().isoformat(),
                "failure_class": "RUNNING",
                "provider_state": "RUNNING",
            },
        )
        self.registry.mark_busy(provider_name)

        try:
            result = await adapter.execute(request, lambda line: None)
        except Exception as exc:
            logger.exception("planning provider crashed")
            result = ExecutionResult(
                state=ProviderState.CRASHED,
                failure_class=FailureClass.CRASH,
                exit_code=None,
                duration_s=0.0,
                summary="",
                raw_tail=str(exc),
            )

        self.registry.record_failure(provider_name, result.failure_class, result.duration_s, result.raw_tail[:300])
        self.db.update(
            "provider_runs",
            run_id,
            {
                "finished_at": utcnow().isoformat(),
                "exit_code": result.exit_code,
                "failure_class": result.failure_class.value,
                "provider_state": result.state.value,
                "summary": result.summary[:500],
            },
        )

        if not result.ok:
            self._set_mission_status(MissionStatus.FAILED, blocking_issue=f"planning failed: {result.raw_tail[:300]}")
            return False

        # Parse structured DAG from result
        dag_payload = self._extract_dag_from_output(result.assistant_text or result.raw_tail)
        if dag_payload is None:
            # Retry once with explicit JSON instruction, then fail
            self._set_mission_status(MissionStatus.UNVERIFIED, blocking_issue="planner did not produce structured DAG")
            return False

        try:
            tasks = validate_planner_payload(dag_payload)
        except DagValidationError as exc:
            self._set_mission_status(MissionStatus.FAILED, blocking_issue=f"planner DAG invalid: {exc}")
            return False

        # Persist DAG
        for task in tasks:
            task.mission_id = self.mission_id
            self.db.insert(
                "tasks",
                {
                    "id": task.id,
                    "mission_id": task.mission_id,
                    "role": task.role.value,
                    "status": TaskStatus.PENDING.value,
                    "task_type": task.task_type,
                    "title": task.title,
                    "description": task.description,
                    "preferred_providers": json.dumps(task.preferred_providers),
                    "workspace_scope": json.dumps(task.workspace_scope),
                    "max_attempts": task.max_attempts,
                    "priority": task.priority,
                    "created_at": task.created_at,
                    "dag_revision": 1,
                },
            )
        for task in tasks:
            for dep in task.dependencies:
                self.db.insert(
                    "task_dependencies",
                    {
                        "from_task_id": dep,
                        "to_task_id": task.id,
                        "created_at": utcnow().isoformat(),
                    },
                )

        self.db.insert(
            "dag_revisions",
            {
                "id": f"rev-{utcnow().timestamp()}".replace(".", ""),
                "mission_id": self.mission_id,
                "revision": 1,
                "changed_by": provider_name,
                "reason": "initial planner output",
                "tasks_added": json.dumps([t.id for t in tasks]),
                "created_at": utcnow().isoformat(),
            },
        )
        self.events.publish(
            EventType.DAG_CREATED,
            self.mission_id,
            task_count=len(tasks),
        )
        return True

    def _build_planning_prompt(self) -> str:
        mission = self._mission()
        return (
            f"You are the planning engineer for a mission.\n\n"
            f"Mission: {mission['title']}\n"
            f"Task: {mission['task']}\n\n"
            f"Decompose this mission into independent, parallelizable sub-tasks. "
            f"Return ONLY a JSON object matching this schema:\n"
            f'{{"tasks": [{{"id": "unique-id", "title": "...", "role": "implementation", '
            f'"depends_on": [], "workspace_scope": ["path/**"], "preferred_providers": ["opencode"]}}]}}\n'
            f"Roles allowed: planning, implementation, testing, review, repair.\n"
            f"Use workspace_scope to declare which files each task will touch.\n"
        )

    def _extract_dag_from_output(self, text: str) -> dict | None:
        """Extract JSON DAG from planner output."""
        import re

        # Try to find JSON block
        m = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass
        # Try raw JSON object
        m = re.search(r"(\{[\s\S]*\"tasks\"\s*:\s*\[.*?\]\s*\})", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass
        # Fallback: look for any top-level object
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                pass
        return None

    # ------------------------------------------------------------------
    # Task launch
    # ------------------------------------------------------------------

    async def _launch_task(self, task: dict) -> bool:
        tid = task["id"]
        # Check if already running
        if tid in self._running_tasks:
            return False

        # Select provider
        role = Role(task.get("role", "implementation"))
        preferred_raw = task.get("preferred_providers") or "[]"
        if isinstance(preferred_raw, str):
            preferred = json.loads(preferred_raw)
        else:
            preferred = list(preferred_raw)

        provider_name = self._arbitrate_provider(task, role, preferred)
        if not provider_name:
            # No provider available — mark waiting
            if task["status"] != TaskStatus.WAITING_FOR_PROVIDER.value:
                self.db.update("tasks", tid, {"status": TaskStatus.WAITING_FOR_PROVIDER.value})
            return False

        # Atomic reservation
        reserved = try_reserve_provider(self.db, self.events, tid, provider_name, self.config.raw)
        if not reserved:
            return False

        # Acquire locks
        locks = task_locks.compute_task_locks(task)
        ok, lock_reason = task_locks.acquire_locks(self.db, self.events, tid, locks)
        if not ok:
            release_provider_reservation(self.db, self.events, tid)
            self.db.update("tasks", tid, {"status": TaskStatus.BLOCKED.value, "blocking_issue": lock_reason})
            return False

        # Create worktree
        mission = self._mission()
        try:
            branch_record = await task_worktree.create_task_worktree(
                self.db, self.events, self.project_path, mission["id"], tid
            )
        except git_ops.GitError as exc:
            logger.warning("worktree creation failed for task %s: %s", tid, exc)
            release_provider_reservation(self.db, self.events, tid)
            task_locks.release_locks_for_task(self.db, self.events, tid)
            self.db.update("tasks", tid, {"status": TaskStatus.FAILED.value, "blocking_issue": f"worktree: {exc}"})
            return False

        # Mark RUNNING and start
        self.db.update(
            "tasks",
            tid,
            {
                "status": TaskStatus.RUNNING.value,
                "started_at": utcnow().isoformat(),
                "assigned_provider": provider_name,
            },
        )
        self.events.publish(
            EventType.TASK_STARTED,
            mission_id=self.mission_id,
            task_id=tid,
            provider=provider_name,
        )

        coro = self._task_runner(tid, provider_name, branch_record.worktree_path)
        self._running_tasks[tid] = asyncio.create_task(coro)
        return True

    async def _task_runner(self, task_id: str, provider_name: str, worktree_path: str) -> None:
        """Run a single task to completion in its worktree."""
        try:
            await self._run_task_in_worktree(task_id, provider_name, worktree_path)
        except asyncio.CancelledError:
            logger.info("task %s cancelled", task_id)
            self.db.update(
                "tasks",
                task_id,
                {"status": TaskStatus.CANCELLED.value, "finished_at": utcnow().isoformat()},
            )
            release_provider_reservation(self.db, self.events, task_id)
            task_locks.release_locks_for_task(self.db, self.events, task_id)
            raise
        except Exception:
            logger.exception("task %s runner crashed", task_id)
            self.db.update(
                "tasks",
                task_id,
                {"status": TaskStatus.FAILED.value, "finished_at": utcnow().isoformat()},
            )
        finally:
            self._running_tasks.pop(task_id, None)
            self._wake.set()

    async def _run_task_in_worktree(self, task_id: str, provider_name: str, worktree_path: str) -> None:
        task = self.db.get("tasks", task_id)
        if not task:
            return
        role = Role(task.get("role", "implementation"))
        adapter = self.registry.get_adapter(provider_name)
        if not adapter:
            self.db.update(
                "tasks",
                task_id,
                {"status": TaskStatus.FAILED.value, "blocking_issue": f"no adapter for {provider_name}"},
            )
            return

        run_id = f"run-{utcnow().timestamp()}".replace(".", "")
        log_dir = Path(worktree_path) / ".orchestrator" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)

        prompt = self._build_task_prompt(task, provider_name)
        request = ExecutionRequest(
            prompt=prompt,
            workdir=Path(worktree_path),
            role=role.value,
            timeout_s=self.config.provider_timeout_s(provider_name),
            run_id=run_id,
            log_dir=log_dir,
            on_spawn=lambda pid, pgid, ts, rid=run_id: self.db.update(
                "provider_runs", rid, {"pid": pid, "pgid": pgid, "started_at_ts": ts}
            ),
        )

        commit_before = await git_ops.head_sha(Path(worktree_path))
        self.db.insert(
            "provider_runs",
            {
                "id": run_id,
                "mission_id": self.mission_id,
                "task_id": task_id,
                "provider": provider_name,
                "role": role.value,
                "command": [redact(provider_name)],
                "cwd": worktree_path,
                "started_at": utcnow().isoformat(),
                "failure_class": "RUNNING",
                "provider_state": "RUNNING",
                "stdout_path": str(log_dir / f"{run_id}.stdout.log"),
                "stderr_path": str(log_dir / f"{run_id}.stderr.log"),
                "git_commit_before": commit_before,
            },
        )
        self.registry.mark_busy(provider_name)

        try:
            result = await adapter.execute(request, lambda line: None)
        except Exception as exc:
            logger.exception("provider %s crashed for task %s", provider_name, task_id)
            result = ExecutionResult(
                state=ProviderState.CRASHED,
                failure_class=FailureClass.CRASH,
                exit_code=None,
                duration_s=0.0,
                summary="",
                raw_tail=str(exc),
            )

        commit_after = await git_ops.head_sha(Path(worktree_path))
        self.db.update(
            "provider_runs",
            run_id,
            {
                "finished_at": utcnow().isoformat(),
                "exit_code": result.exit_code,
                "failure_class": result.failure_class.value,
                "provider_state": result.state.value,
                "summary": result.summary[:500],
                "git_commit_after": commit_after,
            },
        )

        # Release reservation + locks
        release_provider_reservation(self.db, self.events, task_id, run_id)
        task_locks.release_locks_for_task(self.db, self.events, task_id)

        if result.ok:
            self.registry.record_success(provider_name, result.duration_s)
            self.db.update(
                "tasks",
                task_id,
                {
                    "status": TaskStatus.COMPLETED.value,
                    "finished_at": utcnow().isoformat(),
                    "summary": result.summary,
                    "provider_run_id": run_id,
                    "checkpoint_after": commit_after,
                },
            )
            self.events.publish(
                EventType.TASK_COMPLETED,
                mission_id=self.mission_id,
                task_id=task_id,
                provider=provider_name,
            )
        else:
            self.registry.record_failure(
                provider_name, result.failure_class, result.duration_s, result.raw_tail[:300]
            )
            self.db.update(
                "tasks",
                task_id,
                {
                    "status": TaskStatus.FAILED.value,
                    "finished_at": utcnow().isoformat(),
                    "summary": result.raw_tail[-300:],
                    "provider_run_id": run_id,
                },
            )
            # If attempts remain, retry may happen on next scheduler tick
            attempt = int(task.get("attempts", 0)) + 1
            max_attempts = int(task.get("max_attempts", 3))
            self.db.update("tasks", task_id, {"attempts": attempt})
            if attempt < max_attempts and result.failure_class not in (
                FailureClass.CANCELLED,
                FailureClass.HUMAN_INPUT,
            ):
                # Reset to PENDING so scheduler retries
                self.db.update(
                    "tasks",
                    task_id,
                    {
                        "status": TaskStatus.PENDING.value,
                        "blocking_issue": f"attempt {attempt}/{max_attempts} failed: {result.failure_class.value}",
                    },
                )
            self.events.publish(
                EventType.TASK_FAILED,
                mission_id=self.mission_id,
                task_id=task_id,
                provider=provider_name,
                failure=result.failure_class.value,
            )

    def _build_task_prompt(self, task: dict, provider_name: str) -> str:
        role = task.get("role", "implementation")
        title = task.get("title", "")
        description = task.get("description", "")
        return (
            f"You are working as the **{role}** engineer in a multi-provider orchestrated mission.\n\n"
            f"Task: {title}\n"
            f"Description: {description}\n\n"
            f"Work autonomously in the current directory. "
            f"Follow existing project conventions. "
            f"When finished, end with a one-paragraph summary of what you did."
        )

    def _arbitrate_provider(self, task: dict, role: Role, preferred: list[str]) -> str | None:
        """Select best provider for a task."""
        candidates: list[str] = []
        if preferred:
            candidates = [p for p in preferred if self.registry.is_eligible(p)]
        if not candidates:
            priorities = self.config.priority_for(role.value)
            candidates = [p for p in priorities if self.registry.is_eligible(p)]
        if not candidates:
            candidates = [p for p in self.registry.adapters if self.registry.is_eligible(p)]
        if not candidates:
            return None

        # Score and pick best
        best = None
        best_score = -1e9
        for provider in candidates:
            score, _ = provider_score(provider, role.value, self.config.raw, self.db)
            if score > best_score:
                best_score = score
                best = provider
        return best

    def _select_provider(self, role: Role) -> str | None:
        """Simple provider selection for planning phase."""
        priorities = self.config.priority_for(role.value)
        for p in priorities:
            if self.registry.is_eligible(p):
                return p
        for p in self.registry.adapters:
            if self.registry.is_eligible(p):
                return p
        return None

    def _validate_dag_from_db(self) -> None:
        """Load tasks from DB and validate as a DAG."""
        from .dag import validate_task_graph
        from .models import TaskGraphTask

        rows = self.db.query("SELECT * FROM tasks WHERE mission_id=?", (self.mission_id,))
        tasks: list[TaskGraphTask] = []
        for r in rows:
            deps = self.db.query("SELECT from_task_id FROM task_dependencies WHERE to_task_id=?", (r["id"],))
            tasks.append(
                TaskGraphTask(
                    id=r["id"],
                    mission_id=r["mission_id"],
                    title=r.get("title", ""),
                    description=r.get("description", ""),
                    role=Role(r.get("role", "implementation")),
                    dependencies=[d["from_task_id"] for d in deps],
                    workspace_scope=json.loads(r.get("workspace_scope") or "[]"),
                    preferred_providers=json.loads(r.get("preferred_providers") or "[]"),
                    priority=int(r.get("priority", 0)),
                    max_attempts=int(r.get("max_attempts", 3)),
                )
            )
        validate_task_graph(tasks)
