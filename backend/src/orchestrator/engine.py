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
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import git_ops
from .events import EventBus
from .handoff import persist_handoff, render_handoff
from .locks import ResourceLocks
from .models import (
    TERMINAL_STATUSES,
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
    mark_findings_repair_attempted,
    open_blockers,
    persist_findings,
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
        "Run the project's test/build toolchain. Fix any failures you find. Do not weaken tests to make them pass."
    ),
    Role.REVIEW: REVIEW_INSTRUCTIONS,
    Role.REPAIR: "Fix the open review findings listed below. Verify your fixes by running relevant tests.",
}


FAILURE_TEXT_CHARS = 4000
FAILOVER_EVIDENCE_CHARS = 1500


def _failover_evidence(provider: str, result: ExecutionResult) -> str:
    """Bounded, redacted report of a failed attempt for the next provider.

    Labelled partial and unverified: it is context to re-check, never a
    verified result, and it never overrides the task or findings.
    """
    report = redact((result.summary or "").strip())
    if not report:
        return ""
    if len(report) > FAILOVER_EVIDENCE_CHARS:
        report = report[:FAILOVER_EVIDENCE_CHARS] + "…"
    return (
        f"## Previous attempt (did not complete)\n"
        f"{provider} stopped with {result.failure_class.value} after {result.duration_s:.0f}s. "
        "Its last report is partial and unverified; re-check before relying on it:\n"
        f"{report}"
    )


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
        self._invocation_service: Any = None

    def _invocations(self) -> Any:
        if self._invocation_service is None:
            from .invocations import InvocationService

            self._invocation_service = InvocationService(self.db, self.registry, self.config)
        return self._invocation_service

    def _product_attribution(self) -> tuple[str | None, str | None]:
        try:
            rows = self.db.query(
                "SELECT project_id, id FROM project_phases WHERE mission_id=? LIMIT 1", (self.mission_id,)
            )
            if rows:
                return rows[0].get("project_id"), rows[0].get("id")
        except Exception:
            logger.debug("product attribution lookup failed for %s", self.mission_id, exc_info=True)
        return None, None

    def request_pause(self) -> None:
        self._pause.set()
        self._wake.set()
        if self._current_run_id:
            svc = self._invocations()
            asyncio.create_task(svc.cancel(self._current_run_id))
        elif self._current_adapter and self._current_run_id:
            asyncio.create_task(self._current_adapter.interrupt(self._current_run_id))

    def pause(self) -> None:
        self.request_pause()

    def request_cancel(self) -> None:
        self._cancel.set()
        self._pause.clear()
        self._wake.set()
        if self._current_run_id:
            svc = self._invocations()
            asyncio.create_task(svc.cancel(self._current_run_id))
        elif self._current_adapter and self._current_run_id:
            asyncio.create_task(self._current_adapter.interrupt(self._current_run_id))

    def cancel(self) -> None:
        self.request_cancel()

    def resume(self) -> None:
        self._pause.clear()
        self._gate_resolved.set()
        self._wake.set()

    def resolve_gate(self) -> None:
        self._gate_resolved.set()
        self._wake.set()

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
        if status in TERMINAL_STATUSES:
            data["finished_at"] = utcnow()
        self.db.update("missions", self.mission_id, data)
        self.events.publish(EventType.MISSION_STATUS_CHANGED, self.mission_id, status=status.value, **extra)

    # -- terminal-state invariant -------------------------------------------------
    # A terminal event must correspond to a durable terminal mission state.
    # Order is always: persist terminal state FIRST, then publish the terminal
    # event. The database is authoritative — if event publication fails after
    # persistence, recovery must still see the mission as terminal.

    def _fail(self, reason: str) -> None:
        """Persist FAILED (durable, terminal), then emit MISSION_FAILED."""
        self._set_status(MissionStatus.FAILED, blocking_issue=reason, current_provider=None)
        self.events.publish(EventType.MISSION_FAILED, self.mission_id, reason=reason)

    def _fail_checkpoint_exhaustion(self, error: str) -> None:
        """Transition mission to UNVERIFIED due to fatal checkpoint exhaustion."""
        self._set_status(
            MissionStatus.UNVERIFIED,
            blocking_issue=f"git checkpoint exhausted: {error}",
            current_provider=None,
        )

    def _project_path(self) -> Path:
        mission = self._mission()
        project = self.db.get("projects", mission.project_id)
        if project is None:
            raise RuntimeError("project vanished")
        return Path(project["path"])

    # -- checkpoint -----------------------------------------------------------
    def _checkpoint_failure_count(self) -> int:
        """Durable consecutive-failure count derived from the missions table."""
        row = self.db.get("missions", self.mission_id)
        return int(row.get("checkpoint_failures", 0)) if row else 0

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
                max_mb = int(self.config.get("git.max_auto_commit_file_mb", 5))
                sha = await git_ops.checkpoint(self.project_path, message, max_file_mb=max_mb)
                # Success resets consecutive failure count durably
                self.db.update("missions", self.mission_id, {"checkpoint_failures": 0, "updated_at": utcnow()})
            except git_ops.GitError as exc:
                logger.warning("checkpoint failed: %s", exc)
                failures = self._checkpoint_failure_count() + 1
                self.db.update(
                    "missions",
                    self.mission_id,
                    {"checkpoint_failures": failures, "updated_at": utcnow()},
                )
                self.events.publish(EventType.GIT_CHECKPOINT_FAILED, self.mission_id, error=str(exc), message=message)
                max_ckpt_failures = int(self.config.get("git.max_checkpoint_failures", 2))
                if failures >= max_ckpt_failures:
                    self.db.update(
                        "missions",
                        self.mission_id,
                        {
                            "blocking_issue": f"git checkpoint failed repeatedly ({failures}x): {exc}",
                            "updated_at": utcnow(),
                        },
                    )
                    raise git_ops.GitCheckpointError(f"git checkpoint failed repeatedly ({failures}x): {exc}") from exc
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

        # Separation of duties: the reviewer must be outside the FULL
        # candidate provider-writer set (not merely != last implementer) —
        # repairers join the writer set once they write.
        if role == Role.REVIEW and len(eligible) > 1:
            from .provenance import mission_provider_writers

            writers, _complete = mission_provider_writers(self.db, self.mission_id)
            if not writers:
                _last_impl = self._last_provider_for(Role.IMPLEMENTATION)
                writers = {_last_impl} if _last_impl is not None else set()
            alternatives = [p for p in eligible if p not in writers]
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

        Returns the successful ExecutionResult, or None when the mission stopped
        (paused/cancelled/gated/failed) — in every such case the durable status
        has already been persisted by the responsible path.

        Attempt counting: only actual provider *executions* count toward
        max_phase_attempts. Waiting for a cooling-down provider does not consume
        attempts, but is bounded by max_provider_wait_seconds; if no provider can
        ever become eligible (all disabled/uninstalled), the mission fails fast.
        """
        max_attempts = int(self.config.get("orchestration.max_phase_attempts", 4))
        max_wait_s = float(self.config.get("orchestration.max_provider_wait_seconds", 7200))
        last_provider: str | None = None
        last_run_id: str | None = None
        attempt = 0
        wait_started: float | None = None
        # Dogfood 2026-09-12: a failover retry received a byte-identical prompt
        # and re-derived ~7 min of the failed run's findings. Carry the failed
        # attempt's own (partial, unverified) report forward as evidence.
        failover_note = ""

        while attempt < max_attempts:
            if self._cancel.is_set() or self._pause.is_set():
                self._check_pause_cancel()  # persists PAUSED/CANCELLED durably
                return None
            provider_name = self._select_provider(role)
            if provider_name is None:
                if not self.registry.has_potentially_available():
                    self._fail(f"no provider available for {role.value}: all providers disabled or uninstalled")
                    return None
                if wait_started is None:
                    wait_started = time.monotonic()
                    self._set_status(
                        MissionStatus.WAITING_FOR_PROVIDER, blocking_issue="all providers cooling down or unavailable"
                    )
                elif time.monotonic() - wait_started > max_wait_s:
                    self._fail(f"no eligible provider for {role.value} within {int(max_wait_s)}s")
                    return None
                await self._wait_for_wake()
                continue
            if wait_started is not None:
                wait_started = None
                self._set_status(self._status_for_role(role), blocking_issue=None)

            adapter = self.registry.get_adapter(provider_name)
            if adapter is None:
                attempt += 1
                continue

            # Exact-SHA write provenance (Increment 3): capture the base SHA
            # and pre-existing dirt BEFORE a code-writing run. Unattributed
            # dirty changes are never silently blamed on the provider — the
            # phase fails with an actionable reason instead.
            from .provenance import CODE_WRITING_ROLES, capture_write_start

            if role.value in CODE_WRITING_ROLES:
                _, _write_dirty, _write_paths = await capture_write_start(self.project_path)
                if _write_dirty and self._checkpoint_failure_count() == 0:
                    # Checkpointing works, so this dirt appeared outside GG's
                    # checkpoint flow (human/external mid-mission edit) — it
                    # must not be silently attributed to this provider run.
                    # (When checkpointing itself is broken, the exhaustion
                    # path below owns the outcome instead.)
                    _paths = ", ".join(_write_paths[:5])
                    self._fail(
                        f"workspace has unattributed changes before {role.value} run "
                        f"({_paths}); commit, discard, or adopt them as human operator, then retry. "
                        "Provider was NOT invoked."
                    )
                    return None

            mission = self._mission()
            self.events.publish(EventType.PROVIDER_SELECTED, self.mission_id, provider=provider_name, role=role.value)
            handoff_content = self._make_handoff(role, last_provider, provider_name, ROLE_PROMPTS[role])
            effective_context = f"{extra_context}\n\n{failover_note}".strip() if failover_note else extra_context
            prompt = self._build_prompt(role, mission, handoff_content, effective_context)
            # Role-specific context compiler (Increment 2, strict in compiled
            # mode): deterministic projection over requirements/acceptance/
            # architecture/handoffs. Compiled-mode compilation failure records
            # an honest mission failure; the provider is NEVER invoked with a
            # degraded prompt (no silent legacy fallback). Provider selection
            # above is side-effect-free (no lease, no health change).
            _product_id_early, _phase_id_early = self._product_attribution()
            _ctx_meta: dict[str, Any] = {}
            _constituted_spec: Any = None
            from .context_compiler import ContextCompileError as _CCError
            from .context_compiler import compile_failure_reason as _cc_reason

            try:
                from .context_compiler import (
                    ContextCompileSpec,
                    finding_ids_in_text,
                    latest_candidate_shas,
                    prepare_invocation_context,
                    resolve_phase_requirements,
                    role_for_stage,
                )

                _stage_early = {
                    Role.PLANNING: "mission_plan",
                    Role.IMPLEMENTATION: "implementation",
                    Role.TESTING: "testing",
                    Role.REVIEW: "review",
                    Role.REPAIR: "repair",
                }.get(role, role.value)
                _req_ids = resolve_phase_requirements(self.db, _product_id_early, _phase_id_early)
                _base_sha: str | None = None
                _candidate_sha: str | None = None
                _finding_ids: list[str] = []
                _finding_changed: list[str] = []
                _git_summary: str = ""
                if _stage_early in ("review", "repair"):
                    # Reviewer needs the exact candidate range; repairer needs
                    # the defect contract. Fail open: compiler warns on unknown.
                    # DOG-01/DOG-03: range is the canonical earliest-base ..
                    # latest-tip; changed files come from Git for that exact
                    # range, never from findings metadata.
                    _base_sha, _candidate_sha = latest_candidate_shas(self.db, self.mission_id)
                    if _base_sha and _candidate_sha and self.project_path is not None:
                        try:
                            _finding_changed = await git_ops.diff_names(
                                self.project_path, _base_sha, _candidate_sha, limit=100
                            )
                            _git_summary = await git_ops.diff_stat_range(self.project_path, _base_sha, _candidate_sha)
                        except Exception:
                            logger.debug("review git context failed for %s", self.mission_id, exc_info=True)
                            _finding_changed, _git_summary = [], ""
                    if _stage_early == "repair":
                        _finding_ids = finding_ids_in_text(extra_context)
                _constituted_spec = ContextCompileSpec(
                    role=role_for_stage(_stage_early, role.value),
                    stage=_stage_early,
                    product_project_id=_product_id_early,
                    project_phase_id=_phase_id_early,
                    mission_id=self.mission_id,
                    provider=provider_name,
                    base_sha=_base_sha,
                    candidate_sha=_candidate_sha,
                    git_files_changed=_finding_changed,
                    git_diff_summary=_git_summary,
                    finding_ids=_finding_ids,
                    task_objective=mission.task,
                    task_title=mission.title,
                    task_description=mission.task,
                    requirement_ids=_req_ids,
                    failure_text=effective_context[-FAILURE_TEXT_CHARS:] if effective_context else "",
                    extra_context="",
                    workspace_scope=[],
                    attempt=attempt + 1,
                )
                prompt, _ctx_meta = prepare_invocation_context(
                    legacy_prompt=prompt, spec=_constituted_spec, db=self.db, config=self.config
                )
            except Exception as exc:
                # Fail closed: no provider execution, no lease, no health
                # change, no provider_run. The mission records why it stopped.
                _code = exc.code if isinstance(exc, _CCError) else "CONTEXT_COMPILATION_INTERNAL"
                logger.warning("context compilation failed (%s); mission %s blocked", _code, self.mission_id)
                self._fail(_cc_reason(exc, _constituted_spec))
                return None

            task = TaskRecord(
                mission_id=self.mission_id, role=role, status="running", prompt=prompt[-4000:], attempts=1
            )
            self.db.insert(
                "tasks",
                {
                    "id": task.id,
                    "mission_id": task.mission_id,
                    "role": task.role.value,
                    "status": task.status,
                    "prompt": task.prompt,
                    "summary": "",
                    "attempts": 1,
                    "created_at": task.created_at,
                },
            )
            self.events.publish(
                EventType.TASK_STARTED, self.mission_id, task_id=task.id, role=role.value, provider=provider_name
            )

            if self.project_path is None:
                raise RuntimeError("engine project path not initialized")
            log_dir = self.project_path / ".orchestrator" / "logs"
            commit_before = await git_ops.head_sha(self.project_path) if self.project_path else None

            self.db.update("missions", self.mission_id, {"current_provider": provider_name, "updated_at": utcnow()})
            self.events.publish(EventType.PROVIDER_STARTED, self.mission_id, provider=provider_name, role=role.value)

            def on_output(line: str, provider: str = provider_name) -> None:
                self.events.publish(EventType.PROVIDER_OUTPUT, self.mission_id, provider=provider, line=line)

            # Shared invocation boundary: durable run, lease, usage, health.
            from .invocations import (
                STAGE_IMPLEMENTATION,
                STAGE_MISSION_PLANNING,
                STAGE_REPAIR,
                STAGE_REVIEW,
                STAGE_TESTING,
                InvocationOwner,
                InvocationSpec,
            )

            _stage_by_role = {
                Role.PLANNING: STAGE_MISSION_PLANNING,
                Role.IMPLEMENTATION: STAGE_IMPLEMENTATION,
                Role.TESTING: STAGE_TESTING,
                Role.REVIEW: STAGE_REVIEW,
                Role.REPAIR: STAGE_REPAIR,
            }
            _product_id, _phase_id = self._product_attribution()
            import uuid as _uuid

            _pre_run_id = f"run-{_uuid.uuid4().hex[:12]}"
            _spec = InvocationSpec(
                owner=InvocationOwner(
                    mission_id=self.mission_id,
                    task_id=task.id,
                    product_project_id=_product_id,
                    phase_id=_phase_id,
                ),
                stage=_stage_by_role.get(role, role.value),
                role=role.value,
                prompt=prompt,
                workdir=self.project_path,
                log_dir=log_dir,
                provider=provider_name,
                timeout_s=self.config.provider_timeout_s(provider_name),
                model_requested=getattr(adapter, "model", None) if provider_name == "opencode" else None,
                run_id=_pre_run_id,
                attempt_number=attempt + 1,
                retry_of_run_id=last_run_id,
                cancel_event=self._cancel,
                on_output=on_output,
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
            self._current_adapter, self._current_run_id = adapter, _pre_run_id
            try:
                outcome = await self._invocations().execute(_spec)
            except RuntimeError as exc:
                # Capacity refusal from the shared boundary: wait, do not
                # consume an attempt.
                logger.warning("invocation capacity refused: %s", exc)
                self.db.update(
                    "tasks", task.id, {"status": "failed", "summary": str(exc)[:300], "finished_at": utcnow()}
                )
                await self._wait_for_wake()
                continue
            run_id = outcome.run_id
            last_run_id = run_id
            self._current_adapter, self._current_run_id = None, None
            # Attach Git linkage without touching terminal outcome.
            try:
                commit_after = await git_ops.head_sha(self.project_path) if self.project_path else None
            except Exception:
                commit_after = None
            try:
                self.db.execute(
                    "UPDATE provider_runs SET git_commit_before=?, git_commit_after=? WHERE id=?",
                    (commit_before, commit_after, run_id),
                )
            except Exception:
                logger.debug("git linkage update failed for run %s", run_id, exc_info=True)
            # Adapt the shared outcome to the engine's existing result shape.
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
                used = set(self._mission().providers_used)
                if provider_name not in used:
                    self.db.update(
                        "missions",
                        self.mission_id,
                        {
                            "providers_used": sorted(set(self._mission().providers_used) | {provider_name}),
                            "updated_at": utcnow(),
                        },
                    )
                self.db.update(
                    "tasks", task.id, {"status": "completed", "summary": result.summary, "finished_at": utcnow()}
                )
                self.events.publish(EventType.TASK_COMPLETED, self.mission_id, task_id=task.id, provider=provider_name)
                try:
                    _ckpt_sha = await self._checkpoint(f"agent({provider_name}): {role.value} checkpoint")
                except git_ops.GitCheckpointError as exc:
                    self._fail_checkpoint_exhaustion(str(exc))
                    return None
                if role.value in CODE_WRITING_ROLES:
                    # Bind this run to its checkpoint SHA (or the unchanged
                    # HEAD when the run modified nothing — recorded honestly).
                    from .provenance import record_provider_write, repo_identity

                    _repo_key = ""
                    try:
                        _repo_key = await repo_identity(self.project_path)
                    except Exception:
                        logger.debug("repo key capture failed", exc_info=True)
                    await record_provider_write(
                        self.db,
                        workdir=self.project_path,
                        run_id=run_id,
                        mission_id=self.mission_id,
                        task_id=task.id,
                        product_project_id=_product_id,
                        phase_id=_phase_id,
                        provider=provider_name,
                        role=role.value,
                        base_sha=commit_before,
                        repo_key_value=_repo_key,
                    )
                from .handoff import truncate_coherent as _coherent

                self._completed_work.append(f"[{role.value}] {provider_name}: {_coherent(result.summary, 2000)}")
                return result

            # failure path (health already accounted by InvocationService)
            if result.gate_refused:
                state = ProviderState.AVAILABLE
            else:
                from .providers.classify import FAILURE_TO_STATE as _FTS

                state = _FTS.get(result.failure_class, ProviderState.CRASHED)
            self.db.update(
                "tasks", task.id, {"status": "failed", "summary": result.raw_tail[-300:], "finished_at": utcnow()}
            )
            failed = set(self._mission().providers_failed)
            failed.add(provider_name)
            self.db.update("missions", self.mission_id, {"providers_failed": sorted(failed), "updated_at": utcnow()})
            event_type = (
                EventType.PROVIDER_RATE_LIMITED
                if result.failure_class in (FailureClass.RATE_LIMIT, FailureClass.QUOTA_EXHAUSTED)
                else EventType.PROVIDER_FAILED
            )
            self.events.publish(
                event_type,
                self.mission_id,
                provider=provider_name,
                failure=result.failure_class.value,
                state=state.value,
                role=role.value,
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

            if self.config.get("orchestration.checkpoint_before_provider_switch", True):
                try:
                    await self._checkpoint(f"orchestrator: checkpoint before provider switch ({role.value})")
                except git_ops.GitCheckpointError as exc:
                    self._fail_checkpoint_exhaustion(str(exc))
                    return None
                if role.value in CODE_WRITING_ROLES:
                    # A failed run may still have left partial work behind;
                    # attribute the checkpoint to this run rather than the next.
                    from .provenance import record_provider_write as _record_write2

                    await _record_write2(
                        self.db,
                        workdir=self.project_path,
                        run_id=run_id,
                        mission_id=self.mission_id,
                        task_id=task.id,
                        product_project_id=_product_id,
                        phase_id=_phase_id,
                        provider=provider_name,
                        role=role.value,
                        base_sha=commit_before,
                    )
            last_provider = provider_name
            # A spawn-gate refusal is orchestrator-internal, not provider evidence.
            failover_note = "" if result.gate_refused else _failover_evidence(provider_name, result)
            attempt += 1

        mission = self._mission()
        self._fail(
            f"phase {role.value} exhausted after {attempt} attempt(s); "
            f"providers failed: {', '.join(mission.providers_failed) or 'none eligible'}"
        )
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
        self.events.publish(
            EventType.HUMAN_GATE_CREATED, self.mission_id, gate_id=gate_id, reason=reason, choices=choices
        )
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
        # Observe an already-requested stop before doing any startup work
        # (pause/resume/cancel can arrive in the same tick as the launch).
        if self._check_pause_cancel():
            return
        # Resume/recovery may start mid-sequence (skipping ANALYZING), which
        # used to leave workspace unset and silently disable ALL checkpoints
        # (evidence loss). Re-inspect read-only so checkpoint/verify paths
        # always observe the true workspace.
        if self.workspace is None and self.project_path is not None:
            try:
                self.workspace = await inspect_workspace(self.project_path, self.config.allowed_roots())
            except Exception:
                logger.debug("workspace re-inspection failed", exc_info=True)
            # Re-check after the (yielding) inspection: a stop requested
            # during startup must win over phase execution.
            if self._check_pause_cancel():
                return
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
        try:
            await self._checkpoint("orchestrator: final verified state")
        except git_ops.GitCheckpointError as exc:
            self._fail_checkpoint_exhaustion(str(exc))
            return
        head = await git_ops.head_sha(self.project_path) if self.workspace and self.workspace.is_git_repo else None
        self._set_status(MissionStatus.COMPLETED, current_provider=None, git_head=head)
        self.events.publish(EventType.MISSION_COMPLETED, self.mission_id)

    async def _gate_unattributed_dirt(
        self, project_path: Path, st: Any, second: bool = False, paths: list[str] | None = None
    ) -> None:
        """Block mission start on unattributed dirt (F-PROV-03/S-03/S-04).

        Nothing is staged, committed, or attributed. The operator adopts
        (explicit HUMAN_OPERATOR), commits/cleans manually, then resolves.
        """
        from .provenance import capture_write_start

        if paths is None:
            _, _, paths = await capture_write_start(project_path)
        listed = ", ".join((paths or [])[:8]) or "unlisted paths"
        head = st.head if st else None
        if second:
            await self._create_gate(
                reason="Workspace still has unattributed changes",
                detail=(
                    "The workspace is still dirty after the gate resolution"
                    f" ({listed}). Adopt, commit, or clean"
                    " the remaining changes, then resolve. No provider has been started."
                ),
                choices=["Adopted/committed/cleaned — continue", "Cancel mission"],
                recommended="Adopted/committed/cleaned — continue",
            )
            return
        await self._create_gate(
            reason="Unattributed workspace changes need an operator decision",
            detail=(
                "Pre-existing repository changes were detected before any provider ran"
                f" (HEAD {head or 'unknown'}; {listed}). GG cannot safely attribute"
                " these changes, so nothing was staged, committed, or assigned to a provider."
                " Choose: explicitly adopt them (POST"
                f" /api/missions/{self.mission_id}/adopt-changes, records HUMAN_OPERATOR),"
                " commit/manage them manually, or clean/revert them manually — then resolve"
                " this gate. No provider has been started."
            ),
            choices=["Adopted/committed/cleaned — continue", "Cancel mission"],
            recommended="Adopted/committed/cleaned — continue",
        )

    async def _phase_analyze(self) -> bool:
        project_path = self._project_path()
        self.project_path = project_path
        self.workspace = await inspect_workspace(project_path, self.config.allowed_roots())
        if not self.workspace.is_git_repo:
            await git_ops.init_repo(project_path)
            self.workspace.is_git_repo = True
            try:
                await self._checkpoint("orchestrator: initial repository checkpoint")
            except git_ops.GitCheckpointError as exc:
                self._fail_checkpoint_exhaustion(str(exc))
                return False
        else:
            st = await git_ops.status(project_path)
            if not st.branch:
                # F-10: detached HEAD detected. Check out a dedicated mission branch.
                mission_branch = f"gg/mission-{self.mission_id[:8]}"
                logger.info("detached HEAD detected; checking out mission branch %s", mission_branch)
                await git_ops._git(project_path, "checkout", "-b", mission_branch)
                st = await git_ops.status(project_path)
            if not st.is_clean:
                # Pre-existing dirt is NEVER staged, committed, or attributed
                # automatically — but only at true mission start. Mid-flight
                # re-entry (recovery/resume with provider history) leaves
                # uncommitted provider work behind when checkpointing is
                # broken; that belongs to the checkpoint-exhaustion path,
                # which owns the outcome. Gate only when GG has no history
                # here yet, i.e. the dirt provably predates any GG run.
                _has_history = bool(
                    self.db.query("SELECT id FROM provider_runs WHERE mission_id=? LIMIT 1", (self.mission_id,))
                    or self.db.query("SELECT id FROM write_provenance WHERE mission_id=? LIMIT 1", (self.mission_id,))
                )
                if _has_history:
                    try:
                        await self._checkpoint("orchestrator: checkpoint before mission start (pre-existing changes)")
                    except git_ops.GitCheckpointError as exc:
                        self._fail_checkpoint_exhaustion(str(exc))
                        return False
                else:
                    await self._gate_unattributed_dirt(project_path, st)
                    if self._cancel.is_set() or self._pause.is_set():
                        return False
                    st = await git_ops.status(project_path)
                    if not st.is_clean:
                        from .provenance import capture_write_start as _capture_analyze

                        _, _, _still_blocking = await _capture_analyze(project_path)
                        if _still_blocking:
                            await self._gate_unattributed_dirt(project_path, st, second=True, paths=_still_blocking)
                            if self._cancel.is_set() or self._pause.is_set():
                                return False
                            st = await git_ops.status(project_path)
                            if not st.is_clean:
                                self._fail(
                                    "workspace still has unattributed changes after two operator gates;"
                                    " clean, commit, or adopt them, then retry the mission."
                                    " Provider was NOT invoked."
                                )
                                return False
        self.db.update("projects", self._mission().project_id, {"detected_type": self.workspace.project_type})
        return True

    async def _phase_provider(self, role: Role) -> bool:
        if role == Role.IMPLEMENTATION and self._mission().autonomy == Autonomy.SAFE:
            gates = self.db.query(
                "SELECT id, status, resolution FROM human_gates WHERE mission_id=? "
                "AND reason LIKE 'Approve implementation%' ORDER BY created_at DESC LIMIT 1",
                (self.mission_id,),
            )
            if not gates:
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
            else:
                last_gate = gates[0]
                if last_gate["status"] == "open":
                    await self._wait_for_gate()
                    if self._cancel.is_set() or self._pause.is_set():
                        return False
                elif last_gate["status"] == "resolved":
                    resolution = (last_gate["resolution"] or "").strip().lower()
                    if resolution in ("cancel", "cancel mission"):
                        self.request_cancel()
                        return False
            if self._mission().status == MissionStatus.WAITING_FOR_HUMAN:
                self._set_status(self._status_for_role(role))
        result = await self._run_provider_phase(role)
        if result is None:
            return False
        if role == Role.TESTING:
            self._tests_run.append(f"provider testing phase: {result.summary[:150]}")
        return True

    async def _record_review_provenance(self, review_parsed: bool = True) -> dict[str, Any] | None:
        """Persist who reviewed vs. who implemented, with truthful independence.

        Self-review is an accepted V1 degraded mode when no alternative provider
        is eligible — but it is always recorded and disclosed, never presented
        as independent review. The attempt is bound to its exact reviewed SHA
        range plus the full candidate writer set (Increment 3).
        """
        from .provenance import record_review_attempt

        repo = self.project_path
        try:
            return await record_review_attempt(
                self.db, self.events, mission_id=self.mission_id, repo=repo, review_parsed=review_parsed
            )
        except Exception:
            logger.debug("review attempt recording failed", exc_info=True)
            return None

    def _unparseable_review_count(self) -> int:
        """Count unparseable reviews from persisted state (survives restart)."""
        rows = self.db.query(
            "SELECT COUNT(*) as cnt FROM reviews WHERE mission_id=? AND review_parsed=0",
            (self.mission_id,),
        )
        return rows[0]["cnt"] if rows else 0

    def _prior_findings_context(self) -> str:
        """List prior findings with stable IDs so the reviewer can re-flag or verify each one.

        DOG-02: includes inherited unresolved findings from the retry lineage
        (marked inherited from <mission>). Omission without explicit
        VERIFIED_FIXED evidence never resolves them.
        """
        rows = self.db.query(
            "SELECT id, severity, status, file, description, fingerprint FROM review_findings "
            "WHERE mission_id=? AND status IN ('open','repair_attempted') ORDER BY created_at ASC",
            (self.mission_id,),
        )
        try:
            from .review import inherited_open_findings as _inh3

            _inh_rows = _inh3(self.db, self.mission_id)
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

    async def _phase_review_loop(self) -> bool:
        if not self.config.get("orchestration.review_required", True):
            return True
        max_cycles = int(self.config.get("orchestration.max_repair_cycles", 3))
        max_unparseable_attempts = int(self.config.get("orchestration.max_unparseable_review_attempts", 2))
        while True:
            result = await self._run_provider_phase(Role.REVIEW, extra_context=self._prior_findings_context())
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
                    self._set_status(
                        MissionStatus.UNVERIFIED,
                        blocking_issue="reviewer output unparseable after retries — cannot verify code review",
                        current_provider=None,
                    )
                    return False
                continue

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
                f"- [{f['severity']}] id={f['id']} {f['file'] or ''}: {f['description']} → {f['recommended_fix']}"
                for f in blockers
            )
            repair = await self._run_provider_phase(
                Role.REPAIR, extra_context=f"## Open findings to fix\n{findings_text}"
            )
            if repair is None:
                return False
            mark_findings_repair_attempted(self.db, self.mission_id)
            self._set_status(MissionStatus.REVIEWING)

    async def _phase_final_validation(self) -> bool:
        if self.project_path is None:
            raise RuntimeError("engine project path not initialized")
        # Re-inspect workspace: the provider may have created new project files
        # (pyproject.toml, package.json, etc.) since the initial analysis phase.
        self.workspace = await inspect_workspace(self.project_path, self.config.allowed_roots())
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
            # An environment/tooling-provisioning failure (e.g. a package
            # manager needing network to self-provision inside a sandbox that
            # deliberately has none) can't be fixed by any code change — no
            # amount of REPAIR-role attempts against the repo's own source
            # will make a DNS lookup succeed. Route straight to a clearly
            # labeled blocking state instead of spending repair cycles (real
            # provider spawns, real time) on something structurally
            # unfixable from inside the mission.
            if failures and all(r.likely_environment_issue for r in failures):
                self._set_status(
                    MissionStatus.UNVERIFIED,
                    blocking_issue=(
                        "verification could not run due to an environment/tooling issue, "
                        f"not a code defect — no repair attempt was made:\n{detail[:800]}"
                    ),
                    current_provider=None,
                )
                return False
            # one automated repair attempt via the repair role before giving up
            cycles = self._mission().repair_cycles
            if cycles < int(self.config.get("orchestration.max_repair_cycles", 3)):
                self.db.update("missions", self.mission_id, {"repair_cycles": cycles + 1, "updated_at": utcnow()})
                self._set_status(MissionStatus.REPAIRING)  # current_phase stays FINAL_VALIDATION
                repair = await self._run_provider_phase(
                    Role.REPAIR, extra_context=f"## Verification failures to fix\n{detail}"
                )
                if repair is None:
                    return False
                if not await self._phase_final_validation():
                    return False
                # Repair changed the artifact: pre-repair review evidence no
                # longer applies (P-10/P-11). Re-review the repaired SHA
                # before the mission may complete.
                return await self._rereview_after_final_repair()
            self._set_status(
                MissionStatus.UNVERIFIED, blocking_issue=f"verification failed:\n{detail[:800]}", current_provider=None
            )
            return False
        return True

    async def _rereview_after_final_repair(self) -> bool:
        """Require a fresh independent review of the repaired SHA (P-11).

        No-op when the repair left the reviewed SHA unchanged. Bounded by the
        existing review loop (repair cycles already incremented); new blockers
        route through the normal repair path, which revalidates on return.
        """
        try:
            head = await git_ops.head_sha(self.project_path) if self.project_path else None
        except Exception:
            head = None
        try:
            from .provenance import latest_review_for_mission

            last = latest_review_for_mission(self.db, self.mission_id)
        except Exception:
            last = None
        if not head or not last or last.get("reviewed_head_sha") == head:
            return True
        self._set_status(MissionStatus.REVIEWING)
        ok = await self._phase_review_loop()
        if not ok:
            return False
        # Any further repair revalidates before completion.
        return await self._phase_final_validation()


def request_prompt_safe_command(request: ExecutionRequest) -> list[str]:
    """Deprecated — argv now travels on ExecutionResult; kept for API stability."""
    return [f"<{request.role} prompt len={len(request.prompt)}>"]
