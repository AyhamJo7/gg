"""Mission engine: the durable orchestration state machine.

One engine instance drives one mission through phases. All state transitions
are persisted before the action they describe, so a backend restart can
safely reconstruct execution (see recovery.py / Orchestrator.start).

Provider-driven phases are idempotent: providers are stateless per run and
always receive a fresh structured handoff, so re-running a phase after a
crash is safe and never duplicates git commits (checkpoints only commit
actual changes).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import git_ops
from .events import EventBus
from .handoff import persist_handoff, render_handoff
from .locks import ResourceLocks
from .models import (
    Autonomy,
    EventType,
    FailureClass,
    Mission,
    MissionStatus,
    ProviderState,
    Role,
    TaskRecord,
    utcnow,
)
from .providers.base import ExecutionRequest, ExecutionResult, ProviderAdapter
from .providers.registry import ProviderRegistry
from .review import (
    REVIEW_INSTRUCTIONS,
    open_blockers,
    persist_findings,
    resolve_open_findings,
)
from .security import ensure_gitignore_protections, redact
from .verify import run_verification
from .workspace import WorkspaceInfo, inspect_workspace

if TYPE_CHECKING:
    from .config import Config
    from .db import Database

logger = logging.getLogger(__name__)

PROVIDER_PHASES: dict[MissionStatus, Role] = {
    MissionStatus.PLANNING: Role.PLANNING,
    MissionStatus.IMPLEMENTING: Role.IMPLEMENTATION,
    MissionStatus.TESTING: Role.TESTING,
    MissionStatus.REVIEWING: Role.REVIEW,
    MissionStatus.REPAIRING: Role.REPAIR,
}

PHASE_SEQUENCE = [
    MissionStatus.ANALYZING,
    MissionStatus.PLANNING,
    MissionStatus.IMPLEMENTING,
    MissionStatus.TESTING,
    MissionStatus.REVIEWING,
    MissionStatus.FINAL_VALIDATION,
]

ROLE_PROMPTS = {
    Role.PLANNING: (
        "Produce a concrete implementation plan: decompose the mission into phases and atomic tasks. "
        "Be specific about files, architecture, and verification steps. Keep it actionable for the next engineer."
    ),
    Role.IMPLEMENTATION: (
        "Implement the mission requirements in the workspace. Write real, working code. "
        "Follow existing project conventions and instruction files. Keep changes focused."
    ),
    Role.TESTING: (
        "Run the project's test/build toolchain. Fix any failures you find. "
        "Do not weaken tests to make them pass."
    ),
    Role.REVIEW: REVIEW_INSTRUCTIONS,
    Role.REPAIR: "Fix the open review findings listed below. Verify your fixes by running relevant tests.",
}


class MissionEngine:
    def __init__(
        self,
        mission_id: str,
        db: Database,
        events: EventBus,
        registry: ProviderRegistry,
        config: Config,
        locks: ResourceLocks,
    ):
        self.mission_id = mission_id
        self.db = db
        self.events = events
        self.registry = registry
        self.config = config
        self.locks = locks
        self._pause = asyncio.Event()  # set → paused requested
        self._cancel = asyncio.Event()
        self._gate_resolved = asyncio.Event()
        self._wake = asyncio.Event()  # scheduler tick wake-up
        self._current_adapter: ProviderAdapter | None = None
        self._current_run_id: str | None = None
        self._completed_work: list[str] = []
        self._tests_run: list[str] = []
        self.workspace: WorkspaceInfo | None = None
        self.project_path: Path | None = None

    # -- control ------------------------------------------------------------
    def request_pause(self) -> None:
        self._pause.set()
        self._wake.set()
        if self._current_adapter and self._current_run_id:
            asyncio.create_task(self._current_adapter.interrupt(self._current_run_id))

    def request_cancel(self) -> None:
        self._cancel.set()
        self._pause.clear()
        self._wake.set()
        if self._current_adapter and self._current_run_id:
            asyncio.create_task(self._current_adapter.interrupt(self._current_run_id))

    def resolve_gate(self) -> None:
        self._gate_resolved.set()

    def wake(self) -> None:
        self._wake.set()

    # -- persistence helpers ---------------------------------------------------
    def _mission(self) -> Mission:
        row = self.db.get("missions", self.mission_id)
        if row is None:
            raise RuntimeError(f"mission {self.mission_id} vanished")
        import json as _json

        row["providers_used"] = _json.loads(row.get("providers_used") or "[]")
        row["providers_failed"] = _json.loads(row.get("providers_failed") or "[]")
        return Mission(**{k: v for k, v in row.items() if k in Mission.model_fields})

    def _set_status(self, status: MissionStatus, **extra: Any) -> None:
        data: dict[str, Any] = {"status": status.value, "updated_at": utcnow()}
        data.update(extra)
        if status in (MissionStatus.COMPLETED, MissionStatus.FAILED, MissionStatus.CANCELLED, MissionStatus.UNVERIFIED):
            data["finished_at"] = utcnow()
        self.db.update("missions", self.mission_id, data)
        self.events.publish(EventType.MISSION_STATUS_CHANGED, self.mission_id, status=status.value, **extra)

    def _project_path(self) -> Path:
        mission = self._mission()
        project = self.db.get("projects", mission.project_id)
        if project is None:
            raise RuntimeError("project vanished")
        return Path(project["path"])

    # -- checkpoint -----------------------------------------------------------
    async def _checkpoint(self, message: str) -> str | None:
        if self.project_path is None:
            raise RuntimeError("engine project path not initialized")
        if not self.workspace or not self.workspace.is_git_repo:
            return None
        if not self.config.get("git.auto_checkpoint", True):
            return None
        async with self.locks.git():
            try:
                ensure_gitignore_protections(self.project_path)
                sha = await git_ops.checkpoint(self.project_path, message)
            except git_ops.GitError as exc:
                logger.warning("checkpoint failed: %s", exc)
                return None
        if sha:
            self.db.insert(
                "checkpoints",
                {
                    "id": f"ckpt-{utcnow().timestamp()}",
                    "mission_id": self.mission_id,
                    "project_id": self._mission().project_id,
                    "commit_sha": sha,
                    "message": message,
                    "created_at": utcnow(),
                },
            )
            self.events.publish(EventType.GIT_CHECKPOINT_CREATED, self.mission_id, sha=sha, message=message)
            self.db.update("missions", self.mission_id, {"git_head": sha, "updated_at": utcnow()})
        return sha

    # -- handoff ---------------------------------------------------------------
    def _make_handoff(self, role: Role, from_provider: str | None, to_provider: str | None, next_action: str) -> str:
        mission = self._mission()
        findings = [f"[{f['severity']}] {f['description']}" for f in open_blockers(self.db, mission.id)]
        content = render_handoff(
            mission=mission,
            role=role.value,
            from_provider=from_provider,
            to_provider=to_provider,
            workspace_summary=self.workspace.summary() if self.workspace else "unknown",
            completed_work=self._completed_work[-15:],
            tests=self._tests_run[-15:],
            review_findings=findings,
            next_action=next_action,
            git_head=mission.git_head,
        )
        if self.project_path is None:
            raise RuntimeError("engine project path not initialized")
        handoff = persist_handoff(
            self.db, self.project_path, mission, role.value, content, from_provider, to_provider, mission.git_head
        )
        self.events.publish(EventType.HANDOFF_CREATED, self.mission_id, handoff_id=handoff.id, role=role.value)
        return content

    # -- provider selection ------------------------------------------------------
    def _last_provider_for(self, role: Role) -> str | None:
        rows = self.db.query(
            """SELECT provider FROM provider_runs WHERE mission_id=? AND role=? AND failure_class='NONE'
               ORDER BY started_at DESC LIMIT 1""",
            (self.mission_id, role.value),
        )
        return rows[0]["provider"] if rows else None

    def _select_provider(self, role: Role) -> str | None:
        """Priority order from config, filtered by eligibility, with separation of duties."""
        priorities: list[str] = self.config.priority_for(role.value)
        if not priorities:
            priorities = list(self.registry.adapters.keys())
        mission = self._mission()
        profile_overrides = self.db.get("settings", f"profile.{mission.profile}", key="key")
        if profile_overrides:
            import json as _json

            stored = _json.loads(profile_overrides["value"])
            if role.value in stored:
                priorities = stored[role.value]
        eligible = [p for p in priorities if self.registry.is_eligible(p)]

        # Separation of duties: reviewer should differ from implementer when possible.
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

    # -- provider execution -------------------------------------------------------
    async def _run_provider_phase(self, role: Role, extra_context: str = "") -> ExecutionResult | None:
        """Run one provider for a role, with failover across the priority list.

        Returns the successful ExecutionResult, or None if the mission was
        paused/cancelled or all providers are unavailable.
        """
        max_attempts = int(self.config.get("orchestration.max_phase_attempts", 4))
        last_provider: str | None = None
        attempt = 0

        while attempt < max_attempts:
            if self._cancel.is_set() or self._pause.is_set():
                return None
            provider_name = self._select_provider(role)
            if provider_name is None:
                self._set_status(MissionStatus.WAITING_FOR_PROVIDER, blocking_issue="no eligible provider")
                await self._wait_for_wake()
                self._set_status(self._status_for_role(role))
                attempt += 1
                continue

            adapter = self.registry.get_adapter(provider_name)
            if adapter is None:
                attempt += 1
                continue

            mission = self._mission()
            self.events.publish(EventType.PROVIDER_SELECTED, self.mission_id, provider=provider_name, role=role.value)
            handoff_content = self._make_handoff(role, last_provider, provider_name, ROLE_PROMPTS[role])
            prompt = self._build_prompt(role, mission, handoff_content, extra_context)

            task = TaskRecord(mission_id=self.mission_id, role=role, status="running", prompt=prompt[-4000:],
                attempts=1)
            self.db.insert(
                "tasks",
                {
                    "id": task.id, "mission_id": task.mission_id, "role": task.role.value,
                    "status": task.status, "prompt": task.prompt, "summary": "",
                    "attempts": 1, "created_at": task.created_at,
                },
            )
            self.events.publish(EventType.TASK_STARTED, self.mission_id, task_id=task.id, role=role.value,
                provider=provider_name)

            run_id = f"run-{utcnow().timestamp()}".replace(".", "")
            if self.project_path is None:
                raise RuntimeError("engine project path not initialized")
            log_dir = self.project_path / ".orchestrator" / "logs"
            request = ExecutionRequest(
                prompt=prompt,
                workdir=self.project_path,
                role=role.value,
                timeout_s=self.config.provider_timeout_s(provider_name),
                run_id=run_id,
                log_dir=log_dir,
            )
            commit_before = await git_ops.head_sha(self.project_path) if self.project_path else None

            self.registry.mark_busy(provider_name)
            self.db.update("missions", self.mission_id, {"current_provider": provider_name, "updated_at": utcnow()})
            self.events.publish(EventType.PROVIDER_STARTED, self.mission_id, provider=provider_name, role=role.value)
            self._current_adapter, self._current_run_id = adapter, run_id

            def on_output(line: str, provider: str = provider_name) -> None:
                self.events.publish(EventType.PROVIDER_OUTPUT, self.mission_id, provider=provider, line=line)

            try:
                result = await adapter.execute(request, on_output)
            except Exception as exc:  # adapter itself blew up — treat as crash
                logger.exception("provider %s crashed", provider_name)
                result = ExecutionResult(
                    state=ProviderState.CRASHED,
                    failure_class=FailureClass.CRASH,
                    exit_code=None,
                    duration_s=0.0,
                    summary="",
                    raw_tail=str(exc),
                )
            finally:
                self._current_adapter, self._current_run_id = None, None

            commit_after = await git_ops.head_sha(self.project_path) if self.project_path else None
            self._record_run(run_id, task.id, provider_name, role, request, result, commit_before, commit_after)

            if result.ok:
                self.registry.record_success(provider_name, result.duration_s)
                used = set(self._mission().providers_used)
                if provider_name not in used:
                    self.db.update(
                        "missions", self.mission_id,
                        {"providers_used": sorted(set(self._mission().providers_used) | {provider_name}),
                            "updated_at": utcnow()},
                    )
                self.db.update("tasks", task.id, {"status": "completed", "summary": result.summary,
                    "finished_at": utcnow()})
                self.events.publish(EventType.TASK_COMPLETED, self.mission_id, task_id=task.id, provider=provider_name)
                await self._checkpoint(f"agent({provider_name}): {role.value} checkpoint")
                self._completed_work.append(f"[{role.value}] {provider_name}: {result.summary[:200]}")
                return result

            # failure path
            state = self.registry.record_failure(provider_name, result.failure_class, result.duration_s,
                result.raw_tail[:300])
            self.db.update("tasks", task.id, {"status": "failed", "summary": result.raw_tail[-300:],
                "finished_at": utcnow()})
            failed = set(self._mission().providers_failed)
            failed.add(provider_name)
            self.db.update("missions", self.mission_id, {"providers_failed": sorted(failed), "updated_at": utcnow()})
            event_type = EventType.PROVIDER_RATE_LIMITED if result.failure_class in (FailureClass.RATE_LIMIT,
                FailureClass.QUOTA_EXHAUSTED) else EventType.PROVIDER_FAILED
            self.events.publish(
                event_type, self.mission_id,
                provider=provider_name, failure=result.failure_class.value, state=state.value, role=role.value,
            )
            if result.failure_class == FailureClass.CANCELLED:
                if self._cancel.is_set():
                    self._set_status(MissionStatus.CANCELLED, current_provider=None)
                else:
                    self._set_status(MissionStatus.PAUSED, current_provider=None)
                    self.events.publish(EventType.MISSION_PAUSED, self.mission_id)
                return None
            if result.failure_class == FailureClass.HUMAN_INPUT:
                await self._create_gate(
                    reason=f"Provider {provider_name} requires human input",
                    detail=result.raw_tail[-500:],
                    choices=["Continue", "Skip provider", "Cancel mission"],
                    recommended="Skip provider",
                )
                return None
            if result.failure_class == FailureClass.AUTH:
                await self._create_gate(
                    reason=f"Provider {provider_name} is not authenticated",
                    detail=f"Run `{provider_name}` interactively to re-authenticate, then resolve this gate.",
                    choices=["Retry provider", "Skip provider", "Cancel mission"],
                    recommended="Skip provider",
                )
                return None

            await self._checkpoint(f"orchestrator: checkpoint before provider switch ({role.value})")
            last_provider = provider_name
            attempt += 1

        self.events.publish(EventType.MISSION_FAILED, self.mission_id, reason=f"phase {role.value} exhausted providers")
        return None

    def _record_run(
        self,
        run_id: str,
        task_id: str,
        provider: str,
        role: Role,
        request: ExecutionRequest,
        result: ExecutionResult,
        before: str | None,
        after: str | None,
    ) -> None:
        self.db.insert(
            "provider_runs",
            {
                "id": run_id,
                "mission_id": self.mission_id,
                "task_id": task_id,
                "provider": provider,
                "role": role.value,
                "command": [redact(a if len(a) < 300 else f"<prompt {len(a)} chars>") for a in result.argv],
                "cwd": str(request.workdir),
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
            },
        )

    def _status_for_role(self, role: Role) -> MissionStatus:
        return {v: k for k, v in PROVIDER_PHASES.items()}[role]

    def _build_prompt(self, role: Role, mission: Mission, handoff_content: str, extra_context: str) -> str:
        sections = [
            f"You are working as the **{role.value}** engineer in a multi-provider orchestrated mission.",
            "",
            handoff_content,
            "",
            f"## Your instructions for this phase\n{ROLE_PROMPTS[role]}",
        ]
        if extra_context:
            sections.append(f"## Additional context\n{extra_context}")
        sections.append(
            "\n## Output contract\nWork autonomously in the current directory. "
            "Do not ask questions; make reasonable decisions and document them. "
            "When finished, end with a one-paragraph summary of what you did."
        )
        return "\n".join(sections)

    async def _create_gate(self, reason: str, detail: str, choices: list[str], recommended: str) -> None:
        gate_id = f"gate-{utcnow().timestamp()}".replace(".", "")
        self.db.insert(
            "human_gates",
            {
                "id": gate_id,
                "mission_id": self.mission_id,
                "reason": reason,
                "detail": redact(detail),
                "choices": choices,
                "recommended": recommended,
                "status": "open",
                "created_at": utcnow(),
            },
        )
        self._set_status(MissionStatus.WAITING_FOR_HUMAN, blocking_issue=reason)
        self.events.publish(EventType.HUMAN_GATE_CREATED, self.mission_id, gate_id=gate_id, reason=reason,
            choices=choices)
        await self._wait_for_gate()

    async def _wait_for_gate(self) -> None:
        while not self._cancel.is_set():
            try:
                await asyncio.wait_for(self._gate_resolved.wait(), timeout=1.0)
                self._gate_resolved.clear()
                return
            except TimeoutError:
                if self._pause.is_set():
                    return

    async def _wait_for_wake(self) -> None:
        self._wake.clear()
        tick = float(self.config.get("orchestration.scheduler_tick_seconds", 2))
        while not self._cancel.is_set() and not self._pause.is_set():
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=tick * 5)
                self._wake.clear()
                return
            except TimeoutError:
                return  # re-check eligibility periodically

    # -- control checks ---------------------------------------------------------
    def _check_pause_cancel(self) -> bool:
        """Return True if the mission should stop the engine loop."""
        if self._cancel.is_set():
            self._set_status(MissionStatus.CANCELLED, current_provider=None)
            return True
        if self._pause.is_set():
            self._set_status(MissionStatus.PAUSED)
            self.events.publish(EventType.MISSION_PAUSED, self.mission_id)
            return True
        return False

    # -- main loop ----------------------------------------------------------------
    async def run(self) -> None:
        self.project_path = self._project_path()
        mission = self._mission()
        start_phase = mission.current_phase or MissionStatus.ANALYZING
        if mission.status in (MissionStatus.RECOVERING, MissionStatus.WAITING_FOR_PROVIDER):
            start_phase = mission.current_phase or MissionStatus.ANALYZING
        try:
            start_index = PHASE_SEQUENCE.index(MissionStatus(start_phase)) if start_phase in PHASE_SEQUENCE else 0
        except ValueError:
            start_index = 0

        for phase in PHASE_SEQUENCE[start_index:]:
            if self._check_pause_cancel():
                return
            self._set_status(phase, current_phase=phase.value)
            self.events.publish(EventType.PHASE_STARTED, self.mission_id, phase=phase.value)

            if phase == MissionStatus.ANALYZING:
                ok = await self._phase_analyze()
            elif phase == MissionStatus.FINAL_VALIDATION:
                ok = await self._phase_final_validation()
                if ok:
                    break
            elif phase == MissionStatus.REVIEWING:
                ok = await self._phase_review_loop()
            else:
                ok = await self._phase_provider(PROVIDER_PHASES[phase])

            if not ok:
                return  # paused / cancelled / waiting-for-human / waiting-for-provider already persisted
            self.events.publish(EventType.PHASE_COMPLETED, self.mission_id, phase=phase.value)

        # all phases done
        head = await git_ops.head_sha(self.project_path) if self.workspace and self.workspace.is_git_repo else None
        await self._checkpoint("orchestrator: final verified state")
        self._set_status(MissionStatus.COMPLETED, current_provider=None, git_head=head)
        self.events.publish(EventType.MISSION_COMPLETED, self.mission_id)

    async def _phase_analyze(self) -> bool:
        self.workspace = await inspect_workspace(self._project_path())
        if not self.workspace.is_git_repo:
            await git_ops.init_repo(self.project_path)  # type: ignore[arg-type]
            self.workspace.is_git_repo = True
            await self._checkpoint("orchestrator: initial repository checkpoint")
        else:
            st = await git_ops.status(self.project_path)  # type: ignore[arg-type]
            if not st.is_clean:
                await self._checkpoint("orchestrator: checkpoint before mission start (pre-existing changes)")
        self.db.update(
            "projects", self._mission().project_id, {"detected_type": self.workspace.project_type}
        )
        return True

    async def _phase_provider(self, role: Role) -> bool:
        if role == Role.IMPLEMENTATION and self._mission().autonomy == Autonomy.SAFE:
            open_gate = self.db.query(
                "SELECT id FROM human_gates WHERE mission_id=? "
                "AND reason LIKE 'Approve implementation%' AND status='open'",
                (self.mission_id,),
            )
            if not open_gate:
                plan = self.db.query(
                    "SELECT summary FROM tasks WHERE mission_id=? AND role='planning' ORDER BY created_at DESC LIMIT 1",
                    (self.mission_id,),
                )
                plan_text = plan[0]["summary"][:600] if plan else "(no plan recorded)"
                await self._create_gate(
                    reason="Approve implementation start",
                    detail=f"Planned approach:\n{plan_text}",
                    choices=["Approve", "Cancel mission"],
                    recommended="Approve",
                )
                if self._cancel.is_set() or self._pause.is_set():
                    return False
        result = await self._run_provider_phase(role)
        if result is None:
            return False
        if role == Role.TESTING:
            self._tests_run.append(f"provider testing phase: {result.summary[:150]}")
        return True

    async def _phase_review_loop(self) -> bool:
        if not self.config.get("orchestration.review_required", True):
            return True
        max_cycles = int(self.config.get("orchestration.max_repair_cycles", 3))
        while True:
            result = await self._run_provider_phase(Role.REVIEW)
            if result is None:
                return False
            findings = persist_findings(self.db, self.mission_id, result.raw_tail + "\n" + result.summary)
            for f in findings:
                self.events.publish(
                    EventType.REVIEW_FINDING_CREATED, self.mission_id,
                    severity=f.severity.value, description=f.description[:200],
                )
            blockers = open_blockers(self.db, self.mission_id)
            if not blockers:
                return True
            cycles = self._mission().repair_cycles
            if cycles >= max_cycles:
                self._set_status(
                    MissionStatus.UNVERIFIED,
                    blocking_issue=f"{len(blockers)} blocker/high findings remain after {cycles} repair cycles",
                    current_provider=None,
                )
                return False
            self.db.update("missions", self.mission_id, {"repair_cycles": cycles + 1, "updated_at": utcnow()})
            # NOTE: current_phase stays REVIEWING — REPAIRING is a sub-state, so
            # recovery after restart re-enters the review loop correctly.
            self._set_status(MissionStatus.REPAIRING)
            findings_text = "\n".join(
                f"- [{f['severity']}] {f['file'] or ''}: {f['description']} → {f['recommended_fix']}" for f in blockers
            )
            repair = await self._run_provider_phase(
                Role.REPAIR, extra_context=f"## Open findings to fix\n{findings_text}"
            )
            if repair is None:
                return False
            resolve_open_findings(self.db, self.mission_id)
            self._set_status(MissionStatus.REVIEWING)

    async def _phase_final_validation(self) -> bool:
        if self.project_path is None:
            raise RuntimeError("engine project path not initialized")
        if self.workspace is None:
            self.workspace = await inspect_workspace(self.project_path)
        report = await run_verification(self.workspace, self.db, self.events, self.mission_id, self.project_path)
        self._tests_run.extend(r.command for r in report.results)
        if not report.attempted:
            self._set_status(
                MissionStatus.UNVERIFIED,
                blocking_issue="no verification toolchain detected — cannot prove correctness",
                current_provider=None,
            )
            return False
        if not report.all_passed:
            failures = [r for r in report.results if not r.passed]
            detail = "\n".join(f"{r.command}: exit={r.exit_code}\n{r.tail[-400:]}" for r in failures)
            # one automated repair attempt via the repair role before giving up
            cycles = self._mission().repair_cycles
            if cycles < int(self.config.get("orchestration.max_repair_cycles", 3)):
                self.db.update("missions", self.mission_id, {"repair_cycles": cycles + 1, "updated_at": utcnow()})
                self._set_status(MissionStatus.REPAIRING)  # current_phase stays FINAL_VALIDATION
                repair = await self._run_provider_phase(Role.REPAIR,
                    extra_context=f"## Verification failures to fix\n{detail}")
                if repair is None:
                    return False
                return await self._phase_final_validation()
            self._set_status(MissionStatus.UNVERIFIED, blocking_issue=f"verification failed:\n{detail[:800]}",
                current_provider=None)
            return False
        return True


def request_prompt_safe_command(request: ExecutionRequest) -> list[str]:
    """Deprecated — argv now travels on ExecutionResult; kept for API stability."""
    return [f"<{request.role} prompt len={len(request.prompt)}>"]
