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

from .context_compiler import BlockType, Priority
from .db import Database
from .invocations import STATUS_SUCCEEDED, TERMINAL_RUN_STATUSES
from .mission_summary import bounded_redacted, inherited_for, review_summary, serialize_finding
from .security import redact

logger = logging.getLogger(__name__)

# Handoffs routinely exceed 20k characters; the relay reads a bounded prefix
# and serves the full redacted text on demand.
HANDOFF_PREVIEW_CHARS = 1200
# Extra prefix read past the preview so a secret straddling the preview
# boundary is still matched (and redacted) as a whole before slicing.
REDACTION_MARGIN_CHARS = 512
HANDOFF_MAX_CHARS = 1_000_000
RUN_SUMMARY_CHARS = 400
# Evidence blocks below this size are flagged: the dogfood review ran on a
# 131-character GIT_DIFF while believing it saw the change.
THIN_EVIDENCE_BLOCK_CHARS = 500
# Only blocks that carry evidence about the candidate can be "thin". Task
# objectives, criteria and framing blocks are legitimately short.
EVIDENCE_BLOCK_TYPES = frozenset(
    {
        BlockType.GIT_DIFF,
        BlockType.TEST_RESULT,
        BlockType.FAILURE_EVIDENCE,
        BlockType.RELEVANT_CODE,
        BlockType.DEPENDENCY_HANDOFF,
    }
)
# A run that consumed this long and still failed is surfaced as lost work.
LOST_WORK_MIN_MS = 60_000
RELAY_RUN_LIMIT = 500
RELAY_ITEM_LIMIT = 500
UNKNOWN_OUTCOME = "UNKNOWN"


def _loads(raw: object) -> Any:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _context_summary(manifest: dict[str, Any] | None) -> dict[str, Any] | None:
    if not manifest:
        return None
    blocks = _loads(manifest.get("blocks_json"))
    blocks = blocks if isinstance(blocks, list) else []
    warnings = _loads(manifest.get("warnings_json"))
    thin: list[dict[str, Any]] = []
    reduced: list[dict[str, Any]] = []
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
            # Recorded compiler facts; the relay never infers why.
            "representation": b.get("representation"),
            "reason": b.get("reason") or None,
        }
        if (
            included
            and block_type in EVIDENCE_BLOCK_TYPES
            and str(b.get("priority") or "") == Priority.MANDATORY
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
            reduced.append(entry)
    return {
        "capture_status": manifest.get("capture_status"),
        "schema_version": manifest.get("schema_version"),
        "prompt_chars": manifest.get("prompt_chars"),
        "estimated_prompt_tokens": manifest.get("estimated_prompt_tokens"),
        "block_count": len(blocks),
        "warnings": [str(w) for w in warnings] if isinstance(warnings, list) else [],
        "thin_evidence_blocks": thin,
        "reduced_blocks": reduced,
    }


def _outcome(run: dict[str, Any]) -> tuple[str, str]:
    status = str(run.get("run_status") or "")
    if status:
        return status, "run_status"
    # Pre-observability rows carry only failure_class; say so explicitly. An
    # unfinished legacy row is an unrecorded outcome, not work in flight.
    if not run.get("finished_at"):
        return UNKNOWN_OUTCOME, "legacy"
    failure = str(run.get("failure_class") or "")
    if failure == "NONE":
        return STATUS_SUCCEEDED, "legacy"
    return failure or UNKNOWN_OUTCOME, "legacy"


def _commit_change(run: dict[str, Any]) -> bool | None:
    before, after = run.get("git_commit_before"), run.get("git_commit_after")
    if not before or not after:
        return None
    return str(before) != str(after)


def _run_entry(run: dict[str, Any], manifest: dict[str, Any] | None) -> dict[str, Any]:
    outcome, source = _outcome(run)
    summary, summary_truncated = bounded_redacted(run.get("summary"), RUN_SUMMARY_CHARS)
    duration = run.get("duration_ms")
    flags: list[str] = []
    context = _context_summary(manifest)
    if context and context["thin_evidence_blocks"]:
        flags.append("THIN_REQUIRED_EVIDENCE")
    if context and context["reduced_blocks"]:
        flags.append("CONTEXT_REDUCED")
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
    # ``head`` is a SQL prefix of the stored content: redact once, then slice.
    head = redact(str(row.get("head") or ""))
    stored = int(row.get("stored_chars") or 0)
    preview = head[:HANDOFF_PREVIEW_CHARS]
    return {
        "kind": "handoff",
        "id": row["id"],
        "at": row.get("created_at"),
        "from_provider": row.get("from_provider"),
        "to_provider": row.get("to_provider"),
        "role": row.get("role"),
        "git_head": row.get("git_head"),
        "stored_chars": stored,
        "preview": preview,
        "preview_truncated": stored > HANDOFF_PREVIEW_CHARS,
    }


def _outcome_bucket(run: dict[str, Any]) -> str:
    outcome = run["outcome"]
    if outcome == STATUS_SUCCEEDED:
        return "succeeded"
    if outcome == UNKNOWN_OUTCOME:
        return "outcome_unknown"
    # Legacy rows reaching here are finished (they have finished_at).
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
                "outcome_unknown": 0,
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


def _limited(db: Database, sql: str, params: tuple[Any, ...]) -> tuple[list[dict[str, Any]], bool]:
    rows = db.query(sql + " LIMIT ?", (*params, RELAY_ITEM_LIMIT + 1))
    return rows[:RELAY_ITEM_LIMIT], len(rows) > RELAY_ITEM_LIMIT


def mission_relay(db: Database, mission: dict[str, Any]) -> dict[str, Any]:
    mission_id = str(mission["id"])
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
            "SELECT run_id, capture_status, schema_version, prompt_chars, estimated_prompt_tokens, "  # noqa: S608
            f"blocks_json, warnings_json FROM run_context_manifests WHERE run_id IN ({placeholders})",
            tuple(r["id"] for r in runs),
        ):
            manifests[str(m["run_id"])] = m
    run_entries = [_run_entry(r, manifests.get(str(r["id"]))) for r in runs]
    handoff_rows, handoffs_truncated = _limited(
        db,
        "SELECT id, from_provider, to_provider, role, git_head, created_at, "
        "substr(content, 1, ?) AS head, length(content) AS stored_chars "
        "FROM handoffs WHERE mission_id=? ORDER BY created_at, rowid",
        (HANDOFF_PREVIEW_CHARS + REDACTION_MARGIN_CHARS, mission_id),
    )
    handoffs = [_handoff_entry(h) for h in handoff_rows]
    review_rows, reviews_truncated = _limited(
        db, "SELECT * FROM reviews WHERE mission_id=? ORDER BY created_at, rowid", (mission_id,)
    )
    reviews = []
    for r in review_rows:
        summary = review_summary(r) or {}
        reviews.append({"kind": "review", "at": r.get("created_at"), **summary})
    finding_rows, findings_truncated = _limited(
        db, "SELECT * FROM review_findings WHERE mission_id=? ORDER BY created_at, rowid", (mission_id,)
    )
    findings = [serialize_finding(f) for f in finding_rows]
    inherited, inherited_available = inherited_for(db, mission)
    findings.extend(serialize_finding(f) for f in inherited)
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
        "handoffs_truncated": handoffs_truncated,
        "reviews_truncated": reviews_truncated,
        "findings_truncated": findings_truncated,
        "inherited_findings_available": inherited_available,
        "limits": {
            "handoff_preview_chars": HANDOFF_PREVIEW_CHARS,
            "thin_evidence_block_chars": THIN_EVIDENCE_BLOCK_CHARS,
            "lost_work_min_ms": LOST_WORK_MIN_MS,
            "run_limit": RELAY_RUN_LIMIT,
            "item_limit": RELAY_ITEM_LIMIT,
        },
    }


def handoff_content(db: Database, mission_id: str, handoff_id: str) -> dict[str, Any] | None:
    rows = db.query(
        "SELECT id, from_provider, to_provider, role, created_at, substr(content, 1, ?) AS head, "
        "length(content) AS stored_chars FROM handoffs WHERE id=? AND mission_id=?",
        (HANDOFF_MAX_CHARS, handoff_id, mission_id),
    )
    if not rows:
        return None
    row = rows[0]
    stored = int(row.get("stored_chars") or 0)
    return {
        "id": row["id"],
        "from_provider": row.get("from_provider"),
        "to_provider": row.get("to_provider"),
        "role": row.get("role"),
        "content": redact(str(row.get("head") or "")),
        "stored_chars": stored,
        "truncated": stored > HANDOFF_MAX_CHARS,
        "created_at": row.get("created_at"),
    }
