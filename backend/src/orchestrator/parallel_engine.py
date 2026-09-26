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
from .failover import failover_note_from_run
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
from .providers.base import ExecutionResult
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
        self._invocation_service: Any = None
        self._active_invocation_runs: dict[str, str] = {}

    def request_pause(self) -> None:
        self._pause.set()
        self._wake.set()
        # Ownership-first pause: interrupt owned provider processes via the
        # shared boundary; asyncio tasks complete naturally after exit so
        # capacity is released only after confirmed termination.
        try:
            svc = self._invocations()
            for run_id in list(self._active_invocation_runs.values()):
                try:
                    import asyncio as _asyncio

                    _asyncio.create_task(svc.cancel(run_id))
                except Exception:
                    logger.debug("pause interrupt failed for %s", run_id, exc_info=True)
        except Exception:
            logger.debug("pause interrupt fan-out failed", exc_info=True)

    def request_cancel(self) -> None:
        self._cancel.set()
        self._pause.clear()
        self._wake.set()
        try:
            svc = self._invocations()
            for run_id in list(self._active_invocation_runs.values()):
                try:
                    import asyncio as _asyncio

                    _asyncio.create_task(svc.cancel(run_id))
                except Exception:
                    logger.debug("cancel interrupt failed for %s", run_id, exc_info=True)
        except Exception:
            logger.debug("cancel interrupt fan-out failed", exc_info=True)

    async def cancel_task_execution(self, task_id: str) -> bool:
        """Interrupt one running task's owned provider process and wait for
        confirmed exit before the caller releases capacity."""
        run_id = self._active_invocation_runs.get(task_id)
        if not run_id:
            # No active invocation: check for a RUNNING run row as fallback.
            rows = self.db.query(
                "SELECT id FROM provider_runs WHERE task_id=? AND finished_at IS NULL ORDER BY started_at DESC LIMIT 1",
                (task_id,),
            )
            if not rows:
                return False
            run_id = rows[0]["id"]
        try:
            result = await self._invocations().cancel(run_id)
            return bool(result)
        except Exception:
            logger.debug("task cancel failed for %s", task_id, exc_info=True)
            return False

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
                if dep_task is None or dep_task.get("mission_id") != self.mission_id:
                    # Unknown/foreign dependency ID: fail closed before any
                    # provider execution (D-38). Planners and the manual DAG
                    # API validate up front; this guards corrupted state.
                    self.db.update(
                        "tasks",
                        tid,
                        {
                            "status": TaskStatus.FAILED.value,
                            "blocking_issue": f"unknown dependency {dep['from_task_id']}: no such task",
                        },
                    )
                    self.events.publish(
                        EventType.TASK_FAILED,
                        mission_id=self.mission_id,
                        task_id=tid,
                        reason="unknown_dependency",
                    )
                    break
                if dep_task and dep_task["status"] in (
                    TaskStatus.FAILED.value,
                    TaskStatus.CANCELLED.value,
                    TaskStatus.UNVERIFIED.value,
                ):
                    self.db.update(
                        "tasks",
                        tid,
                        {
                            "status": TaskStatus.FAILED.value,
                            "blocking_issue": (
                                f"permanently blocked: dependency {dep['from_task_id']} {dep_task['status'].lower()}"
                            ),
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
            "AND status NOT IN ('COMPLETED','FAILED','CANCELLED','UNVERIFIED','STALE')",
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

        # Stale descendants (upstream retried after they completed) block
        # completion until the operator resubmits them as new attempts.
        stale = self.db.query(
            "SELECT id, blocking_issue FROM tasks WHERE mission_id=? AND status='STALE'",
            (self.mission_id,),
        )
        if stale:
            self._set_mission_status(
                MissionStatus.FAILED,
                blocking_issue="stale descendant attempts: "
                + ", ".join(f"{r['id']} ({(r.get('blocking_issue') or '')[:160]})" for r in stale[:5]),
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
            merged_commit = result.get("merged_commit")

        # Final coverage (D-31/§78/§107): every required COMPLETED task result
        # must be represented through ancestry in the final candidate. DB
        # status alone never suffices. With nothing to integrate, the
        # candidate is the current HEAD itself.
        if not merged_commit:
            try:
                merged_commit = await git_ops.head_sha(project_path)
            except Exception:
                merged_commit = None
        missing = await self._final_coverage_missing(project_path, merged_commit)
        if missing:
            self._set_mission_status(
                MissionStatus.FAILED,
                blocking_issue="final candidate missing required task outputs: " + ", ".join(missing[:8]),
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
            environment_issue = bool(failures) and all(r.likely_environment_issue for r in failures)
            prefix = (
                "verification could not run due to an environment/tooling issue, not a code defect"
                if environment_issue
                else "verification failed"
            )
            self._set_mission_status(
                MissionStatus.UNVERIFIED,
                blocking_issue=f"{prefix}:\n{detail[:800]}",
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
        """List prior findings with stable IDs so the reviewer can re-flag or verify each one.

        DOG-02: includes inherited unresolved findings from the retry lineage.
        """
        rows = self.db.query(
            "SELECT id, severity, status, file, description, fingerprint FROM review_findings "
            "WHERE mission_id=? AND status IN ('open','repair_attempted') ORDER BY created_at ASC",
            (self.mission_id,),
        )
        try:
            from .review import inherited_open_findings as _inh3p

            _inh_rows = _inh3p(self.db, self.mission_id)
        except Exception:
            _inh_rows = []
        if not rows and not _inh_rows:
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
        for r in _inh_rows:
            lines.append(
                f"- id={r['id']} fp={r.get('fingerprint') or '-'} [{r['severity']}/{r['status']}] "
                f"{r['file'] or ''}: {r['description'][:300]}"
                f" (inherited from {r.get('inherited_from_mission_id') or 'prior mission'})"
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
            review_row = await self._record_review_provenance(review_parsed=parsed_ok)
            if review_row:
                from .provenance import attach_finding_lineage

                attach_finding_lineage(
                    self.db,
                    self.mission_id,
                    str(review_row.get("id")),
                    review_row.get("reviewed_head_sha"),
                    [f.id for f in findings],
                )
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

    def _invocations(self) -> Any:
        if not hasattr(self, "_invocation_service") or self._invocation_service is None:
            from .invocations import InvocationService

            self._invocation_service = InvocationService(self.db, self.registry, self.config)
        return self._invocation_service

    def _product_attribution(self) -> tuple[Any, Any]:
        try:
            rows = self.db.query(
                "SELECT project_id, id FROM project_phases WHERE mission_id=? LIMIT 1", (self.mission_id,)
            )
            if rows:
                return rows[0].get("project_id"), rows[0].get("id")
        except Exception:
            logger.debug("product attribution failed for %s", self.mission_id, exc_info=True)
        return None, None

    async def _run_provider_phase_for_role(self, role: Role, extra_context: str = "") -> ExecutionResult | None:
        provider_name = self._select_provider_for_role(role)
        if not provider_name:
            return None

        adapter = self.registry.get_adapter(provider_name)
        if not adapter:
            return None

        project_path = self._require_project_path()
        log_dir = project_path / ".orchestrator" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)

        handoff_content = await self._make_handoff(role, None, provider_name, f"Run {role.value} phase")
        prompt = self._build_prompt(role, handoff_content, extra_context)
        commit_before = await git_ops.head_sha(project_path)

        # Exact-SHA write provenance: repair runs must start clean, or prior
        # dirt would be silently attributed to the repairer. Skipped while
        # checkpointing itself is broken (the exhaustion path owns that).
        from .provenance import CODE_WRITING_ROLES, capture_write_start

        if role.value in CODE_WRITING_ROLES:
            _, _dirty, _paths = await capture_write_start(project_path)
            _ckpt_failures = int(self._mission().get("checkpoint_failures") or 0)
            if _dirty and _ckpt_failures == 0:
                self._set_mission_status(
                    MissionStatus.FAILED,
                    blocking_issue=(
                        f"workspace has unattributed changes before {role.value} run "
                        f"({', '.join(_paths[:5])}); commit, discard, or adopt them as human "
                        "operator, then retry. Provider was NOT invoked."
                    ),
                )
                return None

        import uuid as _uuid

        from .invocations import InvocationOwner, InvocationSpec

        _product_id, _phase_id = self._product_attribution()
        _stage = role.value if role.value in ("review", "repair") else role.value
        _ctx_meta: dict[str, Any] = {}
        _spec_cc: Any = None
        from .context_compiler import ContextCompileError as _CCError2
        from .context_compiler import compile_failure_reason as _cc_reason2

        try:
            from .context_compiler import (
                ContextCompileSpec,
                finding_ids_in_text,
                latest_candidate_shas,
                prepare_invocation_context,
                resolve_phase_requirements,
                role_for_stage,
            )

            _mission = self._mission()
            _p_base: str | None = None
            _p_cand: str | None = None
            _p_findings: list[str] = []
            _p_files: list[str] = []
            _p_summary: str = ""
            if _stage in ("review", "repair"):
                _p_base, _p_cand = latest_candidate_shas(self.db, self.mission_id)
                if _p_base and _p_cand:
                    try:
                        _p_files = await git_ops.diff_names(project_path, _p_base, _p_cand, limit=100)
                        _p_summary = await git_ops.diff_stat_range(project_path, _p_base, _p_cand)
                    except Exception:
                        logger.debug("review git context failed for %s", self.mission_id, exc_info=True)
                        _p_files, _p_summary = [], ""
                if _stage == "repair":
                    _p_findings = finding_ids_in_text(extra_context)
            _spec_cc = ContextCompileSpec(
                role=role_for_stage(_stage, role.value),
                stage=_stage,
                product_project_id=_product_id,
                project_phase_id=_phase_id,
                mission_id=self.mission_id,
                provider=provider_name,
                base_sha=_p_base,
                candidate_sha=_p_cand,
                git_files_changed=_p_files,
                git_diff_summary=_p_summary,
                finding_ids=_p_findings,
                task_objective=str(_mission.get("task", "")),
                task_title=str(_mission.get("title", "")),
                task_description=str(_mission.get("task", "")),
                requirement_ids=resolve_phase_requirements(self.db, _product_id, _phase_id),
                failure_text=extra_context[:4000] if extra_context else "",
            )
            prompt, _ctx_meta = prepare_invocation_context(
                legacy_prompt=prompt, spec=_spec_cc, db=self.db, config=self.config
            )
        except Exception as exc:
            # Fail closed: no provider execution, no lease, no health change,
            # no provider_run. The mission records why this phase stopped.
            _code = exc.code if isinstance(exc, _CCError2) else "CONTEXT_COMPILATION_INTERNAL"
            logger.warning("context compilation failed (%s); mission %s blocked", _code, self.mission_id)
            self._active_invocation_runs.pop(f"__role_{role.value}", None)
            self._set_mission_status(MissionStatus.FAILED, blocking_issue=_cc_reason2(exc, _spec_cc))
            return None
        _pre_run_id = f"run-{_uuid.uuid4().hex[:12]}"
        _track_key = f"__role_{role.value}"
        self._active_invocation_runs[_track_key] = _pre_run_id
        try:
            outcome = await self._invocations().execute(
                InvocationSpec(
                    owner=InvocationOwner(
                        mission_id=self.mission_id,
                        product_project_id=_product_id,
                        phase_id=_phase_id,
                    ),
                    stage=_stage,
                    role=role.value,
                    prompt=prompt,
                    workdir=project_path,
                    log_dir=log_dir,
                    provider=provider_name,
                    timeout_s=self.config.provider_timeout_s(provider_name),
                    model_requested=getattr(adapter, "model", None) if provider_name == "opencode" else None,
                    run_id=_pre_run_id,
                    cancel_event=self._cancel,
                    prompt_template_version=_ctx_meta.get("prompt_template_version"),
                    context_policy_version=_ctx_meta.get("context_policy_version"),
                    context_blocks_json=_ctx_meta.get("context_blocks_json"),
                    context_warnings_json=_ctx_meta.get("context_warnings_json"),
                    context_budget=_ctx_meta.get("context_budget"),
                    context_used=_ctx_meta.get("context_used"),
                    context_remaining=_ctx_meta.get("context_remaining"),
                    context_repeated_ratio=_ctx_meta.get("context_repeated_ratio"),
                    context_plan_revision=_ctx_meta.get("context_plan_revision"),
                )
            )
        except RuntimeError as exc:
            self._active_invocation_runs.pop(_track_key, None)
            logger.warning("invocation capacity refused: %s", exc)
            return None
        run_id = outcome.run_id
        self._active_invocation_runs.pop(_track_key, None)
        try:
            commit_after = await git_ops.head_sha(project_path)
        except Exception:
            commit_after = None
            logger.debug("git head failed for %s", project_path, exc_info=True)
        try:
            self.db.execute(
                "UPDATE provider_runs SET git_commit_before=?, git_commit_after=? WHERE id=?",
                (commit_before, commit_after, run_id),
            )
        except Exception:
            logger.debug("git linkage failed for run %s", run_id, exc_info=True)
        # Health already accounted exactly once by InvocationService.
        result = ExecutionResult(
            state=ProviderState(outcome.provider_state)
            if outcome.provider_state in ProviderState.__members__.values()
            else (ProviderState.COMPLETED if outcome.ok else ProviderState.CRASHED),
            failure_class=FailureClass(outcome.failure_class)
            if outcome.failure_class in FailureClass.__members__.values()
            else FailureClass.CRASH,
            exit_code=outcome.exit_code,
            duration_s=outcome.duration_s,
            summary=outcome.summary,
            argv=[],
            stdout_path=Path(outcome.stdout_path) if outcome.stdout_path else None,
            stderr_path=Path(outcome.stderr_path) if outcome.stderr_path else None,
            raw_tail=outcome.raw_tail,
            assistant_text=outcome.assistant_text,
            pid=outcome.pid,
            pgid=outcome.pgid,
            gate_refused=outcome.gate_refused,
        )
        if role.value in CODE_WRITING_ROLES and result.ok:
            # Checkpoint repair work immediately (mirroring sequential phase
            # semantics) so the repair run binds to an exact result SHA
            # instead of dissolving into the final checkpoint. Skipped when
            # clean so checkpoint-failure accounting stays with real ones.
            try:
                _repair_st = await git_ops.status(project_path)
                if not _repair_st.is_clean:
                    max_mb = int(self.config.get("git.max_auto_commit_file_mb", 5))
                    _repair_sha = await git_ops.checkpoint(
                        project_path,
                        f"agent({provider_name}): {role.value} checkpoint",
                        max_file_mb=max_mb,
                    )
                    if _repair_sha:
                        _mission_row = self._mission()
                        self.db.insert(
                            "checkpoints",
                            {
                                "id": f"ckpt-{utcnow().timestamp()}".replace(".", ""),
                                "mission_id": self.mission_id,
                                "project_id": _mission_row.get("project_id"),
                                "commit_sha": _repair_sha,
                                "message": f"agent({provider_name}): {role.value} checkpoint",
                                "created_at": utcnow().isoformat(),
                            },
                        )
            except git_ops.GitCheckpointError:
                _failures = int(self._mission().get("checkpoint_failures") or 0) + 1
                self.db.update("missions", self.mission_id, {"checkpoint_failures": _failures})
                logger.debug("repair checkpoint failed (count=%d)", _failures, exc_info=True)
            except git_ops.GitError:
                logger.debug("repair checkpoint deferred to final checkpoint", exc_info=True)
            from .provenance import record_provider_write as _record_repair_write

            await _record_repair_write(
                self.db,
                workdir=project_path,
                run_id=run_id,
                mission_id=self.mission_id,
                product_project_id=_product_id,
                phase_id=_phase_id,
                provider=provider_name,
                role=role.value,
                base_sha=commit_before,
            )
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
        from .handoff import truncate_coherent as _coherent2

        completed_work = [_coherent2(str(r.get("summary") or ""), 2000) for r in completed_rows if r.get("summary")]

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

    async def _record_review_provenance(self, review_parsed: bool = True) -> dict[str, Any] | None:
        """Immutable review attempt bound to its exact reviewed SHA range (Increment 3)."""
        from .provenance import record_review_attempt

        try:
            return await record_review_attempt(
                self.db,
                self.events,
                mission_id=self.mission_id,
                repo=self._require_project_path(),
                review_parsed=review_parsed,
            )
        except Exception:
            logger.debug("review attempt recording failed", exc_info=True)
            return None

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
            from .provenance import mission_provider_writers

            _writers, _ = mission_provider_writers(self.db, self.mission_id)
            if not _writers:
                _last_impl = self._last_provider_for(Role.IMPLEMENTATION)
                _writers = {_last_impl} if _last_impl is not None else set()
            alternatives = [p for p in eligible if p not in _writers]
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
        _plan_prev_run_id: str | None = None
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

            self.events.publish(EventType.PROVIDER_SELECTED, self.mission_id, provider=provider_name, role=role.value)
            import uuid as _uuid2

            from .invocations import STAGE_DAG_PLANNING, InvocationOwner, InvocationSpec

            _product_id, _phase_id = self._product_attribution()
            _plan_key = "__dag_planning"
            _plan_run_id = f"run-{_uuid2.uuid4().hex[:12]}"
            self._active_invocation_runs[_plan_key] = _plan_run_id
            _dag_ctx: dict[str, Any] = {}
            _dag_spec: Any = None
            from .context_compiler import ContextCompileError as _CCError3
            from .context_compiler import compile_failure_reason as _cc_reason3

            try:
                from .context_compiler import ContextCompileSpec, prepare_invocation_context

                _mission_dag = self._mission()
                _dag_spec = ContextCompileSpec(
                    role="planner",
                    stage="dag_plan",
                    product_project_id=_product_id,
                    project_phase_id=_phase_id,
                    mission_id=self.mission_id,
                    provider=provider_name,
                    task_objective=str(_mission_dag.get("task", "")),
                    task_title=str(_mission_dag.get("title", "")),
                    task_description=str(_mission_dag.get("task", "")),
                    extra_context=prompt,
                )
                _compiled_prompt, _dag_ctx = prepare_invocation_context(
                    legacy_prompt=prompt, spec=_dag_spec, db=self.db, config=self.config
                )
            except Exception as exc:
                # Fail closed: planner provider never invoked; mission fails
                # honestly instead of planning from a degraded prompt.
                _code = exc.code if isinstance(exc, _CCError3) else "CONTEXT_COMPILATION_INTERNAL"
                logger.warning("dag planning compilation failed (%s); mission %s blocked", _code, self.mission_id)
                self._active_invocation_runs.pop(_plan_key, None)
                self._set_mission_status(MissionStatus.FAILED, blocking_issue=_cc_reason3(exc, _dag_spec))
                return False
            try:
                outcome = await self._invocations().execute(
                    InvocationSpec(
                        owner=InvocationOwner(
                            mission_id=self.mission_id,
                            product_project_id=_product_id,
                            phase_id=_phase_id,
                        ),
                        stage=STAGE_DAG_PLANNING,
                        role=role.value,
                        prompt=_compiled_prompt,
                        workdir=project_path,
                        log_dir=log_dir,
                        provider=provider_name,
                        timeout_s=self.config.provider_timeout_s(provider_name),
                        model_requested=getattr(adapter, "model", None) if provider_name == "opencode" else None,
                        run_id=_plan_run_id,
                        attempt_number=attempt + 1,
                        retry_of_run_id=_plan_prev_run_id,
                        cancel_event=self._cancel,
                        prompt_template_version=_dag_ctx.get("prompt_template_version"),
                        context_policy_version=_dag_ctx.get("context_policy_version"),
                        context_blocks_json=_dag_ctx.get("context_blocks_json"),
                        context_warnings_json=_dag_ctx.get("context_warnings_json"),
                        context_budget=_dag_ctx.get("context_budget"),
                        context_used=_dag_ctx.get("context_used"),
                        context_remaining=_dag_ctx.get("context_remaining"),
                        context_repeated_ratio=_dag_ctx.get("context_repeated_ratio"),
                        context_plan_revision=_dag_ctx.get("context_plan_revision"),
                    )
                )
            except RuntimeError as exc:
                self._active_invocation_runs.pop(_plan_key, None)
                logger.warning("invocation capacity refused: %s", exc)
                await self._wait_tick()
                continue
            self._active_invocation_runs.pop(_plan_key, None)
            _plan_prev_run_id = outcome.run_id
            result = ExecutionResult(
                state=ProviderState(outcome.provider_state)
                if outcome.provider_state in ProviderState.__members__.values()
                else (ProviderState.COMPLETED if outcome.ok else ProviderState.CRASHED),
                failure_class=FailureClass(outcome.failure_class)
                if outcome.failure_class in FailureClass.__members__.values()
                else FailureClass.CRASH,
                exit_code=outcome.exit_code,
                duration_s=outcome.duration_s,
                summary=outcome.summary,
                argv=[],
                stdout_path=Path(outcome.stdout_path) if outcome.stdout_path else None,
                stderr_path=Path(outcome.stderr_path) if outcome.stderr_path else None,
                raw_tail=outcome.raw_tail,
                assistant_text=outcome.assistant_text,
                pid=outcome.pid,
                pgid=outcome.pgid,
                gate_refused=outcome.gate_refused,
            )

            if result.ok:
                # Health already accounted exactly once by InvocationService.
                used = set(json.loads(self._mission().get("providers_used") or "[]"))
                if provider_name not in used:
                    self.db.update(
                        "missions",
                        self.mission_id,
                        {"providers_used": sorted(used | {provider_name}), "updated_at": utcnow()},
                    )
                break

            if result.gate_refused:
                state = ProviderState.AVAILABLE
            else:
                from .providers.classify import FAILURE_TO_STATE as _FTS2

                state = _FTS2.get(result.failure_class, ProviderState.CRASHED)
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
        # Checkpoint planner-side files (if any) so later task checkpoints
        # cannot silently absorb unattributed planner output. Skipped when
        # clean so checkpoint-failure accounting stays with real checkpoints.
        try:
            _plan_st = await git_ops.status(project_path)
            if not _plan_st.is_clean:
                max_mb = int(self.config.get("git.max_auto_commit_file_mb", 5))
                await git_ops.checkpoint(
                    project_path, f"agent({provider_name}): planning checkpoint", max_file_mb=max_mb
                )
        except git_ops.GitCheckpointError:
            # Bookkeeping failure feeds the shared exhaustion counter so
            # dirty-gates and final accounting see one world state.
            _failures = int(self._mission().get("checkpoint_failures") or 0) + 1
            self.db.update("missions", self.mission_id, {"checkpoint_failures": _failures})
            logger.debug("planning checkpoint failed (count=%d)", _failures, exc_info=True)
        except git_ops.GitError:
            logger.debug("planning checkpoint skipped", exc_info=True)
        try:
            from .provenance import record_provider_write as _record_plan_write

            _plan_run_rows = self.db.query(
                "SELECT id FROM provider_runs WHERE mission_id=? AND stage='dag_plan'"
                " AND failure_class='NONE' ORDER BY started_at DESC LIMIT 1",
                (self.mission_id,),
            )
            if _plan_run_rows:
                await _record_plan_write(
                    self.db,
                    workdir=project_path,
                    run_id=_plan_run_rows[0]["id"],
                    mission_id=self.mission_id,
                    product_project_id=_product_id,
                    phase_id=_phase_id,
                    provider=provider_name,
                    role="planning",
                )
        except Exception:
            logger.debug("planning provenance insert failed", exc_info=True)
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
            for dep in dict.fromkeys(task.dependencies):
                self.db.insert(
                    "task_dependencies",
                    {
                        "from_task_id": dep,
                        "to_task_id": task.id,
                        "created_at": utcnow().isoformat(),
                    },
                )
        # Pin the mission DAG base: root tasks execute against this exact
        # artifact, never moving integration HEAD (D-02/D-16).
        try:
            _dag_head = await git_ops.head_sha(project_path)
        except Exception:
            _dag_head = None
            logger.debug("dag base capture failed", exc_info=True)
        if _dag_head:
            try:
                self.db.update("missions", self.mission_id, {"dag_base_sha": _dag_head})
            except Exception:
                logger.debug("dag base persist failed", exc_info=True)

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

        mission = self._mission()
        project_path = self._require_project_path()

        # Dependency artifact preparation FIRST (Increment 3B): local Git
        # work only — before provider selection, reservation, lease, or BUSY.
        # Any failure blocks the task with zero provider side effects (D-09).
        # Prepared rows are content-matched and reused across scheduler ticks
        # and backend restarts (D-24/D-40/D-56).
        try:
            from .dep_inputs import DependencyInputError, prepare_task_input

            dag_base = mission.get("dag_base_sha") or await self._ensure_dag_base(project_path)
            prepared = await prepare_task_input(
                self.db,
                project_path,
                self.mission_id,
                tid,
                dag_base,
                attempt_number=int(task.get("attempts") or 0),
            )
        except DependencyInputError as exc:
            if exc.code == "CONFLICT":
                self.db.update(
                    "tasks",
                    tid,
                    {
                        "status": TaskStatus.FAILED.value,
                        "blocking_issue": f"dependency integration conflict: {exc}"[:1000],
                        "finished_at": utcnow().isoformat(),
                    },
                )
                self.events.publish(
                    EventType.MERGE_CONFLICT,
                    mission_id=self.mission_id,
                    task_id=tid,
                    reason="dependency_input_conflict",
                    detail=str(exc)[:500],
                )
            else:
                self.db.update(
                    "tasks",
                    tid,
                    {
                        "status": TaskStatus.FAILED.value,
                        "blocking_issue": f"dependency artifact unavailable ({exc.code}): {exc}"[:1000],
                        "finished_at": utcnow().isoformat(),
                    },
                )
            return False
        except Exception as exc:
            logger.debug("dependency preparation failed for %s", tid, exc_info=True)
            self.db.update(
                "tasks",
                tid,
                {
                    "status": TaskStatus.FAILED.value,
                    "blocking_issue": f"dependency preparation failed: {exc}"[:1000],
                    "finished_at": utcnow().isoformat(),
                },
            )
            return False
        self.db.update("tasks", tid, {"input_sha": prepared.input_sha})

        # Select provider (read-only arbitration; no capacity consumed yet).
        role = Role(task.get("role", "implementation"))
        preferred_raw = task.get("preferred_providers") or "[]"
        if isinstance(preferred_raw, str):
            preferred = json.loads(preferred_raw)
        else:
            preferred = list(preferred_raw)

        provider_name = self._arbitrate_provider(task, role, preferred)
        if not provider_name:
            # No provider available — mark waiting (input artifact stays
            # prepared and reusable for a later tick/restart).
            if task["status"] != TaskStatus.WAITING_FOR_PROVIDER.value:
                self.db.update("tasks", tid, {"status": TaskStatus.WAITING_FOR_PROVIDER.value})
            return False

        # Atomic reservation (only now that artifact preconditions hold).
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

        # Create worktree from the pinned input SHA (never moving HEAD).
        # Stale worktree (previous attempt advanced the branch, or a new
        # input was computed): detach the checkout but KEEP the old branch
        # ref so prior attempt commits stay reachable for provenance, then
        # create a fresh worktree+branch from the pinned input.
        try:
            branch_record = await task_worktree.create_task_worktree(
                self.db, self.events, project_path, mission["id"], tid, base_commit=prepared.input_sha
            )
            _fresh_head: str | None = None
            try:
                _fresh_head = (
                    await git_ops.head_sha(Path(branch_record.worktree_path)) if branch_record.worktree_path else None
                )
            except Exception:
                _fresh_head = None
            if branch_record.base_commit != prepared.input_sha or _fresh_head != prepared.input_sha:
                await task_worktree.remove_task_worktree(self.db, self.events, project_path, tid, keep_branch=True)
                branch_record = await task_worktree.create_task_worktree(
                    self.db, self.events, project_path, mission["id"], tid, base_commit=prepared.input_sha
                )
        except git_ops.GitError as exc:
            logger.warning("worktree creation failed for task %s: %s", tid, exc)
            release_provider_reservation(self.db, self.events, tid)
            task_locks.release_locks_for_task(self.db, self.events, tid)
            self.db.update("tasks", tid, {"status": TaskStatus.FAILED.value, "blocking_issue": f"worktree: {exc}"})
            return False

        # Verify worktree matches the planned input before anyone runs.
        wt_path: str | None = branch_record.worktree_path
        if not wt_path:
            release_provider_reservation(self.db, self.events, tid)
            task_locks.release_locks_for_task(self.db, self.events, tid)
            self.db.update(
                "tasks",
                tid,
                {"status": TaskStatus.FAILED.value, "blocking_issue": "worktree path missing"},
            )
            return False
        try:
            wt_head = await git_ops.head_sha(Path(wt_path))
            wt_status = await git_ops.status(Path(wt_path))
            wt_op = await git_ops.operation_in_progress(Path(wt_path))
        except Exception as exc:
            logger.warning("worktree verification failed for task %s: %s", tid, exc)
            wt_head, wt_status, wt_op = None, None, f"verify failed: {exc}"
        _wt_problems: list[str] = []
        if wt_head != prepared.input_sha:
            _wt_problems.append(f"HEAD {wt_head} != pinned input {prepared.input_sha[:8]}")
        if wt_op:
            _wt_problems.append(f"unfinished git operation: {wt_op}")
        if wt_status is not None and not wt_status.is_clean:
            _wt_problems.append("unattributed changes present in fresh worktree")
        if _wt_problems:
            release_provider_reservation(self.db, self.events, tid)
            task_locks.release_locks_for_task(self.db, self.events, tid)
            self.db.update(
                "tasks",
                tid,
                {
                    "status": TaskStatus.FAILED.value,
                    "blocking_issue": (
                        f"worktree failed pre-launch verification ({'; '.join(_wt_problems)})."
                        " Provider was NOT invoked."
                    )[:1000],
                },
            )
            return False

        # Mark RUNNING and start (wt_path already verified above).
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

        coro = self._task_runner(tid, provider_name, wt_path)
        self._running_tasks[tid] = asyncio.create_task(coro)
        return True

    async def _ensure_dag_base(self, project_path: Path) -> str | None:
        """Mission DAG base SHA, persisting a lazy fallback when planning did not set one.

        Root tasks must share one immutable base even if siblings finish
        first (main HEAD only moves via planning/repair/integration/final
        checkpoints, never via task completion, so a lazily sampled HEAD is
        stable across the scheduling window — and planning always sets it).
        """
        mission = self._mission()
        base = mission.get("dag_base_sha")
        if base:
            return str(base)
        try:
            head = await git_ops.head_sha(project_path)
        except Exception:
            return None
        if head:
            try:
                self.db.update("missions", self.mission_id, {"dag_base_sha": head})
            except Exception:
                logger.debug("dag base persist failed", exc_info=True)
            return head
        return None

    async def _final_coverage_missing(self, project_path: Path, final_sha: str | None) -> list[str]:
        """Task results absent from the final candidate through Git ancestry.

        Returns ['task-id@sha...'] for required COMPLETED tasks whose
        effective result is neither the final SHA nor its ancestor.
        """
        if not final_sha:
            return ["final integration produced no candidate SHA"]
        try:
            if not await git_ops.commit_exists(project_path, final_sha):
                return [f"final candidate {(final_sha or '')[:8]} does not exist"]
        except Exception:
            return ["final candidate unverifiable"]
        missing: list[str] = []
        try:
            completed = self.db.query(
                "SELECT id, result_sha, checkpoint_after, input_sha, provider_run_id FROM tasks"
                " WHERE mission_id=? AND status='COMPLETED'",
                (self.mission_id,),
            )
        except Exception:
            return ["task results unreadable"]
        for t in completed:
            tid = str(t.get("id"))
            if not t.get("input_sha") and not t.get("provider_run_id"):
                try:
                    _linked = self.db.query("SELECT id FROM write_provenance WHERE task_id=? LIMIT 1", (tid,))
                except Exception:
                    _linked = []
                if not _linked:
                    # Pre-artifact legacy row (no input, no run, no write
                    # linkage): cannot prove inclusion, but must not rewrite
                    # history — readability preserved, enforcement applies to
                    # tracked attempts going forward.
                    continue
            result = t.get("result_sha") or t.get("checkpoint_after")
            if not result:
                missing.append(f"{tid}@missing-result")
                continue
            try:
                covered = result == final_sha or await git_ops.is_ancestor(project_path, str(result), final_sha)
            except Exception:
                covered = False
            if not covered:
                missing.append(f"{tid}@{str(result)[:8]}")
        return missing

    async def _mark_descendants_stale(self, task_id: str, new_result_sha: str) -> None:
        """Mark completed direct dependents STALE when an upstream retry produced new output (D-18/§59).

        Their recorded history (consumed old result) stays immutable; they
        simply cannot remain valid outputs of the new DAG state. Operator
        resubmits them (task retry) into new attempts against current results.
        """
        try:
            dependents = self.db.query("SELECT to_task_id FROM task_dependencies WHERE from_task_id=?", (task_id,))
        except Exception:
            return
        for dep in dependents:
            tid = str(dep.get("to_task_id") or "")
            row = self.db.get("tasks", tid)
            if not row or row.get("mission_id") != self.mission_id:
                continue
            if row.get("status") != TaskStatus.COMPLETED.value:
                continue
            dep_input = row.get("input_sha")
            try:
                contains = bool(dep_input) and (
                    dep_input == new_result_sha
                    or await git_ops.is_ancestor(self._require_project_path(), new_result_sha, str(dep_input))
                )
            except Exception:
                contains = False
            if contains:
                continue
            self.db.update(
                "tasks",
                tid,
                {
                    "status": TaskStatus.STALE.value,
                    "blocking_issue": f"upstream {task_id} produced a newer result {(new_result_sha or '')[:8]}"
                    " not contained in this attempt's input; resubmit for a new attempt"[:500],
                    "finished_at": utcnow().isoformat(),
                },
            )
            self.events.publish(
                EventType.TASK_FAILED,
                mission_id=self.mission_id,
                task_id=tid,
                reason="upstream_retry_stale",
            )

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

        import uuid as _uuid

        log_dir = Path(worktree_path) / ".orchestrator" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)

        prompt = self._build_task_prompt(task, provider_name)
        _wt = Path(worktree_path)
        try:
            commit_before = await git_ops.head_sha(_wt)
        except Exception:
            commit_before = None
            logger.debug("git head failed for task %s", task_id, exc_info=True)

        # Input/filesystem agreement immediately before provider invocation
        # (D-10/D-11/D-25/D-67): the worktree must still be exactly the
        # pinned input artifact, clean, with no unfinished Git operation.
        # Unattributed dirt fails closed exactly like at launch.
        _planned_input = task.get("input_sha")
        _agree_problem: str | None = None
        try:
            _agree_head = await git_ops.head_sha(_wt)
            _agree_status = await git_ops.status(_wt)
            _agree_op = await git_ops.operation_in_progress(_wt)
            if _planned_input and _agree_head != _planned_input:
                _agree_problem = f"worktree HEAD {_agree_head} != pinned input {_planned_input[:8]}"
            elif not _agree_status.is_clean or _agree_op:
                _agree_problem = f"worktree not clean for launch (op={_agree_op})"
        except Exception as exc:
            _agree_problem = f"worktree verification failed: {exc}"
            logger.debug("pre-launch worktree verification failed for %s", task_id, exc_info=True)
        if _agree_problem is None:
            from .provenance import capture_write_start as _capture_task_start

            _, _task_dirty, _task_paths = await _capture_task_start(_wt)
            _task_ckpt_failures = int((self.db.get("missions", self.mission_id) or {}).get("checkpoint_failures") or 0)
            if _task_dirty and _task_ckpt_failures == 0:
                _agree_problem = (
                    f"task worktree has unattributed changes before provider run "
                    f"({', '.join(_task_paths[:5])}); clean or adopt them, then retry."
                )
        if _agree_problem is not None:
            release_provider_reservation(self.db, self.events, task_id)
            task_locks.release_locks_for_task(self.db, self.events, task_id)
            self.db.update(
                "tasks",
                task_id,
                {
                    "status": TaskStatus.FAILED.value,
                    "blocking_issue": f"{_agree_problem} Provider was NOT invoked."[:1000],
                    "finished_at": utcnow().isoformat(),
                },
            )
            self.events.publish(
                EventType.TASK_FAILED,
                mission_id=self.mission_id,
                task_id=task_id,
                reason="worktree_input_mismatch",
            )
            return

        # Retry lineage: link to this task's latest prior run when relaunching.
        _task_prev_runs = self.db.query(
            "SELECT id FROM provider_runs WHERE task_id=? ORDER BY started_at DESC LIMIT 1", (task_id,)
        )
        _task_retry_of = _task_prev_runs[0]["id"] if _task_prev_runs else None

        from .invocations import STAGE_TASK, InvocationOwner, InvocationSpec

        _product_id, _phase_id = self._product_attribution()
        _task_ctx: dict[str, Any] = {}
        _task_ctx_spec: Any = None
        from .context_compiler import ContextCompileError as _CCError4
        from .context_compiler import compile_failure_reason as _cc_reason4

        try:
            import json as _json2

            from .context_compiler import (
                ContextCompileSpec,
                prepare_invocation_context,
                resolve_phase_requirements,
                role_for_stage,
            )

            try:
                _scope = _json2.loads(task.get("workspace_scope") or "[]")
            except Exception:
                _scope = []
            from .dep_inputs import dependency_ids as _dep_ids_for_spec
            from .dep_inputs import verify_input_covers as _verify_input_covers

            _dep_list = _dep_ids_for_spec(self.db, task_id)
            _verified_map: dict[str, bool] = {}
            if _planned_input:
                try:
                    _verified_map = await _verify_input_covers(self.db, _wt, _planned_input, _dep_list)
                except Exception:
                    logger.debug("dependency presence proof failed for %s", task_id, exc_info=True)
            # A retried task carries its previous failed run's partial report
            # (never for review tasks: reviewer independence).
            _task_failover = ""
            if int(task.get("attempts") or 0) > 0 and str(task.get("role") or "") != Role.REVIEW.value:
                # Newest run for the task: crash recovery never rewrites
                # tasks.provider_run_id, so that column can point at an older run.
                _last_runs = self.db.query(
                    "SELECT * FROM provider_runs WHERE task_id=? ORDER BY started_at DESC, rowid DESC LIMIT 1",
                    (task_id,),
                )
                _task_failover = failover_note_from_run(_last_runs[0] if _last_runs else None)
            _task_ctx_spec = ContextCompileSpec(
                role=role_for_stage(STAGE_TASK, str(task.get("role", "implementation"))),
                stage=STAGE_TASK,
                product_project_id=_product_id,
                project_phase_id=_phase_id,
                mission_id=self.mission_id,
                task_id=task_id,
                provider=provider_name,
                base_sha=_planned_input or task.get("checkpoint_before") or None,
                task_objective=str(task.get("description", "")),
                task_title=str(task.get("title", "")),
                task_description=str(task.get("description", "")),
                requirement_ids=resolve_phase_requirements(self.db, _product_id, _phase_id),
                dependency_ids=_dep_list,
                dependency_verified=_verified_map,
                workspace_scope=list(_scope) if isinstance(_scope, list) else [],
                failover_text=_task_failover,
            )
            prompt, _task_ctx = prepare_invocation_context(
                legacy_prompt=prompt, spec=_task_ctx_spec, db=self.db, config=self.config
            )
        except Exception as exc:
            # Fail closed: task never enters provider execution. Dependents
            # react through the existing failed-dependency rules; no
            # provider_run, lease, or health change exists for this task.
            _code = exc.code if isinstance(exc, _CCError4) else "CONTEXT_COMPILATION_INTERNAL"
            logger.warning("task %s compilation failed (%s); task blocked", task_id, _code)
            release_provider_reservation(self.db, self.events, task_id)
            task_locks.release_locks_for_task(self.db, self.events, task_id)
            self.db.update(
                "tasks",
                task_id,
                {
                    "status": TaskStatus.FAILED.value,
                    "blocking_issue": _cc_reason4(exc, _task_ctx_spec)[:1000],
                    "finished_at": utcnow().isoformat(),
                },
            )
            self.events.publish(
                EventType.TASK_FAILED,
                mission_id=self.mission_id,
                task_id=task_id,
                reason="context_compilation_failed",
            )
            return
        _pre_run_id = f"run-{_uuid.uuid4().hex[:12]}"
        self._active_invocation_runs[task_id] = _pre_run_id
        try:
            outcome = await self._invocations().execute(
                InvocationSpec(
                    owner=InvocationOwner(
                        mission_id=self.mission_id,
                        task_id=task_id,
                        product_project_id=_product_id,
                        phase_id=_phase_id,
                    ),
                    stage=STAGE_TASK,
                    role=role.value,
                    prompt=prompt,
                    workdir=Path(worktree_path),
                    log_dir=log_dir,
                    provider=provider_name,
                    timeout_s=self.config.provider_timeout_s(provider_name),
                    model_requested=getattr(adapter, "model", None) if provider_name == "opencode" else None,
                    run_id=_pre_run_id,
                    cancel_event=self._cancel,
                    retry_of_run_id=_task_retry_of,
                    prompt_template_version=_task_ctx.get("prompt_template_version"),
                    context_policy_version=_task_ctx.get("context_policy_version"),
                    context_blocks_json=_task_ctx.get("context_blocks_json"),
                    context_warnings_json=_task_ctx.get("context_warnings_json"),
                    context_budget=_task_ctx.get("context_budget"),
                    context_used=_task_ctx.get("context_used"),
                    context_remaining=_task_ctx.get("context_remaining"),
                    context_repeated_ratio=_task_ctx.get("context_repeated_ratio"),
                    context_plan_revision=_task_ctx.get("context_plan_revision"),
                )
            )
        except RuntimeError as exc:
            logger.warning("invocation capacity refused for task %s: %s", task_id, exc)
            self._active_invocation_runs.pop(task_id, None)
            release_provider_reservation(self.db, self.events, task_id)
            task_locks.release_locks_for_task(self.db, self.events, task_id)
            self.db.update(
                "tasks",
                task_id,
                {"status": TaskStatus.PENDING.value, "blocking_issue": f"capacity refused: {exc}"},
            )
            return
        finally:
            self._active_invocation_runs.pop(task_id, None)
        run_id = outcome.run_id
        result = ExecutionResult(
            state=ProviderState(outcome.provider_state)
            if outcome.provider_state in ProviderState.__members__.values()
            else (ProviderState.COMPLETED if outcome.ok else ProviderState.CRASHED),
            failure_class=FailureClass(outcome.failure_class)
            if outcome.failure_class in FailureClass.__members__.values()
            else FailureClass.CRASH,
            exit_code=outcome.exit_code,
            duration_s=outcome.duration_s,
            summary=outcome.summary,
            argv=[],
            stdout_path=Path(outcome.stdout_path) if outcome.stdout_path else None,
            stderr_path=Path(outcome.stderr_path) if outcome.stderr_path else None,
            raw_tail=outcome.raw_tail,
            assistant_text=outcome.assistant_text,
            pid=outcome.pid,
            pgid=outcome.pgid,
            gate_refused=outcome.gate_refused,
        )
        try:
            commit_after = await git_ops.head_sha(Path(worktree_path))
        except Exception:
            commit_after = None
            logger.debug("git head failed for task %s", task_id, exc_info=True)
        try:
            self.db.execute(
                "UPDATE provider_runs SET git_commit_before=?, git_commit_after=? WHERE id=?",
                (commit_before, commit_after, run_id),
            )
        except Exception:
            logger.debug("git linkage failed for run %s", run_id, exc_info=True)

        # Ancestry validation (D-14/D-15/§49/§65): the result must descend
        # from the pinned input. A provider that rewrites history (reset,
        # divergent commit) fails provenance here — its output is never
        # checkpointed as a valid result nor integrated downstream.
        if result.ok and _planned_input and commit_after and commit_after != _planned_input:
            try:
                _descends = await git_ops.is_ancestor(Path(worktree_path), _planned_input, commit_after)
            except Exception:
                _descends = False
            if not _descends:
                release_provider_reservation(self.db, self.events, task_id, run_id)
                task_locks.release_locks_for_task(self.db, self.events, task_id)
                self.db.update(
                    "tasks",
                    task_id,
                    {
                        "status": TaskStatus.FAILED.value,
                        "blocking_issue": f"provider rewrote task history (input {_planned_input[:8]}"
                        f" not ancestor of result {(commit_after or '')[:8]}); output rejected"[:1000],
                        "finished_at": utcnow().isoformat(),
                    },
                )
                self.events.publish(
                    EventType.TASK_FAILED,
                    mission_id=self.mission_id,
                    task_id=task_id,
                    reason="history_rewrite_rejected",
                )
                return

        # Late cancellation guard: a task cancelled while the provider was
        # still running must stay CANCELLED; the late result only releases
        # capacity, never resurrects the task.
        _current_task = self.db.get("tasks", task_id) or {}
        if _current_task.get("status") == TaskStatus.CANCELLED.value:
            release_provider_reservation(self.db, self.events, task_id, run_id)
            task_locks.release_locks_for_task(self.db, self.events, task_id)
            return

        # Release reservation + locks only now, after confirmed process exit.
        release_provider_reservation(self.db, self.events, task_id, run_id)
        task_locks.release_locks_for_task(self.db, self.events, task_id)

        if result.ok:
            # Health already accounted exactly once by InvocationService.

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

            _previous_result = task.get("result_sha")
            _final_result = checkpoint_sha or _planned_input
            # Capture async state first; then publish row + status with NO
            # awaits between them, so concurrent downstream readers never
            # observe "COMPLETED but unattributed" intermediate state.
            from .provenance import ACTOR_PROVIDER as _ACTOR_PROV
            from .provenance import capture_write_state, record_write

            _cap_result, _cap_tree, _cap_ident = await capture_write_state(Path(worktree_path))
            if _cap_result != _final_result:
                logger.debug("task %s HEAD moved during checkpoint (%s -> %s)", task_id, _final_result, _cap_result)
            try:
                record_write(
                    self.db,
                    run_id=run_id,
                    mission_id=self.mission_id,
                    task_id=task_id,
                    product_project_id=_product_id,
                    phase_id=_phase_id,
                    actor_type=_ACTOR_PROV,
                    actor_detail="",
                    provider=provider_name,
                    role=role.value,
                    base_sha=commit_before,
                    result_sha=_final_result,
                    tree_sha=_cap_tree,
                    repo_key_value=_cap_ident,
                )
            except Exception:
                logger.debug("task write row insert failed for %s", task_id, exc_info=True)
            self.db.update(
                "tasks",
                task_id,
                {
                    "status": TaskStatus.COMPLETED.value,
                    "finished_at": utcnow().isoformat(),
                    "summary": result.summary,
                    "provider_run_id": run_id,
                    "checkpoint_after": checkpoint_sha,
                    # Immutable per attempt: explicit checkpoint, else the
                    # unchanged input (no-change runs are honest no-ops).
                    "result_sha": _final_result,
                },
            )
            # Upstream retry invalidates completed descendants (D-18/§59):
            # a new result that obsoletes a previously recorded one marks
            # completed direct dependents STALE unless they already contain it.
            _new_result = _final_result
            if _previous_result and _new_result and _previous_result != _new_result:
                await self._mark_descendants_stale(task_id, str(_new_result))
            self.events.publish(
                EventType.TASK_COMPLETED,
                mission_id=self.mission_id,
                task_id=task_id,
                provider=provider_name,
            )
        else:
            # Health already accounted exactly once by InvocationService.
            # CANCELLED tasks stay cancelled; never flip to FAILED on a late event.
            if result.failure_class == FailureClass.CANCELLED:
                self.db.update(
                    "tasks",
                    task_id,
                    {
                        "status": TaskStatus.CANCELLED.value,
                        "finished_at": utcnow().isoformat(),
                        "summary": result.raw_tail[-300:],
                        "provider_run_id": run_id,
                    },
                )
                self.events.publish(
                    EventType.TASK_CANCELLED,
                    mission_id=self.mission_id,
                    task_id=task_id,
                    provider=provider_name,
                )
                return
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
