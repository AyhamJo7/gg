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
from .dag import DagValidationError, namespace_dag_ids, validate_planner_payload
from .events import EventBus
from .handoff import persist_handoff, render_handoff
from .models import (
    EventType,
    FailureClass,
    IntegrationStatus,
    Mission,
    MissionStatus,
    ProviderState,
    Role,
    TaskStatus,
    utcnow,
)
from .providers.base import ExecutionRequest, ExecutionResult
from .readiness import compute_ready_tasks
from .reservations import (
    active_reservations_for_provider,
    provider_score,
    release_provider_reservation,
    try_reserve_provider,
)
from .review import (
    REVIEW_INSTRUCTIONS,
    mark_findings_repair_attempted,
    open_blockers,
    persist_findings,
)
from .security import redact
from .workspace import inspect_workspace

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

    def _mission(self) -> dict[str, Any]:
        row = self.db.get("missions", self.mission_id)
        if not row:
            raise RuntimeError(f"mission {self.mission_id} vanished")
        return row

    def _mission_model(self) -> Mission:
        row = self._mission()
        row["providers_used"] = json.loads(row.get("providers_used") or "[]")
        row["providers_failed"] = json.loads(row.get("providers_failed") or "[]")
        return Mission(**{k: v for k, v in row.items() if k in Mission.model_fields})

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

    def _require_project_path(self) -> Path:
        if self.project_path is None:
            raise RuntimeError("engine project path not initialized")
        return self.project_path

    def _on_spawn_handler(self, run_id: str) -> Any:
        def handler(pid: int, pgid: int, ts: float) -> None:
            self.db.update("provider_runs", run_id, {"pid": pid, "pgid": pgid, "started_at_ts": ts})

        return handler

    # ------------------------------------------------------------------
    # Recovery
    # ------------------------------------------------------------------

    async def _reconcile_running_tasks(self) -> None:
        """On startup, reconcile tasks that were RUNNING/CLAIMED before crash.

        Invariants: no task is left without a runner; reservations and locks
        held by dead tasks are released; attempts are durably counted; a
        still-alive but unverifiable process is never killed and never
        requeued blindly (loud FAILED instead of silent stuck or duplicate
        writers); stale reservations/locks on non-running tasks are dropped
        (no runner exists at engine start to own them).
        """
        from .orphans import kill_process_tree, process_alive, verify_process_ownership

        running = self.db.query(
            "SELECT * FROM tasks WHERE mission_id=? AND status IN ('RUNNING','CLAIMED')",
            (self.mission_id,),
        )
        for task in running:
            tid = task["id"]
            runs = self.db.query(
                "SELECT * FROM provider_runs WHERE task_id=? ORDER BY started_at DESC LIMIT 1",
                (tid,),
            )
            if not runs:
                # No provider_run record — just clean up and reset
                release_provider_reservation(self.db, self.events, tid)
                task_locks.release_locks_for_task(self.db, self.events, tid)
                self.db.update(
                    "tasks",
                    tid,
                    {
                        "status": TaskStatus.PENDING.value,
                        "blocking_issue": "SIGKILL recovery: no active process record",
                    },
                )
                continue
            run = runs[0]
            pid = run.get("pid")
            if pid is None or not process_alive(pid):
                # Safe to retry: the spawn handshake guarantees a provider
                # child can only exec after its identity is persisted, so a
                # missing pid (or a dead/zombie one) proves nothing ever wrote.
                self._reset_interrupted_task(task, run["id"], f"SIGKILL recovery: process {pid} vanished")
                continue
            provider = run.get("provider") or ""
            pgid = run.get("pgid")
            verified = pgid is not None and await asyncio.to_thread(
                verify_process_ownership, pid, pgid, run.get("started_at_ts"), provider
            )
            if verified and pgid is not None:
                # Positively ours: terminate the orphan, then retry like vanished
                logger.warning("reaping verified orphan provider process pid=%s pgid=%s task=%s", pid, pgid, tid)
                await asyncio.to_thread(kill_process_tree, pgid)
                self._reset_interrupted_task(task, run["id"], f"SIGKILL recovery: orphaned process {pid} reaped")
                continue
            # Alive but not verifiable as ours: never kill, never requeue.
            # Fail loudly with evidence instead of stuck-forever or duplicates.
            self.db.update(
                "provider_runs",
                run["id"],
                {
                    "failure_class": FailureClass.CRASH.value,
                    "provider_state": ProviderState.CRASHED.value,
                    "finished_at": utcnow().isoformat(),
                    "summary": "orphaned process unverifiable on backend startup",
                },
            )
            release_provider_reservation(self.db, self.events, tid)
            task_locks.release_locks_for_task(self.db, self.events, tid)
            self.db.update(
                "tasks",
                tid,
                {
                    "status": TaskStatus.FAILED.value,
                    "blocking_issue": (
                        f"SIGKILL recovery: live process {pid} not verifiable as ours "
                        "(not killed, not requeued — manual review required)"
                    ),
                },
            )
            self.events.publish(
                EventType.TASK_FAILED,
                mission_id=self.mission_id,
                task_id=tid,
                reason="sigkill_recovery_unverifiable",
            )
        # Drop stale reservations/locks owned by tasks with no runner.
        for task in self.db.query(
            "SELECT id FROM tasks WHERE mission_id=? AND status NOT IN ('RUNNING','CLAIMED','WAITING_FOR_PROVIDER')",
            (self.mission_id,),
        ):
            release_provider_reservation(self.db, self.events, task["id"])
            task_locks.release_locks_for_task(self.db, self.events, task["id"])

    def _reset_interrupted_task(self, task: dict[str, Any], run_id: str, reason: str) -> None:
        """Mark the run crashed and return the task to PENDING (or FAILED when out of attempts)."""
        tid = task["id"]
        self.db.update(
            "provider_runs",
            run_id,
            {
                "failure_class": FailureClass.CRASH.value,
                "provider_state": ProviderState.CRASHED.value,
                "finished_at": utcnow().isoformat(),
            },
        )
        release_provider_reservation(self.db, self.events, tid)
        task_locks.release_locks_for_task(self.db, self.events, tid)

        attempt = int(task.get("attempts", 0)) + 1
        max_attempts = int(task.get("max_attempts", 3))
        self.db.update("tasks", tid, {"attempts": attempt})
        if attempt < max_attempts:
            self.db.update(
                "tasks",
                tid,
                {"status": TaskStatus.PENDING.value, "blocking_issue": reason},
            )
        else:
            self.db.update(
                "tasks",
                tid,
                {"status": TaskStatus.FAILED.value, "blocking_issue": f"{reason}, exhausted attempts"},
            )
        self.events.publish(
            EventType.TASK_FAILED,
            mission_id=self.mission_id,
            task_id=tid,
            reason="sigkill_recovery",
        )

    # ------------------------------------------------------------------
    # Blockage detection
    # ------------------------------------------------------------------

    def _detect_permanent_blockage(self) -> None:
        """Fail tasks whose dependencies are permanently terminal."""
        non_terminal = self.db.query(
            "SELECT * FROM tasks WHERE mission_id=? AND status NOT IN ('COMPLETED','FAILED','CANCELLED','UNVERIFIED')",
            (self.mission_id,),
        )
        for task in non_terminal:
            tid = task["id"]
            deps = self.db.query(
                "SELECT from_task_id FROM task_dependencies WHERE to_task_id=?",
                (tid,),
            )
            for dep in deps:
                dep_task = self.db.get("tasks", dep["from_task_id"])
                if dep_task and dep_task["status"] in (
                    TaskStatus.FAILED.value,
                    TaskStatus.CANCELLED.value,
                ):
                    self.db.update(
                        "tasks",
                        tid,
                        {
                            "status": TaskStatus.FAILED.value,
                            "blocking_issue": f"permanently blocked: dependency {dep['from_task_id']} failed",
                        },
                    )
                    self.events.publish(
                        EventType.TASK_FAILED,
                        mission_id=self.mission_id,
                        task_id=tid,
                        reason="permanent_blockage",
                    )
                    break

    def _reset_waiting_without_reservation(self) -> None:
        """Reset WAITING_FOR_PROVIDER tasks that lost their reservation."""
        waiting = self.db.query(
            "SELECT id FROM tasks WHERE mission_id=? AND status=?",
            (self.mission_id, TaskStatus.WAITING_FOR_PROVIDER.value),
        )
        for w in waiting:
            has_res = self.db.query(
                "SELECT 1 FROM provider_reservations WHERE task_id=? AND released_at IS NULL",
                (w["id"],),
            )
            if not has_res:
                self.db.update("tasks", w["id"], {"status": TaskStatus.PENDING.value})

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        self.project_path = self._project_path()
        _ = self._mission()

        # SIGKILL recovery
        await self._reconcile_running_tasks()

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

            # Wake up WAITING_FOR_PROVIDER tasks that lost their reservation
            self._reset_waiting_without_reservation()

            # Detect permanent blockage before terminal check
            self._detect_permanent_blockage()

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
        return int(rows[0]["cnt"]) == 0

    async def _on_all_tasks_terminal(self) -> None:
        """When all tasks are terminal, run integration + review + verification."""
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

        project_path = self._require_project_path()

        # Integration idempotency
        completed_integrations = self.db.query(
            "SELECT * FROM task_integrations WHERE mission_id=? AND status=? ORDER BY created_at DESC LIMIT 1",
            (self.mission_id, IntegrationStatus.COMPLETED.value),
        )
        if completed_integrations:
            merged_commit = completed_integrations[0].get("merged_commit")
            logger.info("integration already completed for mission %s at %s", self.mission_id, merged_commit)
        else:
            self._set_mission_status(MissionStatus.IMPLEMENTING)
            result = await integration.run_integration(self.db, self.events, project_path, self.mission_id)

            if result["status"] == IntegrationStatus.MERGE_CONFLICT.value:
                self._set_mission_status(
                    MissionStatus.WAITING_FOR_HUMAN,
                    blocking_issue=f"merge conflict: {', '.join(result['conflict_files'])}",
                )
                return
            if result["status"] == IntegrationStatus.FAILED.value:
                self._set_mission_status(
                    MissionStatus.FAILED,
                    blocking_issue=f"integration failed: {result['summary']}",
                )
                return

        # Certified review pipeline
        review_ok = await self._phase_review_loop()
        if not review_ok:
            return

        # Final validation
        from .verify import run_verification

        self._set_mission_status(MissionStatus.FINAL_VALIDATION)
        workspace = await inspect_workspace(project_path, self.config.allowed_roots())
        report = await run_verification(workspace, self.db, self.events, self.mission_id, project_path)

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

        head = await self._final_checkpoint(project_path, "orchestrator: final verified state")
        if head is None:
            return
        self._set_mission_status(MissionStatus.COMPLETED, current_provider=None, git_head=head)
        self.events.publish(
            EventType.MISSION_COMPLETED,
            self.mission_id,
        )

    async def _final_checkpoint(self, project_path: Path, message: str) -> str | None:
        """Commit the final verified tree and return its SHA.

        Mirrors the certified v1 checkpoint semantics: sensitive files,
        `.orchestrator/` metadata, and oversized files are excluded by
        `git_ops.checkpoint`; consecutive failures are counted durably and
        exhaust into UNVERIFIED (never COMPLETED). A transient failure
        leaves the mission in FINAL_VALIDATION so resume/restart retries
        idempotently. Returns None when completion must not proceed.
        """
        if not self.config.get("git.auto_checkpoint", True):
            return await git_ops.head_sha(project_path)
        try:
            max_mb = int(self.config.get("git.max_auto_commit_file_mb", 5))
            sha = await git_ops.checkpoint(project_path, message, max_file_mb=max_mb)
        except git_ops.GitCheckpointError as exc:
            logger.warning("final checkpoint failed: %s", exc)
            row = self.db.get("missions", self.mission_id)
            failures = int((row or {}).get("checkpoint_failures", 0)) + 1
            self.db.update("missions", self.mission_id, {"checkpoint_failures": failures, "updated_at": utcnow()})
            self.events.publish(EventType.GIT_CHECKPOINT_FAILED, self.mission_id, error=str(exc))
            max_failures = int(self.config.get("git.max_checkpoint_failures", 2))
            if failures >= max_failures:
                self._set_mission_status(
                    MissionStatus.UNVERIFIED,
                    blocking_issue=f"final checkpoint failed repeatedly ({failures}x): {exc}",
                    current_provider=None,
                )
            return None
        self.db.update("missions", self.mission_id, {"checkpoint_failures": 0, "updated_at": utcnow()})
        if sha:
            mission = self._mission()
            self.db.insert(
                "checkpoints",
                {
                    "id": f"ckpt-{utcnow().timestamp()}",
                    "mission_id": self.mission_id,
                    "project_id": mission.get("project_id"),
                    "commit_sha": sha,
                    "message": message,
                    "created_at": utcnow(),
                },
            )
            self.events.publish(EventType.GIT_CHECKPOINT_CREATED, self.mission_id, sha=sha)
            return sha
        return await git_ops.head_sha(project_path)

    def _prior_findings_context(self) -> str:
        """List prior findings with stable IDs so the reviewer can re-flag or verify each one."""
        rows = self.db.query(
            "SELECT id, severity, status, file, description, fingerprint FROM review_findings "
            "WHERE mission_id=? AND status IN ('open','repair_attempted') ORDER BY created_at ASC",
            (self.mission_id,),
        )
        if not rows:
            return ""
        lines = [
            "## Prior findings (re-flag if still present, or verify fixed with evidence)",
            "Omission without verification leaves a finding UNVERIFIED.",
        ]
        lines.extend(
            f"- id={r['id']} fp={r.get('fingerprint') or '-'} [{r['severity']}/{r['status']}] "
            f"{r['file'] or ''}: {r['description'][:300]}"
            for r in rows
        )
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Review pipeline (mirrors v1 MissionEngine)
    # ------------------------------------------------------------------

    async def _phase_review_loop(self) -> bool:
        if not self.config.get("orchestration.review_required", True):
            return True
        max_cycles = int(self.config.get("orchestration.max_repair_cycles", 3))
        max_unparseable_attempts = int(self.config.get("orchestration.max_unparseable_review_attempts", 2))
        while True:
            result = await self._run_provider_phase_for_role(Role.REVIEW, extra_context=self._prior_findings_context())
            if result is None:
                return False
            review_input = result.assistant_text + "\n" + result.summary
            parsed_ok, findings = persist_findings(self.db, self.mission_id, review_input)
            self._record_review_provenance(review_parsed=parsed_ok)
            for f in findings:
                self.events.publish(
                    EventType.REVIEW_FINDING_CREATED,
                    self.mission_id,
                    severity=f.severity.value,
                    description=f.description[:200],
                )
            if not parsed_ok:
                unparseable_count = self._unparseable_review_count()
                if unparseable_count >= max_unparseable_attempts:
                    self._set_mission_status(
                        MissionStatus.UNVERIFIED,
                        blocking_issue="reviewer output unparseable after retries — cannot verify code review",
                        current_provider=None,
                    )
                    return False
                continue

            blockers = open_blockers(self.db, self.mission_id)
            if not blockers:
                return True
            cycles = int(self._mission().get("repair_cycles", 0))
            if cycles >= max_cycles:
                self._set_mission_status(
                    MissionStatus.UNVERIFIED,
                    blocking_issue=f"{len(blockers)} blocker/high findings remain after {cycles} repair cycles",
                    current_provider=None,
                )
                return False
            self.db.update(
                "missions",
                self.mission_id,
                {"repair_cycles": cycles + 1, "updated_at": utcnow().isoformat()},
            )
            self._set_mission_status(MissionStatus.REPAIRING)
            findings_text = "\n".join(
                f"- [{f['severity']}] id={f['id']} {f['file'] or ''}: {f['description']} → {f['recommended_fix']}"
                for f in blockers
            )
            repair = await self._run_provider_phase_for_role(
                Role.REPAIR, extra_context=f"## Open findings to fix\n{findings_text}"
            )
            if repair is None:
                return False
            mark_findings_repair_attempted(self.db, self.mission_id)
            self._set_mission_status(MissionStatus.REVIEWING)

    async def _run_provider_phase_for_role(self, role: Role, extra_context: str = "") -> ExecutionResult | None:
        provider_name = self._select_provider_for_role(role)
        if not provider_name:
            return None

        adapter = self.registry.get_adapter(provider_name)
        if not adapter:
            return None

        project_path = self._require_project_path()

        run_id = f"run-{utcnow().timestamp()}".replace(".", "")
        log_dir = project_path / ".orchestrator" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)

        handoff_content = await self._make_handoff(role, None, provider_name, f"Run {role.value} phase")
        prompt = self._build_prompt(role, handoff_content, extra_context)

        request = ExecutionRequest(
            prompt=prompt,
            workdir=project_path,
            role=role.value,
            timeout_s=self.config.provider_timeout_s(provider_name),
            run_id=run_id,
            log_dir=log_dir,
            on_spawn=self._on_spawn_handler(run_id),
        )

        commit_before = await git_ops.head_sha(project_path)
        self.db.insert(
            "provider_runs",
            {
                "id": run_id,
                "mission_id": self.mission_id,
                "provider": provider_name,
                "role": role.value,
                "command": [redact(provider_name)],
                "cwd": str(project_path),
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
            logger.exception("provider %s crashed for role %s", provider_name, role.value)
            result = ExecutionResult(
                state=ProviderState.CRASHED,
                failure_class=FailureClass.CRASH,
                exit_code=None,
                duration_s=0.0,
                summary="",
                raw_tail=str(exc),
            )

        commit_after = await git_ops.head_sha(project_path)
        self._record_run(run_id, provider_name, role, result, commit_before, commit_after)

        if result.ok:
            self.registry.record_success(provider_name, result.duration_s)
        else:
            self.registry.record_failure(provider_name, result.failure_class, result.duration_s, result.raw_tail[:300])

        return result

    async def _make_handoff(
        self,
        role: Role,
        from_provider: str | None,
        to_provider: str | None,
        next_action: str,
    ) -> str:
        mission = self._mission_model()
        findings = [f"[{f['severity']}] {f['description']}" for f in open_blockers(self.db, mission.id)]
        completed_rows = self.db.query(
            "SELECT summary FROM tasks WHERE mission_id=? AND status='COMPLETED' ORDER BY finished_at ASC",
            (self.mission_id,),
        )
        completed_work = [r["summary"][:200] for r in completed_rows if r.get("summary")]

        project_path = self._require_project_path()
        workspace = await inspect_workspace(project_path, self.config.allowed_roots())

        content = render_handoff(
            mission=mission,
            role=role.value,
            from_provider=from_provider,
            to_provider=to_provider,
            workspace_summary=workspace.summary() if workspace else "unknown",
            completed_work=completed_work[-15:],
            tests=[],
            review_findings=findings,
            next_action=next_action,
            git_head=mission.git_head,
        )
        handoff = persist_handoff(
            self.db, project_path, mission, role.value, content, from_provider, to_provider, mission.git_head
        )
        self.events.publish(EventType.HANDOFF_CREATED, self.mission_id, handoff_id=handoff.id, role=role.value)
        return content

    def _build_prompt(self, role: Role, handoff_content: str, extra_context: str) -> str:
        role_prompts: dict[Role, str] = {
            Role.REVIEW: REVIEW_INSTRUCTIONS,
            Role.REPAIR: "Fix the open review findings listed below. Verify your fixes by running relevant tests.",
        }
        sections = [
            f"You are working as the **{role.value}** engineer in a multi-provider orchestrated mission.",
            "",
            handoff_content,
            "",
            f"## Your instructions for this phase\n{role_prompts.get(role, 'Work autonomously.')}",
        ]
        if extra_context:
            sections.append(f"## Additional context\n{extra_context}")
        sections.append(
            "\n## Output contract\nWork autonomously in the current directory. "
            "Do not ask questions; make reasonable decisions and document them. "
            "When finished, end with a one-paragraph summary of what you did."
        )
        return "\n".join(sections)

    def _record_run(
        self,
        run_id: str,
        provider: str,
        role: Role,
        result: ExecutionResult,
        before: str | None,
        after: str | None,
    ) -> None:
        self.db.update(
            "provider_runs",
            run_id,
            {
                "command": [redact(a if len(a) < 300 else f"<prompt {len(a)} chars>") for a in result.argv],
                "started_at": result.started_at,
                "finished_at": result.finished_at,
                "exit_code": result.exit_code,
                "failure_class": result.failure_class.value,
                "provider_state": result.state.value,
                "stdout_path": str(result.stdout_path) if result.stdout_path else None,
                "stderr_path": str(result.stderr_path) if result.stderr_path else None,
                "git_commit_before": before,
                "git_commit_after": after,
                "summary": result.summary[:500],
                "pid": result.pid,
                "pgid": result.pgid,
            },
        )

    def _record_review_provenance(self, review_parsed: bool = True) -> None:
        implementer = self._last_provider_for(Role.IMPLEMENTATION)
        rows = self.db.query(
            "SELECT provider FROM provider_runs WHERE mission_id=? AND role='review' AND failure_class='NONE' "
            "ORDER BY started_at DESC LIMIT 1",
            (self.mission_id,),
        )
        if not rows:
            return
        reviewer = rows[0]["provider"]
        if implementer is None:
            independent = False
            reason = "implementation provider unknown (cannot prove independence)"
        elif reviewer == implementer:
            independent = False
            reason = "no alternative provider eligible; reviewer is the implementer (self-review)"
        else:
            independent = True
            reason = None
        self.db.insert(
            "reviews",
            {
                "id": f"rev-{utcnow().timestamp()}".replace(".", ""),
                "mission_id": self.mission_id,
                "implementation_provider": implementer,
                "review_provider": reviewer,
                "independent": int(independent),
                "degradation_reason": reason,
                "review_parsed": int(review_parsed),
                "created_at": utcnow().isoformat(),
            },
        )
        self.events.publish(
            EventType.REVIEW_RECORDED,
            self.mission_id,
            review_provider=reviewer,
            implementation_provider=implementer,
            independent=independent,
            degradation_reason=reason,
        )

    def _unparseable_review_count(self) -> int:
        rows = self.db.query(
            "SELECT COUNT(*) as cnt FROM reviews WHERE mission_id=? AND review_parsed=0",
            (self.mission_id,),
        )
        return rows[0]["cnt"] if rows else 0

    def _last_provider_for(self, role: Role) -> str | None:
        rows = self.db.query(
            """SELECT provider FROM provider_runs WHERE mission_id=? AND role=? AND failure_class='NONE'
               ORDER BY started_at DESC LIMIT 1""",
            (self.mission_id, role.value),
        )
        return rows[0]["provider"] if rows else None

    def _select_provider_for_role(self, role: Role) -> str | None:
        priorities: list[str] = self.config.priority_for(role.value)
        if not priorities:
            priorities = list(self.registry.adapters.keys())
        eligible = [p for p in priorities if self.registry.is_eligible(p)]

        if role == Role.REVIEW and len(eligible) > 1:
            implementer = self._last_provider_for(Role.IMPLEMENTATION)
            alternatives = [p for p in eligible if p != implementer]
            if alternatives:
                eligible = alternatives
        if role == Role.REPAIR and len(eligible) > 1:
            reviewer = self._last_provider_for(Role.REVIEW)
            alternatives = [p for p in eligible if p != reviewer]
            if alternatives:
                eligible = alternatives
        return eligible[0] if eligible else None

    # ------------------------------------------------------------------
    # Planning → DAG
    # ------------------------------------------------------------------

    async def _run_planning_phase(self) -> bool:
        """Run the planning provider and convert output to a persisted DAG.

        Failover mirrors the certified v1 phase loop: each execution counts as
        one attempt bounded by max_phase_attempts; a failed planner is recorded
        (failure classification + cooldown) and the next eligible planner is
        selected. Returns True once a valid DAG is persisted.
        """

        import time

        project_path = self._require_project_path()

        role = Role.PLANNING
        max_attempts = int(self.config.get("orchestration.max_phase_attempts", 4))
        max_wait_s = float(self.config.get("orchestration.max_provider_wait_seconds", 7200))
        prompt = self._build_planning_prompt()
        log_dir = project_path / ".orchestrator" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)

        attempt = 0
        wait_started: float | None = None
        result: ExecutionResult | None = None
        while attempt < max_attempts:
            if self._cancel.is_set():
                self._set_mission_status(MissionStatus.CANCELLED)
                return False
            if self._pause.is_set():
                self._set_mission_status(MissionStatus.PAUSED)
                return False
            provider_name = self._select_provider(role)
            if provider_name is None:
                if not self.registry.has_potentially_available():
                    self._set_mission_status(MissionStatus.FAILED, blocking_issue="no provider available for planning")
                    return False
                if wait_started is None:
                    wait_started = time.monotonic()
                    self._set_mission_status(
                        MissionStatus.WAITING_FOR_PROVIDER,
                        blocking_issue="all planning providers cooling down",
                    )
                elif time.monotonic() - wait_started > max_wait_s:
                    self._set_mission_status(
                        MissionStatus.FAILED,
                        blocking_issue=f"no eligible planning provider within {int(max_wait_s)}s",
                    )
                    return False
                await self._wait_tick()
                continue
            wait_started = None

            adapter = self.registry.get_adapter(provider_name)
            if not adapter:
                attempt += 1
                continue

            run_id = f"run-{utcnow().timestamp()}".replace(".", "")
            request = ExecutionRequest(
                prompt=prompt,
                workdir=project_path,
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
                    "cwd": str(project_path),
                    "started_at": utcnow().isoformat(),
                    "failure_class": "RUNNING",
                    "provider_state": "RUNNING",
                },
            )
            self.registry.mark_busy(provider_name)
            self.events.publish(EventType.PROVIDER_SELECTED, self.mission_id, provider=provider_name, role=role.value)

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

            if result.ok:
                self.registry.record_success(provider_name, result.duration_s)
                used = set(json.loads(self._mission().get("providers_used") or "[]"))
                if provider_name not in used:
                    self.db.update(
                        "missions",
                        self.mission_id,
                        {"providers_used": sorted(used | {provider_name}), "updated_at": utcnow()},
                    )
                break

            state = self.registry.record_failure(
                provider_name, result.failure_class, result.duration_s, result.raw_tail[:300]
            )
            failed = set(json.loads(self._mission().get("providers_failed") or "[]"))
            failed.add(provider_name)
            self.db.update("missions", self.mission_id, {"providers_failed": sorted(failed), "updated_at": utcnow()})
            self.events.publish(
                EventType.PROVIDER_RATE_LIMITED
                if result.failure_class in (FailureClass.RATE_LIMIT, FailureClass.QUOTA_EXHAUSTED)
                else EventType.PROVIDER_FAILED,
                self.mission_id,
                provider=provider_name,
                failure=result.failure_class.value,
                state=state.value,
                role=role.value,
            )
            attempt += 1

        if result is None or not result.ok:
            self._set_mission_status(
                MissionStatus.FAILED,
                blocking_issue=f"planning exhausted after {attempt} attempt(s)",
            )
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

        # Persist DAG (namespaced IDs: tasks.id is a global primary key)
        for task in tasks:
            task.mission_id = self.mission_id
        namespace_dag_ids(self.mission_id, tasks)
        for task in tasks:
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

    @staticmethod
    def _find_balanced_brace(text: str, start: int) -> int | None:
        """Find the index of the brace that balances the '{' at start."""
        depth = 0
        in_string = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_string:
                if escape:
                    escape = False
                    continue
                if ch == "\\":
                    escape = True
                    continue
                if ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return i
        return None

    @classmethod
    def _extract_dag_from_output(cls, text: str) -> dict[str, Any] | None:
        """Extract JSON DAG from planner output.

        Tries fenced code blocks first, then balanced brace scanning.
        Only returns a dict that contains a 'tasks' array.
        """
        import re

        candidates: list[str] = []

        # 1. Fenced code blocks (```json ... ```)
        for fence in re.finditer(r"```(?:json)?\s*([\s\S]*?)\s*```", text):
            candidates.append(fence.group(1).strip())

        # 2. Balanced brace objects — scan each '{' position
        for match in re.finditer(r"\{", text):
            start = match.start()
            end = cls._find_balanced_brace(text, start)
            if end is not None:
                candidates.append(text[start : end + 1])

        for payload in candidates:
            try:
                parsed = json.loads(payload)
                if isinstance(parsed, dict) and isinstance(parsed.get("tasks"), list):
                    return parsed
            except json.JSONDecodeError:
                continue
        return None

    # ------------------------------------------------------------------
    # Task launch
    # ------------------------------------------------------------------

    async def _launch_task(self, task: dict[str, Any]) -> bool:
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
        project_path = self._require_project_path()
        try:
            branch_record = await task_worktree.create_task_worktree(
                self.db, self.events, project_path, mission["id"], tid
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

        wt_path = branch_record.worktree_path
        if not wt_path:
            logger.error("worktree path missing for task %s", tid)
            release_provider_reservation(self.db, self.events, tid)
            task_locks.release_locks_for_task(self.db, self.events, tid)
            self.db.update("tasks", tid, {"status": TaskStatus.FAILED.value, "blocking_issue": "worktree path missing"})
            return False
        coro = self._task_runner(tid, provider_name, wt_path)
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
            on_spawn=self._on_spawn_handler(run_id),
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

            # Checkpoint before marking COMPLETED
            try:
                max_file_mb = int(self.config.get("git.max_auto_commit_file_mb", 5))
                checkpoint_sha = await git_ops.checkpoint(
                    Path(worktree_path),
                    f"orchestrator: checkpoint task {task_id}",
                    max_file_mb=max_file_mb,
                )
            except git_ops.GitCheckpointError as exc:
                self.db.update(
                    "tasks",
                    task_id,
                    {
                        "status": TaskStatus.FAILED.value,
                        "finished_at": utcnow().isoformat(),
                        "blocking_issue": f"checkpoint failed: {exc}",
                    },
                )
                self.events.publish(
                    EventType.TASK_FAILED,
                    mission_id=self.mission_id,
                    task_id=task_id,
                    provider=provider_name,
                    failure=FailureClass.CRASH.value,
                )
                return

            self.db.update(
                "tasks",
                task_id,
                {
                    "status": TaskStatus.COMPLETED.value,
                    "finished_at": utcnow().isoformat(),
                    "summary": result.summary,
                    "provider_run_id": run_id,
                    "checkpoint_after": checkpoint_sha,
                },
            )
            self.events.publish(
                EventType.TASK_COMPLETED,
                mission_id=self.mission_id,
                task_id=task_id,
                provider=provider_name,
            )
        else:
            self.registry.record_failure(provider_name, result.failure_class, result.duration_s, result.raw_tail[:300])
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

    def _build_task_prompt(self, task: dict[str, Any], provider_name: str) -> str:
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

    def _arbitrate_provider(self, task: dict[str, Any], role: Role, preferred: list[str]) -> str | None:
        """Select best provider for a task, respecting concurrency limits."""

        def _is_candidate(provider: str) -> bool:
            if self.registry.is_eligible(provider):
                return True
            row = self.db.get("providers", provider, key="name")
            if row and row.get("state") == ProviderState.BUSY.value and self._provider_has_capacity(provider):
                return True
            return False

        candidates: list[str] = []
        if preferred:
            candidates = [p for p in preferred if _is_candidate(p)]
        if not candidates:
            priorities = self.config.priority_for(role.value)
            candidates = [p for p in priorities if _is_candidate(p)]
        if not candidates:
            candidates = [p for p in self.registry.adapters if _is_candidate(p)]
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

    def _provider_has_capacity(self, provider: str) -> bool:
        limits: dict[str, int] = {
            "claude": 1,
            "codex": 1,
            "agy": 1,
            "opencode": 1,
        }
        overrides = self.config.raw.get("scheduler", {}).get("max_parallel_per_provider", {})
        if isinstance(overrides, dict):
            for k, v in overrides.items():
                if isinstance(v, int):
                    limits[k] = v
        max_for_provider = limits.get(provider, 1)
        active = active_reservations_for_provider(self.db, provider)
        return active < max_for_provider

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
