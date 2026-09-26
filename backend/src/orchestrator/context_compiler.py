"""Deterministic role-specific context compiler (Increment 2).

Compiles structured project state into a budgeted, role-specific prompt
without any provider call. Default path is fully deterministic; no LLM
rewrite is performed.

Versions:
  template compiled-v2, policy context-policy-v2, estimator char4-v1.
Legacy baseline remains legacy-v1 for controlled comparison.

Legacy prompt inventory (all families now route through this compiler;
the legacy string is kept as the fallback baseline):

  PRODUCT_PLANNER      product_plan.build_planner_prompt -> project_engine
                         ._run_planning_provider (stage product_plan) -> planner
  PRODUCT_PLAN_REPAIR  product_plan.build_plan_repair_prompt -> same -> planner
  MISSION_PLANNER      engine.ROLE_PROMPTS[PLANNING] + handoff
                         (stage mission_plan) -> planner
  DAG_PLANNER          parallel_engine DAG planning prompt (stage dag_plan) -> planner
  IMPLEMENTER          engine.ROLE_PROMPTS[IMPLEMENTATION] + handoff -> implementer
  DAG_TASK_IMPLEMENTER parallel_engine._build_task_prompt + dependency_ids
                         (stage task) -> implementer
  TESTING_AGENT        engine ROLE_PROMPTS[TESTING] -> testing
  REVIEWER             ROLE_PROMPTS[REVIEW] + prior-findings context
                         (+ candidate SHA range when known) -> reviewer
  REPAIRER             ROLE_PROMPTS[REPAIR] + blocker/verification-failure text
                         (+ finding_ids) -> repairer
  FINAL_VALIDATION     deterministic verify.run_verification, no LLM prompt.

FINAL_VALIDATION has no provider prompt by design: objective verification
stays in sandboxed tools; its failures feed the repairer as evidence.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from dataclasses import dataclass, field
from typing import Any

from .security import redact

logger = logging.getLogger(__name__)

TEMPLATE_COMPILED_V2 = "compiled-v2"
POLICY_COMPILED_V2 = "context-policy-v2"
TEMPLATE_LEGACY_V1 = "legacy-v1"
POLICY_LEGACY_V1 = "legacy-v1"
ESTIMATOR_ID = "char4-v1"


# -- taxonomy ---------------------------------------------------------------


class BlockType:
    SYSTEM_INSTRUCTIONS = "SYSTEM_INSTRUCTIONS"
    TASK_OBJECTIVE = "TASK_OBJECTIVE"
    PROJECT_SUMMARY = "PROJECT_SUMMARY"
    PRODUCT_REQUIREMENT = "PRODUCT_REQUIREMENT"
    ACCEPTANCE_CRITERION = "ACCEPTANCE_CRITERION"
    ARCHITECTURE_DECISION = "ARCHITECTURE_DECISION"
    PHASE_CONTEXT = "PHASE_CONTEXT"
    DEPENDENCY_HANDOFF = "DEPENDENCY_HANDOFF"
    RELEVANT_CODE = "RELEVANT_CODE"
    GIT_DIFF = "GIT_DIFF"
    OPEN_FINDING = "OPEN_FINDING"
    FAILURE_EVIDENCE = "FAILURE_EVIDENCE"
    TEST_RESULT = "TEST_RESULT"
    ENVIRONMENT_CONTRACT = "ENVIRONMENT_CONTRACT"
    OUTPUT_CONTRACT = "OUTPUT_CONTRACT"
    WRITER_PROVENANCE = "WRITER_PROVENANCE"


class Priority:
    MANDATORY = "MANDATORY"
    PREFERRED = "PREFERRED"
    BUDGET_DEPENDENT = "BUDGET_DEPENDENT"
    SUMMARY_ONLY = "SUMMARY_ONLY"
    RETRIEVE_ON_DEMAND = "RETRIEVE_ON_DEMAND"
    PROHIBITED = "PROHIBITED"


class Representation:
    FULL = "FULL"
    COMPACT = "COMPACT"
    REFERENCE = "REFERENCE"
    OMITTED = "OMITTED"


class OmissionReason:
    NOT_RELEVANT = "NOT_RELEVANT"
    BUDGET = "BUDGET"
    PROHIBITED = "PROHIBITED"
    SUPERSEDED = "SUPERSEDED"
    REFERENCE_ONLY = "REFERENCE_ONLY"


# -- data -------------------------------------------------------------------


@dataclass
class ContextBlock:
    id: str
    type: str
    priority: str
    content: str
    source_kind: str = ""
    source_ref: str = ""
    required: bool = False
    summarizable: bool = True
    reason: str = ""
    sensitivity: str = "safe"
    compact_content: str | None = None
    reference_content: str | None = None

    @property
    def chars(self) -> int:
        return len(self.content)

    @property
    def estimated_tokens(self) -> int:
        return math.ceil(len(self.content) / 4) if self.content else 0

    def content_hash(self) -> str:
        return hashlib.sha256(redact(self.content).encode("utf-8")).hexdigest()[:16]


@dataclass
class BlockDecision:
    block_id: str
    type: str
    source_kind: str
    source_ref: str
    priority: str
    original_chars: int
    included_chars: int
    estimated_tokens: int
    representation: str
    included: bool
    reason: str
    hash: str


@dataclass
class ContextCompileSpec:
    role: str  # planner | implementer | reviewer | repairer | testing | planning
    stage: str = ""
    project_id: str | None = None
    product_project_id: str | None = None
    project_phase_id: str | None = None
    mission_id: str | None = None
    task_id: str | None = None
    provider: str | None = None
    base_sha: str | None = None
    candidate_sha: str | None = None
    task_objective: str = ""
    task_title: str = ""
    task_description: str = ""
    requirement_ids: list[str] = field(default_factory=list)
    dependency_ids: list[str] = field(default_factory=list)
    # Verified artifact presence per dependency ({dep_id: result-ancestor-of
    # task-input}). Computed by the caller from Git ancestry AFTER dependency
    # input preparation. An explicit False fails closed (UNVERIFIED_DEPENDENCY_ARTIFACT);
    # absent entries keep the legacy DEPENDENCY_CODE_NOT_PRESENT warning path.
    dependency_verified: dict[str, bool] = field(default_factory=dict)
    failure_text: str = ""
    # A failed-over attempt's own partial report: separate from orchestrator
    # evidence so it can never displace "observed vs expected" (never set for
    # reviewers, to keep review independent of unverified claims).
    failover_text: str = ""
    failure_command: str = ""
    failure_exit_code: int | None = None
    finding_ids: list[str] = field(default_factory=list)
    extra_context: str = ""
    plan_revision: int | None = None
    git_diff_summary: str = ""
    git_files_changed: list[str] = field(default_factory=list)
    workspace_scope: list[str] = field(default_factory=list)
    attempt: int = 1


@dataclass
class CompiledContext:
    prompt: str
    prompt_hash: str
    prompt_chars: int
    prompt_bytes: int
    prompt_words: int
    estimated_tokens: int
    template_version: str = TEMPLATE_COMPILED_V2
    policy_version: str = POLICY_COMPILED_V2
    blocks: list[BlockDecision] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    budget_estimated_tokens: int = 16000
    used_estimated_tokens: int = 0
    remaining_estimated_tokens: int = 16000
    repeated_context_ratio: float = 0.0
    plan_revision: int | None = None


class ContextCompileError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# -- budgets -----------------------------------------------------------------

DEFAULT_BUDGETS: dict[str, int] = {
    "planner": 20000,
    "planning": 20000,
    "implementer": 16000,
    "implementation": 16000,
    "reviewer": 18000,
    "review": 18000,
    "repairer": 12000,
    "repair": 12000,
    "testing": 12000,
    "default": 16000,
}


def budget_for_role(role: str, config: Any | None = None) -> int:
    if config is not None:
        try:
            raw = config.raw if hasattr(config, "raw") else {}
            ctx = raw.get("context") or {}
            roles = ctx.get("roles") or {}
            if isinstance(roles, dict) and role in roles:
                return int(roles[role])
            if isinstance(ctx.get("default_input_budget_estimate"), int):
                return int(ctx["default_input_budget_estimate"])
        except Exception:
            logger.debug("context budget config read failed", exc_info=True)
    return DEFAULT_BUDGETS.get(role, DEFAULT_BUDGETS["default"])


def context_mode(config: Any | None = None) -> str:
    try:
        raw: Any = config.raw if config is not None and hasattr(config, "raw") else {}
        if isinstance(raw, dict):
            mode = (raw.get("context") or {}).get("mode", "compiled")
            if mode in ("legacy", "compiled", "shadow"):
                return str(mode)
    except Exception:
        logger.debug("context mode read failed, defaulting to compiled", exc_info=True)
    return "compiled"


# -- projections --------------------------------------------------------------

_summary_cache: dict[tuple[str, int], str] = {}


def get_project_summary(db: Any, product_project_id: str) -> tuple[str, int | None]:
    """Deterministic compact summary derived from current plan revision."""
    product = db.get("product_projects", product_project_id) if product_project_id else None
    if not product:
        return "", None
    revision = int(product.get("plan_revision") or 0)
    key = (product_project_id, revision)
    if key in _summary_cache:
        return _summary_cache[key], revision
    plan_rows = db.query(
        "SELECT plan_json FROM plan_revisions WHERE project_id=? AND revision=? LIMIT 1",
        (product_project_id, revision),
    )
    if not plan_rows:
        return "", revision
    try:
        plan = (
            json.loads(plan_rows[0]["plan_json"])
            if isinstance(plan_rows[0]["plan_json"], str)
            else plan_rows[0]["plan_json"]
        )
    except Exception:
        return "", revision
    arch = plan.get("architecture") or {}
    parts = [
        f"Product: {plan.get('product_name', '')}",
        f"Goal: {plan.get('goal', '')}",
        f"Users: {plan.get('users', '')}",
        f"Stack: backend={arch.get('backend', '')} frontend={arch.get('frontend', '')} db={arch.get('database', '')}",
        f"Constraints: {'; '.join((plan.get('non_functional') or [])[:4])}",
    ]
    summary = "\n".join(p for p in parts if p.strip())
    _summary_cache[key] = summary
    return summary, revision


def project_requirements(db: Any, product_project_id: str) -> list[dict[str, Any]]:
    product = db.get("product_projects", product_project_id) if product_project_id else None
    if not product:
        return []
    revision = int(product.get("plan_revision") or 0)
    rows = db.query(
        "SELECT plan_json FROM plan_revisions WHERE project_id=? AND revision=? LIMIT 1",
        (product_project_id, revision),
    )
    if not rows:
        return []
    try:
        plan = json.loads(rows[0]["plan_json"]) if isinstance(rows[0]["plan_json"], str) else rows[0]["plan_json"]
    except Exception:
        return []
    reqs = plan.get("requirements") or []
    return [r for r in reqs if isinstance(r, dict)]


def phase_spec(db: Any, phase_id: str | None) -> dict[str, Any] | None:
    if not phase_id:
        return None
    try:
        row: dict[str, Any] | None = db.get("project_phases", phase_id)
    except Exception:
        return None
    return dict(row) if isinstance(row, dict) else None


def mapped_requirements(
    db: Any, product_project_id: str | None, requirement_ids: list[str], phase_id: str | None = None
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """Return (mapped req dicts, missing ids, warnings).

    IDs are stably deduplicated (F-04): duplicates must not consume duplicate
    mandatory budget for one rendered requirement body. Includes globals
    (non_functional as constraints separately).
    """
    warnings: list[str] = []
    if not product_project_id:
        return [], list(dict.fromkeys(requirement_ids)), warnings
    all_reqs = {r.get("id"): r for r in project_requirements(db, product_project_id) if r.get("id")}
    ids: list[str] = list(dict.fromkeys(requirement_ids))
    if phase_id:
        phase = phase_spec(db, phase_id)
        if phase:
            try:
                plan = None
                product = db.get("product_projects", product_project_id)
                rev = int((product or {}).get("plan_revision") or 0)
                rows = db.query(
                    "SELECT plan_json FROM plan_revisions WHERE project_id=? AND revision=? LIMIT 1",
                    (product_project_id, rev),
                )
                if rows:
                    raw = rows[0]["plan_json"]
                    plan = json.loads(raw) if isinstance(raw, str) else raw
                if plan:
                    for p in plan.get("phases") or []:
                        if isinstance(p, dict) and p.get("key") == phase.get("phase_key"):
                            for rid in p.get("requirement_ids") or []:
                                if rid not in ids:
                                    ids.append(rid)
                            break
            except Exception:
                warnings.append("ARCHITECTURE_MAPPING_INFERRED")
    mapped = [all_reqs[i] for i in ids if i in all_reqs]
    missing = [i for i in ids if i not in all_reqs]
    if missing:
        warnings.append("MISSING_REQUIREMENT_MAPPING")
    return mapped, missing, warnings


def relevant_architecture(
    db: Any, product_project_id: str | None, requirement_ids: list[str], task_text: str = ""
) -> tuple[list[dict[str, str]], list[str]]:
    """Deterministic relevance: global/critical always, scoped by keyword overlap otherwise.

    Known limitation (F-05, deferred): lexical matching can miss plurals and
    synonyms (payments/payment, checkout/Stripe billing). Safe because
    global/critical decisions are always included; a future improvement
    should use explicit deterministic tags/mappings, not an LLM classifier.
    """
    warnings: list[str] = []
    if not product_project_id:
        return [], warnings
    product = db.get("product_projects", product_project_id)
    if not product:
        return [], warnings
    rev = int(product.get("plan_revision") or 0)
    rows = db.query(
        "SELECT plan_json FROM plan_revisions WHERE project_id=? AND revision=? LIMIT 1",
        (product_project_id, rev),
    )
    if not rows:
        return [], warnings
    try:
        raw = rows[0]["plan_json"]
        plan = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return [], warnings
    arch = plan.get("architecture") or {}
    decisions = arch.get("decisions") or []
    haystack = f"{task_text} {' '.join(requirement_ids)}".lower()
    selected: list[dict[str, str]] = []
    for d in decisions:
        if not isinstance(d, dict):
            continue
        area = str(d.get("area", ""))
        choice = str(d.get("choice", ""))
        rationale = str(d.get("rationale", ""))
        blob = f"{area} {choice} {rationale}".lower()
        # Global: auth/security/data/api always relevant; else keyword overlap.
        is_global = any(k in blob for k in ("auth", "security", "data", "api", "database"))
        overlaps = any(tok in blob for tok in haystack.split() if len(tok) > 3)
        if is_global or overlaps or not haystack.strip():
            selected.append({"area": area, "choice": choice, "rationale": rationale})
    # Always include top-level stack lines as one decision if present.
    stack = f"backend={arch.get('backend', '')} frontend={arch.get('frontend', '')} db={arch.get('database', '')}"
    if stack.strip(" =") and not any(s["area"] == "stack" for s in selected):
        selected.insert(0, {"area": "stack", "choice": stack, "rationale": ""})
    if not selected:
        warnings.append("ARCHITECTURE_MAPPING_INFERRED")
    return selected[:8], warnings


def dependency_handoffs(
    db: Any, mission_id: str | None, dependency_ids: list[str]
) -> tuple[list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    handoffs: list[dict[str, Any]] = []
    if not mission_id or not dependency_ids:
        return handoffs, warnings
    for dep_id in dependency_ids:
        task = db.get("tasks", dep_id)
        if not task or task.get("mission_id") != mission_id:
            warnings.append("INVALID_DEPENDENCY_HANDOFF")
            continue
        runs = db.query(
            "SELECT id, git_commit_after, summary FROM provider_runs WHERE task_id=? ORDER BY started_at DESC LIMIT 1",
            (dep_id,),
        )
        run = runs[0] if runs else {}
        branch = db.query("SELECT branch_name FROM task_branches WHERE task_id=? LIMIT 1", (dep_id,))
        handoffs.append(
            {
                "task_id": dep_id,
                "title": task.get("title", ""),
                "status": task.get("status", ""),
                "checkpoint_sha": task.get("checkpoint_after") or run.get("git_commit_after") or "",
                "branch": branch[0]["branch_name"] if branch else "",
                "summary": (task.get("summary") or run.get("summary") or "")[:500],
            }
        )
        if not (task.get("checkpoint_after") or run.get("git_commit_after")):
            warnings.append("DEPENDENCY_CODE_NOT_PRESENT")
    return handoffs, warnings


def open_findings_for_scope(
    db: Any, mission_id: str | None, files_changed: list[str] | None = None, limit: int = 8
) -> list[dict[str, Any]]:
    if not mission_id:
        return []
    try:
        rows: list[dict[str, Any]] = db.query(
            "SELECT id, severity, category, file, description, recommended_fix, fingerprint FROM review_findings"
            " WHERE mission_id=? AND status IN ('open','repair_attempted') ORDER BY created_at DESC LIMIT ?",
            (mission_id, limit * 2),
        )
    except Exception:
        return []
    # DOG-02: include unresolved retry lineage (deduped by fingerprint;
    # local rows shadow inherited duplicates; resolved locals hide inherited).
    try:
        from .review import inherited_open_findings as _inh

        _local_fps = {str(r.get("fingerprint") or "") for r in rows if r.get("fingerprint")}
        try:
            _local_all = db.query("SELECT fingerprint FROM review_findings WHERE mission_id=?", (mission_id,))
            _local_fps |= {str(r.get("fingerprint") or "") for r in _local_all if r.get("fingerprint")}
        except Exception:
            logger.debug("local fingerprint lookup failed", exc_info=True)
        for _inh_row in _inh(db, mission_id):
            _fp = str(_inh_row.get("fingerprint") or "")
            if not _fp or _fp in _local_fps:
                continue
            _local_fps.add(_fp)
            rows.append(
                {
                    "id": _inh_row.get("id"),
                    "severity": _inh_row.get("severity"),
                    "category": _inh_row.get("category"),
                    "file": _inh_row.get("file"),
                    "description": _inh_row.get("description"),
                    "recommended_fix": _inh_row.get("recommended_fix"),
                    "fingerprint": _fp,
                }
            )
    except Exception:
        logger.debug("inherited findings lookup failed", exc_info=True)
    if not files_changed:
        return list(rows[:limit])
    changed = {f.lower() for f in files_changed}
    scored: list[tuple[int, dict[str, Any]]] = []
    for r in rows:
        f = str(r.get("file") or "").lower()
        score = 1 if f and any(f in c or c in f for c in changed) else 0
        sev_boost = {"BLOCKER": 3, "HIGH": 2, "MEDIUM": 1, "LOW": 0}.get(str(r.get("severity", "")).upper(), 0)
        scored.append((score * 10 + sev_boost, r))
    scored.sort(key=lambda x: -x[0])
    return [r for _, r in scored[:limit]]


def environment_contract(repo_hint: str = "", workspace_scope: list[str] | None = None) -> str:
    scope = " ".join(workspace_scope or []).lower()
    lines = ["Environment: local repo; toolchain via repo manifests."]
    if "frontend" in scope or "node" in repo_hint.lower() or not scope:
        lines.append("Frontend: npm run <script> (npm only); node <path>.js single file.")
    if "backend" in scope or "python" in repo_hint.lower() or not scope:
        lines.append("Backend: pytest <path>, python3 -m {pytest,mypy,ruff}, uv run <inner>.")
    lines.append("Work only in the assigned workspace; never touch secrets or .env values.")
    return "\n".join(lines)


# -- role policies ------------------------------------------------------------

OUTPUT_CONTRACTS: dict[str, str] = {
    "planner": "Output exactly one PRODUCT_PLAN_JSON block matching the schema. No prose after it.",
    "implementer": (
        "Implement the task contract in the workspace. Run relevant checks. "
        "End with a one-paragraph summary of files changed and verification."
    ),
    "reviewer": (
        "Output exactly one REVIEW_FINDINGS_JSON line plus optional VERIFIED_FIXED_JSON. No implementation prose."
    ),
    "repairer": (
        "Fix the listed findings only within scope. Rerun failing checks. "
        "End with a short summary of the fix and evidence."
    ),
    "testing": (
        "Execute the listed scope with the repo toolchain. "
        "Report failing tests with command, exit code, and short excerpt."
    ),
}

SAFE_RULES = (
    "Work only in the assigned repository/worktree. Do not fabricate results. "
    "Respect scope. Do not expose secrets. Run verification before claiming success."
)


def _canonical_objective(task_title: str, task_description: str, task_objective: str) -> tuple[str, bool]:
    """Deduplicate identical source lines emitted via multiple plumbing paths.

    The sequential/parallel engines historically set
    task_objective=f"{title}\\n{task}" while also setting task_title/title
    and task_description/task, so naive concatenation emits title+task twice.
    Dedupe exact duplicate lines (stripped) preserving order; distinct roles
    (requirement vs acceptance vs safety) live in separate blocks and are
    never merged here. Returns (text, deduped).
    """
    seen: set[str] = set()
    out_lines: list[str] = []
    total = 0
    for chunk in (task_title or "", task_description or "", task_objective or ""):
        for raw in chunk.splitlines():
            line = raw.strip()
            if not line:
                continue
            total += 1
            if line in seen:
                continue
            seen.add(line)
            out_lines.append(line)
    # Single-line inputs without newlines are handled above; when all inputs
    # are single-line identical (e.g. description == objective), the loop
    # already dedupes. Fall back to stripped whole-text when no lines.
    if not out_lines:
        combined = f"{task_title}\n{task_description}\n{task_objective}".strip()
        return combined, False
    return "\n".join(out_lines), total > len(out_lines)


def prior_planning_summary(db: Any, mission_id: str | None, max_chars: int = 2000) -> str:
    """Smallest structured planning artifact for the implementer (DOG-04).

    Sequential repository missions have no plan_revisions/requirements; the
    only durable planning decision record is the planning task summary.
    Returned coherently truncated (never a mid-JSON slice) and empty when
    no planning ran. Callers render it as a budget-managed PREFERRED block,
    never as an arbitrary [:200] fragment.
    """
    if not mission_id:
        return ""
    try:
        rows = db.query(
            "SELECT summary FROM tasks WHERE mission_id=? AND role='planning'"
            " AND status='completed' AND summary IS NOT NULL AND summary != ''"
            " ORDER BY finished_at DESC, created_at DESC LIMIT 1",
            (mission_id,),
        )
    except Exception:
        return ""
    if not rows:
        try:
            rows = db.query(
                "SELECT summary FROM tasks WHERE mission_id=? AND role='planning'"
                " AND summary IS NOT NULL AND summary != ''"
                " ORDER BY created_at DESC LIMIT 1",
                (mission_id,),
            )
        except Exception:
            return ""
    if not rows:
        return ""
    raw = str(rows[0].get("summary") or "").strip()
    if not raw:
        return ""
    try:
        from .handoff import truncate_coherent as _coherent
    except Exception:
        return raw[:max_chars]
    return _coherent(raw, max_chars)


FAILOVER_TEXT_CHARS = 1800


def _add_failover_block(add: Any, spec: ContextCompileSpec) -> None:
    """Previous failed attempt's own report: PREFERRED, clearly unverified."""
    if not spec.failover_text:
        return
    add(
        "failover",
        BlockType.FAILURE_EVIDENCE,
        Priority.PREFERRED,
        f"Previous attempt (partial, unverified — re-check before relying on it):\n"
        f"{spec.failover_text[-FAILOVER_TEXT_CHARS:]}".strip(),
        source_kind="evidence",
        source_ref="failover",
    )


def build_candidate_blocks(
    spec: ContextCompileSpec, db: Any, config: Any | None = None
) -> tuple[list[ContextBlock], dict[str, Any], list[str]]:
    """Collect candidate blocks per role. Returns (blocks, aux, warnings)."""
    warnings: list[str] = []
    blocks: list[ContextBlock] = []
    aux: dict[str, Any] = {}
    role = spec.role.lower()

    def add(
        bid: str,
        btype: str,
        priority: str,
        content: str,
        *,
        source_kind: str = "",
        source_ref: str = "",
        required: bool = False,
        summarizable: bool = True,
        reason: str = "",
        compact: str | None = None,
        reference: str | None = None,
    ) -> None:
        if not content.strip():
            return
        blocks.append(
            ContextBlock(
                id=bid,
                type=btype,
                priority=priority,
                content=content,
                source_kind=source_kind,
                source_ref=source_ref,
                required=required,
                summarizable=summarizable,
                reason=reason or priority,
                compact_content=compact,
                reference_content=reference,
            )
        )

    # System instructions: single canonical copy per role (dedup source).
    role_instruction = {
        "planner": "You are the product planner. Produce a complete, verifiable product plan.",
        "implementer": "You are the implementer. Implement exactly the task contract.",
        "reviewer": "You are an independent reviewer. Judge the candidate against the contract only.",
        "repairer": "You are the repairer. Fix the listed defects within scope.",
        "testing": "You are the testing engineer. Execute checks and report evidence.",
    }.get(role, f"You are the {role} engineer.")
    add(
        "sys",
        BlockType.SYSTEM_INSTRUCTIONS,
        Priority.MANDATORY,
        f"{role_instruction} {SAFE_RULES}",
        source_kind="policy",
        source_ref=f"role:{role}",
        required=True,
        summarizable=False,
        reason="role authority + safety",
    )

    if role in ("planner", "planning", "product_planner", "mission_planner", "dag_planner"):
        summary, rev = get_project_summary(db, spec.product_project_id or "") if spec.product_project_id else ("", None)
        if spec.task_objective or spec.task_title:
            _plan_obj, _plan_dedup = _canonical_objective(spec.task_title, "", spec.task_objective)
            if _plan_dedup:
                warnings.append("OBJECTIVE_DUPLICATE_SUPPRESSED")
            add(
                "objective",
                BlockType.TASK_OBJECTIVE,
                Priority.MANDATORY,
                f"Objective:\n{_plan_obj}".strip(),
                source_kind="spec",
                source_ref="task_objective",
                required=True,
                summarizable=False,
            )
        # Planner keeps schema/rules as mandatory (from legacy builder, preserved verbatim path via caller).
        if spec.extra_context:
            add(
                "planner-rules",
                BlockType.PHASE_CONTEXT,
                Priority.MANDATORY,
                spec.extra_context,
                source_kind="policy",
                source_ref="planner-schema",
                required=True,
                summarizable=False,
            )
        if summary:
            add(
                "proj-sum",
                BlockType.PROJECT_SUMMARY,
                Priority.PREFERRED,
                summary,
                source_kind="plan",
                source_ref=f"plan_rev:{rev}",
                reason="existing decisions",
            )
        aux["plan_revision"] = rev if rev is not None else spec.plan_revision
        _add_failover_block(add, spec)
        contract = OUTPUT_CONTRACTS.get("planner", OUTPUT_CONTRACTS["implementer"])
        add(
            "output",
            BlockType.OUTPUT_CONTRACT,
            Priority.MANDATORY,
            f"OUTPUT CONTRACT\n{contract}",
            source_kind="policy",
            source_ref="output:planner",
            required=True,
            summarizable=False,
        )
        return blocks, aux, warnings

    # Non-planner roles: objective first. Identical lines arriving via
    # multiple plumbing paths (title + description + objective) are emitted
    # once; distinct semantic roles live in separate blocks and are kept.
    objective, _obj_dedup = _canonical_objective(spec.task_title, spec.task_description, spec.task_objective)
    if _obj_dedup:
        warnings.append("OBJECTIVE_DUPLICATE_SUPPRESSED")
    if objective:
        add(
            "objective",
            BlockType.TASK_OBJECTIVE,
            Priority.MANDATORY,
            f"OBJECTIVE / CURRENT TASK\n{objective}",
            source_kind="task" if spec.task_id else "mission",
            source_ref=spec.task_id or spec.mission_id or "",
            required=True,
            summarizable=False,
        )

    # Requirement + acceptance projection (mandatory when mapped).
    # Unresolved explicit references fail closed for contract roles (F-02):
    # legacy may omit the same context compiled mode exists to guarantee.
    mapped, missing, w1 = mapped_requirements(db, spec.product_project_id, spec.requirement_ids, spec.project_phase_id)
    warnings.extend(w1)
    if missing and _is_contract_role(role):
        raise ContextCompileError(
            "MISSING_REQUIREMENT_MAPPING",
            f"required requirement reference(s) cannot be resolved: {', '.join(missing)}. "
            "Provider will NOT be invoked.",
        )
    aux["mapped_requirement_ids"] = [r.get("id") for r in mapped if r.get("id")]
    for r in mapped:
        rid = str(r.get("id", ""))
        add(
            f"req-{rid}",
            BlockType.PRODUCT_REQUIREMENT,
            Priority.MANDATORY,
            f"Requirement {rid} — {r.get('title', '')}\n{r.get('description', '')}".strip(),
            source_kind="plan",
            source_ref=f"req:{rid}",
            required=True,
            summarizable=False,
        )
        for a in r.get("acceptance") or []:
            if not isinstance(a, dict):
                continue
            aid = str(a.get("id", ""))
            add(
                f"acc-{aid}",
                BlockType.ACCEPTANCE_CRITERION,
                Priority.MANDATORY,
                f"Acceptance {aid}: {a.get('description', '')}\nVerify: {a.get('verify', '')}".strip(),
                source_kind="plan",
                source_ref=f"criterion:{aid}",
                required=True,
                summarizable=False,
            )
    if spec.requirement_ids and not mapped:
        warnings.append("MISSING_REQUIREMENT_MAPPING")

    # Architecture projection.
    arch, w2 = relevant_architecture(db, spec.product_project_id, spec.requirement_ids, objective)
    warnings.extend(w2)
    for i, d in enumerate(arch):
        add(
            f"arch-{i}",
            BlockType.ARCHITECTURE_DECISION,
            Priority.MANDATORY if i == 0 else Priority.PREFERRED,
            f"Architecture [{d['area']}]: {d['choice']}\n{d['rationale']}".strip(),
            source_kind="plan",
            source_ref=f"arch:{d['area']}",
            required=(i == 0 and role in ("implementer", "reviewer", "repairer")),
            compact=f"[{d['area']}]: {d['choice']}" if d["choice"] else None,
        )

    # Prior planning decisions for the implementer (DOG-04). The full plan
    # is never pasted; the durable planning task summary is carried as a
    # budget-managed PREFERRED block with compact/reference fallbacks, so a
    # 44k-char plan becomes a coherent bounded handoff, never a [:200]
    # mid-JSON fragment. Mandatory contract (objective/requirements/
    # acceptance/architecture) still fails closed on overflow.
    if role in ("implementer", "implementation"):
        try:
            _plan_sum = prior_planning_summary(db, spec.mission_id, 2000)
        except Exception:
            _plan_sum = ""
        if _plan_sum:
            _compact = _plan_sum[:500].rstrip()
            if len(_plan_sum) > 500:
                _compact += "\n... [planning summary compacted]"
            add(
                "plan-handoff",
                BlockType.PHASE_CONTEXT,
                Priority.PREFERRED,
                f"Prior planning decisions (bounded handoff, full plan in task logs):\n{_plan_sum}",
                source_kind="handoff",
                source_ref="planning-summary",
                compact=f"Planning: {_compact}",
                reference="Planning summary available in task logs; inspect before implementing.",
            )

    # Phase context (bounded, no full plan JSON).
    if spec.project_phase_id:
        phase = phase_spec(db, spec.project_phase_id)
        if phase:
            phase_text = f"Phase {phase.get('phase_key', '')} — {phase.get('title', '')}\nGoal: {phase.get('goal', '')}"
            add(
                "phase",
                BlockType.PHASE_CONTEXT,
                Priority.PREFERRED,
                phase_text,
                source_kind="phase",
                source_ref=str(spec.project_phase_id),
            )

    # Dependency handoffs (explicit, attributable; never full logs).
    deps, w3 = dependency_handoffs(db, spec.mission_id, spec.dependency_ids)
    warnings.extend(w3)
    aux["dependency_handoffs"] = deps
    for h in deps:
        verified = spec.dependency_verified.get(h["task_id"])
        if verified is False:
            # Proven-absent required artifact: fail closed, never warn-and-run.
            raise ContextCompileError(
                "UNVERIFIED_DEPENDENCY_ARTIFACT",
                f"dependency {h['task_id']} result is NOT present in the task input artifact."
                " Provider will NOT be invoked.",
            )
        presence = (
            "present in task input artifact: YES"
            if verified
            else "present in task input artifact: UNKNOWN (verify locally before relying on it)"
        )
        full = (
            f"Dependency {h['task_id']} ({h['title']}) — {h['status']}\n"
            f"Checkpoint: {h['checkpoint_sha'] or '(no checkpoint)'}\n"
            f"{presence}\n"
            f"Summary: {h['summary'] or '(no summary)'}"
        )
        compact = f"{h['task_id']} @ {h['checkpoint_sha'][:8] if h['checkpoint_sha'] else 'no-sha'}: {h['title']}"
        reference = f"Dependency checkpoint: {h['checkpoint_sha'] or 'unknown'}. Inspect worktree when necessary."
        add(
            f"dep-{h['task_id']}",
            BlockType.DEPENDENCY_HANDOFF,
            Priority.MANDATORY,
            full,
            source_kind="task",
            source_ref=str(h["task_id"]),
            required=True,
            compact=compact,
            reference=reference,
        )
        if not h["checkpoint_sha"]:
            warnings.append("DEPENDENCY_CODE_NOT_PRESENT")

    # Git context per role (bounded metadata, never full history).
    # DOG-03: reviewer map is derived from Git for the exact reviewed range
    # (base..head), never from findings metadata. Bounded with honest
    # omission markers; the reviewer inspects full hunks locally.
    if spec.candidate_sha or spec.base_sha:
        if role == "reviewer":
            if spec.base_sha and spec.candidate_sha:
                _git_files = list(spec.git_files_changed or [])
                _shown = _git_files[:30]
                if len(_git_files) > 30:
                    warnings.append("GIT_DIFF_FILE_LIST_TRUNCATED")
                _summary = (spec.git_diff_summary or "")[:2000]
                if len(spec.git_diff_summary or "") > 2000:
                    warnings.append("GIT_DIFF_SUMMARY_TRUNCATED")
                    _summary = _summary.rstrip() + "\n... [summary truncated; inspect locally]"
                if _git_files:
                    _files_line = f"Changed files ({len(_git_files)}): {', '.join(_shown)}"
                    if len(_git_files) > 30:
                        _files_line += f" ... [+{len(_git_files) - 30} more; list truncated]"
                else:
                    _files_line = "Changed files (0): no file changes in range"
                _git_body = (
                    f"Review exact range: base={spec.base_sha} head={spec.candidate_sha}\n"
                    f"{_files_line}\n"
                    f"{_summary}".strip()
                    + f"\nInspect the exact range locally: git diff {spec.base_sha}..{spec.candidate_sha} "
                    "(do not rely on the summary alone)."
                )
                add(
                    "git",
                    BlockType.GIT_DIFF,
                    Priority.MANDATORY,
                    _git_body,
                    source_kind="git",
                    source_ref=f"{spec.base_sha}..{spec.candidate_sha}",
                    required=True,
                    summarizable=False,
                    reference=f"Diff {spec.base_sha[:8]}..{spec.candidate_sha[:8]}; inspect locally for full hunks.",
                )
            else:
                warnings.append("MODEL_CONTEXT_LIMIT_UNKNOWN")
        elif role == "repairer":
            _rel_files = ", ".join(spec.git_files_changed[:20]) or ", ".join(spec.workspace_scope[:20])
            add(
                "git",
                BlockType.GIT_DIFF,
                Priority.PREFERRED,
                f"Candidate: {spec.candidate_sha or spec.base_sha or 'unknown'}\n"
                f"Relevant files: {_rel_files or '(see scope)'}",
                source_kind="git",
                source_ref=str(spec.candidate_sha or spec.base_sha or ""),
            )
        else:
            add(
                "git",
                BlockType.GIT_DIFF,
                Priority.BUDGET_DEPENDENT,
                f"Base: {spec.base_sha or 'unknown'}",
                source_kind="git",
                source_ref=str(spec.base_sha or ""),
                reference=f"Base {(spec.base_sha or '')[:8]}; inspect worktree as needed.",
            )

    # Findings / failure evidence.
    if role == "reviewer":
        findings = open_findings_for_scope(db, spec.mission_id, spec.git_files_changed)
        # Exclude resolved/unrelated by construction (only open queried, relevance-sorted).
        for f in findings[:6]:
            _fix = f.get("recommended_fix", "")
            add(
                f"find-{f['id']}",
                BlockType.OPEN_FINDING,
                Priority.PREFERRED,
                f"Open finding [{f['severity']}] {f['id']}: {f['description']} → {_fix}".strip(),
                source_kind="finding",
                source_ref=str(f["id"]),
            )
        # PROHIBITED: implementer self-assessment never added (enforced by omission).
        # Writer identity (not reasoning) from exact provenance so the reviewer
        # knows whose code it judges; agrees with reviewer-selection exclusion.
        if spec.mission_id:
            try:
                from .provenance import mission_provider_writers

                _writers, _complete = mission_provider_writers(db, spec.mission_id)
                if _writers and _complete:
                    add(
                        "writers",
                        BlockType.WRITER_PROVENANCE,
                        Priority.PREFERRED,
                        "Candidate writers (identity only, no self-assessment): "
                        + ", ".join(sorted(_writers))
                        + ". Judge the artifact against the contract, not their claims.",
                        source_kind="provenance",
                        source_ref=str(spec.mission_id),
                    )
                elif not _complete:
                    warnings.append("WRITER_PROVENANCE_INCOMPLETE")
            except Exception:
                warnings.append("WRITER_PROVENANCE_INCOMPLETE")
    elif role == "repairer":
        # Only requested/current findings, not all history.
        if spec.finding_ids and spec.mission_id:
            try:
                _placeholders = ",".join("?" for _ in spec.finding_ids)
                _base = "SELECT id, severity, file, description, recommended_fix FROM review_findings "
                _sql = _base + "WHERE mission_id=? AND id IN (" + _placeholders + ")"  # noqa: S608
                rows = db.query(_sql, (spec.mission_id, *spec.finding_ids))  # noqa: S608
            except Exception:
                rows = []
            for f in rows:
                _desc = f.get("description", "")
                _rec = f.get("recommended_fix", "")
                add(
                    f"find-{f['id']}",
                    BlockType.OPEN_FINDING,
                    Priority.MANDATORY,
                    f"Defect [{f.get('severity', '')}] {f['id']} {f.get('file') or ''}: {_desc} → {_rec}".strip(),
                    source_kind="finding",
                    source_ref=str(f["id"]),
                    required=True,
                    summarizable=False,
                )
        if spec.failure_text:
            tail = spec.failure_text[-2000:]
            cmd = f"Command: {spec.failure_command} (exit {spec.failure_exit_code})" if spec.failure_command else ""
            add(
                "failure",
                BlockType.FAILURE_EVIDENCE,
                Priority.MANDATORY,
                f"Observed vs expected:\n{tail}\n{cmd}".strip(),
                source_kind="evidence",
                source_ref="failure-tail",
                required=True,
                summarizable=False,
            )
    else:
        if spec.failure_text:
            add(
                "failure",
                BlockType.FAILURE_EVIDENCE,
                Priority.PREFERRED,
                f"Prior failure (bounded tail):\n{spec.failure_text[-1500:]}".strip(),
                source_kind="evidence",
                source_ref="failure-tail",
            )

    if role != "reviewer":
        _add_failover_block(add, spec)

    # Environment contract (compact, scoped).
    add(
        "env",
        BlockType.ENVIRONMENT_CONTRACT,
        Priority.PREFERRED,
        environment_contract("", spec.workspace_scope),
        source_kind="config",
        source_ref="toolchain",
    )

    # Output contract (single, role-specific).
    contract = OUTPUT_CONTRACTS.get(role, OUTPUT_CONTRACTS["implementer"])
    add(
        "output",
        BlockType.OUTPUT_CONTRACT,
        Priority.MANDATORY,
        f"OUTPUT CONTRACT\n{contract}",
        source_kind="policy",
        source_ref=f"output:{role}",
        required=True,
        summarizable=False,
    )

    # Code references on demand (never embed large source).
    if spec.workspace_scope:
        _scope_full = ", ".join(spec.workspace_scope[:20])
        add(
            "code-ref",
            BlockType.RELEVANT_CODE,
            Priority.RETRIEVE_ON_DEMAND,
            f"Relevant scope: {_scope_full}. Inspect files locally as needed; "
            "do not paste large files into the response.",
            source_kind="scope",
            source_ref="workspace_scope",
            reference=f"Scope: {', '.join(spec.workspace_scope[:10])}",
        )

    # Plan revision binds all plan-derived context for invalidation.
    if spec.product_project_id:
        try:
            _prod = db.get("product_projects", spec.product_project_id)
            if _prod is not None:
                aux["plan_revision"] = int(_prod.get("plan_revision") or 0)
        except Exception:
            logger.debug("plan revision lookup failed", exc_info=True)
    if spec.plan_revision is not None and aux.get("plan_revision") is None:
        aux["plan_revision"] = spec.plan_revision

    return blocks, aux, warnings


# -- budget + render ----------------------------------------------------------

SECTION_HEADINGS = {
    BlockType.SYSTEM_INSTRUCTIONS: "ROLE AND AUTHORITY",
    BlockType.TASK_OBJECTIVE: "OBJECTIVE / CURRENT TASK",
    BlockType.PRODUCT_REQUIREMENT: "REQUIRED BEHAVIOR AND ACCEPTANCE IDS",
    BlockType.ACCEPTANCE_CRITERION: "REQUIRED BEHAVIOR AND ACCEPTANCE IDS",
    BlockType.ARCHITECTURE_DECISION: "RELEVANT PROJECT / ARCHITECTURE CONTRACTS",
    BlockType.DEPENDENCY_HANDOFF: "DEPENDENCY ARTIFACTS",
    BlockType.GIT_DIFF: "FILES / ALLOWED SCOPE / BASE AND HEAD",
    BlockType.OPEN_FINDING: "CURRENT EVIDENCE OR FAILURES",
    BlockType.FAILURE_EVIDENCE: "CURRENT EVIDENCE OR FAILURES",
    BlockType.PROJECT_SUMMARY: "RELEVANT PROJECT / ARCHITECTURE CONTRACTS",
    BlockType.PHASE_CONTEXT: "OBJECTIVE / CURRENT TASK",
    BlockType.ENVIRONMENT_CONTRACT: "VERIFICATION COMMANDS AND EXPECTED OBSERVATIONS",
    BlockType.OUTPUT_CONTRACT: "OUTPUT CONTRACT",
    BlockType.WRITER_PROVENANCE: "ROLE AND AUTHORITY",
    BlockType.RELEVANT_CODE: "FILES / ALLOWED SCOPE / BASE AND HEAD",
    BlockType.TEST_RESULT: "CURRENT EVIDENCE OR FAILURES",
}

SECTION_ORDER = [
    "ROLE AND AUTHORITY",
    "OBJECTIVE / CURRENT TASK",
    "REQUIRED BEHAVIOR AND ACCEPTANCE IDS",
    "RELEVANT PROJECT / ARCHITECTURE CONTRACTS",
    "DEPENDENCY ARTIFACTS",
    "FILES / ALLOWED SCOPE / BASE AND HEAD",
    "CURRENT EVIDENCE OR FAILURES",
    "CONSTRAINTS / UNKNOWNS / STOP CONDITIONS",
    "VERIFICATION COMMANDS AND EXPECTED OBSERVATIONS",
    "OUTPUT CONTRACT",
]


def _select_with_budget(
    blocks: list[ContextBlock], budget: int
) -> tuple[list[tuple[ContextBlock, str, str]], list[BlockDecision], list[str], int]:
    """Return ([(block, representation, text)], decisions, warnings, used_tokens)."""
    decisions: list[BlockDecision] = []
    warnings: list[str] = []
    selected: list[tuple[ContextBlock, str, str]] = []
    used = 0

    def tokens(text: str) -> int:
        return math.ceil(len(text) / 4) if text else 0

    # Prohibited never included.
    live = [b for b in blocks if b.priority != Priority.PROHIBITED]
    for b in blocks:
        if b.priority == Priority.PROHIBITED:
            decisions.append(
                BlockDecision(
                    b.id,
                    b.type,
                    b.source_kind,
                    b.source_ref,
                    b.priority,
                    b.chars,
                    0,
                    0,
                    Representation.OMITTED,
                    False,
                    OmissionReason.PROHIBITED,
                    b.content_hash(),
                )
            )

    # Mandatory first.
    for b in [x for x in live if x.priority == Priority.MANDATORY]:
        t = tokens(b.content)
        if used + t > budget:
            raise ContextCompileError(
                "MANDATORY_CONTEXT_OVERFLOW",
                f"mandatory block {b.id} ({t} est) exceeds remaining budget ({budget - used})",
            )
        selected.append((b, Representation.FULL, b.content))
        used += t
        decisions.append(
            BlockDecision(
                b.id,
                b.type,
                b.source_kind,
                b.source_ref,
                b.priority,
                b.chars,
                b.chars,
                t,
                Representation.FULL,
                True,
                "included",
                b.content_hash(),
            )
        )
    # Preferred (+ budget-dependent full, else compact/reference).
    for b in [x for x in live if x.priority in (Priority.PREFERRED, Priority.BUDGET_DEPENDENT)]:
        full_t = tokens(b.content)
        if used + full_t <= budget:
            selected.append((b, Representation.FULL, b.content))
            used += full_t
            decisions.append(
                BlockDecision(
                    b.id,
                    b.type,
                    b.source_kind,
                    b.source_ref,
                    b.priority,
                    b.chars,
                    b.chars,
                    full_t,
                    Representation.FULL,
                    True,
                    "included",
                    b.content_hash(),
                )
            )
            continue
        # Try compact then reference.
        for rep, text in ((Representation.COMPACT, b.compact_content), (Representation.REFERENCE, b.reference_content)):
            if not text:
                continue
            t = tokens(text)
            if used + t <= budget:
                selected.append((b, rep, text))
                used += t
                decisions.append(
                    BlockDecision(
                        b.id,
                        b.type,
                        b.source_kind,
                        b.source_ref,
                        b.priority,
                        b.chars,
                        len(text),
                        t,
                        rep,
                        True,
                        "compacted",
                        b.content_hash(),
                    )
                )
                warnings.append("PROMPT_BUDGET_PRESSURE")
                break
        else:
            if b.priority == Priority.PREFERRED and not b.summarizable:
                # Required-ish preferred without compact variant: keep if mandatory-like? else omit with warning.
                pass
            decisions.append(
                BlockDecision(
                    b.id,
                    b.type,
                    b.source_kind,
                    b.source_ref,
                    b.priority,
                    b.chars,
                    0,
                    0,
                    Representation.OMITTED,
                    False,
                    OmissionReason.BUDGET,
                    b.content_hash(),
                )
            )
            warnings.append("PROMPT_BUDGET_PRESSURE")
    # Summary-only / retrieve-on-demand → reference form.
    for b in [x for x in live if x.priority in (Priority.SUMMARY_ONLY, Priority.RETRIEVE_ON_DEMAND)]:
        text = b.reference_content or b.compact_content
        if text and used + tokens(text) <= budget:
            selected.append((b, Representation.REFERENCE, text))
            used += tokens(text)
            decisions.append(
                BlockDecision(
                    b.id,
                    b.type,
                    b.source_kind,
                    b.source_ref,
                    b.priority,
                    b.chars,
                    len(text),
                    tokens(text),
                    Representation.REFERENCE,
                    True,
                    OmissionReason.REFERENCE_ONLY,
                    b.content_hash(),
                )
            )
        else:
            decisions.append(
                BlockDecision(
                    b.id,
                    b.type,
                    b.source_kind,
                    b.source_ref,
                    b.priority,
                    b.chars,
                    0,
                    0,
                    Representation.OMITTED,
                    False,
                    OmissionReason.REFERENCE_ONLY,
                    b.content_hash(),
                )
            )
    return selected, decisions, warnings, used


def render_prompt(selected: list[tuple[ContextBlock, str, str]]) -> str:
    grouped: dict[str, list[str]] = {}
    for block, _rep, text in selected:
        heading = SECTION_HEADINGS.get(block.type, "OBJECTIVE / CURRENT TASK")
        grouped.setdefault(heading, []).append(text)
    parts: list[str] = []
    for heading in SECTION_ORDER:
        bodies = grouped.get(heading)
        if not bodies:
            continue
        # Deduplicate identical bodies within a section (single copy).
        seen: set[str] = set()
        unique: list[str] = []
        for body in bodies:
            h = hashlib.sha256(body.encode()).hexdigest()
            if h not in seen:
                seen.add(h)
                unique.append(body)
        parts.append(f"## {heading}\n" + "\n\n".join(unique))
    return "\n\n".join(parts).strip() + "\n"


class ContextCompiler:
    def __init__(self, db: Any = None, config: Any | None = None):
        self.db = db
        self.config = config

    def compile(self, spec: ContextCompileSpec) -> CompiledContext:
        if self.db is None:
            raise ContextCompileError("MISSING_PLAN_REVISION", "compiler requires a database handle")
        blocks, aux, warnings = build_candidate_blocks(spec, self.db, self.config)
        # Prohibited safety net: drop anything secret-shaped (defense in depth; source should never include).
        safe_blocks: list[ContextBlock] = []
        for b in blocks:
            if b.sensitivity == "secret":
                warnings.append("PROHIBITED")
                continue
            safe_blocks.append(b)
        budget = budget_for_role(spec.role, self.config)
        try:
            selected, decisions, w2, used = _select_with_budget(safe_blocks, budget)
        except ContextCompileError:
            raise
        warnings.extend(w2)
        # Unknown model limits are never fabricated; note when relevant.
        if not spec.provider:
            warnings.append("MODEL_CONTEXT_LIMIT_UNKNOWN")
        prompt = render_prompt(selected)
        if not prompt.strip():
            raise ContextCompileError("MISSING_REQUIREMENT_MAPPING", "compiled prompt is empty")
        # Writer provenance warning when knowable and ambiguous.
        if spec.role == "reviewer":
            if not spec.candidate_sha:
                warnings.append("CANDIDATE_SHA_UNKNOWN")
            if spec.mission_id:
                try:
                    from .provenance import mission_provider_writers as _mpw

                    _, _wcomplete = _mpw(self.db, spec.mission_id)
                    if not _wcomplete:
                        warnings.append("WRITER_PROVENANCE_INCOMPLETE")
                except Exception:
                    warnings.append("WRITER_PROVENANCE_INCOMPLETE")
        from .context_manifest import count_words, redacted_hash

        digest, _basis = redacted_hash(prompt)
        plan_rev = aux.get("plan_revision")
        if spec.plan_revision is not None:
            plan_rev = spec.plan_revision
        # Repeated-context ratio vs previous compiled/legacy prompt for same owner (hash overlap proxy:
        # fraction of included block hashes seen in the immediately preceding run's manifest).
        repeated_ratio = 0.0
        try:
            prev_ratio = _previous_block_overlap(self.db, spec, decisions)
            repeated_ratio = prev_ratio
        except Exception:
            logger.debug("repeated ratio computation failed", exc_info=True)
        return CompiledContext(
            prompt=prompt,
            prompt_hash=digest,
            prompt_chars=len(prompt),
            prompt_bytes=len(prompt.encode("utf-8")),
            prompt_words=count_words(prompt),
            estimated_tokens=math.ceil(len(prompt) / 4) if prompt else 0,
            blocks=decisions,
            warnings=sorted(set(warnings)),
            budget_estimated_tokens=budget,
            used_estimated_tokens=used,
            remaining_estimated_tokens=max(0, budget - used),
            repeated_context_ratio=repeated_ratio,
            plan_revision=plan_rev,
        )


def _previous_block_overlap(db: Any, spec: ContextCompileSpec, decisions: list[BlockDecision]) -> float:
    clauses: list[str] = []
    params: list[Any] = []
    if spec.mission_id:
        clauses.append("mission_id=?")
        params.append(spec.mission_id)
    elif spec.product_project_id:
        clauses.append("product_project_id=?")
        params.append(spec.product_project_id)
    else:
        return 0.0
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = db.query(
        f"SELECT id FROM provider_runs {where} ORDER BY started_at DESC LIMIT 1",  # noqa: S608
        tuple(params),
    )
    if not rows:
        return 0.0
    manifest = db.get("run_context_manifests", rows[0]["id"], key="run_id")
    if not manifest or not isinstance(manifest.get("blocks_json"), str):
        return 0.0
    try:
        prev = json.loads(manifest["blocks_json"])
    except Exception:
        return 0.0
    prev_hashes = set()
    if isinstance(prev, dict):
        for b in prev.get("blocks", []) or []:
            if isinstance(b, dict) and b.get("hash"):
                prev_hashes.add(b["hash"])
    elif isinstance(prev, list):
        for b in prev:
            if isinstance(b, dict) and b.get("hash"):
                prev_hashes.add(b["hash"])
    if not prev_hashes:
        return 0.0
    included = [d for d in decisions if d.included]
    if not included:
        return 0.0
    overlap_chars = sum(d.included_chars for d in included if d.hash in prev_hashes)
    total_chars = sum(d.included_chars for d in included)
    return round(overlap_chars / total_chars, 4) if total_chars else 0.0


def should_use_compiled(config: Any | None, role: str | None = None) -> bool:
    _ = role
    return context_mode(config) in ("compiled", "shadow")


# -- canonical stage/role mapping (one mapping, no divergence) -----------------
# stage (invocation) -> context role (compiler policy). Both persisted.

STAGE_ROLE_MAP: dict[str, str] = {
    "product_plan": "planner",
    "mission_plan": "planner",
    "dag_plan": "planner",
    "planning": "planner",
    "implementation": "implementer",
    "task": "implementer",
    "testing": "testing",
    "review": "reviewer",
    "repair": "repairer",
}


def role_for_stage(stage: str, role: str) -> str:
    """Canonical mapping between invocation stage and compiler role."""
    mapped = STAGE_ROLE_MAP.get(stage, "")
    if mapped:
        return mapped
    return (role or "implementer").lower()


# -- optional refinement (disabled by default) ---------------------------------
# Future LLM prompt refinement stays behind this interface. Default path is
# deterministic and consumes zero provider quota.


class PromptRefiner:
    enabled = False

    def refine(self, prompt: str, spec: ContextCompileSpec) -> str:
        raise NotImplementedError("prompt refinement is disabled by default")


# -- phase handoffs -------------------------------------------------------------
# Compact durable handoff derived deterministically from phase/mission state.
# No full transcript; primarily SHAs, requirement coverage, files, tests.


def build_phase_handoff(
    db: Any,
    product_project_id: str,
    phase_id: str,
) -> dict[str, Any]:
    phase = phase_spec(db, phase_id)
    if not phase:
        return {}
    mission_id = phase.get("mission_id") or ""
    mission = db.get("missions", mission_id) if mission_id else None
    runs = (
        db.query(
            "SELECT id, git_commit_after, summary FROM provider_runs "
            "WHERE mission_id=? ORDER BY started_at DESC LIMIT 3",
            (mission_id,),
        )
        if mission_id
        else []
    )
    candidate_sha = (mission or {}).get("git_head") or (runs[0].get("git_commit_after") if runs else "")
    try:
        findings = db.query(
            "SELECT COUNT(*) as n FROM review_findings WHERE mission_id=? AND status='open'",
            (mission_id,),
        )
        open_findings = int((findings[0].get("n") if findings else 0) or 0)
    except Exception:
        open_findings = 0
    return {
        "phase_id": phase_id,
        "phase_key": phase.get("phase_key", ""),
        "mission_id": mission_id,
        "candidate_sha": candidate_sha or "",
        "status": phase.get("status", ""),
        "open_findings": open_findings,
    }


def resolve_phase_requirements(db: Any, product_project_id: str | None, phase_id: str | None) -> list[str]:
    """Deterministic requirement projection for a phase (plan revision bound)."""
    if not product_project_id or not phase_id:
        return []
    phase = phase_spec(db, phase_id)
    if not phase:
        return []
    try:
        product = db.get("product_projects", product_project_id)
        rev = int((product or {}).get("plan_revision") or 0)
        rows = db.query(
            "SELECT plan_json FROM plan_revisions WHERE project_id=? AND revision=? LIMIT 1",
            (product_project_id, rev),
        )
        if not rows:
            return []
        raw = rows[0]["plan_json"]
        plan = json.loads(raw) if isinstance(raw, str) else raw
        for p in plan.get("phases") or []:
            if isinstance(p, dict) and p.get("key") == phase.get("phase_key"):
                return [str(r) for r in (p.get("requirement_ids") or [])]
    except Exception:
        logger.debug("phase requirement resolution failed", exc_info=True)
    return []


def task_dependency_ids(db: Any, task_id: str | None) -> list[str]:
    if not task_id:
        return []
    try:
        rows = db.query("SELECT from_task_id FROM task_dependencies WHERE to_task_id=?", (task_id,))
        return [str(r["from_task_id"]) for r in rows]
    except Exception:
        return []


def latest_candidate_shas(db: Any, mission_id: str | None) -> tuple[str | None, str | None]:
    """Canonical reviewed artifact range for reviewer/repairer specs.

    DOG-01: single source of truth shared with
    provenance.mission_review_range (earliest base .. latest candidate tip),
    so the prompt range always equals the persisted reviewed range. Returns
    (base_sha, candidate_sha) or (None, None) when unknown. Fails open: the
    compiler records MODEL_CONTEXT_LIMIT_UNKNOWN rather than fabricating
    SHAs; persistence then refuses to certify (fail closed).
    """
    if not mission_id:
        return None, None
    try:
        from .provenance import mission_review_range as _range
    except Exception:
        _range = None  # type: ignore[assignment]
    if _range is not None:
        try:
            return _range(db, mission_id)
        except Exception:
            return None, None
    try:
        rows = db.query(
            "SELECT git_commit_before, git_commit_after FROM provider_runs WHERE mission_id=?"
            " AND failure_class='NONE' AND role IN ('implementation','task','testing','repair')"
            " ORDER BY started_at DESC LIMIT 1",
            (mission_id,),
        )
    except Exception:
        return None, None
    if not rows:
        return None, None
    return rows[0].get("git_commit_before"), rows[0].get("git_commit_after")


def finding_ids_in_text(text: str | None) -> list[str]:
    """Extract finding IDs (id=<id>) from repair extra_context."""
    import re

    if not text:
        return []
    return re.findall(r"\bid=([A-Za-z0-9_.\-]+)", text)


def finding_files(db: Any, mission_id: str | None) -> list[str]:
    """Files named by open/repair-attempted findings (diff relevance hint).

    DOG-02: includes inherited retry lineage so relevance scoring sees
    unresolved ancestor files. DOG-03: this never defines review scope;
    the reviewed file list comes from Git (diff_names) for the exact range.
    """
    if not mission_id:
        return []
    try:
        rows = db.query(
            "SELECT DISTINCT file FROM review_findings"
            " WHERE mission_id=? AND status IN ('open','repair_attempted') AND file IS NOT NULL AND file != ''",
            (mission_id,),
        )
    except Exception:
        rows = []
    files = [str(r["file"]) for r in rows if r.get("file")]
    try:
        from .review import inherited_open_findings as _inh2

        for r in _inh2(db, mission_id):
            f = str(r.get("file") or "")
            if f and f not in files:
                files.append(f)
    except Exception:
        logger.debug("inherited file lookup failed", exc_info=True)
    return files[:20]


# -- integration helper ----------------------------------------------------------
# Single choke point for legacy/compiled/shadow selection.
#
# Mode contract:
#   legacy   — never compile; execute legacy-v1; record legacy honestly.
#   shadow   — compile for measurement; ALWAYS execute legacy (exactly one
#              provider call); compilation failure is observable via
#              SHADOW_COMPILATION_FAILED + error code, never a fake success.
#   compiled — STRICT: compilation failure raises; the provider MUST NOT
#              execute. No silent legacy fallback (F-01/F-02).
# Compilation itself consumes zero provider quota in every mode.


#: Roles that carry a requirement/acceptance task contract. Unresolved
#: explicit requirement references fail closed for these roles; planner
#: returns before requirement processing and is unaffected.
CONTRACT_ROLES = frozenset({"implementer", "implementation", "reviewer", "review", "repairer", "repair", "testing"})


def _is_contract_role(role: str) -> bool:
    return role.lower() in CONTRACT_ROLES


def compile_failure_reason(exc: BaseException, spec: ContextCompileSpec | None = None) -> str:
    """Operator-visible compile-failure reason. Never includes raw context."""
    code = exc.code if isinstance(exc, ContextCompileError) else "CONTEXT_COMPILATION_INTERNAL"
    detail = (str(exc)[:300] if str(exc) else code).replace("\n", " ").rstrip(". ")
    role = spec.role if spec is not None else "?"
    stage = spec.stage if spec is not None and spec.stage else "?"
    return (
        f"Context compilation failed: {code} (role={role} stage={stage}, policy={POLICY_COMPILED_V2}). "
        f"{detail}. Provider was NOT invoked."
    )


def prepare_invocation_context(
    *,
    legacy_prompt: str,
    spec: ContextCompileSpec,
    db: Any,
    config: Any | None = None,
) -> tuple[str, dict[str, Any]]:
    """Return (prompt_to_execute, invocation_metadata)."""
    from .context_manifest import CONTEXT_POLICY_LEGACY, TEMPLATE_VERSION_LEGACY

    mode = context_mode(config)
    if mode == "legacy":
        return legacy_prompt, {
            "prompt_template_version": TEMPLATE_VERSION_LEGACY,
            "context_policy_version": CONTEXT_POLICY_LEGACY,
            "context_blocks_json": None,
            "context_warnings_json": "[]",
            "context_budget": None,
            "context_used": None,
            "context_remaining": None,
            "context_repeated_ratio": None,
            "context_plan_revision": spec.plan_revision,
        }
    try:
        compiler = ContextCompiler(db, config)
        # Optional refinement stays disabled; interface preserved for future.
        compiled = compiler.compile(spec)
    except ContextCompileError as exc:
        if mode == "shadow":
            # Shadow explicitly preserves legacy execution for measurement,
            # but the failure is observable: legacy versions are recorded
            # (the executed prompt IS legacy) and no compiled success is
            # claimed. Exactly one provider call happens downstream.
            logger.warning("shadow context compilation failed (%s); executing legacy", exc.code)
            return legacy_prompt, {
                "prompt_template_version": TEMPLATE_VERSION_LEGACY,
                "context_policy_version": CONTEXT_POLICY_LEGACY,
                "context_blocks_json": None,
                "context_warnings_json": json.dumps(
                    ["SHADOW_COMPILATION_FAILED", exc.code, "SHADOW_MODE_LEGACY_EXECUTED"]
                ),
                "context_budget": None,
                "context_used": None,
                "context_remaining": None,
                "context_repeated_ratio": None,
                "context_plan_revision": spec.plan_revision,
            }
        # Compiled mode is strict: mandatory overflow, unresolvable required
        # requirements, and any other compile failure must NOT silently
        # execute legacy. Callers record the owner failure; no provider runs.
        raise
    except Exception as exc:
        # Internal compiler bug: never hide behind legacy execution.
        if mode == "shadow":
            logger.warning("shadow context compilation crashed; executing legacy", exc_info=True)
            return legacy_prompt, {
                "prompt_template_version": TEMPLATE_VERSION_LEGACY,
                "context_policy_version": CONTEXT_POLICY_LEGACY,
                "context_blocks_json": None,
                "context_warnings_json": json.dumps(
                    ["SHADOW_COMPILATION_FAILED", "CONTEXT_COMPILATION_INTERNAL", "SHADOW_MODE_LEGACY_EXECUTED"]
                ),
                "context_budget": None,
                "context_used": None,
                "context_remaining": None,
                "context_repeated_ratio": None,
                "context_plan_revision": spec.plan_revision,
            }
        raise ContextCompileError(
            "CONTEXT_COMPILATION_INTERNAL", f"compiler bug, refusing to launch degraded prompt: {exc}"
        ) from exc
    meta: dict[str, Any] = {
        "prompt_template_version": compiled.template_version,
        "context_policy_version": compiled.policy_version,
        "context_blocks_json": blocks_to_json(compiled.blocks),
        "context_warnings_json": json.dumps(compiled.warnings),
        "context_budget": compiled.budget_estimated_tokens,
        "context_used": compiled.used_estimated_tokens,
        "context_remaining": compiled.remaining_estimated_tokens,
        "context_repeated_ratio": compiled.repeated_context_ratio,
        "context_plan_revision": compiled.plan_revision,
    }
    if mode == "shadow":
        # Shadow: measure compiled, execute legacy, record comparison honestly.
        shadow_warnings = list(compiled.warnings) + ["SHADOW_MODE_LEGACY_EXECUTED"]
        meta = {
            "prompt_template_version": TEMPLATE_VERSION_LEGACY,
            "context_policy_version": CONTEXT_POLICY_LEGACY,
            "context_blocks_json": None,
            "context_warnings_json": json.dumps(shadow_warnings + [f"SHADOW_COMPILED_EST={compiled.estimated_tokens}"]),
            "context_budget": compiled.budget_estimated_tokens,
            "context_used": compiled.used_estimated_tokens,
            "context_remaining": compiled.remaining_estimated_tokens,
            "context_repeated_ratio": compiled.repeated_context_ratio,
            "context_plan_revision": compiled.plan_revision,
        }
        return legacy_prompt, meta
    return compiled.prompt, meta


def blocks_to_json(decisions: list[BlockDecision]) -> str:
    return json.dumps(
        [
            {
                "block_type": d.type,
                "block_id": d.block_id,
                "source_kind": d.source_kind,
                "source_ref": d.source_ref,
                "priority": d.priority,
                "original_chars": d.original_chars,
                "included_chars": d.included_chars,
                "estimated_tokens": d.estimated_tokens,
                "representation": d.representation,
                "included": d.included,
                "reason": d.reason,
                "hash": d.hash,
            }
            for d in decisions
        ]
    )
