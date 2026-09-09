"""Idea-to-Product lifecycle coordinator.

A project-level coordinator ABOVE the existing mission engine. It never
schedules provider work itself: every roadmap phase becomes exactly one
mission executed by the certified MissionEngine (scheduler, reservations,
checkpoints, review/repair, verification, Human Gates, restart recovery).

Safety properties:
- Durable state before action; every advance step is idempotent.
- One mission per phase (phase.mission_id); retries create a new mission
  only after the previous one reached a terminal state.
- Same-repo phases serialize through the existing workspace-ownership
  exclusion — never two active writers on one target repo.
- Terminal project states are never mutated; DELIVERED requires evidence.
- Single-process advancement (scheduler tick + boot recovery share one
  asyncio lock); no concurrent coordinators on the same project.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import shlex
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import git_ops
from .criterion import confined_to_repo, evaluate_criteria, is_executable_command
from .models import (
    ACTIVE_PRODUCT_STATUSES,
    TERMINAL_PHASE_STATUSES,
    TERMINAL_PRODUCT_STATUSES,
    TERMINAL_STATUSES,
    AcceptanceState,
    EventType,
    FailureClass,
    MissionStatus,
    ProductStatus,
    ProjectPhaseStatus,
    Role,
    utcnow,
)
from .process import run_process
from .product_plan import (
    AcceptanceCriterion,
    ProductPlan,
    build_plan_repair_prompt,
    build_planner_prompt,
    extract_product_plan,
    validate_product_plan,
)
from .providers.base import ExecutionRequest, ExecutionResult
from .sandbox import run_sandboxed, sandbox_available
from .security import redact, validate_workspace_path
from .verify import run_verification
from .workspace import inspect_workspace

if TYPE_CHECKING:
    from .config import Config
    from .events import EventBus
    from .orchestrator import Orchestrator

logger = logging.getLogger(__name__)

TERMINAL_MISSION_VALUES = frozenset(s.value for s in TERMINAL_STATUSES)
MAX_PLAN_ATTEMPTS = 3


class ProductValidationError(ValueError):
    """User-correctable product configuration problem (paths, plans)."""


def _slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:48] or "product"


class ProjectCoordinator:
    """Advances product projects through plan → execute → accept → deliver."""

    def __init__(self, orchestrator: Orchestrator):
        self.orch = orchestrator
        self.db = orchestrator.db
        self.events: EventBus = orchestrator.events
        self.registry = orchestrator.registry
        self.config: Config = orchestrator.config
        self._advance_lock = asyncio.Lock()

    # -- project CRUD ------------------------------------------------------
    def create_project(
        self,
        name: str,
        idea: str,
        constraints: str = "",
        auto_execute: bool = False,
        require_plan_approval: bool = True,
        target_repo_path: str = "",
    ) -> dict[str, Any]:
        if not name.strip():
            raise ValueError("project name is required")
        if not idea.strip():
            raise ValueError("idea is required")
        if target_repo_path.strip():
            try:
                validate_workspace_path(target_repo_path.strip(), self.orch.config.allowed_roots())
            except ValueError as exc:
                raise ProductValidationError(f"target repository path rejected: {exc}") from exc
        project_id = uuid.uuid4().hex[:16]
        now = utcnow()
        self.db.insert(
            "product_projects",
            {
                "id": project_id,
                "name": name.strip(),
                "idea": idea.strip(),
                "constraints_text": constraints or "",
                "state": ProductStatus.DRAFT.value,
                "acceptance_state": AcceptanceState.PENDING.value,
                "auto_execute": 1 if auto_execute else 0,
                "require_plan_approval": 1 if require_plan_approval else 0,
                "target_repo_path": target_repo_path or "",
                "plan_revision": 0,
                "created_at": now,
                "updated_at": now,
            },
        )
        self.events.publish(EventType.PRODUCT_PROJECT_CREATED, None, product_project_id=project_id, name=name.strip())
        return self.get_project(project_id) or {}

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        row = self.db.get("product_projects", project_id)
        if not row:
            return None
        row["phases"] = self.db.query(
            "SELECT * FROM project_phases WHERE project_id=? ORDER BY created_at ASC, rowid ASC",
            (project_id,),
        )
        row["gates"] = self.db.query(
            "SELECT * FROM project_gates WHERE project_id=? ORDER BY created_at ASC, rowid ASC",
            (project_id,),
        )
        row["evidence"] = self.db.query(
            "SELECT * FROM requirement_evidence WHERE project_id=? ORDER BY requirement_id ASC",
            (project_id,),
        )
        row["criterion_results"] = self.db.query(
            "SELECT * FROM criterion_results WHERE project_id=? ORDER BY criterion_id ASC",
            (project_id,),
        )
        row["waivers"] = self.db.query(
            "SELECT * FROM acceptance_waivers WHERE project_id=? ORDER BY created_at ASC",
            (project_id,),
        )
        plan = self.db.query(
            "SELECT * FROM plan_revisions WHERE project_id=? ORDER BY revision DESC LIMIT 1",
            (project_id,),
        )
        row["plan"] = json.loads(plan[0]["plan_json"]) if plan else None
        row["plan_revision_count"] = self.db.query(
            "SELECT COUNT(*) AS n FROM plan_revisions WHERE project_id=?", (project_id,)
        )[0]["n"]
        return row

    def list_projects(self) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT * FROM product_projects ORDER BY created_at DESC")
        for row in rows:
            counts = self.db.query(
                """SELECT status, COUNT(*) AS n FROM project_phases
                   WHERE project_id=? GROUP BY status""",
                (row["id"],),
            )
            row["phase_counts"] = {c["status"]: c["n"] for c in counts}
            row["open_gates"] = self.db.query(
                "SELECT COUNT(*) AS n FROM project_gates WHERE project_id=? AND status='open'",
                (row["id"],),
            )[0]["n"]
        return rows

    def _set_state(
        self,
        project_id: str,
        state: ProductStatus,
        reason: str = "",
        finished: bool = False,
    ) -> None:
        row = self.db.get("product_projects", project_id)
        if not row:
            return
        if row["state"] in {s.value for s in TERMINAL_PRODUCT_STATUSES}:
            logger.warning("refusing to mutate terminal project %s (%s)", project_id, row["state"])
            return
        update: dict[str, Any] = {"state": state.value, "updated_at": utcnow()}
        if reason:
            update["blocking_reason"] = reason
        if finished or state in TERMINAL_PRODUCT_STATUSES:
            update["finished_at"] = utcnow()
        self.db.update("product_projects", project_id, update)
        self.events.publish(
            EventType.PRODUCT_STATUS_CHANGED,
            None,
            product_project_id=project_id,
            status=state.value,
            reason=reason,
        )

    # -- planning ----------------------------------------------------------
    def _current_plan(self, project_id: str) -> ProductPlan | None:
        plan = self.db.query(
            "SELECT plan_json FROM plan_revisions WHERE project_id=? ORDER BY revision DESC LIMIT 1",
            (project_id,),
        )
        if not plan:
            return None
        return ProductPlan.model_validate(json.loads(plan[0]["plan_json"]))

    # -- acceptance waivers ------------------------------------------------
    #: Finding categories that gate product delivery while unresolved.
    BLOCKING_FINDING_CATEGORIES = frozenset({"requirements", "correctness", "security", "tests"})

    @staticmethod
    def _criterion_content_hash(criterion: AcceptanceCriterion) -> str:
        payload = f"{criterion.id}\x00{criterion.description}\x00{criterion.verify}"
        return hashlib.sha256(payload.encode()).hexdigest()

    @staticmethod
    def _finding_content_hash(row: dict[str, Any]) -> str:
        fields = (row.get("id", ""), row.get("severity", ""), row.get("category", ""), row.get("description", ""))
        return hashlib.sha256("\x00".join(fields).encode()).hexdigest()

    async def create_waiver(
        self,
        project_id: str,
        target_kind: str,
        target_id: str,
        reason: str,
        actor: str = "human",
    ) -> dict[str, Any]:
        """Record an authorized acceptance waiver (auditable, versioned).

        A waiver excuses one criterion or finding from the DELIVERED gate.
        It records why and under which plan revision — never silently, never
        retroactively editable. It is also bound to the exact content of the
        target at creation time: a later plan revision that reuses the same
        criterion id for a different check must not silently inherit this
        waiver (see _waived_targets).

        `actor` is a self-reported label for the audit trail, not a verified
        identity — this tool authenticates callers with one shared bearer
        token (see api/auth.py), which proves "holds the token," not "is a
        specific person." Anyone able to call this endpoint can set `actor`
        to any string. Treat it as a note, never as a security control.
        """
        async with self._advance_lock:
            row = self.db.get("product_projects", project_id)
            if not row:
                raise KeyError(f"product project {project_id} not found")
            if row["state"] in {s.value for s in TERMINAL_PRODUCT_STATUSES}:
                raise ValueError(f"project {project_id} is terminal ({row['state']})")
            if target_kind not in ("criterion", "finding"):
                raise ProductValidationError("target_kind must be 'criterion' or 'finding'")
            if not target_id.strip():
                raise ProductValidationError("target_id is required")
            if not reason.strip():
                raise ProductValidationError("waiver reason is required")
            content_hash = self._validate_waiver_target(project_id, target_kind, target_id.strip())
            waiver_id = uuid.uuid4().hex[:16]
            self.db.execute(
                """INSERT INTO acceptance_waivers(id, project_id, target_kind, target_id, reason, actor,
                   plan_revision, content_hash, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(project_id, target_kind, target_id) DO UPDATE SET
                     reason=excluded.reason, actor=excluded.actor, plan_revision=excluded.plan_revision,
                     content_hash=excluded.content_hash, created_at=excluded.created_at""",
                (
                    waiver_id,
                    project_id,
                    target_kind,
                    target_id.strip(),
                    reason.strip(),
                    actor,
                    int(row.get("plan_revision") or 0),
                    content_hash,
                    utcnow().isoformat(),
                ),
            )
            self.events.publish(
                EventType.PRODUCT_ACCEPTANCE_RECORDED,
                None,
                product_project_id=project_id,
                waiver=f"{target_kind}:{target_id.strip()}",
                reason=reason.strip()[:200],
            )
            return {"ok": True, "target": f"{target_kind}:{target_id.strip()}"}

    def _validate_waiver_target(self, project_id: str, target_kind: str, target_id: str) -> str:
        """Confirm the target exists now and return its current content hash."""
        if target_kind == "criterion":
            plan = self._current_plan(project_id)
            criteria = {a.id: a for r in plan.requirements for a in r.acceptance} if plan is not None else {}
            criterion = criteria.get(target_id)
            if criterion is None:
                raise ProductValidationError(f"unknown criterion {target_id} in the current plan")
            return self._criterion_content_hash(criterion)
        mission_ids = [
            p["mission_id"]
            for p in self.db.query("SELECT mission_id FROM project_phases WHERE project_id=?", (project_id,))
            if p.get("mission_id")
        ]
        for mid in mission_ids:
            rows = self.db.query("SELECT * FROM review_findings WHERE id=? AND mission_id=?", (target_id, mid))
            if rows:
                return self._finding_content_hash(rows[0])
        raise ProductValidationError(f"unknown finding {target_id} in this project")

    def _waived_targets(self, project_id: str) -> dict[str, str]:
        """Map ``"kind:id"`` -> content hash at waiver-creation time.

        Callers must compare against the target's CURRENT content hash before
        honoring a waiver — a stale hash means the underlying criterion/finding
        changed since the waiver was authorized and the waiver no longer
        applies (F-LIFE-01: never silently reapply across plan revisions).
        """
        rows = self.db.query(
            "SELECT target_kind, target_id, content_hash FROM acceptance_waivers WHERE project_id=?", (project_id,)
        )
        return {f"{r['target_kind']}:{r['target_id']}": r.get("content_hash") or "" for r in rows}

    async def generate_plan(self, project_id: str) -> dict[str, Any]:
        """Run the planning provider with bounded repair; persist a revision."""
        row = self.db.get("product_projects", project_id)
        if not row:
            raise KeyError(f"product project {project_id} not found")
        if row["state"] in {s.value for s in TERMINAL_PRODUCT_STATUSES}:
            raise ValueError(f"project {project_id} is terminal ({row['state']})")
        self._set_state(project_id, ProductStatus.PLANNING, reason="")
        prompt = build_planner_prompt(row["idea"], row.get("constraints_text") or "")
        raw_output = ""
        errors: list[str] = ["no planner output yet"]
        plan_data: dict[str, Any] | None = None
        for attempt in range(MAX_PLAN_ATTEMPTS):
            if attempt > 0:
                prompt = build_plan_repair_prompt(raw_output, errors)
            raw_output = await self._run_planning_provider(prompt, project_id)
            plan_data = extract_product_plan(raw_output)
            if plan_data is None:
                errors = ["planner output contained no PRODUCT_PLAN_JSON block"]
                continue
            errors = validate_product_plan(plan_data)
            if not errors:
                break
            plan_data = None
        if plan_data is None:
            self._set_state(
                project_id,
                ProductStatus.BLOCKED,
                reason=f"planner failed after {MAX_PLAN_ATTEMPTS} attempts: {'; '.join(errors)}",
            )
            return {"ok": False, "errors": errors}
        revision = int(row.get("plan_revision") or 0) + 1
        self.db.insert(
            "plan_revisions",
            {
                "id": uuid.uuid4().hex[:16],
                "project_id": project_id,
                "revision": revision,
                "plan_json": plan_data,
                "created_by": "planner",
                "reason": "initial plan" if revision == 1 else "regenerated",
                "created_at": utcnow(),
            },
        )
        self.db.update("product_projects", project_id, {"plan_revision": revision, "updated_at": utcnow()})
        self._set_state(project_id, ProductStatus.PLAN_READY, reason="")
        self.events.publish(EventType.PRODUCT_PLAN_READY, None, product_project_id=project_id, revision=revision)
        return {"ok": True, "revision": revision, "plan": plan_data}

    async def _run_planning_provider(self, prompt: str, project_id: str) -> str:
        provider_name = self._select_planning_provider()
        if provider_name is None:
            raise RuntimeError("no planning provider available")
        adapter = self.registry.get_adapter(provider_name)
        if adapter is None:  # pragma: no cover - defensive
            raise RuntimeError(f"planning adapter {provider_name} missing")
        workdir = Path(tempfile.gettempdir())
        request = ExecutionRequest(
            prompt=prompt,
            workdir=workdir,
            role=Role.PLANNING.value,
            timeout_s=self.config.provider_timeout_s(provider_name),
            run_id=f"plan-{uuid.uuid4().hex[:8]}",
            log_dir=workdir,
        )
        collected: list[str] = []
        self.registry.mark_busy(provider_name)
        try:
            result: ExecutionResult = await adapter.execute(request, collected.append)
        except Exception as exc:
            self.registry.record_failure(provider_name, FailureClass.CRASH, 0.0, str(exc)[:500])
            raise
        self.registry.record_success(provider_name, result.duration_s)
        return result.assistant_text or result.raw_tail or "\n".join(collected)

    def _select_planning_provider(self) -> str | None:
        priorities: list[str] = self.config.priority_for("planning")
        for name in priorities:
            if self.registry.get_adapter(name) is None:
                continue
            try:
                if self.registry.is_eligible(name):
                    return name
            except Exception:
                logger.debug("planning provider check failed", exc_info=True)
                continue
        return None

    def revise_plan(self, project_id: str, plan_data: dict[str, Any], reason: str) -> dict[str, Any]:
        """Persist an edited plan as a new immutable revision (auditable)."""
        row = self.db.get("product_projects", project_id)
        if not row:
            raise KeyError(f"product project {project_id} not found")
        if row["state"] in {s.value for s in TERMINAL_PRODUCT_STATUSES}:
            raise ValueError(f"project {project_id} is terminal ({row['state']})")
        errors = validate_product_plan(plan_data)
        if errors:
            return {"ok": False, "errors": errors}
        if not reason.strip():
            return {"ok": False, "errors": ["revision reason is required"]}
        # Never silently drop completed work: a revision may not remove phases
        # that already completed or remove coverage for satisfied requirements.
        old = self._current_plan(project_id)
        if old is not None:
            done_keys = {
                p["phase_key"]
                for p in self.db.query(
                    "SELECT phase_key FROM project_phases WHERE project_id=? AND status='COMPLETED'",
                    (project_id,),
                )
            }
            new_keys = {p["key"] for p in plan_data.get("phases", [])}
            removed = done_keys - new_keys
            if removed:
                return {
                    "ok": False,
                    "errors": [f"revision removes already-completed phases: {', '.join(sorted(removed))}"],
                }
        revision = int(row.get("plan_revision") or 0) + 1
        self.db.insert(
            "plan_revisions",
            {
                "id": uuid.uuid4().hex[:16],
                "project_id": project_id,
                "revision": revision,
                "plan_json": plan_data,
                "created_by": "human",
                "reason": reason.strip(),
                "created_at": utcnow(),
            },
        )
        self.db.update("product_projects", project_id, {"plan_revision": revision, "updated_at": utcnow()})
        # Sync phase rows: add new phases, keep existing rows (history preserved).
        plan = ProductPlan.model_validate(plan_data)
        existing = {
            p["phase_key"]: p for p in self.db.query("SELECT * FROM project_phases WHERE project_id=?", (project_id,))
        }
        for spec in plan.phases:
            if spec.key in existing:
                self.db.update(
                    "project_phases",
                    existing[spec.key]["id"],
                    {
                        "title": spec.title,
                        "goal": spec.goal,
                        "depends_on": [d for d in spec.depends_on],
                        "acceptance_json": [a.model_dump() for a in spec.acceptance],
                        "updated_at": utcnow(),
                    },
                )
            else:
                self._insert_phase(project_id, spec)
        return {"ok": True, "revision": revision}

    def _insert_phase(self, project_id: str, spec: Any) -> str:
        phase_id = uuid.uuid4().hex[:16]
        now = utcnow()
        self.db.insert(
            "project_phases",
            {
                "id": phase_id,
                "project_id": project_id,
                "phase_key": spec.key,
                "title": spec.title,
                "goal": spec.goal,
                "status": ProjectPhaseStatus.PENDING.value,
                "depends_on": list(spec.depends_on),
                "acceptance_json": [a.model_dump() for a in spec.acceptance],
                "evidence_json": {},
                "created_at": now,
                "updated_at": now,
            },
        )
        return phase_id

    # -- target repo -------------------------------------------------------
    def ensure_target_repo(self, project_id: str) -> dict[str, Any]:
        """Provision (or adopt) the target git repository for generated code."""
        row = self.db.get("product_projects", project_id)
        if not row:
            raise KeyError(f"product project {project_id} not found")
        if row.get("target_project_id"):
            existing = self.db.get("projects", row["target_project_id"])
            if existing:
                return existing
        raw_path = (row.get("target_repo_path") or "").strip()
        if raw_path:
            try:
                repo_path = validate_workspace_path(raw_path, self.config.allowed_roots())
            except ValueError as exc:
                raise ProductValidationError(
                    f"target repository path rejected: {exc} — configure a path inside "
                    f"allowed roots ({', '.join(str(r) for r in self.config.allowed_roots()) or '$HOME, /tmp'})"
                ) from exc
        else:
            default_root = Path(self.config.get("lifecycle.workspace_root", str(Path.home() / "gg-products")))
            candidate = default_root / f"{_slug(row['name'])}-{project_id[:8]}"
            try:
                candidate.mkdir(parents=True, exist_ok=True)
                repo_path = validate_workspace_path(candidate, self.config.allowed_roots())
            except ValueError:
                # The default product root is outside this deployment's allowed
                # roots: fall back to the first allowed root (explicit user
                # paths are never rerouted — only our own default).
                roots = self.config.allowed_roots()
                if not roots:
                    raise ProductValidationError(
                        f"default product root {default_root} is outside the allowed workspace roots — "
                        "set lifecycle.workspace_root or provide a target repository path"
                    ) from None
                fallback = roots[0] / "gg-products" / f"{_slug(row['name'])}-{project_id[:8]}"
                fallback.mkdir(parents=True, exist_ok=True)
                try:
                    repo_path = validate_workspace_path(fallback, self.config.allowed_roots())
                except ValueError as exc:
                    raise ProductValidationError(f"target repository path rejected: {exc}") from exc
            self.db.update("product_projects", project_id, {"target_repo_path": str(repo_path)})
        repo_path.mkdir(parents=True, exist_ok=True)
        if not (repo_path / ".git").exists():
            subprocess.run(["git", "init", "-q", str(repo_path)], check=True)  # noqa: S603,S607 - argv array, fixed binary
            (repo_path / "README.md").write_text(f"# {row['name']}\n\n{row['idea']}\n")
            subprocess.run(  # noqa: S603 - argv array, fixed binaries
                ["git", "-C", str(repo_path), "add", "-A"], check=True
            )
            subprocess.run(  # noqa: S603 - argv array, fixed binaries
                ["git", "-C", str(repo_path), "commit", "-q", "-m", "chore: initialize product workspace"],
                check=True,
            )
        by_path = self.db.get("projects", str(repo_path), key="path")
        if by_path:
            target_id = by_path["id"]
        else:
            info = {"detected_type": "unknown"}
            target_id = uuid.uuid4().hex[:16]
            self.db.insert(
                "projects",
                {
                    "id": target_id,
                    "name": row["name"],
                    "path": str(repo_path),
                    "detected_type": info["detected_type"],
                    "created_at": utcnow(),
                },
            )
        self.db.update("product_projects", project_id, {"target_project_id": target_id})
        return self.db.get("projects", target_id) or {}

    # -- execution ---------------------------------------------------------
    def start_project(self, project_id: str) -> dict[str, Any]:
        row = self.db.get("product_projects", project_id)
        if not row:
            raise KeyError(f"product project {project_id} not found")
        if row["state"] not in (ProductStatus.PLAN_READY.value, ProductStatus.DRAFT.value):
            raise ValueError(f"project {project_id} cannot start from {row['state']}")
        plan = self._current_plan(project_id)
        if plan is None:
            raise ValueError("a valid plan revision is required before start")
        self.ensure_target_repo(project_id)
        existing = self.db.query("SELECT id FROM project_phases WHERE project_id=?", (project_id,))
        if not existing:
            for spec in plan.phases:
                self._insert_phase(project_id, spec)
        # Seed requirement evidence rows.
        for req in plan.requirements:
            present = self.db.query(
                "SELECT * FROM requirement_evidence WHERE project_id=? AND requirement_id=?",
                (project_id, req.id),
            )
            if not present:
                self.db.insert(
                    "requirement_evidence",
                    {
                        "project_id": project_id,
                        "requirement_id": req.id,
                        "status": "PENDING",
                        "evidence_json": {},
                        "updated_at": utcnow(),
                    },
                )
        # External prerequisites become project gates up front (names only).
        for pre in plan.external_prerequisites:
            dup = self.db.query(
                "SELECT id FROM project_gates WHERE project_id=? AND blocked_ref=? AND status='open'",
                (project_id, f"prereq:{pre.key}"),
            )
            if not dup:
                self.db.insert(
                    "project_gates",
                    {
                        "id": uuid.uuid4().hex[:16],
                        "project_id": project_id,
                        "gate_type": "secret" if pre.required_vars else "action",
                        "title": pre.title,
                        "what_required": pre.what_required,
                        "why_required": pre.why_required,
                        "blocked_ref": f"prereq:{pre.key}",
                        "completed_so_far": "project plan approved",
                        "human_action": pre.human_action,
                        "where_to_provide": pre.where_to_provide,
                        "validation": pre.validation,
                        "after_resolve": "blocked phases resume automatically",
                        "required_vars": list(pre.required_vars),
                        "status": "open",
                        "created_at": utcnow(),
                    },
                )
                self.events.publish(
                    EventType.PRODUCT_GATE_CREATED, None, product_project_id=project_id, title=pre.title
                )
        self._set_state(project_id, ProductStatus.EXECUTING, reason="")
        return self.get_project(project_id) or {}

    def _deps_satisfied(self, project_id: str, phase: dict[str, Any]) -> bool:
        deps: list[str] = json.loads(phase.get("depends_on") or "[]")
        if not deps:
            return True
        rows = {
            r["phase_key"]: r["status"]
            for r in self.db.query("SELECT phase_key, status FROM project_phases WHERE project_id=?", (project_id,))
        }
        return all(rows.get(d) in ("COMPLETED", "SKIPPED") for d in deps)

    def _prereqs_open(self, project_id: str, phase_key: str, prereq_keys: list[str]) -> list[str]:
        """Open gate blocked_refs that gate this phase (prereq:* or phase:*)."""
        open_gates = self.db.query(
            "SELECT blocked_ref FROM project_gates WHERE project_id=? AND status='open'", (project_id,)
        )
        refs = {g["blocked_ref"] for g in open_gates}
        return [k for k in prereq_keys if f"prereq:{k}" in refs]

    async def advance_all(self) -> None:
        async with self._advance_lock:
            rows = self.db.query(
                "SELECT id FROM product_projects WHERE state IN ("  # noqa: S608 -- placeholders only
                + ",".join("?" for _ in ACTIVE_PRODUCT_STATUSES)
                + ")",
                tuple(s.value for s in ACTIVE_PRODUCT_STATUSES),
            )
            for row in rows:
                try:
                    await self._advance_locked(row["id"])
                except Exception:
                    logger.exception("coordinator advance failed for %s", row["id"])

    async def advance_project(self, project_id: str) -> dict[str, Any]:
        async with self._advance_lock:
            return await self._advance_locked(project_id)

    async def _advance_locked(self, project_id: str) -> dict[str, Any]:
        row = self.db.get("product_projects", project_id)
        if not row:
            raise KeyError(f"product project {project_id} not found")
        state = row["state"]
        if state in {s.value for s in TERMINAL_PRODUCT_STATUSES}:
            return {"ok": True, "state": state, "note": "terminal"}
        if state == ProductStatus.PLAN_READY.value and row.get("auto_execute"):
            self.start_project(project_id)
            row = self.db.get("product_projects", project_id) or row
            state = row["state"]
        if state == ProductStatus.PLANNING.value:
            return {"ok": True, "state": state, "note": "planning in progress"}
        if state in (ProductStatus.DRAFT.value, ProductStatus.PLAN_READY.value):
            return {"ok": True, "state": state, "note": "awaiting start"}
        if state == ProductStatus.FINAL_ACCEPTANCE.value:
            await self._run_acceptance_locked(project_id)
            return {"ok": True, "state": (self.db.get("product_projects", project_id) or {})["state"]}
        if state == ProductStatus.REVIEWING.value:
            self._set_state(project_id, ProductStatus.FINAL_ACCEPTANCE, reason="")
            await self._run_acceptance_locked(project_id)
            return {"ok": True, "state": (self.db.get("product_projects", project_id) or {})["state"]}
        # EXECUTING / WAITING_FOR_HUMAN / REVIEWING / BLOCKED: drive phases.
        plan = self._current_plan(project_id)
        if plan is None:
            self._set_state(project_id, ProductStatus.BLOCKED, reason="plan revision missing")
            return {"ok": False, "state": ProductStatus.BLOCKED.value}
        prereq_by_phase = {p.key: list(p.human_prerequisites) for p in plan.phases}
        phases = self.db.query(
            "SELECT * FROM project_phases WHERE project_id=? ORDER BY created_at ASC, rowid ASC", (project_id,)
        )
        progressed = False
        for phase in phases:
            if await self._advance_phase_locked(project_id, phase, prereq_by_phase.get(phase["phase_key"], [])):
                progressed = True
        self._recompute_project_state_locked(project_id)
        current = (self.db.get("product_projects", project_id) or {}).get("state")
        return {"ok": True, "state": current, "progressed": progressed}

    async def _advance_phase_locked(self, project_id: str, phase: dict[str, Any], prereq_keys: list[str]) -> bool:
        status = phase["status"]
        if status in {s.value for s in TERMINAL_PHASE_STATUSES}:
            return False
        # Gate mirroring: a phase mission waiting on a human becomes a project gate.
        if phase.get("mission_id"):
            mission = self.db.get("missions", phase["mission_id"])
            if mission and mission["status"] == MissionStatus.WAITING_FOR_HUMAN.value:
                self._mirror_mission_gate_locked(project_id, phase, mission)
                if phase["status"] != ProjectPhaseStatus.WAITING_FOR_HUMAN.value:
                    self.db.update(
                        "project_phases",
                        phase["id"],
                        {"status": ProjectPhaseStatus.WAITING_FOR_HUMAN.value, "updated_at": utcnow()},
                    )
                return False
        if status == ProjectPhaseStatus.WAITING_FOR_HUMAN.value:
            # Resume only when every gate blocking this phase resolved.
            blocking = self.db.query(
                """SELECT id FROM project_gates WHERE project_id=? AND status='open'
                   AND (phase_id=? OR blocked_ref LIKE ?)""",
                (project_id, phase["id"], f"phase:{phase['phase_key']}%"),
            )
            mission_blocked = bool(
                phase.get("mission_id")
                and (self.db.get("missions", phase["mission_id"]) or {}).get("status")
                == MissionStatus.WAITING_FOR_HUMAN.value
            )
            if not blocking and not mission_blocked:
                self.db.update(
                    "project_phases", phase["id"], {"status": ProjectPhaseStatus.READY.value, "updated_at": utcnow()}
                )
                return True
            return False
        if status in (ProjectPhaseStatus.PENDING.value, ProjectPhaseStatus.READY.value):
            if not self._deps_satisfied(project_id, phase):
                return False
            open_pre = self._prereqs_open(project_id, phase["phase_key"], prereq_keys)
            if open_pre:
                self.db.update(
                    "project_phases",
                    phase["id"],
                    {
                        "status": ProjectPhaseStatus.WAITING_FOR_HUMAN.value,
                        "blocking_issue": f"waiting on prerequisites: {', '.join(open_pre)}",
                        "updated_at": utcnow(),
                    },
                )
                return True
            if phase.get("mission_id"):
                # Mission exists but not terminal and not human-blocked: engine owns it.
                mission = self.db.get("missions", phase["mission_id"])
                if mission and mission["status"] not in TERMINAL_MISSION_VALUES:
                    if phase["status"] != ProjectPhaseStatus.RUNNING.value:
                        self.db.update(
                            "project_phases",
                            phase["id"],
                            {"status": ProjectPhaseStatus.RUNNING.value, "updated_at": utcnow()},
                        )
                    return False
            self._launch_phase_mission_locked(project_id, phase)
            return True
        if status == ProjectPhaseStatus.RUNNING.value:
            mission = self.db.get("missions", phase["mission_id"]) if phase.get("mission_id") else None
            if mission is None:
                self.db.update(
                    "project_phases", phase["id"], {"status": ProjectPhaseStatus.READY.value, "updated_at": utcnow()}
                )
                return True
            if mission["status"] not in TERMINAL_MISSION_VALUES:
                return False
            return await self._evaluate_phase_mission_locked(project_id, phase, mission)
        return False

    def _phase_prompt(self, project_name: str, phase: dict[str, Any], plan: ProductPlan) -> str:
        spec = next((p for p in plan.phases if p.key == phase["phase_key"]), None)
        tasks = "\n".join(f"- {t}" for t in (spec.tasks if spec else []))
        criteria = "\n".join(
            f"- [{a['id']}] {a['description']} (verify: {a.get('verify', '')})"
            for a in json.loads(phase.get("acceptance_json") or "[]")
        )
        verify_cmds = "\n".join(f"- {c}" for c in (spec.verify_commands if spec else []))
        return f"""You are implementing one phase of the product "{project_name}".

PHASE: {phase["title"]}
GOAL: {phase["goal"]}

TASKS:
{tasks or "(see goal)"}

ACCEPTANCE CRITERIA (all must hold when you finish):
{criteria}

SUGGESTED VERIFICATION COMMANDS:
{verify_cmds or "(use the repo toolchain)"}

RULES:
- Write real, working code in the workspace. Follow existing conventions.
- Run the repo test/build/lint toolchain and fix failures; never weaken tests.
- Do not commit (the orchestrator checkpoints); do not touch secrets or .env values.
"""

    def _launch_phase_mission_locked(self, project_id: str, phase: dict[str, Any]) -> None:
        # Idempotent: never create a second mission for a phase that has one
        # unless the previous mission reached a terminal state.
        fresh = self.db.get("project_phases", phase["id"])
        if fresh and fresh.get("mission_id"):
            mission = self.db.get("missions", fresh["mission_id"])
            if mission and mission["status"] not in TERMINAL_MISSION_VALUES:
                return
        proj = self.db.get("product_projects", project_id) or {}
        plan = self._current_plan(project_id)
        target_id = proj.get("target_project_id")
        if not target_id or plan is None:
            raise RuntimeError(f"project {project_id} cannot launch phase without repo + plan")
        attempts = int(fresh.get("attempts") or 0) + 1 if fresh else 1
        mission = self.orch.create_mission(
            project_id=target_id,
            title=f"[{proj.get('name')}] {phase['title']}",
            task=self._phase_prompt(proj.get("name", project_id), phase, plan),
            autonomy="BALANCED",
            profile="balanced",
        )
        self.db.update(
            "project_phases",
            phase["id"],
            {
                "mission_id": mission["id"],
                "status": ProjectPhaseStatus.RUNNING.value,
                "attempts": attempts,
                "blocking_issue": None,
                "updated_at": utcnow(),
            },
        )
        self.events.publish(
            EventType.PRODUCT_PHASE_STARTED,
            mission["id"],
            product_project_id=project_id,
            phase_key=phase["phase_key"],
            attempt=attempts,
        )
        self.orch.start_mission(mission["id"])

    async def _evaluate_phase_mission_locked(
        self, project_id: str, phase: dict[str, Any], mission: dict[str, Any]
    ) -> bool:
        status = mission["status"]
        if status == MissionStatus.COMPLETED.value:
            evidence = {
                "mission_id": mission["id"],
                "git_head": mission.get("git_head"),
                "finished_at": mission.get("finished_at"),
            }
            self.db.update(
                "project_phases",
                phase["id"],
                {
                    "status": ProjectPhaseStatus.COMPLETED.value,
                    "evidence_json": evidence,
                    "blocking_issue": None,
                    "updated_at": utcnow(),
                },
            )
            self._mark_requirements_work_completed_locked(project_id, phase, evidence)
            self.events.publish(
                EventType.PRODUCT_PHASE_COMPLETED,
                mission["id"],
                product_project_id=project_id,
                phase_key=phase["phase_key"],
            )
            return True
        if status == MissionStatus.WAITING_FOR_HUMAN.value:
            self._mirror_mission_gate_locked(project_id, phase, mission)
            self.db.update(
                "project_phases",
                phase["id"],
                {"status": ProjectPhaseStatus.WAITING_FOR_HUMAN.value, "updated_at": utcnow()},
            )
            return True
        if status in (MissionStatus.FAILED.value, MissionStatus.UNVERIFIED.value, MissionStatus.CANCELLED.value):
            attempts = int(phase.get("attempts") or 1)
            max_attempts = int(phase.get("max_attempts") or 2)
            if status == MissionStatus.CANCELLED.value:
                self.db.update(
                    "project_phases",
                    phase["id"],
                    {
                        "status": ProjectPhaseStatus.BLOCKED.value,
                        "blocking_issue": "phase mission cancelled by operator — retry the phase to continue",
                        "updated_at": utcnow(),
                    },
                )
                return True
            if attempts < max_attempts:
                # Bounded repair: clear the terminal mission link so the next
                # advance launches a fresh attempt (no duplicate writers: the
                # old mission is terminal, its engine is gone).
                self.db.update(
                    "project_phases",
                    phase["id"],
                    {
                        "mission_id": None,
                        "status": ProjectPhaseStatus.READY.value,
                        "updated_at": utcnow(),
                    },
                )
                return True
            self.db.update(
                "project_phases",
                phase["id"],
                {
                    "status": ProjectPhaseStatus.FAILED.value,
                    "blocking_issue": (
                        f"phase mission {status.lower()} after {attempts} attempts: "
                        f"{mission.get('blocking_issue') or 'see mission'}"
                    ),
                    "updated_at": utcnow(),
                },
            )
            return True
        return False

    def _mark_requirements_work_completed_locked(
        self, project_id: str, phase: dict[str, Any], evidence: dict[str, Any]
    ) -> None:
        """Record that implementation work finished (F-LIFE-01).

        Mission COMPLETED proves work was done — never that acceptance
        criteria hold. Mapped requirements move to WORK_COMPLETED; only
        executed criterion checks (or authorized waivers) grant SATISFIED.
        """
        plan = self._current_plan(project_id)
        if plan is None:
            return
        spec = next((p for p in plan.phases if p.key == phase["phase_key"]), None)
        for rid in spec.requirement_ids if spec else []:
            current = self.db.query(
                "SELECT status FROM requirement_evidence WHERE project_id=? AND requirement_id=?",
                (project_id, rid),
            )
            if current and current[0]["status"] in ("SATISFIED", "FAILED"):
                continue  # acceptance verdicts are never overwritten by work completion
            self.db.execute(
                """INSERT INTO requirement_evidence(project_id, requirement_id, status, evidence_json, updated_at)
                   VALUES (?, ?, 'WORK_COMPLETED', ?, ?)
                   ON CONFLICT(project_id, requirement_id) DO UPDATE SET
                     status='WORK_COMPLETED', evidence_json=excluded.evidence_json,
                     updated_at=excluded.updated_at""",
                (project_id, rid, json.dumps({**evidence, "phase_key": phase["phase_key"]}), utcnow().isoformat()),
            )

    def _mirror_mission_gate_locked(self, project_id: str, phase: dict[str, Any], mission: dict[str, Any]) -> None:
        gates = self.db.query(
            "SELECT * FROM human_gates WHERE mission_id=? AND status='open' ORDER BY created_at DESC",
            (mission["id"],),
        )
        for gate in gates:
            dup = self.db.query("SELECT id FROM project_gates WHERE mission_gate_id=? AND status='open'", (gate["id"],))
            if dup:
                continue
            self.db.insert(
                "project_gates",
                {
                    "id": uuid.uuid4().hex[:16],
                    "project_id": project_id,
                    "phase_id": phase["id"],
                    "mission_gate_id": gate["id"],
                    "gate_type": "mission",
                    "title": f"Mission input required: {phase['title']}",
                    "what_required": gate.get("reason") or "",
                    "why_required": f"phase mission {mission['id']} is waiting for human input",
                    "blocked_ref": f"phase:{phase['phase_key']}",
                    "completed_so_far": mission.get("task", "")[:500],
                    "human_action": gate.get("detail") or "respond to the mission gate",
                    "where_to_provide": "project gates view",
                    "validation": "mission resumes after the gate resolves",
                    "after_resolve": "phase mission resumes; roadmap continues",
                    "required_vars": [],
                    "status": "open",
                    "created_at": utcnow(),
                },
            )
            self.events.publish(
                EventType.PRODUCT_GATE_CREATED,
                mission["id"],
                product_project_id=project_id,
                title=gate.get("reason"),
            )

    def _phase_launchable_locked(self, project_id: str, phase: dict[str, Any]) -> bool:
        """A PENDING/READY phase that could start on the next advance."""
        if phase["status"] not in (ProjectPhaseStatus.PENDING.value, ProjectPhaseStatus.READY.value):
            return False
        if not self._deps_satisfied(project_id, phase):
            return False
        plan = self._current_plan(project_id)
        prereqs: list[str] = []
        if plan is not None:
            spec = next((p for p in plan.phases if p.key == phase["phase_key"]), None)
            prereqs = list(spec.human_prerequisites) if spec else []
        if self._prereqs_open(project_id, phase["phase_key"], prereqs):
            return False
        if phase.get("mission_id"):
            mission = self.db.get("missions", phase["mission_id"])
            if mission and mission["status"] not in TERMINAL_MISSION_VALUES:
                return False
        return True

    def _recompute_project_state_locked(self, project_id: str) -> None:
        row = self.db.get("product_projects", project_id)
        if not row or row["state"] in {s.value for s in TERMINAL_PRODUCT_STATUSES}:
            return
        phases = self.db.query("SELECT * FROM project_phases WHERE project_id=?", (project_id,))
        if not phases:
            return
        statuses = {p["status"] for p in phases}
        open_gates = self.db.query(
            "SELECT COUNT(*) AS n FROM project_gates WHERE project_id=? AND status='open'", (project_id,)
        )[0]["n"]
        if statuses <= {"COMPLETED", "SKIPPED"}:
            if row["state"] != ProductStatus.REVIEWING.value:
                self._set_state(project_id, ProductStatus.REVIEWING, reason="all phases complete — product review")
            return
        live = [
            p
            for p in phases
            if p["status"] == ProjectPhaseStatus.RUNNING.value or self._phase_launchable_locked(project_id, p)
        ]
        waiting = [p for p in phases if p["status"] == ProjectPhaseStatus.WAITING_FOR_HUMAN.value]
        failed = [p for p in phases if p["status"] == ProjectPhaseStatus.FAILED.value]
        if live:
            if row["state"] in (ProductStatus.BLOCKED.value, ProductStatus.WAITING_FOR_HUMAN.value):
                self._set_state(project_id, ProductStatus.EXECUTING, reason="")
            elif row["state"] not in (
                ProductStatus.EXECUTING.value,
                ProductStatus.REVIEWING.value,
                ProductStatus.FINAL_ACCEPTANCE.value,
            ):
                self._set_state(project_id, ProductStatus.EXECUTING, reason="")
            return
        if waiting or open_gates:
            if row["state"] != ProductStatus.WAITING_FOR_HUMAN.value:
                self._set_state(project_id, ProductStatus.WAITING_FOR_HUMAN, reason="waiting on human gates")
            return
        if failed:
            detail = "; ".join(f"{f['phase_key']}: {f['blocking_issue'] or 'failed'}" for f in failed)
            self._set_state(project_id, ProductStatus.BLOCKED, reason=f"phase(s) failed: {detail}")
            return
        # No live, waiting, or failed phase but work remains: genuine deadlock.
        self._set_state(project_id, ProductStatus.BLOCKED, reason="no phase can progress (unsatisfied dependencies)")

    # -- operator actions --------------------------------------------------
    async def pause_project(self, project_id: str) -> None:
        async with self._advance_lock:
            row = self.db.get("product_projects", project_id)
            if not row:
                raise KeyError(f"product project {project_id} not found")
            for phase in self.db.query(
                "SELECT mission_id FROM project_phases WHERE project_id=? AND status='RUNNING'", (project_id,)
            ):
                if phase.get("mission_id"):
                    try:
                        self.orch.pause_mission(phase["mission_id"])
                    except Exception:
                        logger.debug("pause of %s failed", phase["mission_id"], exc_info=True)

    async def cancel_project(self, project_id: str) -> None:
        async with self._advance_lock:
            row = self.db.get("product_projects", project_id)
            if not row:
                raise KeyError(f"product project {project_id} not found")
            if row["state"] in {s.value for s in TERMINAL_PRODUCT_STATUSES}:
                return
            for phase in self.db.query(
                "SELECT mission_id FROM project_phases WHERE project_id=? AND mission_id IS NOT NULL", (project_id,)
            ):
                try:
                    self.orch.cancel_mission(phase["mission_id"])
                except Exception:
                    logger.debug("cancel of %s failed", phase["mission_id"], exc_info=True)
            self._set_state(project_id, ProductStatus.CANCELLED, reason="cancelled by operator", finished=True)

    def retry_phase(self, project_id: str, phase_key: str) -> dict[str, Any]:
        rows = self.db.query("SELECT * FROM project_phases WHERE project_id=? AND phase_key=?", (project_id, phase_key))
        if not rows:
            raise KeyError(f"phase {phase_key} not found")
        phase = rows[0]
        if phase.get("mission_id"):
            mission = self.db.get("missions", phase["mission_id"])
            if mission and mission["status"] not in TERMINAL_MISSION_VALUES:
                raise ValueError("phase mission still active — pause/cancel it first")
        if phase["status"] == ProjectPhaseStatus.COMPLETED.value:
            raise ValueError("phase already COMPLETED — revise the plan to redo work")
        self.db.update(
            "project_phases",
            phase["id"],
            {
                "mission_id": None,
                "status": ProjectPhaseStatus.READY.value,
                "blocking_issue": None,
                "updated_at": utcnow(),
            },
        )
        row = self.db.get("product_projects", project_id)
        if row and row["state"] == ProductStatus.BLOCKED.value:
            self._set_state(project_id, ProductStatus.EXECUTING, reason="operator retried phase")
        return {"ok": True, "phase_key": phase_key}

    # -- gates -------------------------------------------------------------
    async def resolve_gate(self, project_id: str, gate_id: str, resolution: str) -> dict[str, Any]:
        # The external-command validation below can take up to 120s (see
        # _run_gate_validation). Holding _advance_lock for that whole span would
        # stall advance_all()'s ~2s scheduler tick for every other active
        # project, so the slow part runs unlocked; the lock is only held for
        # the state read that decides what to do and the state write that
        # persists the outcome, with a re-check between them since the gate or
        # project may have changed while validation ran (e.g. cancelled).
        async with self._advance_lock:
            gate = self.db.get("project_gates", gate_id)
            if not gate or gate["project_id"] != project_id or gate["status"] != "open":
                raise KeyError(f"open gate {gate_id} not found")
            if gate.get("mission_gate_id"):
                # Delegate to the mission gate contract; mirror on success.
                self.orch.resolve_gate(gate["mission_gate_id"], resolution or "continue")
                self.db.update(
                    "project_gates",
                    gate_id,
                    {"status": "resolved", "resolution": resolution or "continue", "resolved_at": utcnow()},
                )
                self.events.publish(
                    EventType.PRODUCT_GATE_RESOLVED, None, product_project_id=project_id, gate_id=gate_id
                )
                return {"ok": True, "gate_id": gate_id}
            if gate["gate_type"] == "secret":
                check = self._validate_secret_gate(project_id, gate)
                if not check["ok"]:
                    return check
                self.db.update(
                    "project_gates",
                    gate_id,
                    {
                        "status": "resolved",
                        "resolution": "prerequisite configured (values never stored)",
                        "resolved_at": utcnow(),
                    },
                )
                self.events.publish(
                    EventType.PRODUCT_GATE_RESOLVED, None, product_project_id=project_id, gate_id=gate_id
                )
                return {"ok": True, "gate_id": gate_id}
            if not (resolution or "").strip():
                return {"ok": False, "error": "resolution text is required"}
            validation_cmd = (gate.get("validation") or "").strip()
            # Only an explicit `run <command>` validation executes anything;
            # all other validation text is human-attested instruction. The
            # command was already checked against the same allowlist at
            # plan-validation time (validate_product_plan); re-checked here
            # too — defense in depth, never trust a prior check alone.
            needs_exec = validation_cmd.lower().startswith("run ")
            exec_command = validation_cmd[4:].strip() if needs_exec else ""
            if not needs_exec:
                self.db.update(
                    "project_gates",
                    gate_id,
                    {"status": "resolved", "resolution": (resolution or "").strip(), "resolved_at": utcnow()},
                )
                self.events.publish(
                    EventType.PRODUCT_GATE_RESOLVED, None, product_project_id=project_id, gate_id=gate_id
                )
                return {"ok": True, "gate_id": gate_id}

        # -- unlocked: the potentially slow part --
        passed = await self._run_gate_validation(project_id, exec_command)

        async with self._advance_lock:
            gate = self.db.get("project_gates", gate_id)
            if not gate or gate["project_id"] != project_id or gate["status"] != "open":
                return {"ok": False, "error": "gate no longer open — project state changed during validation"}
            project_row = self.db.get("product_projects", project_id)
            if not project_row or project_row["state"] in TERMINAL_PRODUCT_STATUSES:
                # cancel_project/other terminal transitions only ever mutate
                # product_projects, never project_gates — so the gate itself
                # can still read status=='open' here even though its project
                # became terminal while validation ran unlocked. Discard the
                # outcome rather than resolving a gate for a dead project.
                return {"ok": False, "error": "project no longer active — validation outcome discarded"}
            if not passed:
                return {"ok": False, "error": "validation command failed — prerequisite not satisfied"}
            self.db.update(
                "project_gates",
                gate_id,
                {"status": "resolved", "resolution": (resolution or "").strip(), "resolved_at": utcnow()},
            )
            self.events.publish(EventType.PRODUCT_GATE_RESOLVED, None, product_project_id=project_id, gate_id=gate_id)
            return {"ok": True, "gate_id": gate_id}

    def _target_repo(self, project_id: str) -> Path | None:
        row = self.db.get("product_projects", project_id) or {}
        target_id = row.get("target_project_id")
        if not target_id:
            return None
        target = self.db.get("projects", target_id)
        return Path(target["path"]) if target else None

    def _read_env_keys(self, repo: Path) -> set[str]:
        keys: set[str] = set()
        env_file = repo / ".env"
        if not env_file.exists():
            return keys
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name = line.split("=", 1)[0].strip().strip('"').strip("'")
            if name and all(c.isalnum() or c == "_" for c in name):
                keys.add(name)
        return keys

    def _validate_secret_gate(self, project_id: str, gate: dict[str, Any]) -> dict[str, Any]:
        """Confirm required names exist in the target repo .env — values never read into GG."""
        required: list[str] = json.loads(gate.get("required_vars") or "[]")
        repo = self._target_repo(project_id)
        if repo is None or not repo.exists():
            return {"ok": False, "error": "target repository not provisioned yet"}
        present = self._read_env_keys(repo)
        missing = [v for v in required if v not in present]
        if missing:
            return {
                "ok": False,
                "error": f"still missing in .env: {', '.join(missing)} — values stay in your repo, GG stores nothing",
            }
        return {"ok": True}

    async def _run_gate_validation(self, project_id: str, command: str) -> bool:
        """Execute a gate's `run <command>` validation text.

        Uses the same allowlist as criterion verification (is_executable_command)
        rather than a denylist — this text originates as LLM-authored plan JSON
        (ExternalPrerequisite.validation) and validate_product_plan already
        rejects non-allowlisted text at plan-save time, but that check must
        never be trusted alone: it is re-applied here, at execution time.
        """
        repo = self._target_repo(project_id)
        if repo is None or not command:
            return False
        ok, safe_command = is_executable_command(command)
        if not ok:
            return False
        if not confined_to_repo(safe_command, repo):
            return False
        if not sandbox_available():
            return False
        argv = shlex.split(safe_command)
        result = await run_sandboxed(argv, repo, timeout_s=120)
        return result.exit_code == 0

    async def _evaluate_requirement_criteria_locked(
        self, project_id: str, plan: ProductPlan, repo: Path, sha: str
    ) -> list[str]:
        """Execute every required criterion check; record per-criterion verdicts.

        Restart-safe: a recorded result for the same (criterion, command, SHA)
        is reused, never re-executed — checks may have side effects (e.g. HTTP
        probes that create records), so acceptance must not duplicate runs.
        Returns blocking finding strings (empty when all criteria satisfied).
        """
        problems: list[str] = []
        waived = self._waived_targets(project_id)
        for req in plan.requirements:
            req_ok = True
            for criterion in req.acceptance:
                cid = criterion.id
                if waived.get(f"criterion:{cid}") == self._criterion_content_hash(criterion):
                    self._record_criterion_waived(project_id, req.id, cid, sha)
                    continue
                recorded = self.db.query(
                    "SELECT * FROM criterion_results WHERE project_id=? AND criterion_id=?", (project_id, cid)
                )
                verify = criterion.verify or ""
                ok, command = is_executable_command(verify)
                if not ok:
                    problems.append(
                        f"criterion {cid} has no executable verification "
                        f"(revise the plan with an allowlisted command): {verify[:120]}"
                    )
                    self._record_criterion(project_id, req.id, cid, "UNVERIFIED", "", None, "no executable verify", sha)
                    req_ok = False
                    continue
                if recorded and recorded[0].get("command") == command and recorded[0].get("sha") == sha:
                    if recorded[0]["status"] != "SATISFIED":
                        problems.append(f"criterion {cid} failed: {command} (exit={recorded[0].get('exit_code')})")
                        req_ok = False
                    continue
                checks = await evaluate_criteria(repo, [{"id": cid, "verify": verify}])
                check = checks[0]
                status = "SATISFIED" if check.passed else "FAILED"
                self._record_criterion(
                    project_id, req.id, cid, status, command, check.exit_code, check.output_tail, sha
                )
                if not check.passed:
                    problems.append(f"criterion {cid} failed: {command} (exit={check.exit_code})")
                    req_ok = False
            self.db.execute(
                """INSERT INTO requirement_evidence(project_id, requirement_id, status, evidence_json, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(project_id, requirement_id) DO UPDATE SET
                     status=excluded.status, evidence_json=excluded.evidence_json, updated_at=excluded.updated_at""",
                (
                    project_id,
                    req.id,
                    "SATISFIED" if req_ok else "FAILED",
                    json.dumps({"sha": sha, "criteria": [a.id for a in req.acceptance]}),
                    utcnow().isoformat(),
                ),
            )
        return problems

    def _record_criterion(
        self,
        project_id: str,
        requirement_id: str,
        criterion_id: str,
        status: str,
        command: str,
        exit_code: int | None,
        output_tail: str,
        sha: str,
    ) -> None:
        self.db.execute(
            """INSERT INTO criterion_results(project_id, criterion_id, requirement_id, status, command,
                   exit_code, output_tail, sha, checked_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(project_id, criterion_id) DO UPDATE SET
                 requirement_id=excluded.requirement_id, status=excluded.status, command=excluded.command,
                 exit_code=excluded.exit_code, output_tail=excluded.output_tail, sha=excluded.sha,
                 checked_at=excluded.checked_at""",
            (
                project_id,
                criterion_id,
                requirement_id,
                status,
                command,
                exit_code,
                output_tail[:3000],
                sha,
                utcnow().isoformat(),
            ),
        )

    def _record_criterion_waived(self, project_id: str, requirement_id: str, criterion_id: str, sha: str) -> None:
        self._record_criterion(project_id, requirement_id, criterion_id, "WAIVED", "", None, "authorized waiver", sha)

    def _blocking_findings_locked(self, project_id: str) -> list[str]:
        """Unresolved requirement/correctness findings across phase missions.

        Open findings plus repair-attempted-but-unverified findings at MEDIUM
        or above block delivery unless explicitly waived. Omission by a later
        review never clears them (F-LIFE-02).
        """
        waived = self._waived_targets(project_id)
        mission_ids = [
            p["mission_id"]
            for p in self.db.query("SELECT mission_id FROM project_phases WHERE project_id=?", (project_id,))
            if p.get("mission_id")
        ]
        problems: list[str] = []
        for mid in mission_ids:
            rows = self.db.query(
                """SELECT id, severity, category, status, description FROM review_findings
                   WHERE mission_id=? AND status IN ('open','repair_attempted')
                   AND severity IN ('BLOCKER','HIGH','MEDIUM')""",
                (mid,),
            )
            for r in rows:
                if str(r.get("category", "")).lower() not in self.BLOCKING_FINDING_CATEGORIES:
                    continue
                if waived.get(f"finding:{r['id']}") == self._finding_content_hash(r):
                    continue
                problems.append(
                    f"unresolved {r['severity']} {r['category']} finding {r['id']} "
                    f"({r['status']}): {(r['description'] or '')[:160]}"
                )
        return problems

    # -- acceptance + delivery ---------------------------------------------
    async def _run_acceptance_locked(self, project_id: str) -> dict[str, Any]:
        row = self.db.get("product_projects", project_id)
        plan = self._current_plan(project_id)
        if not row or plan is None:
            return {"ok": False}
        repo = self._target_repo(project_id)
        findings: list[str] = []
        # 1. Requirement criteria: executed checks, never mission completion.
        # (Runs after the SHA is fixed below; evaluated in step 3b.)
        # 2. Open human gates block delivery.
        open_gates = self.db.query(
            "SELECT title FROM project_gates WHERE project_id=? AND status='open'", (project_id,)
        )
        if open_gates:
            findings.append(f"open human gates: {', '.join(g['title'] for g in open_gates)}")
        # 3. Clean tree + toolchain verification on the working tree.
        sha: str | None = None
        verify_ok = False
        fresh_ok = False
        if repo is None or not repo.exists():
            findings.append("target repository missing")
        else:
            st = await git_ops.status(repo)
            if not st.is_repo:
                findings.append("target path is not a git repository")
            else:
                if st.modified or st.untracked:
                    # Final checkpoint must contain the accepted state: commit
                    # actual changes (secret-safe) before recording the SHA.
                    sha = await git_ops.checkpoint(repo, "chore: final acceptance checkpoint")
                    st = await git_ops.status(repo)
                    if st.modified or st.untracked:
                        findings.append(f"uncommittable changes remain: {st.modified + st.untracked}")
                sha = sha or st.head
                info = await inspect_workspace(repo)
                report = await run_verification(info, self.db, self.events, "", repo)
                verify_ok = report.all_passed
                if not report.attempted:
                    findings.append("no toolchain commands detected — nothing objectively verified")
                elif not verify_ok:
                    findings.append(f"toolchain verification failed:\n{report.summary()}")
                # 3b. Criterion-level acceptance against the accepted SHA.
                if sha and not findings:
                    findings.extend(await self._evaluate_requirement_criteria_locked(project_id, plan, repo, sha))
                # 3c. Unresolved requirement/correctness findings block delivery.
                if not findings:
                    findings.extend(self._blocking_findings_locked(project_id))
                # 4. Fresh-checkout reproduction of the accepted SHA.
                if sha and verify_ok and not findings:
                    fresh_ok, fresh_detail = await self._fresh_checkout_verify(repo, sha)
                    if not fresh_ok:
                        findings.append(f"fresh checkout of {sha} did not reproduce verification: {fresh_detail}")
        if findings:
            blocked_state = AcceptanceState.EXTERNALLY_BLOCKED.value if open_gates else AcceptanceState.UNVERIFIED.value
            self.db.update(
                "product_projects",
                project_id,
                {
                    "acceptance_state": blocked_state,
                    "blocking_reason": "; ".join(findings)[:2000],
                    "updated_at": utcnow(),
                },
            )
            self._set_state(
                project_id,
                ProductStatus.BLOCKED if not open_gates else ProductStatus.WAITING_FOR_HUMAN,
                reason="; ".join(findings)[:2000],
            )
            self.events.publish(
                EventType.PRODUCT_ACCEPTANCE_RECORDED, None, product_project_id=project_id, passed=False
            )
            return {"ok": False, "findings": findings}
        # Accepted: record SHA + delivery report, then DELIVERED.
        delivery = self._build_delivery_report(project_id, plan, repo, sha or "")
        self.db.update(
            "product_projects",
            project_id,
            {
                "acceptance_state": AcceptanceState.DELIVERED.value,
                "delivery_sha": sha,
                "delivery_report": delivery,
                "blocking_reason": None,
                "updated_at": utcnow(),
            },
        )
        self.events.publish(EventType.PRODUCT_DELIVERED, None, product_project_id=project_id, sha=sha)
        self._set_state(project_id, ProductStatus.DELIVERED, reason="", finished=True)
        return {"ok": True, "sha": sha}

    async def _fresh_checkout_verify(self, repo: Path, sha: str) -> tuple[bool, str]:
        """Clone the accepted SHA fresh, install dependencies, reproduce verification.

        Returns (ok, detail). Dependency install is part of reproducibility:
        a checkout that cannot install + pass its toolchain is not accepted.
        """

        def _clone_and_checkout() -> Path | None:
            tmp = Path(tempfile.mkdtemp(prefix="gg-accept-"))
            clone = tmp / "checkout"
            # noqa: S603 - argv arrays, fixed git binary
            proc = subprocess.run(["git", "clone", "-q", str(repo), str(clone)], capture_output=True, timeout=120)
            if proc.returncode != 0:
                return None
            proc = subprocess.run(["git", "-C", str(clone), "checkout", "-q", sha], capture_output=True, timeout=60)
            return clone if proc.returncode == 0 else None

        async def _install(cmd: list[str], cwd: Path) -> tuple[bool, str]:
            # Routed through run_process (not subprocess.run): redacts secrets
            # from captured output before it is ever persisted/rendered, and
            # tree-kills the whole process group on timeout instead of leaving
            # orphaned native-build children running past the deadline.
            result = await run_process(cmd, cwd=cwd, timeout_s=600)
            return result.exit_code == 0, result.combined_tail[-1500:]

        tmp_root: Path | None = None
        try:
            clone = await asyncio.to_thread(_clone_and_checkout)
            if clone is None:
                return False, "git clone/checkout failed"
            tmp_root = clone.parent
            info = await inspect_workspace(clone)
            if not (info.test_commands or info.build_commands):
                return True, "no toolchain to reproduce beyond the recorded SHA"
            install_note = "no install step needed"
            # --ignore-scripts: this is freshly cloned, potentially AI-generated
            # code — lifecycle scripts (preinstall/postinstall) are an
            # unnecessary extra code-execution surface during acceptance.
            if (clone / "package-lock.json").exists():
                ok, log = await _install(["npm", "ci", "--no-audit", "--no-fund", "--ignore-scripts"], clone)
                install_note = "npm ci " + ("ok" if ok else f"FAILED: {log[-500:]}")
                if not ok:
                    return False, install_note
            elif (clone / "package.json").exists():
                ok, log = await _install(["npm", "install", "--no-audit", "--no-fund", "--ignore-scripts"], clone)
                install_note = "npm install " + ("ok" if ok else f"FAILED: {log[-500:]}")
                if not ok:
                    return False, install_note
            elif (clone / "uv.lock").exists() or (clone / "pyproject.toml").exists():
                ok, log = await _install(["uv", "sync", "--frozen"], clone)
                install_note = "uv sync " + ("ok" if ok else f"FAILED: {log[-500:]}")
                if not ok:
                    return False, install_note
            report = await run_verification(info, self.db, self.events, "", clone)
            detail = f"{install_note}; {report.summary()}"
            return report.all_passed, detail
        except Exception as exc:
            logger.exception("fresh checkout verify failed")
            return False, str(exc)[:500]
        finally:
            if tmp_root is not None:
                shutil.rmtree(tmp_root, ignore_errors=True)

    def _build_delivery_report(self, project_id: str, plan: ProductPlan, repo: Path | None, sha: str) -> dict[str, Any]:
        phases = self.db.query("SELECT * FROM project_phases WHERE project_id=?", (project_id,))
        evidence = self.db.query("SELECT * FROM requirement_evidence WHERE project_id=?", (project_id,))
        gates = self.db.query("SELECT * FROM project_gates WHERE project_id=?", (project_id,))
        criteria = self.db.query("SELECT * FROM criterion_results WHERE project_id=?", (project_id,))
        waivers = self.db.query("SELECT * FROM acceptance_waivers WHERE project_id=?", (project_id,))
        review_notes: list[str] = []
        for phase in phases:
            mission_id = phase.get("mission_id")
            if not mission_id:
                continue
            rows = self.db.query("SELECT severity, description FROM review_findings WHERE mission_id=?", (mission_id,))
            for f in rows:
                review_notes.append(f"[{phase['phase_key']}/{f['severity']}] {f['description']}")
        test_summary: list[str] = []
        recent = self.db.query(
            "SELECT type, payload FROM events WHERE type IN ('TEST_PASSED','TEST_FAILED')"
            " ORDER BY created_at DESC LIMIT 50"
        )
        for ev in recent:
            try:
                payload = json.loads(ev["payload"]) if isinstance(ev["payload"], str) else ev["payload"]
            except Exception:
                payload = {}
            test_summary.append(f"{ev['type']}: {payload.get('command', '')}")
        return {
            "product_name": plan.product_name,
            "goal": plan.goal,
            "stack": {
                "frontend": plan.architecture.frontend,
                "backend": plan.architecture.backend,
                "database": plan.architecture.database,
                "auth": plan.architecture.auth,
                "deployment": plan.architecture.deployment,
            },
            "decisions": [d.model_dump() for d in plan.architecture.decisions],
            "requirements": [
                {
                    "id": r.id,
                    "title": r.title,
                    "status": next((e["status"] for e in evidence if e["requirement_id"] == r.id), "PENDING"),
                    "criteria": [
                        {
                            "id": a.id,
                            "status": next((c["status"] for c in criteria if c["criterion_id"] == a.id), "PENDING"),
                            "command": next((c["command"] for c in criteria if c["criterion_id"] == a.id), ""),
                            "exit_code": next((c["exit_code"] for c in criteria if c["criterion_id"] == a.id), None),
                            "sha": next((c["sha"] for c in criteria if c["criterion_id"] == a.id), ""),
                        }
                        for a in r.acceptance
                    ],
                }
                for r in plan.requirements
            ],
            "phases": [
                {"key": p["phase_key"], "title": p["title"], "status": p["status"], "mission_id": p.get("mission_id")}
                for p in phases
            ],
            "git_sha": sha,
            "repo_path": str(repo) if repo else "",
            "review_notes": review_notes[:100],
            "recent_toolchain": test_summary[:50],
            "human_gates": [
                {"title": g["title"], "status": g["status"], "resolution": g.get("resolution")} for g in gates
            ],
            "env_vars": sorted({v for g in gates for v in json.loads(g.get("required_vars") or "[]")}),
            "waivers": [
                {
                    "target": f"{w['target_kind']}:{w['target_id']}",
                    "reason": w["reason"],
                    "actor": w["actor"],
                    "plan_revision": w["plan_revision"],
                }
                for w in waivers
            ],
            "run_instructions": self._run_instructions(plan),
        }

    def _run_instructions(self, plan: ProductPlan) -> str:
        a = plan.architecture
        parts = [f"Stack: {a.frontend} / {a.backend} / {a.database}".strip(" /")]
        if a.deployment:
            parts.append(f"Deploy: {a.deployment}")
        parts.append(
            "Clone the repo at the delivery SHA, install dependencies, then run the verify commands from each phase."
        )
        return "\n".join(parts)

    async def run_acceptance(self, project_id: str) -> dict[str, Any]:
        async with self._advance_lock:
            row = self.db.get("product_projects", project_id)
            if not row:
                raise KeyError(f"product project {project_id} not found")
            if row["state"] not in (
                ProductStatus.FINAL_ACCEPTANCE.value,
                ProductStatus.BLOCKED.value,
                ProductStatus.WAITING_FOR_HUMAN.value,
            ):
                return {"ok": False, "error": f"acceptance not available from {row['state']}"}
            if row["state"] != ProductStatus.FINAL_ACCEPTANCE.value:
                self.db.update(
                    "product_projects",
                    project_id,
                    {"state": ProductStatus.FINAL_ACCEPTANCE.value, "updated_at": utcnow()},
                )
            return await self._run_acceptance_locked(project_id)

    # -- recovery ----------------------------------------------------------
    async def recover(self) -> None:
        """Boot recovery: re-drive every non-terminal project (idempotent)."""
        rows = self.db.query(
            "SELECT id FROM product_projects WHERE state IN (" + ",".join("?" for _ in ACTIVE_PRODUCT_STATUSES) + ")",  # noqa: S608 -- placeholders only
            tuple(s.value for s in ACTIVE_PRODUCT_STATUSES),
        )
        for row in rows:
            try:
                await self.advance_project(row["id"])
            except Exception:
                logger.exception("coordinator recovery failed for %s", row["id"])

    def redacted_idea(self, project_id: str) -> str:
        row = self.db.get("product_projects", project_id) or {}
        return redact(row.get("idea", ""))
