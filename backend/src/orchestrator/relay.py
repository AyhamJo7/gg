"""Read-only Agent Relay: how providers passed work to each other in a mission.

Assembles, from persisted rows only, the chronological chain an operator
otherwise reconstructs from SQLite and raw logs: which provider ran in which
role, what context it was given (manifest metadata, never raw prompt text),
what it handed off, what reviews recorded, and how findings moved from origin
to resolution. Heuristic flags are labelled as observations with the
underlying numbers attached; absent data stays ``None`` and is never zeroed.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .db import Database
from .invocations import STATUS_SUCCEEDED, TERMINAL_RUN_STATUSES
from .mission_summary import review_summary
from .review import inherited_open_findings
from .security import redact

logger = logging.getLogger(__name__)

# Handoffs routinely exceed 20k characters; the relay shows a bounded preview
# and serves the full redacted text on demand.
HANDOFF_PREVIEW_CHARS = 1200
FINDING_TEXT_CHARS = 600
RUN_SUMMARY_CHARS = 400
# Required evidence blocks below this size are flagged: the dogfood review ran
# on a 131-character GIT_DIFF while believing it saw the change.
THIN_EVIDENCE_BLOCK_CHARS = 500
# Framing blocks are short by design and never count as thin evidence.
FRAMING_BLOCK_TYPES = frozenset({"SYSTEM_INSTRUCTIONS", "OUTPUT_CONTRACT"})
MANDATORY_PRIORITY = "MANDATORY"
# A run that consumed this long and still failed is surfaced as lost work.
LOST_WORK_MIN_MS = 60_000
RELAY_RUN_LIMIT = 500


def _loads(raw: object) -> Any:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _bounded(text: object, limit: int) -> tuple[str, bool]:
    value = redact(str(text or ""))
    if len(value) <= limit:
        return value, False
    return value[:limit], True


def _context_summary(manifest: dict[str, Any] | None) -> dict[str, Any] | None:
    if not manifest:
        return None
    blocks = _loads(manifest.get("blocks_json"))
    blocks = blocks if isinstance(blocks, list) else []
    warnings = _loads(manifest.get("warnings_json"))
    thin: list[dict[str, Any]] = []
    truncated: list[dict[str, Any]] = []
    for b in blocks:
        if not isinstance(b, dict):
            continue
        block_type = str(b.get("block_type") or "")
        included = bool(b.get("included", True))
        included_chars = b.get("included_chars")
        original_chars = b.get("original_chars")
        entry = {
            "block_type": block_type,
            "block_id": str(b.get("block_id") or ""),
            "included_chars": included_chars,
            "original_chars": original_chars,
        }
        if (
            included
            and str(b.get("priority") or "") == MANDATORY_PRIORITY
            and block_type not in FRAMING_BLOCK_TYPES
            and isinstance(included_chars, int)
            and included_chars < THIN_EVIDENCE_BLOCK_CHARS
        ):
            thin.append(entry)
        if (
            included
            and isinstance(included_chars, int)
            and isinstance(original_chars, int)
            and included_chars < original_chars
        ):
            truncated.append(entry)
    return {
        "capture_status": manifest.get("capture_status"),
        "schema_version": manifest.get("schema_version"),
        "prompt_chars": manifest.get("prompt_chars"),
        "estimated_prompt_tokens": manifest.get("estimated_prompt_tokens"),
        "block_count": len(blocks),
        "warnings": [str(w) for w in warnings] if isinstance(warnings, list) else [],
        "thin_evidence_blocks": thin,
        "truncated_blocks": truncated,
    }


def _outcome(run: dict[str, Any]) -> tuple[str, str]:
    status = str(run.get("run_status") or "")
    if status:
        return status, "run_status"
    # Pre-observability rows carry only failure_class; say so explicitly.
    if not run.get("finished_at"):
        return "UNKNOWN", "legacy"
    return ("SUCCEEDED" if run.get("failure_class") == "NONE" else str(run.get("failure_class") or "UNKNOWN")), "legacy"


def _commit_change(run: dict[str, Any]) -> bool | None:
    before, after = run.get("git_commit_before"), run.get("git_commit_after")
    if not before or not after:
        return None
    return str(before) != str(after)


def _run_entry(run: dict[str, Any], manifest: dict[str, Any] | None) -> dict[str, Any]:
    outcome, source = _outcome(run)
    summary, summary_truncated = _bounded(run.get("summary"), RUN_SUMMARY_CHARS)
    duration = run.get("duration_ms")
    flags: list[str] = []
    context = _context_summary(manifest)
    if context and context["thin_evidence_blocks"]:
        flags.append("THIN_REQUIRED_EVIDENCE")
    if context and context["truncated_blocks"]:
        flags.append("CONTEXT_TRUNCATED")
    if (
        outcome in TERMINAL_RUN_STATUSES
        and outcome != STATUS_SUCCEEDED
        and isinstance(duration, int)
        and duration >= LOST_WORK_MIN_MS
    ):
        flags.append("LOST_WORK")
    if manifest is None:
        flags.append("CONTEXT_NOT_CAPTURED")
    return {
        "kind": "run",
        "id": run["id"],
        "at": run.get("started_at"),
        "provider": run.get("provider"),
        "role": run.get("role"),
        "stage": run.get("stage") or None,
        "task_id": run.get("task_id"),
        "attempt_number": run.get("attempt_number"),
        "retry_of_run_id": run.get("retry_of_run_id"),
        "outcome": outcome,
        "outcome_source": source,
        "failure_class": run.get("failure_class"),
        "exit_code": run.get("exit_code"),
        "started_at": run.get("started_at"),
        "finished_at": run.get("finished_at"),
        "duration_ms": duration,
        "model_observed": run.get("model_observed"),
        "commit_before": run.get("git_commit_before"),
        "commit_after": run.get("git_commit_after"),
        "changed_commit": _commit_change(run),
        "summary": summary,
        "summary_truncated": summary_truncated,
        "context": context,
        "flags": flags,
    }


def _handoff_entry(row: dict[str, Any]) -> dict[str, Any]:
    content = redact(str(row.get("content") or ""))
    preview, truncated = _bounded(content, HANDOFF_PREVIEW_CHARS)
    return {
        "kind": "handoff",
        "id": row["id"],
        "at": row.get("created_at"),
        "from_provider": row.get("from_provider"),
        "to_provider": row.get("to_provider"),
        "role": row.get("role"),
        "git_head": row.get("git_head"),
        "content_chars": len(content),
        "preview": preview,
        "preview_truncated": truncated,
    }


def _finding_entry(row: dict[str, Any]) -> dict[str, Any]:
    description, d_trunc = _bounded(row.get("description"), FINDING_TEXT_CHARS)
    fix, f_trunc = _bounded(row.get("recommended_fix"), FINDING_TEXT_CHARS)
    return {
        "id": row["id"],
        "severity": row.get("severity"),
        "category": row.get("category"),
        "file": row.get("file"),
        "status": row.get("status"),
        "description": description,
        "recommended_fix": fix,
        "text_truncated": d_trunc or f_trunc,
        "origin_review_id": row.get("origin_review_id"),
        "origin_sha": row.get("origin_sha"),
        "resolved_review_id": row.get("resolved_review_id"),
        "resolved_sha": row.get("resolved_sha"),
        "verified_by": row.get("verified_by"),
        "inherited_from_mission_id": row.get("inherited_from_mission_id"),
        "created_at": row.get("created_at"),
        "resolved_at": row.get("resolved_at"),
    }


def _outcome_bucket(run: dict[str, Any]) -> str:
    outcome = run["outcome"]
    if outcome == STATUS_SUCCEEDED:
        return "succeeded"
    if outcome == "UNKNOWN":
        return "in_flight"
    # Legacy rows are finished (they have finished_at) with a failure class.
    if outcome in TERMINAL_RUN_STATUSES or run["outcome_source"] == "legacy":
        return "not_succeeded"
    return "in_flight"


def _provider_totals(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    totals: dict[str, dict[str, Any]] = {}
    for r in runs:
        name = str(r.get("provider") or "unknown")
        t = totals.setdefault(
            name,
            {
                "provider": name,
                "runs": 0,
                "succeeded": 0,
                "not_succeeded": 0,
                "in_flight": 0,
                "known_duration_ms": 0,
                "unknown_duration_runs": 0,
                "roles": [],
            },
        )
        t["runs"] += 1
        t[_outcome_bucket(r)] += 1
        if isinstance(r["duration_ms"], int):
            t["known_duration_ms"] += r["duration_ms"]
        else:
            t["unknown_duration_runs"] += 1
        role = r.get("role")
        if role and role not in t["roles"]:
            t["roles"].append(role)
    return list(totals.values())


def mission_relay(db: Database, mission_id: str) -> dict[str, Any]:
    runs = db.query(
        "SELECT * FROM provider_runs WHERE mission_id=? ORDER BY started_at, rowid LIMIT ?",
        (mission_id, RELAY_RUN_LIMIT + 1),
    )
    runs_truncated = len(runs) > RELAY_RUN_LIMIT
    runs = runs[:RELAY_RUN_LIMIT]
    manifests: dict[str, dict[str, Any]] = {}
    if runs:
        placeholders = ",".join("?" for _ in runs)
        for m in db.query(
            f"SELECT * FROM run_context_manifests WHERE run_id IN ({placeholders})",  # noqa: S608 - placeholders only
            tuple(r["id"] for r in runs),
        ):
            manifests[str(m["run_id"])] = m
    run_entries = [_run_entry(r, manifests.get(str(r["id"]))) for r in runs]
    handoffs = [
        _handoff_entry(h)
        for h in db.query("SELECT * FROM handoffs WHERE mission_id=? ORDER BY created_at, rowid", (mission_id,))
    ]
    review_rows = db.query("SELECT * FROM reviews WHERE mission_id=? ORDER BY created_at, rowid", (mission_id,))
    reviews = []
    for r in review_rows:
        summary = review_summary(r) or {}
        reviews.append({"kind": "review", "at": r.get("created_at"), **summary})
    findings = [
        _finding_entry(f)
        for f in db.query("SELECT * FROM review_findings WHERE mission_id=? ORDER BY created_at, rowid", (mission_id,))
    ]
    inherited_available = True
    try:
        findings.extend(_finding_entry(f) for f in inherited_open_findings(db, mission_id))
    except Exception:
        logger.warning("inherited findings unavailable for relay %s", mission_id, exc_info=True)
        inherited_available = False
    timeline = sorted(
        [*run_entries, *handoffs, *reviews],
        key=lambda e: (str(e.get("at") or ""), {"run": 0, "handoff": 1, "review": 2}[e["kind"]]),
    )
    return {
        "mission_id": mission_id,
        "timeline": timeline,
        "findings": findings,
        "providers": _provider_totals(run_entries),
        "runs_truncated": runs_truncated,
        "inherited_findings_available": inherited_available,
        "limits": {
            "handoff_preview_chars": HANDOFF_PREVIEW_CHARS,
            "thin_evidence_block_chars": THIN_EVIDENCE_BLOCK_CHARS,
            "lost_work_min_ms": LOST_WORK_MIN_MS,
            "run_limit": RELAY_RUN_LIMIT,
        },
    }


def handoff_content(db: Database, mission_id: str, handoff_id: str) -> dict[str, Any] | None:
    rows = db.query("SELECT * FROM handoffs WHERE id=? AND mission_id=?", (handoff_id, mission_id))
    if not rows:
        return None
    row = rows[0]
    content = redact(str(row.get("content") or ""))
    return {
        "id": row["id"],
        "from_provider": row.get("from_provider"),
        "to_provider": row.get("to_provider"),
        "role": row.get("role"),
        "content": content,
        "content_chars": len(content),
        "created_at": row.get("created_at"),
    }
