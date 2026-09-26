"""Read-only mission trust summaries for operator surfaces.

A mission status such as ``COMPLETED`` says the state machine finished; it
does not say the result is clean. This module derives, from persisted rows
only, the caveats an operator needs next to that status: open review
findings, findings whose repair was claimed but never verified (both
including retry-inherited lineage), and whether the latest review was
certified independent.

Nothing here mutates state or re-decides policy. Absent evidence stays absent
(``review`` is ``None`` when no review was recorded; it is never assumed
clean or independent), and unreadable retry history is reported as
unavailable rather than as zero inherited findings.
"""

from __future__ import annotations

import json
import logging
import threading
from collections import OrderedDict
from typing import Any

from .db import Database
from .models import TERMINAL_STATUSES, Severity
from .review import inherited_open_findings, retry_ancestors
from .security import redact

logger = logging.getLogger(__name__)

OPEN_STATUS = "open"
REPAIR_CLAIMED_STATUS = "repair_attempted"
UNRESOLVED_FINDING_STATUSES = (OPEN_STATUS, REPAIR_CLAIMED_STATUS)
SEVERITY_ORDER = tuple(s.value for s in Severity)
TERMINAL_MISSION_STATUSES = frozenset(s.value for s in TERMINAL_STATUSES)
# Stay well under SQLite's bound-parameter limit for large project listings.
BULK_CHUNK = 400
FINDING_TEXT_CHARS = 600
FINDING_FILE_CHARS = 300
# Terminal missions are immutable in findings/reviews; memoize their trust.
TRUST_CACHE_SIZE = 1000


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


def bounded_redacted(text: object, limit: int) -> tuple[str, bool]:
    """Redact first (so truncation can never split a secret), then bound."""
    value = redact(str(text or ""))
    if len(value) <= limit:
        return value, False
    return value[:limit], True


def serialize_finding(row: dict[str, Any]) -> dict[str, Any]:
    """Allow-listed, redacted, bounded view of one finding row for any API surface."""
    description, d_trunc = bounded_redacted(row.get("description"), FINDING_TEXT_CHARS)
    fix, f_trunc = bounded_redacted(row.get("recommended_fix"), FINDING_TEXT_CHARS)
    file_value = row.get("file")
    file_text, _ = bounded_redacted(file_value, FINDING_FILE_CHARS) if file_value else (None, False)
    return {
        "id": row.get("id"),
        "mission_id": row.get("mission_id"),
        "severity": row.get("severity"),
        "category": row.get("category"),
        "file": file_text,
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


def inherited_for(db: Database, mission: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    """Query-time inherited unresolved findings and whether history was readable.

    ``review.inherited_open_findings`` swallows lookup failures and returns
    ``[]``; a retry whose ancestor chain cannot be resolved is therefore
    reported here as unavailable instead of as "nothing inherited".
    Known limit: if a single ancestor's findings query fails, the sealed
    helper skips that ancestor and still returns normally, so that partial
    undercount cannot be detected here.
    """
    mid = str(mission.get("id") or "")
    if not mid or not mission.get("retry_of_mission_id"):
        return [], True
    try:
        chain = retry_ancestors(db, mid)
        # The walk stops at the first missing row, which it has already
        # appended: a missing tail means the lineage is broken, not empty.
        if not chain or db.get("missions", chain[-1]) is None:
            return [], False
        return inherited_open_findings(db, mid), True
    except Exception:
        logger.warning("inherited findings unavailable for %s", mid, exc_info=True)
        return [], False


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
    reason = row.get("degradation_reason")
    return {
        "id": row.get("id"),
        "reviewer": reviewer,
        "independent": bool(row.get("independent")),
        "degradation_reason": redact(str(reason)) if reason else None,
        "writer_set": writer_set,
        "reviewer_in_writer_set": bool(reviewer) and reviewer in writer_set,
        "parsed": None if parsed is None else bool(parsed),
        "reviewed_base_sha": row.get("reviewed_base_sha"),
        "reviewed_head_sha": row.get("reviewed_head_sha"),
        "created_at": row.get("created_at"),
    }


def _tally(rows: list[dict[str, Any]], into: dict[str, Any]) -> None:
    """Each finding lands in exactly one bucket: open or repair-claimed."""
    for r in rows:
        severity = str(r.get("severity") or "").upper()
        status = str(r.get("status") or "")
        if status not in UNRESOLVED_FINDING_STATUSES:
            continue
        if severity not in into["open_findings"]:
            # Unknown severities are counted separately rather than dropped
            # or promoted into a known bucket.
            into["unrecognized_severity"] += 1
        elif status == OPEN_STATUS:
            into["open_findings"][severity] += 1
        else:
            into["repair_claimed_findings"][severity] += 1


def _blank() -> dict[str, Any]:
    return {
        "open_findings": _empty_counts(),
        "repair_claimed_findings": _empty_counts(),
        "unrecognized_severity": 0,
        "inherited_available": True,
        "review": None,
        "review_count": 0,
    }


def mission_trust_bulk(
    db: Database,
    missions: list[dict[str, Any]],
    inherited: dict[str, tuple[list[dict[str, Any]], bool]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Trust summaries for many missions with a bounded number of queries.

    ``inherited`` lets a caller that already resolved lineage for a mission
    pass it in instead of resolving it twice.
    """
    if len(missions) > BULK_CHUNK:
        merged: dict[str, dict[str, Any]] = {}
        for start in range(0, len(missions), BULK_CHUNK):
            merged.update(mission_trust_bulk(db, missions[start : start + BULK_CHUNK], inherited))
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
        rows, available = inherited[mid] if inherited and mid in inherited else inherited_for(db, m)
        out[mid]["inherited_available"] = available
        _tally(rows, out[mid])

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


def mission_trust(
    db: Database, mission: dict[str, Any], inherited: tuple[list[dict[str, Any]], bool] | None = None
) -> dict[str, Any]:
    mid = str(mission["id"])
    return mission_trust_bulk(db, [mission], {mid: inherited} if inherited is not None else None)[mid]


class TerminalTrustCache:
    """Memoized trust for terminal missions (bounded LRU).

    Keyed by (id, status, updated_at): a terminal mission's findings and
    reviews no longer change (retries create new missions; ancestor rows are
    never mutated), and any row update to the mission itself changes the key.
    Non-terminal missions are never summarised by the list path.
    """

    def __init__(self, size: int = TRUST_CACHE_SIZE) -> None:
        self._size = size
        self._entries: OrderedDict[tuple[str, str, str], dict[str, Any]] = OrderedDict()
        # Sync FastAPI routes run in a threadpool.
        self._lock = threading.Lock()

    def list_trust(self, db: Database, missions: list[dict[str, Any]]) -> dict[str, dict[str, Any] | None]:
        with self._lock:
            return self._list_trust(db, missions)

    def _list_trust(self, db: Database, missions: list[dict[str, Any]]) -> dict[str, dict[str, Any] | None]:
        result: dict[str, dict[str, Any] | None] = {}
        missing: list[dict[str, Any]] = []
        for m in missions:
            mid = str(m["id"])
            if str(m.get("status") or "") not in TERMINAL_MISSION_STATUSES:
                result[mid] = None
                continue
            key = (mid, str(m.get("status") or ""), str(m.get("updated_at") or ""))
            cached = self._entries.get(key)
            if cached is not None:
                self._entries.move_to_end(key)
                result[mid] = cached
            else:
                missing.append(m)
        if missing:
            computed = mission_trust_bulk(db, missing)
            for m in missing:
                mid = str(m["id"])
                trust = computed[mid]
                result[mid] = trust
                # Unreadable history is transient: do not pin it in the cache.
                if trust["inherited_available"]:
                    key = (mid, str(m.get("status") or ""), str(m.get("updated_at") or ""))
                    self._entries[key] = trust
                    self._entries.move_to_end(key)
            while len(self._entries) > self._size:
                self._entries.popitem(last=False)
        return result
