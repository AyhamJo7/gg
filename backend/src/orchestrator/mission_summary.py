"""Read-only mission trust summaries for operator surfaces.

A mission status such as ``COMPLETED`` says the state machine finished; it
does not say the result is clean. This module derives, from persisted rows
only, the caveats an operator needs next to that status: unresolved review
findings (including retry-inherited lineage), repair claims never verified
fixed, and whether the latest review was certified independent.

Nothing here mutates state or re-decides policy. Absent evidence stays absent
(``review`` is ``None`` when no review was recorded; it is never assumed
clean or independent).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .db import Database
from .models import Severity
from .review import inherited_open_findings

logger = logging.getLogger(__name__)

UNRESOLVED_FINDING_STATUSES = ("open", "repair_attempted")
UNVERIFIED_REPAIR_STATUS = "repair_attempted"
SEVERITY_ORDER = tuple(s.value for s in Severity)
# Stay well under SQLite's bound-parameter limit for large project listings.
BULK_CHUNK = 400


def _empty_counts() -> dict[str, int]:
    return dict.fromkeys(SEVERITY_ORDER, 0)


def _parse_list(raw: object) -> list[str]:
    if isinstance(raw, list):
        return [str(x) for x in raw]
    if isinstance(raw, str) and raw:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if isinstance(parsed, list):
            return [str(x) for x in parsed]
    return []


def review_summary(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """Operator view of one review row; ``None`` when no review exists.

    ``reviewer_in_writer_set`` is evidence-based (the recorded writer set
    contains the reviewer). It is ``False`` when the set is empty or unknown,
    so a legacy or incomplete record never produces a self-review claim.
    """
    if not row:
        return None
    reviewer = str(row.get("review_provider") or "")
    writer_set = _parse_list(row.get("writer_set_json"))
    parsed = row.get("review_parsed")
    return {
        "id": row.get("id"),
        "reviewer": reviewer,
        "independent": bool(row.get("independent")),
        "degradation_reason": row.get("degradation_reason"),
        "writer_set": writer_set,
        "reviewer_in_writer_set": bool(reviewer) and reviewer in writer_set,
        "parsed": None if parsed is None else bool(parsed),
        "reviewed_base_sha": row.get("reviewed_base_sha"),
        "reviewed_head_sha": row.get("reviewed_head_sha"),
        "created_at": row.get("created_at"),
    }


def _tally(rows: list[dict[str, Any]], into: dict[str, Any]) -> None:
    for r in rows:
        severity = str(r.get("severity") or "").upper()
        if severity not in into["unresolved_findings"]:
            # Unknown severities are counted separately rather than dropped
            # or promoted into a known bucket.
            into["unresolved_other"] += 1
        else:
            into["unresolved_findings"][severity] += 1
        if str(r.get("status") or "") == UNVERIFIED_REPAIR_STATUS:
            into["unverified_repairs"] += 1
        if r.get("inherited"):
            into["inherited_unresolved"] += 1


def _blank() -> dict[str, Any]:
    return {
        "unresolved_findings": _empty_counts(),
        "unresolved_other": 0,
        "unverified_repairs": 0,
        "inherited_unresolved": 0,
        "inherited_available": True,
        "review": None,
        "review_count": 0,
    }


def mission_trust_bulk(db: Database, missions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Trust summaries for many missions with a bounded number of queries."""
    if len(missions) > BULK_CHUNK:
        merged: dict[str, dict[str, Any]] = {}
        for start in range(0, len(missions), BULK_CHUNK):
            merged.update(mission_trust_bulk(db, missions[start : start + BULK_CHUNK]))
        return merged
    ids = [str(m["id"]) for m in missions if m.get("id")]
    out: dict[str, dict[str, Any]] = {mid: _blank() for mid in ids}
    if not ids:
        return out
    placeholders = ",".join("?" for _ in ids)
    status_ph = ",".join("?" for _ in UNRESOLVED_FINDING_STATUSES)
    findings = db.query(
        f"SELECT mission_id, severity, status FROM review_findings "  # noqa: S608 - placeholders only
        f"WHERE mission_id IN ({placeholders}) AND status IN ({status_ph})",
        (*ids, *UNRESOLVED_FINDING_STATUSES),
    )
    by_mission: dict[str, list[dict[str, Any]]] = {}
    for f in findings:
        by_mission.setdefault(str(f["mission_id"]), []).append(f)
    for mid, rows in by_mission.items():
        _tally(rows, out[mid])

    for m in missions:
        if not m.get("retry_of_mission_id"):
            continue
        mid = str(m["id"])
        try:
            inherited = inherited_open_findings(db, mid)
        except Exception:
            logger.warning("inherited findings unavailable for %s", mid, exc_info=True)
            out[mid]["inherited_available"] = False
            continue
        _tally(inherited, out[mid])

    reviews = db.query(
        "SELECT * FROM (SELECT r.*, ROW_NUMBER() OVER (PARTITION BY mission_id "  # noqa: S608 - placeholders only
        "ORDER BY created_at DESC, rowid DESC) AS rn, COUNT(*) OVER (PARTITION BY mission_id) AS n "
        f"FROM reviews r WHERE mission_id IN ({placeholders})) WHERE rn = 1",
        tuple(ids),
    )
    for r in reviews:
        mid = str(r["mission_id"])
        out[mid]["review"] = review_summary(r)
        out[mid]["review_count"] = int(r.get("n") or 0)
    return out


def mission_trust(db: Database, mission: dict[str, Any]) -> dict[str, Any]:
    return mission_trust_bulk(db, [mission])[str(mission["id"])]
