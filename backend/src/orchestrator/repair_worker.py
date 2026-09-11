"""Production worker for bounded autonomous repair (F-REP-01).

The sealed repair engine (``repair.py``) owns classification, budgets,
review, recheck, and stop logic. This module owns orchestration only:
discover runnable durable cycles, claim exactly one worker per cycle,
build REAL production hooks (InvocationService + compiled-v2 + Git +
provenance + criterion recheck), invoke the sealed coordinator, and hand
successful candidates back to canonical acceptance.

Execution model notes:

* The worker pumps from the existing orchestrator scheduler tick — no second
  scheduler framework. ``pump()`` is non-blocking: it only SELECTs runnable
  cycles and spawns one asyncio task per unclaimed cycle.
* Durable ownership uses ``orchestration_operations`` with
  ``kind=repair:{cycle_id}`` (partial unique index on unfinished ops):
  exactly one worker wins the claim; losers attach and skip. Crash recovery
  reuses ``recover_operations`` — a claim with no live run is cancelled at
  boot and the cycle is claimable again. No stuck ownership.
* No DB transaction is held during provider execution: claims are short
  transactions; all long work happens outside them (W-30).
* Lock ordering: the worker never holds ``_advance_lock`` while awaiting
  providers or subprocesses. Recheck/regress run lock-free (one active
  cycle per project makes them race-free); only acceptance continuation
  takes ``_advance_lock`` via ``advance_project`` — the same order as the
  canonical path (advance -> git), never inverted.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import git_ops
from .models import utcnow

if TYPE_CHECKING:
    from .config import Config
    from .db import Database
    from .orchestrator import Orchestrator
    from .providers.registry import ProviderRegistry

logger = logging.getLogger(__name__)

REPAIR_OP_KIND_PREFIX = "repair:"

# Cycle states the worker may pick up (resumable). WAITING_FOR_PROVIDER is
# included only past the re-probe cooldown so idle waiting consumes no
# provider activity (W-16). CREATED covers the narrow crash window between
# cycle insert and classification; the sealed driver terminalizes
# non-repairable CREATED rows without provider work.
RUNNABLE_CYCLE_STATUSES = (
    "CREATED",
    "CLASSIFIED",
    "REPAIRING",
    "REVIEWING",
    "RECHECKING",
    "WAITING_FOR_PROVIDER",
)

# Product states where repair work may proceed. PAUSED/CANCELLED stop new
# attempts (W-10); terminal states need nothing.
RUNNABLE_PRODUCT_STATES = (
    "EXECUTING",
    "REVIEWING",
    "FINAL_ACCEPTANCE",
    "BLOCKED",
    "WAITING_FOR_HUMAN",
)


class WorkerAbort(Exception):
    """Hook-level abort carrying a terminal cycle state (no executor retry)."""

    def __init__(self, status: str, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


def _op_kind(cycle_id: str) -> str:
    return f"{REPAIR_OP_KIND_PREFIX}{cycle_id}"


def worker_cooldown_s(config: Any) -> float:
    try:
        return max(0.0, float(config.get("repair.worker_cooldown_s", 60.0)))
    except (TypeError, ValueError):
        return 60.0


class RepairWorker:
    """Pump-driven worker; one instance per Orchestrator (W-28)."""

    def __init__(self, orch: Orchestrator) -> None:
        self.orch = orch
        self.db: Database = orch.db
        self.config: Config = orch.config
        self.registry: ProviderRegistry = orch.registry
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._started = False
        self._waiting_since: dict[str, float] = {}

    def start(self) -> None:
        """Idempotent start (W-28)."""
        if self._started:
            return
        self._started = True

    async def stop(self) -> None:
        """Cancel running cycle tasks and wait for them (W-29)."""
        self._started = False
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.debug("repair task shutdown error", exc_info=True)
        self._tasks.clear()

    @property
    def running_cycles(self) -> list[str]:
        return [cid for cid, task in self._tasks.items() if not task.done()]

    async def pump(self) -> list[str]:
        """Discover runnable cycles and spawn one task each (non-blocking).

        Returns the cycle ids now owned locally. Zero provider activity
        unless a runnable cycle exists (W-17); idle waiting consumes no
        provider capacity (W-24).
        """
        if not self._started:
            return []
        for cid, task in list(self._tasks.items()):
            if task.done():
                self._tasks.pop(cid, None)
        spawned: list[str] = []
        try:
            rows = self._runnable_cycles()
        except Exception:
            logger.debug("repair pump discovery failed", exc_info=True)
            return []
        now = time.monotonic()
        cooldown = worker_cooldown_s(self.config)
        for row in rows:
            cid = str(row["id"])
            if cid in self._tasks:
                continue
            if row["status"] == "WAITING_FOR_PROVIDER":
                last = self._waiting_since.get(cid, 0.0)
                if now - last < cooldown:
                    continue
                self._waiting_since[cid] = now
            task = asyncio.create_task(self._process_cycle(cid))
            self._tasks[cid] = task
            spawned.append(cid)
        return spawned

    def _runnable_cycles(self) -> list[dict[str, Any]]:
        placeholders = ",".join("?" for _ in RUNNABLE_CYCLE_STATUSES)
        states = ",".join("?" for _ in RUNNABLE_PRODUCT_STATES)
        return self.db.query(
            "SELECT rc.* FROM repair_cycles rc"  # noqa: S608 -- placeholders only
            " JOIN product_projects pp ON pp.id = rc.project_id"
            f" WHERE rc.status IN ({placeholders})"
            " AND (rc.classification = 'IMPLEMENTATION_DEFECT' OR rc.status = 'CREATED')"
            f" AND pp.state IN ({states})"
            " AND COALESCE(pp.paused, 0) = 0"
            " AND COALESCE(rc.cancel_requested, 0) = 0"
            " ORDER BY rc.created_at ASC",
            (*RUNNABLE_CYCLE_STATUSES, *RUNNABLE_PRODUCT_STATES),
        )

    async def _process_cycle(self, cycle_id: str) -> None:
        from . import operations as _ops
        from .repair import RepairCoordinator, RepairCycleStatus

        coord = RepairCoordinator(self.db, self.config)
        cycle = coord.get_cycle(cycle_id)
        if cycle is None:
            return
        project_id = str(cycle["project_id"])
        # Durable claim BEFORE any provider invocation (W-04). Losers attach
        # to the winner's operation and skip (W-05).
        _op, created = _ops.create_or_attach_operation(self.db, project_id, _op_kind(cycle_id))
        op_id = _op.id
        if not created:
            return
        try:
            await self._run_claimed_cycle(coord, cycle_id, project_id)
            _ops.finish_operation(self.db, op_id, _ops.STATE_SUCCEEDED, error_detail="repair worker pass complete")
        except WorkerAbort as exc:
            try:
                coord.finish_cycle(cycle_id, exc.status, exc.reason)
            except Exception:
                logger.debug("repair abort finalize failed for %s", cycle_id, exc_info=True)
            _ops.finish_operation(self.db, op_id, _ops.STATE_FAILED, error_detail=exc.reason[:500])
        except asyncio.CancelledError:
            # Shutdown mid-cycle: leave durable rows for resume; the claim is
            # released below so restart recovery can reclaim immediately.
            _ops.finish_operation(self.db, op_id, _ops.STATE_CANCELLED, error_detail="worker shutdown")
            raise
        except Exception as exc:
            logger.exception("repair worker error for cycle %s", cycle_id)
            try:
                _abandon_unfinished_attempt(self.db, cycle_id, f"worker error: {exc}")
                coord.finish_cycle(cycle_id, RepairCycleStatus.FAILED.value, f"repair worker error: {exc}"[:1000])
            except Exception:
                logger.debug("repair error finalize failed for %s", cycle_id, exc_info=True)
            _ops.finish_operation(self.db, op_id, _ops.STATE_FAILED, error_detail=str(exc)[:500])
        finally:
            self._tasks.pop(cycle_id, None)

    async def _run_claimed_cycle(self, coord: Any, cycle_id: str, project_id: str) -> None:
        from .repair import RepairCycleStatus, execute_repair_cycle

        cycle = coord.require_cycle(cycle_id)
        # Cancellation observed before invocation (W-11).
        if int(cycle.get("cancel_requested") or 0):
            coord.cancel_cycle(cycle_id)
            return
        # Product pause/terminal gate (W-10). No new attempt launches.
        # Both the durable paused flag and the lifecycle state are checked
        # here (post-claim) so a pause/cancel racing discovery still wins.
        product = self.db.get("product_projects", project_id) or {}
        if int(product.get("paused") or 0):
            return
        if str(product.get("state") or "") not in RUNNABLE_PRODUCT_STATES:
            return
        # Re-check cancellation after the product gate: operator cancel
        # racing the claim must still prevent provider invocation.
        cycle = coord.require_cycle(cycle_id)
        if int(cycle.get("cancel_requested") or 0):
            coord.cancel_cycle(cycle_id)
            return
        repo = self.orch.coordinator._target_repo(project_id)
        if repo is None or not repo.exists():
            raise WorkerAbort(RepairCycleStatus.BLOCKED.value, "target repository missing")
        # Manual supersede before execution: never overwrite B (W-12).
        head = await git_ops.head_sha(repo)
        if head:
            stale = coord.supersede_on_candidate_change(project_id, head)
            if cycle_id in stale:
                return
            cycle = coord.require_cycle(cycle_id)
            if cycle["status"] == RepairCycleStatus.STALE.value:
                return
        # Dirty workspace stays fail-closed: no auto-adopt/auto-clean (W-13).
        try:
            from .provenance import capture_write_start

            _base, blocking_dirt, _paths = await capture_write_start(repo)
            if blocking_dirt:
                coord.finish_cycle(
                    cycle_id,
                    RepairCycleStatus.BLOCKED.value,
                    "unattributed working-tree dirt: explicit adoption required before repair",
                )
                return
        except Exception:
            logger.debug("repair dirt gate failed for %s", cycle_id, exc_info=True)
        hooks = ProductionRepairHooks(self.orch, self)
        final = await execute_repair_cycle(self.db, self.config, hooks.scope_builder, hooks, cycle_id)
        # Acceptance continuation: hand the repaired candidate back to the
        # canonical path. Never DELIVER here (W-25); advance_project owns
        # acceptance and delivery gates (W-24/W-26).
        if final.get("status") == RepairCycleStatus.SUCCEEDED.value:
            try:
                await self.orch.coordinator.advance_project(project_id)
            except Exception:
                logger.exception("repair acceptance continuation failed for %s", project_id)


def _abandon_unfinished_attempt(db: Any, cycle_id: str, reason: str) -> None:
    rows = db.query(
        "SELECT id FROM repair_attempts WHERE cycle_id=? AND finished_at IS NULL"
        " ORDER BY attempt_number DESC LIMIT 1",
        (cycle_id,),
    )
    if not rows:
        return
    db.update(
        "repair_attempts",
        str(rows[0]["id"]),
        {"outcome": "FAILED", "finished_at": utcnow().isoformat(),
         "detail_json": f'{{"abandoned": "{reason[:300]}"}}'},
    )


# ---------------------------------------------------------------------------
# Production hooks: real InvocationService + compiled-v2 + Git + recheck
# ---------------------------------------------------------------------------


class ProductionRepairHooks:
    """Real production hook implementations for the sealed repair driver."""

    def __init__(self, orch: Orchestrator, worker: RepairWorker) -> None:
        self.orch = orch
        self.worker = worker
        self.db = orch.db
        self.config = orch.config
        self.registry = orch.registry

    # -- scope ---------------------------------------------------------

    async def scope_builder(self, cycle: dict[str, Any], n: int, base: str, prev: str) -> Any:
        from .repair import build_repair_scope

        ctx = await self._scope_context(cycle, base)
        return build_repair_scope(
            cycle,
            attempt_number=n,
            expected=ctx["expected"],
            observed=ctx["observed"],
            command=ctx["command"],
            exit_code=ctx["exit_code"],
            output_tail=ctx["output_tail"],
            base_sha=base,
            relevant_files=ctx["relevant_files"],
            protected_files=ctx["protected_files"],
            writers=ctx["writers"],
            previous_attempt_summary=prev,
        )

    async def _scope_context(self, cycle: dict[str, Any], base_sha: str) -> dict[str, Any]:
        project_id = str(cycle["project_id"])
        plan = self.orch.coordinator._current_plan(project_id)
        req_id = cycle.get("target_requirement_id")
        crit_id = cycle.get("target_criterion_id")
        req_text, crit_text, verify_cmd = "", "", ""
        if plan is not None:
            for req in plan.requirements:
                if req_id and req.id != req_id:
                    continue
                req_text = f"{req.title}\n{req.description}"
                for criterion in req.acceptance:
                    if crit_id and criterion.id != crit_id:
                        continue
                    crit_text = f"{criterion.description}\n{criterion.verify}"
                    verify_cmd = criterion.verify
                    if req_id and crit_id:
                        break
                if req_text and (not req_id or True):
                    if crit_id and not crit_text:
                        continue
                    break
        trigger_type = str(cycle.get("trigger_type") or "")
        evidence_id = str(cycle.get("trigger_evidence_id") or "")
        expected = crit_text or req_text or "contract holds"
        observed: str = ""
        command = verify_cmd
        exit_code: int | None = None
        output_tail = ""
        relevant: list[str] = []
        if trigger_type == "CRITERION_FAILED":
            rows = self.db.query("SELECT * FROM criterion_attempts WHERE id=?", (evidence_id,))
            if rows:
                row = rows[0]
                command = str(row.get("command") or verify_cmd)
                try:
                    exit_code = int(row["exit_code"]) if row.get("exit_code") is not None else None
                except (TypeError, ValueError):
                    exit_code = None
                output_tail = str(row.get("output_tail") or "")
                observed = f"criterion {row.get('criterion_id')} failed: {command} (exit={exit_code})"
        elif trigger_type == "VERIFICATION_FAILED":
            rows = self.db.query("SELECT * FROM verification_attempts WHERE id=?", (evidence_id,))
            if rows:
                row = rows[0]
                command = str(row.get("commands_json") or "")[:300]
                try:
                    exit_code = int(row["exit_code"]) if row.get("exit_code") is not None else None
                except (TypeError, ValueError):
                    exit_code = None
                output_tail = str(row.get("summary") or "")
                observed = f"verification failed: {output_tail[:500]}"
        elif trigger_type == "REVIEW_FINDING":
            rows = self.db.query("SELECT * FROM review_findings WHERE id=?", (cycle.get("target_finding_id") or "",))
            if rows:
                row = rows[0]
                expected = str(row.get("recommended_fix") or row.get("description") or expected)
                observed = f"{row.get('severity')} {row.get('category')} finding: {row.get('description')}"
                output_tail = str(row.get("description") or "")
                if row.get("file"):
                    relevant.append(str(row["file"]))
        return {
            "expected": expected[:1000],
            "observed": (observed or output_tail[:500])[:1000],
            "command": command[:500],
            "exit_code": exit_code,
            "output_tail": (output_tail or "")[:3000],
            "relevant_files": relevant,
            "protected_files": await self._protected_files(project_id),
            "writers": await self._candidate_writers(project_id, cycle, base_sha),
        }

    async def _protected_files(self, project_id: str) -> list[str]:
        """Acceptance-contract files the repair must never touch (R-24/R-44).

        Heuristic + config override: test files and toolchain manifests.
        Independent review remains the main control; this is defense in depth.
        """
        configured = self.config.get("repair.protected_paths", None)
        if isinstance(configured, list) and configured:
            return [str(p) for p in configured][:100]
        repo = self.orch.coordinator._target_repo(project_id)
        if repo is None or not repo.exists():
            return []
        protected: list[str] = []
        manifests = {"package.json", "pyproject.toml", "go.mod", "Cargo.toml", "pom.xml", "build.gradle"}
        for path in repo.rglob("*"):
            if len(protected) >= 100:
                break
            if not path.is_file() or ".git/" in str(path) or ".orchestrator/" in str(path):
                continue
            rel = str(path.relative_to(repo))
            name = path.name.lower()
            if (
                rel in manifests
                or rel.startswith("tests/")
                or rel.startswith("test/")
                or "/tests/" in rel
                or "/test/" in rel
                or "test" in name
                or "spec" in name
            ):
                protected.append(rel)
        return protected

    async def _candidate_writers(self, project_id: str, cycle: dict[str, Any], base_sha: str) -> list[str]:
        from .provenance import provider_writers, range_writers
        from .repair import RepairCoordinator

        writers: set[str] = set()
        try:
            repo = self.orch.coordinator._target_repo(project_id)
            trigger_sha = str(cycle.get("trigger_sha") or base_sha)
            if repo is not None:
                info = await range_writers(self.db, repo, trigger_sha, base_sha)
                writers.update(provider_writers(info.get("writers", [])))
        except Exception:
            logger.debug("repair writer range lookup failed", exc_info=True)
        coord = RepairCoordinator(self.db, self.config)
        for attempt in coord.attempts(str(cycle["id"])):
            if attempt.get("provider"):
                writers.add(str(attempt["provider"]))
        return sorted(writers)

    # -- provider selection (existing priority/eligibility, W-18) --------

    def _select_provider(self, role: str, exclude: set[str] | None = None) -> str | None:
        priorities = self.config.priority_for(role) or list(self.registry.adapters.keys())
        excluded = exclude or set()
        for name in priorities:
            try:
                eligible = self.registry.is_eligible(name)
            except Exception:
                logger.debug("repair provider eligibility check failed for %s", name, exc_info=True)
                continue
            if not eligible:
                continue
            if name in excluded:
                # Writer-set exclusion is absolute: an excluded provider can
                # never certify, even as a fallback (R-18).
                continue
            return name
        return None

    # -- invocation core -------------------------------------------------

    async def _invoke(
        self,
        *,
        role: str,
        stage: str,
        provider: str,
        prompt: str,
        meta: dict[str, Any],
        repo: Path,
        project_id: str,
        phase_id: str | None,
        attempt_number: int,
    ) -> Any:
        from .invocations import InvocationOwner, InvocationService, InvocationSpec

        log_dir = repo / ".orchestrator" / "logs" / f"repair-{role}"
        log_dir.mkdir(parents=True, exist_ok=True)
        spec = InvocationSpec(
            owner=InvocationOwner(product_project_id=project_id, phase_id=phase_id),
            stage=stage,
            role=role,
            prompt=prompt,
            workdir=repo,
            log_dir=log_dir,
            provider=provider,
            timeout_s=self.config.provider_timeout_s(provider),
            attempt_number=attempt_number,
            prompt_template_version=meta.get("prompt_template_version"),
            context_policy_version=meta.get("context_policy_version"),
            context_blocks_json=meta.get("context_blocks_json"),
            context_warnings_json=meta.get("context_warnings_json"),
            context_budget=meta.get("context_budget"),
            context_used=meta.get("context_used"),
            context_remaining=meta.get("context_remaining"),
            context_repeated_ratio=meta.get("context_repeated_ratio"),
            context_plan_revision=meta.get("context_plan_revision"),
        )
        svc = InvocationService(self.db, self.registry, self.config)
        return await svc.execute(spec)

    def _compile(
        self,
        *,
        role: str,
        stage: str,
        legacy_prompt: str,
        project_id: str,
        provider: str,
        base_sha: str,
        candidate_sha: str | None,
        requirement_ids: list[str],
        failure_text: str,
        failure_command: str,
        failure_exit: int | None,
        task_objective: str,
        attempt: int,
        workspace_scope: list[str],
        git_files: list[str],
        plan_revision: int | None = None,
    ) -> tuple[str, dict[str, Any]]:
        from .context_compiler import ContextCompileSpec, prepare_invocation_context

        spec = ContextCompileSpec(
            role=role,
            stage=stage,
            product_project_id=project_id,
            provider=provider,
            base_sha=base_sha,
            candidate_sha=candidate_sha,
            task_objective=task_objective,
            requirement_ids=list(requirement_ids),
            failure_text=failure_text,
            failure_command=failure_command,
            failure_exit_code=failure_exit,
            attempt=attempt,
            workspace_scope=list(workspace_scope),
            git_files_changed=list(git_files),
            plan_revision=plan_revision,
        )
        # Compiled-v2 strict: compilation failure blocks invocation with no
        # silent legacy fallback (W-19).
        return prepare_invocation_context(legacy_prompt=legacy_prompt, spec=spec, db=self.db, config=self.config)

    # -- RepairHooks protocol --------------------------------------------

    async def current_head(self, scope: Any) -> str:
        repo = self.orch.coordinator._target_repo(self._project_of(scope))
        head = await git_ops.head_sha(repo) if repo else None
        return (head or "").lower()

    def _project_of(self, scope: Any) -> str:
        cycle = self.db.get("repair_cycles", str(scope.cycle_id))
        return str((cycle or {}).get("project_id") or "")

    async def is_descendant(self, scope: Any, base_sha: str, result_sha: str) -> bool:
        repo = self.orch.coordinator._target_repo(self._project_of(scope))
        if repo is None:
            return False
        try:
            return await git_ops.is_ancestor(repo, base_sha, result_sha)
        except Exception:
            return False

    async def repair(self, scope: Any) -> Any:
        from .repair import ProviderOperationalError

        project_id = self._project_of(scope)
        # Late cancel/pause gate: an operator action racing the claim must
        # still prevent provider invocation (W-10/W-11). Checked here, just
        # before provider selection, in addition to the pre-execute gate.
        cycle_row = self.db.get("repair_cycles", str(scope.cycle_id)) or {}
        if int(cycle_row.get("cancel_requested") or 0):
            raise WorkerAbort("CANCELLED", "operator cancelled repair cycle")
        product_row = self.db.get("product_projects", project_id) or {}
        if int(product_row.get("paused") or 0):
            raise ProviderOperationalError("product paused; repair deferred without budget use")
        plan = self.orch.coordinator._current_plan(project_id)
        if plan is None:
            raise WorkerAbort("BLOCKED", "product plan missing; cannot scope repair")
        repo = self.orch.coordinator._target_repo(project_id)
        if repo is None or not repo.exists():
            raise WorkerAbort("BLOCKED", "target repository missing")
        provider = self._select_provider("repair")
        if provider is None:
            # No eligible repair provider: bounded wait, no budget consumed.
            raise ProviderOperationalError("no eligible repair provider available")
        requirement_ids = [scope.requirement_id] if scope.requirement_id else []
        legacy_prompt = (
            f"Repair the defect in {repo} without changing the acceptance contract.\n"
            f"Expected: {scope.expected}\nObserved: {scope.observed}\n"
            f"Failing command: {scope.command}\nBase SHA: {scope.base_sha}\n"
            f"Attempt {scope.attempt_number}/{scope.max_attempts}.\n"
            "Do not repeat a previous approach the evidence proves ineffective. "
            "Do not change acceptance criteria, tests, or security policy. "
            "Do not disable tests or weaken validation."
        )
        try:
            prompt, meta = self._compile(
                role="repairer", stage="repair", legacy_prompt=legacy_prompt, project_id=project_id,
                provider=provider, base_sha=scope.base_sha, candidate_sha=None,
                requirement_ids=requirement_ids, failure_text=f"{scope.observed}\n{scope.output_tail}",
                failure_command=scope.command, failure_exit=scope.exit_code,
                task_objective=f"Fix: {scope.expected}",
                attempt=scope.attempt_number, workspace_scope=scope.relevant_files,
                git_files=scope.relevant_files,
            )
        except Exception as exc:
            raise ProviderOperationalError(f"repair context compilation failed: {exc}") from exc
        commit_before = await git_ops.head_sha(repo)
        outcome = await self._invoke(
            role="repair", stage="repair", provider=provider, prompt=prompt, meta=meta, repo=repo,
            project_id=project_id, phase_id=self._phase_of(project_id),
            attempt_number=scope.attempt_number,
        )
        self._raise_for_outcome(outcome, role="repair")
        result_sha = await self._checkpoint_result(repo, project_id, provider, "repair", outcome.run_id,
                                                   commit_before or scope.base_sha)
        try:
            touched = await git_ops.diff_names(repo, scope.base_sha, result_sha, limit=100)
        except Exception:
            touched = []
        from .repair import RepairResult

        return RepairResult(result_sha=result_sha, provider=provider, touched_files=touched,
                            summary=str(outcome.summary or "")[:1000], provider_run_id=outcome.run_id)

    async def review(self, scope: Any, result_sha: str) -> Any:
        from .repair import ProviderOperationalError, RepairCoordinator, ReviewVerdict
        from .review import parse_review_output

        project_id = self._project_of(scope)
        repo = self.orch.coordinator._target_repo(project_id)
        writers = set(scope.writers) | {
            str(a["provider"])
            for a in RepairCoordinator(self.db, self.config).attempts(str(scope.cycle_id))
            if a.get("provider")
        }
        reviewer = self._select_provider("review", exclude=writers)
        if reviewer is None:
            # No independent reviewer exists: never fake success (R-31).
            raise ProviderOperationalError("no independent reviewer available outside writer set")
        requirement_ids = [scope.requirement_id] if scope.requirement_id else []
        legacy_prompt = (
            "Independently review the repaired candidate against the contract.\n"
            f"Base SHA: {scope.base_sha}\nCandidate SHA: {result_sha}\n"
            f"Repair intent: {scope.expected}\nFailing command was: {scope.command}\n"
            "Output exactly one REVIEW_FINDINGS_JSON line."
        )
        try:
            prompt, meta = self._compile(
                role="reviewer", stage="review", legacy_prompt=legacy_prompt, project_id=project_id,
                provider=reviewer, base_sha=scope.base_sha, candidate_sha=result_sha,
                requirement_ids=requirement_ids, failure_text="",
                failure_command="", failure_exit=None,
                task_objective=f"Review repair for: {scope.expected}",
                attempt=scope.attempt_number, workspace_scope=scope.relevant_files,
                git_files=scope.relevant_files,
            )
        except Exception as exc:
            raise ProviderOperationalError(f"review context compilation failed: {exc}") from exc
        if repo is None:
            raise WorkerAbort("BLOCKED", "target repository missing")
        commit_before = await git_ops.head_sha(repo)
        outcome = await self._invoke(
            role="review", stage="review", provider=reviewer, prompt=prompt, meta=meta, repo=repo,
            project_id=project_id, phase_id=self._phase_of(project_id),
            attempt_number=scope.attempt_number,
        )
        self._raise_for_outcome(outcome, role="review")
        # A rogue reviewer must not silently rewrite code: checkpoint + provenance.
        await self._checkpoint_result(repo, project_id, reviewer, "review", outcome.run_id,
                                      commit_before or scope.base_sha)
        text = f"{outcome.assistant_text or ''}\n{outcome.raw_tail or ''}"
        try:
            parsed_ok, findings = parse_review_output(text)
        except Exception:
            parsed_ok, findings = False, []
        if not parsed_ok:
            return ReviewVerdict(passed=False, reviewer=reviewer, detail="review output unparseable")
        blockers = [
            f for f in findings
            if str(f.get("severity", "")).upper() in ("BLOCKER", "HIGH")
            or (
                str(f.get("severity", "")).upper() == "MEDIUM"
                and str(f.get("category", "")).lower() in ("requirements", "correctness", "security", "tests")
            )
        ]
        if blockers:
            first = blockers[0]
            return ReviewVerdict(
                passed=False, reviewer=reviewer,
                detail=f"{len(blockers)} blocking finding(s); first: {first.get('description', '')}"[:2000],
            )
        return ReviewVerdict(passed=True, reviewer=reviewer, detail=f"{len(findings)} non-blocking note(s)")

    async def recheck(self, scope: Any, result_sha: str) -> Any:
        from .repair import RecheckVerdict

        project_id = self._project_of(scope)
        plan = self.orch.coordinator._current_plan(project_id)
        repo = self.orch.coordinator._target_repo(project_id)
        if plan is None or repo is None:
            raise WorkerAbort("BLOCKED", "plan or repository missing for recheck")
        # Exact trigger evidence rerun: force a NEW immutable attempt against
        # the exact repaired SHA (no cache reuse).
        await self.orch.coordinator._evaluate_requirement_criteria_locked(
            project_id, plan, repo, result_sha, force=True
        )
        rows = self.db.query(
            "SELECT * FROM criterion_attempts WHERE project_id=? AND criterion_id=? AND checked_sha=?"
            " ORDER BY created_at DESC LIMIT 1",
            (project_id, scope.criterion_id or "", result_sha.lower()),
        )
        if not rows:
            raise WorkerAbort("FAILED", "recheck produced no evidence for the repaired SHA")
        row = rows[0]
        passed = str(row.get("result") or "") == "SATISFIED"
        return RecheckVerdict(passed=passed, attempt_id=str(row["id"]), checked_sha=result_sha,
                              output=str(row.get("output_tail") or "")[:1000])

    async def regress(self, scope: Any, result_sha: str) -> Any:
        from .repair import RecheckVerdict
        from .verify import run_verification
        from .workspace import inspect_workspace

        project_id = self._project_of(scope)
        repo = self.orch.coordinator._target_repo(project_id)
        if repo is None:
            raise WorkerAbort("BLOCKED", "target repository missing for regression check")
        info = await inspect_workspace(repo)
        report = await run_verification(
            info, self.db, self.orch.events, "", repo, sha=result_sha, product_project_id=project_id
        )
        return RecheckVerdict(passed=report.all_passed, attempt_id=f"regress-{result_sha[:12]}",
                              checked_sha=result_sha, output=report.summary()[:1000])

    # -- small helpers ---------------------------------------------------

    def _phase_of(self, project_id: str) -> str | None:
        return None

    def _raise_for_outcome(self, outcome: Any, *, role: str) -> None:
        from .repair import ProviderOperationalError

        if outcome.ok:
            return
        failure = str(getattr(outcome, "failure_class", "") or "")
        state = str(getattr(outcome, "provider_state", "") or "")
        summary = str(getattr(outcome, "summary", "") or outcome)
        if failure in ("AUTH", "HUMAN_INPUT") or state == "AUTH_REQUIRED":
            # Credentials/human choice discovered mid-cycle: stop coding,
            # escalate to the operator (R-30).
            raise WorkerAbort("WAITING_FOR_HUMAN", f"{role} provider requires credentials/human input: {summary}"[:500])
        raise ProviderOperationalError(f"{role} provider invocation failed ({failure or state}): {summary}"[:500])

    async def _checkpoint_result(
        self,
        repo: Path,
        project_id: str,
        provider: str,
        role: str,
        run_id: str,
        base_sha: str,
    ) -> str:
        """Checkpoint provider work under the Git lock + record provenance."""
        head_before = await git_ops.head_sha(repo)
        result_sha = head_before or base_sha
        try:
            async with self.orch.locks.git():
                sha = await git_ops.checkpoint(repo, f"{role} by {provider}")
                if sha:
                    result_sha = sha
        except Exception:
            logger.debug("repair checkpoint failed for %s", provider, exc_info=True)
        try:
            from .provenance import record_provider_write, repo_identity

            await record_provider_write(
                self.db, workdir=repo, run_id=run_id, product_project_id=project_id,
                provider=provider, role=role, base_sha=base_sha,
                repo_key_value=await repo_identity(repo),
            )
        except Exception:
            logger.debug("repair provenance failed for %s", provider, exc_info=True)
        try:
            self.db.execute(
                "UPDATE provider_runs SET git_commit_before=?, git_commit_after=? WHERE id=?",
                (base_sha, result_sha, run_id),
            )
        except Exception:
            logger.debug("repair run SHA linkage failed for %s", run_id, exc_info=True)
        return result_sha.lower()
