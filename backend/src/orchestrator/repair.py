"""Bounded autonomous repair (Increment 4).

GG may autonomously repair a *proven implementation defect* — exact failure
evidence bound to an exact artifact SHA — within hard attempt budgets, with
mandatory independent review and exact evidence recheck. Anything else
(environment, external prerequisites, ambiguous/contradictory contracts,
provider failures, dependency conflicts, unknown causes) stops and escalates
to the operator instead of launching code repair.

Design notes:

* Repair cycles are first-class persistent rows (``repair_cycles``), one per
  trigger, identifiable independently of any provider run (R-01..R-04).
* Each coding attempt is an immutable ``repair_attempts`` row, progressively
  updated through repair -> review -> recheck stages so restart recovery
  resumes without duplicating completed stages (R-32..R-35).
* Classification is deterministic-first over exit codes, commands, redacted
  output patterns, criterion metadata, and review finding categories. No
  classification LLM is added (R-41); UNKNOWN fails closed (R-09).
* Attempt budget increments only when a repair provider invocation actually
  produces an artifact (changed or unchanged). Classification, gates,
  operational provider failures, context errors, and conflicts consume no
  code-repair budget (R-12).
* The repair provider can never declare success by itself: success requires
  a code-changing result with valid ancestry, an independent reviewer outside
  the complete writer set, an explicit new recheck attempt against the exact
  repaired SHA, and required regressions (R-14..R-21).
* A cycle never declares the project DELIVERED; it returns a new candidate
  to canonical acceptance (R-45..R-47).
"""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from .models import StrValueEnum, utcnow

if TYPE_CHECKING:
    from .config import Config
    from .db import Database

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Taxonomy
# ---------------------------------------------------------------------------


class RepairTriggerType(StrValueEnum):
    CRITERION_FAILED = "CRITERION_FAILED"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    REVIEW_FINDING = "REVIEW_FINDING"


class RepairClassification(StrValueEnum):
    IMPLEMENTATION_DEFECT = "IMPLEMENTATION_DEFECT"
    ENVIRONMENT_FAILURE = "ENVIRONMENT_FAILURE"
    EXTERNAL_PREREQUISITE = "EXTERNAL_PREREQUISITE"
    AMBIGUOUS_CONTRACT = "AMBIGUOUS_CONTRACT"
    CONTRADICTORY_CONTRACT = "CONTRADICTORY_CONTRACT"
    TRANSIENT_INFRASTRUCTURE = "TRANSIENT_INFRASTRUCTURE"
    PROVIDER_FAILURE = "PROVIDER_FAILURE"
    DEPENDENCY_CONFLICT = "DEPENDENCY_CONFLICT"
    UNKNOWN = "UNKNOWN"


class RepairCycleStatus(StrValueEnum):
    CREATED = "CREATED"
    CLASSIFIED = "CLASSIFIED"
    REPAIRING = "REPAIRING"
    REVIEWING = "REVIEWING"
    RECHECKING = "RECHECKING"
    SUCCEEDED = "SUCCEEDED"
    BLOCKED = "BLOCKED"
    WAITING_FOR_HUMAN = "WAITING_FOR_HUMAN"
    WAITING_FOR_PROVIDER = "WAITING_FOR_PROVIDER"
    EXHAUSTED = "EXHAUSTED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    STALE = "STALE"


class RepairAttemptOutcome(StrValueEnum):
    PREPARED = "PREPARED"
    RUNNING = "RUNNING"
    CODE_CHANGED = "CODE_CHANGED"
    NO_CHANGE = "NO_CHANGE"
    REVIEW_FAILED = "REVIEW_FAILED"
    RECHECK_FAILED = "RECHECK_FAILED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


ACTIVE_CYCLE_STATUSES = frozenset(
    {
        RepairCycleStatus.CREATED.value,
        RepairCycleStatus.CLASSIFIED.value,
        RepairCycleStatus.REPAIRING.value,
        RepairCycleStatus.REVIEWING.value,
        RepairCycleStatus.RECHECKING.value,
        RepairCycleStatus.WAITING_FOR_PROVIDER.value,
    }
)

TERMINAL_CYCLE_STATUSES = frozenset(
    {
        RepairCycleStatus.SUCCEEDED.value,
        RepairCycleStatus.BLOCKED.value,
        RepairCycleStatus.WAITING_FOR_HUMAN.value,
        RepairCycleStatus.EXHAUSTED.value,
        RepairCycleStatus.FAILED.value,
        RepairCycleStatus.CANCELLED.value,
        RepairCycleStatus.STALE.value,
    }
)

#: Only this class may enter autonomous code repair by default (R-05).
AUTO_REPAIRABLE = frozenset({RepairClassification.IMPLEMENTATION_DEFECT.value})


# ---------------------------------------------------------------------------
# Budgets
# ---------------------------------------------------------------------------

DEFAULT_MAX_REPAIR_ATTEMPTS = 2
MAX_REPAIR_ATTEMPTS_LIMIT = 3
DEFAULT_MAX_REPAIRS_PER_PHASE = 4
DEFAULT_MAX_REPAIRS_PER_PROJECT = 8


def max_repair_attempts(config: Config | None) -> int:
    """Hard per-cycle attempt limit, clamped to [1, MAX_REPAIR_ATTEMPTS_LIMIT]."""
    raw = config.get("repair.max_attempts", DEFAULT_MAX_REPAIR_ATTEMPTS) if config else DEFAULT_MAX_REPAIR_ATTEMPTS
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_REPAIR_ATTEMPTS
    return max(1, min(MAX_REPAIR_ATTEMPTS_LIMIT, value))


def max_repairs_per_phase(config: Config | None) -> int:
    raw = (
        config.get("repair.max_per_phase", DEFAULT_MAX_REPAIRS_PER_PHASE) if config else DEFAULT_MAX_REPAIRS_PER_PHASE
    )
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_MAX_REPAIRS_PER_PHASE


def max_repairs_per_project(config: Config | None) -> int:
    raw = (
        config.get("repair.max_per_project", DEFAULT_MAX_REPAIRS_PER_PROJECT)
        if config
        else DEFAULT_MAX_REPAIRS_PER_PROJECT
    )
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_MAX_REPAIRS_PER_PROJECT


# ---------------------------------------------------------------------------
# Deterministic failure classification (no LLM)
# ---------------------------------------------------------------------------

_ENVIRONMENT_MARKERS = (
    "eai_again",
    "getaddrinfo",
    "enetunreach",
    "command not found",
    "no such file or directory",
    "missing compiler",
    "missing system library",
    "permission denied",
    "address already in use",
    "port unavailable",
    "package manager unavailable",
    "service not running",
    "bubblewrap",
    "bwrap",
    "sandboxed execution unavailable",
    "no network",
    "network unreachable",
    "enotfound",
    "etimedout",
    "connection refused",
)

_EXTERNAL_MARKERS = (
    "api key",
    "apikey",
    "api_key",
    "developer account",
    "account required",
    "credentials required",
    "missing credentials",
    "unauthorized",
    "401",
    "oauth",
    "consent required",
    "legal approval",
    "content license",
    "payment",
    "subscription required",
    "beta users",
    "domain verification",
    "forbidden",
    "403",
)

_AMBIGUOUS_MARKERS = (
    "ambiguous",
    "underspecified",
    "not specified",
    "unclear requirement",
    "todo: define",
    "tbd",
    "missing acceptance",
    "no acceptance criterion",
)

_CONFLICT_MARKERS = (
    "merge conflict",
    "conflicting changes",
    "<<<<<<<",
    ">>>>>>>",
    "automatic merge failed",
    "integration conflict",
    "dependency conflict",
    "conflicting requirement",
)

_PROVIDER_MARKERS = (
    "quota exhausted",
    "rate limit",
    "429",
    "provider timeout",
    "provider crashed",
    "malformed provider response",
    "model overloaded",
    "overloaded",
    "cli crash",
)

_TRANSIENT_MARKERS = (
    "temporary failure",
    "temporarily unavailable",
    "file lock",
    "resource busy",
    "deadlock detected",
    "try again",
)

_ASSERTION_MARKERS = (
    "assertionerror",
    "assert",
    "expected",
    "to deeply equal",
    "to equal",
    "received",
    "test failed",
    "tests failed",
    "failing test",
    "failed:",
    "typeerror",
    "typemismatch",
    "type mismatch",
    "referenceerror",
    "syntaxerror",
    "incorrect",
    "wrong",
    "must return",
    "should return",
    "must reject",
    "should reject",
)


def _contains_any(haystack: str, markers: tuple[str, ...]) -> bool:
    lowered = haystack.lower()
    return any(marker in lowered for marker in markers)


def classify_criterion_failure(
    *,
    command: str,
    exit_code: int | None,
    output_tail: str,
    requirement_text: str = "",
    criterion_text: str = "",
    contradictory: bool = False,
) -> RepairClassification:
    """Classify a failed criterion check deterministically.

    Empty/insufficient evidence fails closed to UNKNOWN (R-09): autonomy is
    allowed only when GG can positively identify an implementation defect.
    """
    evidence = f"{command}\n{output_tail or ''}"
    contract = f"{requirement_text}\n{criterion_text}"
    if _contains_any(evidence, _CONFLICT_MARKERS):
        return RepairClassification.DEPENDENCY_CONFLICT
    if _contains_any(evidence, _PROVIDER_MARKERS):
        return RepairClassification.PROVIDER_FAILURE
    if _contains_any(evidence, _ENVIRONMENT_MARKERS):
        return RepairClassification.ENVIRONMENT_FAILURE
    if _contains_any(evidence, _EXTERNAL_MARKERS):
        return RepairClassification.EXTERNAL_PREREQUISITE
    if contradictory or _contains_any(contract, ("contradicts", "conflicts with", "mutually exclusive")):
        return RepairClassification.CONTRADICTORY_CONTRACT
    if _contains_any(contract, _AMBIGUOUS_MARKERS):
        return RepairClassification.AMBIGUOUS_CONTRACT
    if not (output_tail or "").strip():
        return RepairClassification.UNKNOWN
    if _contains_any(evidence, _TRANSIENT_MARKERS) and exit_code in (None, 124, 137, 143):
        return RepairClassification.TRANSIENT_INFRASTRUCTURE
    if exit_code not in (None, 0) and _contains_any(evidence, _ASSERTION_MARKERS):
        return RepairClassification.IMPLEMENTATION_DEFECT
    if exit_code not in (None, 0) and output_tail.strip():
        # Executable command ran and reported a concrete product failure.
        return RepairClassification.IMPLEMENTATION_DEFECT
    return RepairClassification.UNKNOWN


def classify_verification_failure(
    *,
    command: str,
    exit_code: int | None,
    output_tail: str,
    likely_environment_issue: bool = False,
) -> RepairClassification:
    """Classify a failed generic verification command."""
    if likely_environment_issue or _contains_any(output_tail or "", _ENVIRONMENT_MARKERS):
        return RepairClassification.ENVIRONMENT_FAILURE
    return classify_criterion_failure(command=command, exit_code=exit_code, output_tail=output_tail)


def classify_review_finding(
    *,
    severity: str,
    category: str,
    description: str,
    recommended_fix: str = "",
) -> RepairClassification:
    """Classify an unresolved review finding."""
    text = f"{description}\n{recommended_fix}"
    cat = (category or "").lower()
    sev = (severity or "").upper()
    if _contains_any(text, _CONFLICT_MARKERS):
        return RepairClassification.DEPENDENCY_CONFLICT
    if _contains_any(text, _EXTERNAL_MARKERS):
        return RepairClassification.EXTERNAL_PREREQUISITE
    if _contains_any(text, _ENVIRONMENT_MARKERS):
        return RepairClassification.ENVIRONMENT_FAILURE
    if _contains_any(text, _AMBIGUOUS_MARKERS):
        return RepairClassification.AMBIGUOUS_CONTRACT
    if cat in ("correctness", "security", "tests") and sev in ("BLOCKER", "HIGH", "MEDIUM"):
        if (description or "").strip():
            return RepairClassification.IMPLEMENTATION_DEFECT
        return RepairClassification.UNKNOWN
    return RepairClassification.UNKNOWN


def failure_signature(
    *,
    trigger_type: str,
    identity: str,
    exit_code: int | None,
    output_tail: str,
) -> str:
    """Stable, secret-safe fingerprint of one failure observation (R-26).

    Normalizes volatile tokens (SHAs, numbers, paths, timings) so genuinely
    repeated failures compare equal while distinct failures do not. Only a
    bounded redacted excerpt feeds the hash; raw logs stay in provider runs.
    """
    text = (output_tail or "").lower()
    text = re.sub(r"[0-9a-f]{7,40}", "<sha>", text)
    text = re.sub(r"\d+(?:\.\d+)+", "<n>", text)
    text = re.sub(r"(?m)^\s*(at\s+|duration|elapsed)[^\n]*$", "", text)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()][:3]
    fingerprint = "\n".join(lines)[:400]
    digest = hashlib.sha256(
        "|".join([trigger_type, identity, str(exit_code), fingerprint]).encode("utf-8")
    ).hexdigest()[:32]
    return digest


# ---------------------------------------------------------------------------
# Persistent coordinator
# ---------------------------------------------------------------------------


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class RepairCoordinator:
    """Durable state machine for bounded autonomous repair cycles."""

    def __init__(self, db: Database, config: Config | None = None) -> None:
        self.db = db
        self.config = config

    # -- creation ------------------------------------------------------

    def active_cycle_for_trigger(
        self, project_id: str, trigger_type: str, trigger_evidence_id: str
    ) -> dict[str, Any] | None:
        rows = self.db.query(
            "SELECT * FROM repair_cycles WHERE project_id=? AND trigger_type=?"
            " AND trigger_evidence_id=? AND status IN"
            " ('CREATED','CLASSIFIED','REPAIRING','REVIEWING','RECHECKING','WAITING_FOR_PROVIDER')"
            " ORDER BY created_at DESC LIMIT 1",
            (project_id, trigger_type, trigger_evidence_id),
        )
        return rows[0] if rows else None

    def active_cycle_for_project(self, project_id: str) -> dict[str, Any] | None:
        rows = self.db.query(
            "SELECT * FROM repair_cycles WHERE project_id=? AND status IN"
            " ('CREATED','CLASSIFIED','REPAIRING','REVIEWING','RECHECKING','WAITING_FOR_PROVIDER')"
            " ORDER BY created_at ASC LIMIT 1",
            (project_id,),
        )
        return rows[0] if rows else None

    def project_cycle_count(self, project_id: str) -> int:
        rows = self.db.query("SELECT COUNT(*) AS n FROM repair_cycles WHERE project_id=?", (project_id,))
        return int(rows[0]["n"]) if rows else 0

    def phase_cycle_count(self, phase_id: str) -> int:
        rows = self.db.query("SELECT COUNT(*) AS n FROM repair_cycles WHERE phase_id=?", (phase_id,))
        return int(rows[0]["n"]) if rows else 0

    def create_cycle(
        self,
        *,
        project_id: str,
        trigger_type: str,
        trigger_evidence_id: str,
        trigger_sha: str,
        repo_key: str = "",
        phase_id: str | None = None,
        target_requirement_id: str | None = None,
        target_criterion_id: str | None = None,
        target_finding_id: str | None = None,
        classification: str = RepairClassification.UNKNOWN.value,
        max_attempts: int | None = None,
    ) -> dict[str, Any]:
        """Create a cycle, or return the existing active one (R-39/R-40).

        Returns ``{"cycle": row, "deduplicated": bool}`` on success or
        ``{"cycle": None, "reason": str}`` when creation is refused (another
        active cycle targets the scope, or a project/phase budget is spent).
        """
        existing = self.active_cycle_for_trigger(project_id, trigger_type, trigger_evidence_id)
        if existing:
            return {"cycle": existing, "deduplicated": True}
        # Terminal states are respected: the identical immutable trigger
        # (same evidence + same SHA) never reopens automatically, so canonical
        # acceptance cannot ping-pong an exhausted/blocked cycle forever.
        # A genuinely new observation (new attempt id after an explicit
        # recheck) carries a new evidence id and may open a fresh cycle.
        prior = self.db.query(
            "SELECT * FROM repair_cycles WHERE project_id=? AND trigger_type=?"
            " AND trigger_evidence_id=? AND trigger_sha=?"
            " ORDER BY created_at DESC LIMIT 1",
            (project_id, trigger_type, trigger_evidence_id, trigger_sha.lower()),
        )
        if prior:
            return {
                "cycle": prior[0],
                "deduplicated": True,
                "reason": f"trigger already terminalized as {prior[0]['status']}",
            }
        conflicting = self.active_cycle_for_project(project_id)
        if conflicting:
            return {"cycle": None, "reason": f"active repair cycle {conflicting['id']} already targets this project"}
        if self.project_cycle_count(project_id) >= max_repairs_per_project(self.config):
            return {"cycle": None, "reason": "project autonomous-repair budget exhausted"}
        if phase_id and self.phase_cycle_count(phase_id) >= max_repairs_per_phase(self.config):
            return {"cycle": None, "reason": "phase autonomous-repair budget exhausted"}
        cycle_id = _new_id("rep")
        now = utcnow().isoformat()
        self.db.insert(
            "repair_cycles",
            {
                "id": cycle_id,
                "project_id": project_id,
                "phase_id": phase_id,
                "trigger_type": trigger_type,
                "trigger_evidence_id": trigger_evidence_id,
                "trigger_sha": trigger_sha.lower(),
                "repo_key": repo_key,
                "target_requirement_id": target_requirement_id,
                "target_criterion_id": target_criterion_id,
                "target_finding_id": target_finding_id,
                "classification": classification,
                "status": RepairCycleStatus.CREATED.value,
                "max_attempts": max_attempts if max_attempts is not None else max_repair_attempts(self.config),
                "attempts_used": 0,
                "failure_signatures_json": "[]",
                "providers_used_json": "[]",
                "stop_reason": None,
                "gate_hint": None,
                "cancel_requested": 0,
                "created_at": now,
                "completed_at": None,
            },
        )
        row = self.db.get("repair_cycles", cycle_id)
        if row is None:
            raise KeyError(f"repair cycle {cycle_id} not found after insert")
        return {"cycle": row, "deduplicated": False}

    def classify_cycle(self, cycle_id: str, classification: str) -> dict[str, Any]:
        """Persist classification; non-repairable classes terminalize immediately."""
        cycle = self.db.get("repair_cycles", cycle_id)
        if not cycle:
            raise KeyError(f"repair cycle {cycle_id} not found")
        if classification in AUTO_REPAIRABLE:
            self.db.update(
                "repair_cycles",
                cycle_id,
                {"classification": classification, "status": RepairCycleStatus.CLASSIFIED.value},
            )
        else:
            terminal = (
                RepairCycleStatus.WAITING_FOR_HUMAN.value
                if classification
                in (
                    RepairClassification.EXTERNAL_PREREQUISITE.value,
                    RepairClassification.AMBIGUOUS_CONTRACT.value,
                    RepairClassification.CONTRADICTORY_CONTRACT.value,
                )
                else RepairCycleStatus.BLOCKED.value
            )
            self.db.update(
                "repair_cycles",
                cycle_id,
                {
                    "classification": classification,
                    "status": terminal,
                    "stop_reason": _stop_reason_for_classification(classification),
                    "gate_hint": _gate_hint_for_classification(classification),
                    "completed_at": utcnow().isoformat(),
                },
            )
        row = self.db.get("repair_cycles", cycle_id)
        if row is None:
            raise KeyError(f"repair cycle {cycle_id} not found")
        return row

    # -- attempts ------------------------------------------------------

    def running_attempt(self, cycle_id: str) -> dict[str, Any] | None:
        rows = self.db.query(
            "SELECT * FROM repair_attempts WHERE cycle_id=? AND outcome IN ('PREPARED','RUNNING')"
            " ORDER BY attempt_number DESC LIMIT 1",
            (cycle_id,),
        )
        return rows[0] if rows else None

    def attempts(self, cycle_id: str) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM repair_attempts WHERE cycle_id=? ORDER BY attempt_number ASC", (cycle_id,)
        )

    def start_attempt(self, cycle_id: str, provider: str, base_sha: str) -> dict[str, Any]:
        """Begin one counted attempt (budget consumed: a provider invocation starts)."""
        cycle = self.db.get("repair_cycles", cycle_id)
        if not cycle:
            raise KeyError(f"repair cycle {cycle_id} not found")
        if cycle["status"] in TERMINAL_CYCLE_STATUSES:
            raise ValueError(f"cycle {cycle_id} is terminal ({cycle['status']})")
        if int(cycle.get("cancel_requested") or 0):
            raise ValueError(f"cycle {cycle_id} cancellation was requested")
        if int(cycle["attempts_used"]) >= int(cycle["max_attempts"]):
            raise ValueError(f"cycle {cycle_id} attempt budget exhausted")
        prior = self.running_attempt(cycle_id)
        if prior:
            return prior
        attempt_number = int(cycle["attempts_used"]) + 1
        attempt_id = _new_id("att")
        now = utcnow().isoformat()
        self.db.insert(
            "repair_attempts",
            {
                "id": attempt_id,
                "cycle_id": cycle_id,
                "attempt_number": attempt_number,
                "provider": provider,
                "provider_run_id": None,
                "base_sha": base_sha.lower(),
                "result_sha": None,
                "outcome": RepairAttemptOutcome.RUNNING.value,
                "operational_failure": 0,
                "touched_files_json": "[]",
                "review_reviewer": None,
                "review_outcome": None,
                "review_detail": "",
                "recheck_attempt_id": None,
                "recheck_outcome": None,
                "failure_signature": None,
                "detail_json": "{}",
                "created_at": now,
                "finished_at": None,
            },
        )
        self.db.update(
            "repair_cycles",
            cycle_id,
            {"attempts_used": attempt_number, "status": RepairCycleStatus.REPAIRING.value},
        )
        row = self.db.get("repair_attempts", attempt_id)
        if row is None:
            raise KeyError(f"repair attempt {attempt_id} not found after insert")
        return row

    def update_attempt(self, attempt_id: str, data: dict[str, Any]) -> dict[str, Any]:
        self.db.update("repair_attempts", attempt_id, data)
        row = self.db.get("repair_attempts", attempt_id)
        if row is None:
            raise KeyError(f"repair attempt {attempt_id} not found")
        return row

    def finish_cycle(self, cycle_id: str, status: str, stop_reason: str | None = None) -> dict[str, Any]:
        self.db.update(
            "repair_cycles",
            cycle_id,
            {"status": status, "stop_reason": stop_reason, "completed_at": utcnow().isoformat()},
        )
        row = self.db.get("repair_cycles", cycle_id)
        if row is None:
            raise KeyError(f"repair cycle {cycle_id} not found")
        return row

    def cancel_cycle(self, cycle_id: str) -> dict[str, Any]:
        """Operator cancellation stops all future attempts (R-36)."""
        cycle = self.db.get("repair_cycles", cycle_id)
        if not cycle:
            raise KeyError(f"repair cycle {cycle_id} not found")
        if cycle["status"] in TERMINAL_CYCLE_STATUSES:
            return cycle
        self.db.update("repair_cycles", cycle_id, {"cancel_requested": 1})
        return self.finish_cycle(cycle_id, RepairCycleStatus.CANCELLED.value, "operator cancelled repair cycle")

    def record_signature(self, cycle_id: str, signature: str, provider: str) -> dict[str, Any]:
        import json as _json

        cycle = self.db.get("repair_cycles", cycle_id)
        if cycle is None:
            raise KeyError(f"repair cycle {cycle_id} not found")
        try:
            sigs = _json.loads(cycle.get("failure_signatures_json") or "[]")
        except ValueError:
            sigs = []
        sigs.append(signature)
        try:
            providers = _json.loads(cycle.get("providers_used_json") or "[]")
        except ValueError:
            providers = []
        if provider not in providers:
            providers.append(provider)
        self.db.update(
            "repair_cycles",
            cycle_id,
            {"failure_signatures_json": _json.dumps(sigs), "providers_used_json": _json.dumps(providers)},
        )
        row = self.db.get("repair_cycles", cycle_id)
        if row is None:
            raise KeyError(f"repair cycle {cycle_id} not found")
        return row

    def prior_signatures(self, cycle_id: str) -> list[str]:
        import json as _json

        cycle = self.db.get("repair_cycles", cycle_id)
        if not cycle:
            return []
        try:
            sigs = _json.loads(cycle.get("failure_signatures_json") or "[]")
        except ValueError:
            return []
        return [str(s) for s in sigs]

    # -- lifecycle helpers ---------------------------------------------

    def supersede_on_candidate_change(self, project_id: str, current_sha: str) -> list[str]:
        """Manual candidate change invalidates stale active cycles (R-38)."""
        stale: list[str] = []
        for cycle in self.db.query(
            "SELECT * FROM repair_cycles WHERE project_id=? AND status IN"
            " ('CREATED','CLASSIFIED','REPAIRING','REVIEWING','RECHECKING','WAITING_FOR_PROVIDER')",
            (project_id,),
        ):
            if (cycle.get("trigger_sha") or "").lower() != (current_sha or "").lower():
                chain = self.db.query(
                    "SELECT result_sha FROM repair_attempts WHERE cycle_id=? AND result_sha IS NOT NULL"
                    " ORDER BY attempt_number DESC LIMIT 1",
                    (cycle["id"],),
                )
                latest = chain[0]["result_sha"].lower() if chain else None
                if latest != (current_sha or "").lower():
                    self.finish_cycle(
                        str(cycle["id"]),
                        RepairCycleStatus.STALE.value,
                        "candidate changed (manual fix or new work); cycle superseded, no overwrite",
                    )
                    stale.append(str(cycle["id"]))
        return stale

    def get_cycle(self, cycle_id: str) -> dict[str, Any] | None:
        return self.db.get("repair_cycles", cycle_id)

    def require_cycle(self, cycle_id: str) -> dict[str, Any]:
        cycle = self.db.get("repair_cycles", cycle_id)
        if cycle is None:
            raise KeyError(f"repair cycle {cycle_id} not found")
        return cycle

    def list_cycles(self, project_id: str) -> list[dict[str, Any]]:
        return self.db.query(
            "SELECT * FROM repair_cycles WHERE project_id=? ORDER BY created_at ASC", (project_id,)
        )

    def repair_stats(self, project_id: str) -> dict[str, Any]:
        cycles = self.list_cycles(project_id)
        by_status: dict[str, int] = {}
        attempts = 0
        for cycle in cycles:
            by_status[str(cycle["status"])] = by_status.get(str(cycle["status"]), 0) + 1
            attempts += int(cycle.get("attempts_used") or 0)
        succeeded = by_status.get(RepairCycleStatus.SUCCEEDED.value, 0)
        return {
            "cycles": len(cycles),
            "attempts": attempts,
            "by_status": by_status,
            "success_rate": (succeeded / len(cycles)) if cycles else 0.0,
        }


def _stop_reason_for_classification(classification: str) -> str:
    return {
        RepairClassification.ENVIRONMENT_FAILURE.value: "environment failure: no code repair launched",
        RepairClassification.EXTERNAL_PREREQUISITE.value: "external prerequisite required: waiting for human",
        RepairClassification.AMBIGUOUS_CONTRACT.value: "ambiguous acceptance contract: waiting for human",
        RepairClassification.CONTRADICTORY_CONTRACT.value: "contradictory contract: plan revision required",
        RepairClassification.TRANSIENT_INFRASTRUCTURE.value: "transient infrastructure: bounded retry, no code repair",
        RepairClassification.PROVIDER_FAILURE.value: "provider failure: failover, no code-repair budget consumed",
        RepairClassification.DEPENDENCY_CONFLICT.value: "dependency merge conflict: operator-driven workflow",
        RepairClassification.UNKNOWN.value: "unknown failure cause: failed closed, no code repair",
    }.get(classification, f"not autonomously repairable: {classification}")


def _gate_hint_for_classification(classification: str) -> str | None:
    if classification in (
        RepairClassification.EXTERNAL_PREREQUISITE.value,
        RepairClassification.AMBIGUOUS_CONTRACT.value,
        RepairClassification.CONTRADICTORY_CONTRACT.value,
    ):
        return classification
    return None


# ---------------------------------------------------------------------------
# Bounded execution driver (provider-agnostic; hooks injected)
# ---------------------------------------------------------------------------


class ProviderOperationalError(RuntimeError):
    """Repair provider failed operationally before producing an artifact."""


@dataclass
class RepairScope:
    cycle_id: str
    attempt_number: int
    max_attempts: int
    trigger_type: str
    trigger_evidence_id: str
    requirement_id: str | None
    criterion_id: str | None
    finding_id: str | None
    expected: str
    observed: str
    command: str
    exit_code: int | None
    output_tail: str
    base_sha: str
    repo_key: str
    relevant_files: list[str] = field(default_factory=list)
    protected_files: list[str] = field(default_factory=list)
    writers: list[str] = field(default_factory=list)
    previous_attempt_summary: str = ""


@dataclass
class RepairResult:
    result_sha: str
    provider: str
    touched_files: list[str] = field(default_factory=list)
    summary: str = ""
    provider_run_id: str | None = None


@dataclass
class ReviewVerdict:
    passed: bool
    reviewer: str
    detail: str = ""


@dataclass
class RecheckVerdict:
    passed: bool
    attempt_id: str | None
    checked_sha: str
    output: str = ""


class RepairHooks(Protocol):
    async def repair(self, scope: RepairScope) -> RepairResult: ...
    async def review(self, scope: RepairScope, result_sha: str) -> ReviewVerdict: ...
    async def recheck(self, scope: RepairScope, result_sha: str) -> RecheckVerdict: ...
    async def regress(self, scope: RepairScope, result_sha: str) -> RecheckVerdict: ...
    async def current_head(self, scope: RepairScope) -> str: ...
    async def is_descendant(self, scope: RepairScope, base_sha: str, result_sha: str) -> bool: ...


def build_repair_scope(
    cycle: dict[str, Any],
    *,
    attempt_number: int,
    expected: str,
    observed: str,
    command: str,
    exit_code: int | None,
    output_tail: str,
    base_sha: str,
    relevant_files: list[str] | None = None,
    protected_files: list[str] | None = None,
    writers: list[str] | None = None,
    previous_attempt_summary: str = "",
) -> RepairScope:
    return RepairScope(
        cycle_id=str(cycle["id"]),
        attempt_number=attempt_number,
        max_attempts=int(cycle.get("max_attempts") or DEFAULT_MAX_REPAIR_ATTEMPTS),
        trigger_type=str(cycle.get("trigger_type")),
        trigger_evidence_id=str(cycle.get("trigger_evidence_id")),
        requirement_id=cycle.get("target_requirement_id"),
        criterion_id=cycle.get("target_criterion_id"),
        finding_id=cycle.get("target_finding_id"),
        expected=expected,
        observed=observed,
        command=command,
        exit_code=exit_code,
        output_tail=(output_tail or "")[:3000],
        base_sha=base_sha.lower(),
        repo_key=str(cycle.get("repo_key") or ""),
        relevant_files=list(relevant_files or []),
        protected_files=list(protected_files or []),
        writers=list(writers or []),
        previous_attempt_summary=previous_attempt_summary,
    )


def repair_ticket_text(scope: RepairScope) -> str:
    """Operator-visible scoped intent, persisted before provider execution."""
    return (
        f"Repair {scope.cycle_id} attempt {scope.attempt_number}/{scope.max_attempts}\n"
        f"Trigger: {scope.trigger_type} {scope.trigger_evidence_id}\n"
        f"Expected: {scope.expected[:500]}\n"
        f"Observed: {scope.observed[:500]}\n"
        f"Command: {scope.command[:300]}\n"
        f"Base: {scope.base_sha[:12]}"
    )


def _unfinished_attempt(db: Database, cycle_id: str) -> dict[str, Any] | None:
    """Latest attempt row with no finished_at (crash between stages)."""
    rows = db.query(
        "SELECT * FROM repair_attempts WHERE cycle_id=? AND finished_at IS NULL"
        " ORDER BY attempt_number DESC LIMIT 1",
        (cycle_id,),
    )
    return rows[0] if rows else None


async def execute_repair_cycle(
    db: Database,
    config: Config | None,
    scope_builder: Any,
    hooks: RepairHooks,
    cycle_id: str,
) -> dict[str, Any]:
    """Drive one repair cycle to a terminal state (bounded, restart-safe).

    ``scope_builder`` is an async callable ``(cycle, attempt_number, base_sha,
    previous_summary) -> RepairScope`` supplied by the integration layer, so
    this driver stays free of product-plumbing imports. Every stage is
    persisted before the next begins; a crash between stages resumes without
    duplicating completed provider work.
    """
    coord = RepairCoordinator(db, config)
    cycle = coord.get_cycle(cycle_id)
    if not cycle:
        raise KeyError(f"repair cycle {cycle_id} not found")
    if cycle["status"] in TERMINAL_CYCLE_STATUSES:
        return cycle
    if cycle.get("classification") not in AUTO_REPAIRABLE:
        return coord.finish_cycle(
            cycle_id,
            RepairCycleStatus.BLOCKED.value,
            _stop_reason_for_classification(str(cycle.get("classification") or "UNKNOWN")),
        )

    while True:
        cycle = coord.require_cycle(cycle_id)
        if cycle["status"] in TERMINAL_CYCLE_STATUSES:
            return cycle
        if int(cycle.get("cancel_requested") or 0):
            return coord.finish_cycle(cycle_id, RepairCycleStatus.CANCELLED.value, "operator cancelled repair cycle")
        if int(cycle["attempts_used"]) >= int(cycle["max_attempts"]):
            return coord.finish_cycle(cycle_id, RepairCycleStatus.EXHAUSTED.value, "attempt budget exhausted")

        # Restart resume: an unfinished attempt WITH a persisted result_sha
        # continues from the first incomplete stage — completed provider work
        # is never duplicated (R-33..R-35). An unfinished attempt WITHOUT a
        # result never completed the provider invocation, so it is dropped
        # without consuming budget.
        unfinished = _unfinished_attempt(db, cycle_id)
        if unfinished is not None and not unfinished.get("result_sha"):
            db.execute("DELETE FROM repair_attempts WHERE id=?", (str(unfinished["id"]),))
            db.execute("UPDATE repair_cycles SET attempts_used = attempts_used - 1 WHERE id=?", (cycle_id,))
            logger.info("repair cycle %s resumed: dropped artifact-less running attempt", cycle_id)
            continue
        if unfinished is not None:
            prior_finished = [a for a in coord.attempts(cycle_id) if a["id"] != unfinished["id"]]
            previous_summary = _attempt_summary(prior_finished[-1]) if prior_finished else ""
            resume_scope = await scope_builder(
                cycle, int(unfinished["attempt_number"]), str(unfinished["base_sha"]), previous_summary
            )
            nxt = await _resume_unfinished(db, coord, hooks, cycle_id, resume_scope, unfinished)
            if nxt is None:
                continue
            return nxt

        # Next attempt starts from the latest repaired SHA (R: lineage A->B->C).
        prior = coord.attempts(cycle_id)
        base_sha = str(cycle["trigger_sha"])
        previous_summary = ""
        if prior:
            last = prior[-1]
            if last.get("result_sha"):
                base_sha = str(last["result_sha"])
            previous_summary = _attempt_summary(last)

        scope = await scope_builder(cycle, len(prior) + 1, base_sha, previous_summary)
        # Stale base: the candidate moved under us (manual fix) — never overwrite.
        try:
            head = (await hooks.current_head(scope)).lower()
        except Exception:
            head = ""
        if head and head != base_sha.lower():
            return coord.finish_cycle(
                cycle_id,
                RepairCycleStatus.STALE.value,
                "candidate changed during cycle; superseded, no overwrite",
            )

        attempt = coord.start_attempt(cycle_id, scope.writers[-1] if scope.writers else "repair-provider", base_sha)
        # The repair provider identity comes from the hook result; correct the row.
        try:
            result = await hooks.repair(scope)
        except ProviderOperationalError as exc:
            # Operational failure: no artifact, no code-budget semantics (R-12).
            coord.update_attempt(
                str(attempt["id"]),
                {"outcome": RepairAttemptOutcome.FAILED.value, "operational_failure": 1,
                 "finished_at": utcnow().isoformat(),
                 "detail_json": _detail({"error": str(exc)[:500]})},
            )
            db.execute(
                "UPDATE repair_cycles SET attempts_used = attempts_used - 1 WHERE id=?", (cycle_id,)
            )
            logger.info("repair cycle %s attempt %s provider operational failure", cycle_id, attempt["attempt_number"])
            return coord.finish_cycle(
                cycle_id, RepairCycleStatus.WAITING_FOR_PROVIDER.value, f"repair provider unavailable: {exc}"
            )
        coord.update_attempt(
            str(attempt["id"]),
            {
                "provider": result.provider,
                "provider_run_id": result.provider_run_id,
                "result_sha": result.result_sha.lower(),
                "touched_files_json": _json_list(result.touched_files),
                "detail_json": _detail({"ticket": repair_ticket_text(scope)[:2000], "summary": result.summary[:1000]}),
            },
        )

        normalized_base = base_sha.lower()
        normalized_result = result.result_sha.lower()
        if normalized_result == normalized_base:
            coord.update_attempt(
                str(attempt["id"]),
                {"outcome": RepairAttemptOutcome.NO_CHANGE.value, "finished_at": utcnow().isoformat()},
            )
            if _no_change_failover_used(db, cycle_id, result.provider):
                return coord.finish_cycle(
                    cycle_id, RepairCycleStatus.BLOCKED.value, "repair produced no change twice; bounded stop"
                )
            _record_failover(db, cycle_id, result.provider)
            # One provider failover is allowed; otherwise this attempt ends the
            # cycle — never loop on no-change (R-27).
            _fresh0 = coord.require_cycle(cycle_id)
            if _failover_exhausted(db, cycle_id) or int(_fresh0["attempts_used"]) >= int(
                _fresh0["max_attempts"]
            ):
                return coord.finish_cycle(
                    cycle_id, RepairCycleStatus.BLOCKED.value, "repair produced no change; bounded stop"
                )
            continue

        if not await hooks.is_descendant(scope, normalized_base, normalized_result):
            coord.update_attempt(
                str(attempt["id"]),
                {"outcome": RepairAttemptOutcome.FAILED.value, "finished_at": utcnow().isoformat(),
                 "detail_json": _detail({"error": "result is not a descendant of base; history rewrite rejected"})},
            )
            return coord.finish_cycle(
                cycle_id, RepairCycleStatus.FAILED.value, "repair result not descended from base"
            )
        coord.update_attempt(str(attempt["id"]), {"outcome": RepairAttemptOutcome.CODE_CHANGED.value})
        coord.db.execute(
            "UPDATE repair_cycles SET status=? WHERE id=?", (RepairCycleStatus.REVIEWING.value, cycle_id)
        )

        # Contract tampering guard: repair must not touch protected files
        # (acceptance commands, test assertions, requirement text). Independent
        # review remains the main control; this is defense in depth (R-24/R-44).
        tampered = [f for f in result.touched_files if f in set(scope.protected_files)]
        if tampered:
            coord.update_attempt(
                str(attempt["id"]),
                {
                    "outcome": RepairAttemptOutcome.REVIEW_FAILED.value,
                    "finished_at": utcnow().isoformat(),
                    "detail_json": _detail({"tampered": tampered}),
                },
            )
            return coord.finish_cycle(
                cycle_id,
                RepairCycleStatus.BLOCKED.value,
                f"repair modified protected contract files {tampered}; rejected",
            )

        verdict = await hooks.review(scope, normalized_result)
        coord.update_attempt(
            str(attempt["id"]),
            {
                "review_reviewer": verdict.reviewer,
                "review_outcome": "passed" if verdict.passed else "failed",
                "review_detail": verdict.detail[:2000],
            },
        )
        writers = set(scope.writers) | _cycle_repair_providers(db, cycle_id)
        if verdict.reviewer in writers:
            # Self-review can never certify (R-18).
            coord.update_attempt(
                str(attempt["id"]),
                {"outcome": RepairAttemptOutcome.REVIEW_FAILED.value, "finished_at": utcnow().isoformat()},
            )
            return coord.finish_cycle(
                cycle_id, RepairCycleStatus.BLOCKED.value, f"self-review by {verdict.reviewer} rejected"
            )
        if not verdict.passed:
            coord.update_attempt(
                str(attempt["id"]),
                {"outcome": RepairAttemptOutcome.REVIEW_FAILED.value, "finished_at": utcnow().isoformat()},
            )
            coord.db.execute(
                "UPDATE repair_cycles SET status=? WHERE id=?", (RepairCycleStatus.REPAIRING.value, cycle_id)
            )
            _fresh = coord.require_cycle(cycle_id)
            if int(_fresh["attempts_used"]) >= int(_fresh["max_attempts"]):
                return coord.finish_cycle(cycle_id, RepairCycleStatus.EXHAUSTED.value, "attempt budget exhausted")
            continue

        coord.db.execute(
            "UPDATE repair_cycles SET status=? WHERE id=?", (RepairCycleStatus.RECHECKING.value, cycle_id)
        )
        recheck = await hooks.recheck(scope, normalized_result)
        if not recheck.attempt_id or recheck.checked_sha.lower() != normalized_result:
            coord.update_attempt(
                str(attempt["id"]),
                {"outcome": RepairAttemptOutcome.RECHECK_FAILED.value, "finished_at": utcnow().isoformat()},
            )
            return coord.finish_cycle(
                cycle_id, RepairCycleStatus.FAILED.value, "recheck did not certify the exact repaired SHA"
            )
        coord.update_attempt(
            str(attempt["id"]),
            {
                "recheck_attempt_id": recheck.attempt_id,
                "recheck_outcome": "passed" if recheck.passed else "failed",
            },
        )
        if not recheck.passed:
            signature = failure_signature(
                trigger_type=scope.trigger_type,
                identity=scope.trigger_evidence_id,
                exit_code=scope.exit_code,
                output_tail=recheck.output,
            )
            coord.update_attempt(
                str(attempt["id"]),
                {
                    "outcome": RepairAttemptOutcome.RECHECK_FAILED.value,
                    "failure_signature": signature,
                    "finished_at": utcnow().isoformat(),
                },
            )
            coord.record_signature(cycle_id, signature, result.provider)
            stop = _repetition_stop(coord, cycle_id, signature)
            if stop:
                return coord.finish_cycle(cycle_id, stop[0], stop[1])
            regression = await hooks.regress(scope, normalized_result)
            if not regression.passed:
                return coord.finish_cycle(
                    cycle_id,
                    RepairCycleStatus.WAITING_FOR_HUMAN.value,
                    "repair fixed the trigger but broke a required regression; contradictory contract suspected",
                )
            coord.db.execute(
                "UPDATE repair_cycles SET status=? WHERE id=?", (RepairCycleStatus.REPAIRING.value, cycle_id)
            )
            _fresh2 = coord.require_cycle(cycle_id)
            if int(_fresh2["attempts_used"]) >= int(_fresh2["max_attempts"]):
                return coord.finish_cycle(cycle_id, RepairCycleStatus.EXHAUSTED.value, "attempt budget exhausted")
            continue

        regression = await hooks.regress(scope, normalized_result)
        if not regression.passed:
            coord.update_attempt(
                str(attempt["id"]),
                {"outcome": RepairAttemptOutcome.RECHECK_FAILED.value, "finished_at": utcnow().isoformat()},
            )
            return coord.finish_cycle(
                cycle_id,
                RepairCycleStatus.WAITING_FOR_HUMAN.value,
                "trigger passes but a required regression fails; operator decision required",
            )
        coord.update_attempt(
            str(attempt["id"]),
            {"outcome": RepairAttemptOutcome.SUCCEEDED.value, "finished_at": utcnow().isoformat()},
        )
        return coord.finish_cycle(cycle_id, RepairCycleStatus.SUCCEEDED.value, None)


async def _resume_unfinished(
    db: Database,
    coord: RepairCoordinator,
    hooks: RepairHooks,
    cycle_id: str,
    scope: RepairScope,
    attempt: dict[str, Any],
) -> dict[str, Any] | None:
    """Continue an unfinished attempt from its first incomplete stage.

    Returns a terminal cycle row, or None to continue the driver loop with a
    fresh attempt. Never invokes the repair provider (R-33); never duplicates
    a recorded review (R-34); a recorded recheck PASS success-transitions
    idempotently without new provider calls (R-35).
    """
    import json as _json

    normalized_result = str(attempt.get("result_sha") or "").lower()
    result_provider = str(attempt.get("provider") or "repair-provider")
    try:
        touched = [str(f) for f in _json.loads(attempt.get("touched_files_json") or "[]")]
    except ValueError:
        touched = []

    if str(attempt.get("recheck_outcome") or "") == "passed":
        coord.update_attempt(
            str(attempt["id"]),
            {"outcome": RepairAttemptOutcome.SUCCEEDED.value, "finished_at": utcnow().isoformat()},
        )
        return coord.finish_cycle(cycle_id, RepairCycleStatus.SUCCEEDED.value, None)
    if str(attempt.get("review_outcome") or "") == "failed":
        coord.update_attempt(
            str(attempt["id"]),
            {"outcome": RepairAttemptOutcome.REVIEW_FAILED.value, "finished_at": utcnow().isoformat()},
        )
        coord.db.execute(
            "UPDATE repair_cycles SET status=? WHERE id=?", (RepairCycleStatus.REPAIRING.value, cycle_id)
        )
        return None
    if str(attempt.get("recheck_outcome") or "") == "failed":
        coord.update_attempt(
            str(attempt["id"]),
            {"outcome": RepairAttemptOutcome.RECHECK_FAILED.value, "finished_at": utcnow().isoformat()},
        )
        coord.db.execute(
            "UPDATE repair_cycles SET status=? WHERE id=?", (RepairCycleStatus.REPAIRING.value, cycle_id)
        )
        return None

    if not str(attempt.get("review_outcome") or ""):
        logger.info("repair cycle %s resumed after repair: reviewing %s", cycle_id, normalized_result[:12])
        tampered = [f for f in touched if f in set(scope.protected_files)]
        if tampered:
            coord.update_attempt(
                str(attempt["id"]),
                {
                    "outcome": RepairAttemptOutcome.REVIEW_FAILED.value,
                    "finished_at": utcnow().isoformat(),
                    "detail_json": _detail({"tampered": tampered}),
                },
            )
            return coord.finish_cycle(
                cycle_id, RepairCycleStatus.BLOCKED.value,
                f"repair modified protected contract files {tampered}; rejected",
            )
        verdict = await hooks.review(scope, normalized_result)
        coord.update_attempt(
            str(attempt["id"]),
            {
                "review_reviewer": verdict.reviewer,
                "review_outcome": "passed" if verdict.passed else "failed",
                "review_detail": verdict.detail[:2000],
            },
        )
        writers = set(scope.writers) | _cycle_repair_providers(db, cycle_id)
        if verdict.reviewer in writers:
            coord.update_attempt(
                str(attempt["id"]),
                {"outcome": RepairAttemptOutcome.REVIEW_FAILED.value, "finished_at": utcnow().isoformat()},
            )
            return coord.finish_cycle(
                cycle_id, RepairCycleStatus.BLOCKED.value, f"self-review by {verdict.reviewer} rejected"
            )
        if not verdict.passed:
            coord.update_attempt(
                str(attempt["id"]),
                {"outcome": RepairAttemptOutcome.REVIEW_FAILED.value, "finished_at": utcnow().isoformat()},
            )
            coord.db.execute(
                "UPDATE repair_cycles SET status=? WHERE id=?", (RepairCycleStatus.REPAIRING.value, cycle_id)
            )
            return None

    logger.info("repair cycle %s resumed after review: rechecking %s", cycle_id, normalized_result[:12])
    coord.db.execute(
        "UPDATE repair_cycles SET status=? WHERE id=?", (RepairCycleStatus.RECHECKING.value, cycle_id)
    )
    recheck = await hooks.recheck(scope, normalized_result)
    if not recheck.attempt_id or recheck.checked_sha.lower() != normalized_result:
        coord.update_attempt(
            str(attempt["id"]),
            {"outcome": RepairAttemptOutcome.RECHECK_FAILED.value, "finished_at": utcnow().isoformat()},
        )
        return coord.finish_cycle(
            cycle_id, RepairCycleStatus.FAILED.value, "recheck did not certify the exact repaired SHA"
        )
    coord.update_attempt(
        str(attempt["id"]),
        {"recheck_attempt_id": recheck.attempt_id, "recheck_outcome": "passed" if recheck.passed else "failed"},
    )
    if not recheck.passed:
        signature = failure_signature(
            trigger_type=scope.trigger_type,
            identity=scope.trigger_evidence_id,
            exit_code=scope.exit_code,
            output_tail=recheck.output,
        )
        coord.update_attempt(
            str(attempt["id"]),
            {
                "outcome": RepairAttemptOutcome.RECHECK_FAILED.value,
                "failure_signature": signature,
                "finished_at": utcnow().isoformat(),
            },
        )
        coord.record_signature(cycle_id, signature, result_provider)
        stop = _repetition_stop(coord, cycle_id, signature)
        if stop:
            return coord.finish_cycle(cycle_id, stop[0], stop[1])
        coord.db.execute(
            "UPDATE repair_cycles SET status=? WHERE id=?", (RepairCycleStatus.REPAIRING.value, cycle_id)
        )
        return None
    regression = await hooks.regress(scope, normalized_result)
    if not regression.passed:
        coord.update_attempt(
            str(attempt["id"]),
            {"outcome": RepairAttemptOutcome.RECHECK_FAILED.value, "finished_at": utcnow().isoformat()},
        )
        return coord.finish_cycle(
            cycle_id,
            RepairCycleStatus.WAITING_FOR_HUMAN.value,
            "trigger passes but a required regression fails; operator decision required",
        )
    coord.update_attempt(
        str(attempt["id"]),
        {"outcome": RepairAttemptOutcome.SUCCEEDED.value, "finished_at": utcnow().isoformat()},
    )
    return coord.finish_cycle(cycle_id, RepairCycleStatus.SUCCEEDED.value, None)


def _attempt_summary(attempt: dict[str, Any]) -> str:    return (
        f"attempt {attempt.get('attempt_number')} by {attempt.get('provider')}: "
        f"base={str(attempt.get('base_sha') or '')[:12]} "
        f"result={str(attempt.get('result_sha') or '')[:12]} "
        f"outcome={attempt.get('outcome')} review={attempt.get('review_outcome')} "
        f"recheck={attempt.get('recheck_outcome')}"
    )[:800]


def _detail(data: dict[str, Any]) -> str:
    import json as _json

    return _json.dumps(data)[:4000]


def _json_list(values: list[str]) -> str:
    import json as _json

    return _json.dumps([str(v) for v in values])[:4000]


def _cycle_repair_providers(db: Database, cycle_id: str) -> set[str]:
    rows = db.query("SELECT provider FROM repair_attempts WHERE cycle_id=? AND provider <> ''", (cycle_id,))
    return {str(r["provider"]) for r in rows}


def _no_change_failover_used(db: Database, cycle_id: str, provider: str) -> bool:
    rows = db.query(
        "SELECT COUNT(*) AS n FROM repair_attempts WHERE cycle_id=? AND outcome='NO_CHANGE'", (cycle_id,)
    )
    count = int(rows[0]["n"]) if rows else 0
    return count >= 2


def _record_failover(db: Database, cycle_id: str, provider: str) -> None:
    logger.info("repair cycle %s no-change failover after provider %s", cycle_id, provider)


def _failover_exhausted(db: Database, cycle_id: str) -> bool:
    rows = db.query(
        "SELECT COUNT(*) AS n FROM repair_attempts WHERE cycle_id=? AND outcome='NO_CHANGE'", (cycle_id,)
    )
    return (int(rows[0]["n"]) if rows else 0) >= 2


def _repetition_stop(coord: RepairCoordinator, cycle_id: str, signature: str) -> tuple[str, str] | None:
    """Detect unchanged-failure and oscillation fingerprints (R-26..R-28)."""
    sigs = coord.prior_signatures(cycle_id)
    if len(sigs) >= 2 and sigs[-1] == sigs[-2]:
        return (
            RepairCycleStatus.BLOCKED.value,
            "same failure signature persists across attempts without progress; bounded stop",
        )
    if len(sigs) >= 3 and signature in sigs[:-2] and sigs[-2] != signature:
        return (
            RepairCycleStatus.BLOCKED.value,
            "failure signature oscillation detected (A->B->A); bounded stop",
        )
    return None


def ensure_human_gate(
    db: Database,
    *,
    project_id: str,
    phase_id: str | None,
    cycle: dict[str, Any],
    title: str,
) -> str:
    """Persist an operator-visible gate explaining why automation stopped."""
    gate_id = _new_id("gate")
    classification = str(cycle.get("classification") or "UNKNOWN")
    db.insert(
        "project_gates",
        {
            "id": gate_id,
            "project_id": project_id,
            "phase_id": phase_id,
            "mission_gate_id": None,
            "gate_type": "repair",
            "title": title,
            "what_required": str(cycle.get("stop_reason") or "operator decision required"),
            "why_required": f"autonomous repair cycle {cycle['id']} stopped ({classification})",
            "blocked_ref": f"repair_cycle:{cycle['id']}",
            "completed_so_far": f"attempts_used={cycle.get('attempts_used')}",
            "human_action": str(cycle.get("gate_hint") or "review the blocked repair cycle and act"),
            "where_to_provide": "",
            "validation": "",
            "after_resolve": "",
            "required_vars": "[]",
            "status": "open",
            "resolution": None,
            "created_at": utcnow().isoformat(),
            "resolved_at": None,
        },
    )
    return gate_id


async def resume_repair_cycle(
    db: Database,
    config: Config | None,
    scope_builder: Any,
    hooks: RepairHooks,
    cycle_id: str,
) -> dict[str, Any]:
    """Restart-safe resume: completed stages are never duplicated (R-32..R-35).

    The driver loop is natively resumable (unfinished attempts continue from
    their first incomplete stage), so resume is simply a fresh drive on the
    same durable rows. Terminal cycles return unchanged.
    """
    return await execute_repair_cycle(db, config, scope_builder, hooks, cycle_id)
