"""Review engine: parse reviewer output into structured findings.

The reviewer provider is asked to emit a JSON array after a
`REVIEW_FINDINGS_JSON:` marker. Parsing is tolerant: if no valid block is
found, a single LOW finding records that review output was unstructured.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any

from .db import Database
from .models import ReviewFinding, Severity, utcnow

logger = logging.getLogger(__name__)

MARKER = "REVIEW_FINDINGS_JSON:"

#: Optional reviewer contract: explicit evidence that specific prior findings
#: were verified fixed. Omission of a prior finding proves nothing by itself.
VERIFIED_FIXED_MARKER = "VERIFIED_FIXED_JSON:"

REVIEW_INSTRUCTIONS = f"""
You are performing an independent code review. Inspect the working tree
(correctness, architecture, security, error handling, maintainability, tests,
requirements coverage, edge cases).

After your analysis, output EXACTLY ONE line starting with:
{MARKER}
followed by a JSON array of findings. Each finding must be an object:
{{"severity": "BLOCKER"|"HIGH"|"MEDIUM"|"LOW", "category": "correctness|security|architecture|tests|style|requirements",
  "file": "<path or null>", "description": "...", "recommended_fix": "..."}}
If everything is acceptable, output: {MARKER} []

When prior findings (with IDs) are listed in your instructions, you MUST
either re-flag each still-present issue above, or explicitly record what you
verified fixed with evidence on its own line:
{VERIFIED_FIXED_MARKER} [{{"finding_id": "<id>", "evidence": "<what you checked>"}}]
(finding_id may be replaced by "fingerprint" with the listed fingerprint.)
A prior finding you neither re-flag nor verify stays UNVERIFIED — omitting it
proves nothing and will block product acceptance.
""".strip()


def _is_valid_finding(item: Any) -> bool:
    """Validate that a finding dict has required schema fields."""
    if not isinstance(item, dict):
        return False
    desc = item.get("description")
    if not desc or not isinstance(desc, str):
        return False
    severity = str(item.get("severity", "")).upper()
    if severity not in {"BLOCKER", "HIGH", "MEDIUM", "LOW"}:
        return False
    # category must be a string if present
    category = item.get("category")
    if category is not None and not isinstance(category, str):
        return False
    # file must be string or None
    file_val = item.get("file")
    if file_val is not None and not isinstance(file_val, str):
        return False
    # recommended_fix must be a string if present
    fix = item.get("recommended_fix")
    if fix is not None and not isinstance(fix, str):
        return False
    return True


def finding_fingerprint(severity: str, category: str, file: str | None, description: str) -> str:
    """Stable identity for a finding across review cycles.

    Normalized (case/whitespace-insensitive) so a re-flagged defect matches
    the row left in repair_attempted, while genuinely new findings differ.
    """
    norm = "|".join(
        [
            (severity or "").strip().upper(),
            (category or "").strip().lower(),
            (file or "").strip(),
            re.sub(r"\s+", " ", (description or "").strip().lower()),
        ]
    )
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:32]


def retry_ancestors(db: Any, mission_id: str | None, limit: int = 20) -> list[str]:
    """Retry lineage chain for a mission, nearest parent first (DOG-02).

    Follows missions.retry_of_mission_id; cycle-safe and bounded. Empty
    when the mission is not a retry. Historical rows are never mutated;
    callers union ancestor open findings with local rows (deduped by
    fingerprint) so a retry cannot forget unresolved history.
    """
    if not mission_id:
        return []
    chain: list[str] = []
    seen = {mission_id}
    current = mission_id
    for _ in range(limit):
        try:
            row = db.get("missions", current)
        except Exception:
            logger.debug("retry chain lookup failed for %s", current, exc_info=True)
            break
        if not row:
            break
        parent = row.get("retry_of_mission_id")
        if not parent or parent in seen:
            break
        chain.append(str(parent))
        seen.add(str(parent))
        current = str(parent)
    return chain


def _local_fingerprints(db: Any, mission_id: str) -> set[str]:
    try:
        rows = db.query("SELECT fingerprint FROM review_findings WHERE mission_id=?", (mission_id,))
    except Exception:
        logger.debug("local fingerprint lookup failed for %s", mission_id, exc_info=True)
        return set()
    return {str(r.get("fingerprint") or "") for r in rows if r.get("fingerprint")}


def _current_state_row(group: list[dict[str, Any]]) -> dict[str, Any]:
    """Deterministic current state among same-fingerprint rows in one mission.

    Open wins, then repair_attempted, else the latest terminal row. Rows are
    explicitly ordered (created_at, rowid), never arbitrary SQLite order, so
    legacy duplicate rows resolve the same way on every read. Unresolved
    states stay visible (fail-closed direction); terminal states
    (resolved/wontfix/...) shadow older ancestors.
    """
    ordered = sorted(group, key=lambda r: (str(r.get("created_at") or ""), str(r.get("id") or "")))
    for want in ("open", "repair_attempted"):
        cands = [r for r in ordered if str(r.get("status") or "") == want]
        if cands:
            return dict(cands[-1])
    return dict(ordered[-1])


def nearest_ancestor_finding_states(db: Any, mission_id: str | None, limit: int = 20) -> dict[str, dict[str, Any]]:
    """Nearest-first fingerprint state across the retry chain (AGY-F1).

    Single source of truth shared by query-time visibility
    (inherited_open_findings) and copy-time seeding
    (orchestrator._inherit_retry_findings) so both observe identical
    semantics. For each fingerprint, the nearest ancestor holding any local
    row is authoritative: its current state shadows all older ancestors.
    Returns {fingerprint: {"ancestor": mission_id, "row": row_dict}} in
    nearest-first encounter order. Historical rows are never mutated.
    """
    if not mission_id:
        return {}
    ancestors = retry_ancestors(db, mission_id, limit=limit)
    states: dict[str, dict[str, Any]] = {}
    for anc in ancestors:
        try:
            rows = db.query("SELECT * FROM review_findings WHERE mission_id=?", (anc,))
        except Exception:
            logger.debug("ancestor findings lookup failed for %s", anc, exc_info=True)
            continue
        by_fp: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            fp = str(r.get("fingerprint") or "")
            if fp:
                by_fp.setdefault(fp, []).append(dict(r))
        for fp, group in by_fp.items():
            if fp in states:
                continue
            states[fp] = {"ancestor": anc, "row": _current_state_row(group)}
    return states


def inherited_open_findings(db: Any, mission_id: str | None) -> list[dict[str, Any]]:
    """Unresolved ancestor findings not yet shadowed locally (DOG-02, AGY-F1).

    Returns ancestor rows with status open/repair_attempted whose fingerprint
    has no local row in any status (open/repair_attempted/resolved/wontfix)
    AND whose nearest ancestor state is itself unresolved. A nearer
    RESOLVED/wontfix ancestor therefore suppresses an older OPEN ancestor
    (no resurrection); a local row of any status shadows all ancestors.
    Each row carries inherited_from_mission_id for display.
    """
    if not mission_id:
        return []
    if not retry_ancestors(db, mission_id):
        return []
    local_fps = _local_fingerprints(db, mission_id)
    out: list[dict[str, Any]] = []
    for fp, info in nearest_ancestor_finding_states(db, mission_id).items():
        if fp in local_fps:
            continue
        row = info["row"]
        if str(row.get("status") or "") not in ("open", "repair_attempted"):
            continue
        d = dict(row)
        d["inherited_from_mission_id"] = info["ancestor"]
        d["inherited"] = True
        out.append(d)
    return out


def parse_verified_fixed(raw_output: str) -> list[dict[str, Any]]:
    """Parse the optional explicit resolution mapping from reviewer output."""
    for line in raw_output.splitlines():
        stripped = line.strip()
        if not stripped.startswith(VERIFIED_FIXED_MARKER):
            continue
        payload = stripped[len(VERIFIED_FIXED_MARKER) :].strip()
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(data, list):
            return [d for d in data if isinstance(d, dict) and (d.get("finding_id") or d.get("fingerprint"))]
    return []


def parse_review_output(raw_output: str) -> tuple[bool, list[dict[str, Any]]]:
    for line in raw_output.splitlines():
        stripped = line.strip()
        if not stripped.startswith(MARKER):
            continue
        payload = stripped[len(MARKER) :].strip()
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            # Try to recover a JSON array spanning the rest of the line
            match = re.search(r"\[.*\]", payload)
            if not match:
                continue
            try:
                data = json.loads(match.group(0))
            except json.JSONDecodeError:
                continue
        if isinstance(data, list):
            findings: list[dict[str, Any]] = []
            for item in data:
                if _is_valid_finding(item):
                    findings.append(item)
            if not data or findings:
                return True, findings
            continue

    # No structured review block found: synthesize finding (F-08)
    unstructured_finding = {
        "severity": "MEDIUM",
        "category": "architecture",
        "file": None,
        "description": "Reviewer output was unstructured; no valid REVIEW_FINDINGS_JSON block detected.",
        "recommended_fix": "Provide structured code review adhering to the REVIEW_FINDINGS_JSON contract.",
    }
    return False, [unstructured_finding]


def parse_findings(raw_output: str) -> list[dict[str, Any]]:
    for line in raw_output.splitlines():
        stripped = line.strip()
        if not stripped.startswith(MARKER):
            continue
        payload = stripped[len(MARKER) :].strip()
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            match = re.search(r"\[.*\]", payload)
            if not match:
                continue
            try:
                data = json.loads(match.group(0))
            except json.JSONDecodeError:
                continue
        if isinstance(data, list):
            findings: list[dict[str, Any]] = []
            for item in data:
                if _is_valid_finding(item):
                    findings.append(item)
            if not data or findings:
                return findings
            continue
    return []


def persist_findings(db: Database, mission_id: str, raw_output: str) -> tuple[bool, list[ReviewFinding]]:
    parsed_ok, parsed = parse_review_output(raw_output)
    findings: list[ReviewFinding] = []
    for item in parsed:
        try:
            severity = Severity(str(item.get("severity", "LOW")).upper())
        except ValueError:
            severity = Severity.LOW
        category = str(item.get("category", "general"))
        file_val = item.get("file") if item.get("file") else None
        description = str(item["description"])[:2000]
        fingerprint = finding_fingerprint(severity.value, category, file_val, description)
        # A re-flagged defect reopens its repair_attempted row instead of
        # duplicating it; history is preserved in the row and the event log.
        reopened = db.query(
            "SELECT * FROM review_findings WHERE mission_id=? AND fingerprint=? AND status='repair_attempted'",
            (mission_id, fingerprint),
        )
        if reopened:
            db.execute(
                "UPDATE review_findings SET status='open', verified_by=NULL WHERE id=?",
                (reopened[0]["id"],),
            )
            row = db.get("review_findings", reopened[0]["id"])
            if row:
                findings.append(_finding_from_row(row))
            continue
        # DOG-02 dedup: same fingerprint already open locally (including an
        # inherited copy) coheres to one lineage instead of two copies.
        existing_open = db.query(
            "SELECT * FROM review_findings WHERE mission_id=? AND fingerprint=? AND status='open' LIMIT 1",
            (mission_id, fingerprint),
        )
        if existing_open:
            row = db.get("review_findings", existing_open[0]["id"])
            if row:
                findings.append(_finding_from_row(row))
            continue
        finding = ReviewFinding(
            mission_id=mission_id,
            severity=severity,
            category=category,
            file=file_val,
            description=description,
            recommended_fix=str(item.get("recommended_fix", ""))[:2000],
        )
        findings.append(finding)
        db.insert(
            "review_findings",
            {
                "id": finding.id,
                "mission_id": mission_id,
                "severity": finding.severity.value,
                "category": finding.category,
                "file": finding.file,
                "description": finding.description,
                "recommended_fix": finding.recommended_fix,
                "status": finding.status,
                "fingerprint": fingerprint,
                "created_at": finding.created_at,
            },
        )
    # Explicit resolution mapping: only findings the reviewer verified fixed
    # (with evidence) transition out of repair_attempted. Omission resolves
    # nothing (DOG-02: omission never resolves inherited findings either).
    for item in parse_verified_fixed(raw_output):
        evidence = str(item.get("evidence", ""))[:2000]
        fid = str(item.get("finding_id", "") or "")
        fpr = str(item.get("fingerprint", "") or "")
        if fid:
            try:
                pre = db.query(
                    "SELECT id FROM review_findings WHERE id=? AND mission_id=? AND status='repair_attempted' LIMIT 1",
                    (fid, mission_id),
                )
            except Exception:
                logger.debug("resolve lookup failed", exc_info=True)
                pre = []
            db.execute(
                "UPDATE review_findings SET status='resolved', verified_by=?, resolved_at=? "
                "WHERE id=? AND mission_id=? AND status='repair_attempted'",
                (evidence, utcnow().isoformat(), fid, mission_id),
            )
            if not pre:
                # fid may name an inherited ancestor row: record an explicit
                # resolved marker locally without mutating history.
                try:
                    anc_rows = []
                    for anc in retry_ancestors(db, mission_id):
                        r = db.get("review_findings", fid)
                        if r and str(r.get("mission_id") or "") == anc:
                            anc_rows = [r]
                            break
                    if anc_rows:
                        _resolve_inherited_copy(db, mission_id, anc_rows[0], evidence)
                except Exception:
                    logger.debug("inherited resolve by id failed", exc_info=True)
        elif fpr:
            try:
                pre = db.query(
                    "SELECT id FROM review_findings WHERE fingerprint=? AND mission_id=?"
                    " AND status='repair_attempted' LIMIT 1",
                    (fpr, mission_id),
                )
            except Exception:
                logger.debug("resolve lookup failed", exc_info=True)
                pre = []
            db.execute(
                "UPDATE review_findings SET status='resolved', verified_by=?, resolved_at=? "
                "WHERE fingerprint=? AND mission_id=? AND status='repair_attempted'",
                (evidence, utcnow().isoformat(), fpr, mission_id),
            )
            if not pre:
                try:
                    for inh in inherited_open_findings(db, mission_id):
                        if str(inh.get("fingerprint") or "") == fpr:
                            _resolve_inherited_copy(db, mission_id, inh, evidence)
                            break
                except Exception:
                    logger.debug("inherited resolve by fingerprint failed", exc_info=True)
    return parsed_ok, findings


def _resolve_inherited_copy(db: Any, mission_id: str, ancestor_row: dict[str, Any], evidence: str) -> None:
    """Record explicit verified-fix for an inherited finding (DOG-02).

    The ancestor row is never mutated. A resolved marker with the same
    fingerprint is inserted locally so the retry view considers it fixed
    while history stays auditable via inherited_from_* + fingerprint.
    No-op when a local row with that fingerprint already exists.
    """
    try:
        fp = str(ancestor_row.get("fingerprint") or "")
        if not fp:
            return
        existing = db.query(
            "SELECT id FROM review_findings WHERE mission_id=? AND fingerprint=? LIMIT 1",
            (mission_id, fp),
        )
        if existing:
            return
        import uuid as _uuid

        now = utcnow().isoformat()
        payload: dict[str, Any] = {
            "id": f"f-{_uuid.uuid4().hex[:12]}",
            "mission_id": mission_id,
            "severity": str(ancestor_row.get("severity") or "MEDIUM"),
            "category": str(ancestor_row.get("category") or "general"),
            "file": ancestor_row.get("file"),
            "description": str(ancestor_row.get("description") or ""),
            "recommended_fix": str(ancestor_row.get("recommended_fix") or ""),
            "status": "resolved",
            "fingerprint": fp,
            "verified_by": evidence,
            "resolved_at": now,
            "inherited_from_mission_id": str(
                ancestor_row.get("inherited_from_mission_id") or ancestor_row.get("mission_id") or ""
            ),
            "inherited_from_finding_id": str(ancestor_row.get("id") or ""),
            "created_at": now,
        }
        try:
            db.insert("review_findings", payload)
        except Exception:
            # Pre-migration DBs lack inherited_* columns: preserve the
            # resolution without lineage rather than losing it.
            try:
                payload.pop("inherited_from_mission_id", None)
                payload.pop("inherited_from_finding_id", None)
                db.insert("review_findings", payload)
            except Exception:
                return
    except Exception:
        return


def _finding_from_row(row: dict[str, Any]) -> ReviewFinding:
    return ReviewFinding(
        id=row["id"],
        mission_id=row["mission_id"],
        severity=Severity(str(row.get("severity", "LOW")).upper()),
        category=str(row.get("category", "general")),
        file=row.get("file"),
        description=str(row.get("description", "")),
        recommended_fix=str(row.get("recommended_fix", "")),
        status=str(row.get("status", "open")),
        created_at=row.get("created_at"),  # type: ignore[arg-type]
    )


def open_blockers(db: Database, mission_id: str) -> list[dict[str, Any]]:
    """Open BLOCKER/HIGH findings including inherited retry lineage (DOG-02).

    Existing mission completion policy is preserved; retry lineage obeys it:
    an inherited BLOCKER/HIGH still blocks the retry until explicitly
    verified fixed. MEDIUM and below inherit visibility without blocking,
    exactly as local findings behave.
    """
    local = db.query(
        "SELECT * FROM review_findings WHERE mission_id=? AND status='open' AND severity IN ('BLOCKER','HIGH')",
        (mission_id,),
    )
    try:
        inherited = [r for r in inherited_open_findings(db, mission_id) if str(r.get("status") or "") == "open"]
    except Exception:
        inherited = []
    inherited_blockers = [r for r in inherited if str(r.get("severity") or "").upper() in ("BLOCKER", "HIGH")]
    # Dedup by fingerprint (local shadows inherited); history stays in rows.
    seen = {str(r.get("fingerprint") or r.get("id")) for r in local if r.get("fingerprint") or r.get("id")}
    out = list(local)
    for r in inherited_blockers:
        key = str(r.get("fingerprint") or r.get("id"))
        if key and key not in seen:
            seen.add(key)
            out.append(r)
    return out


def mark_findings_repair_attempted(db: Database, mission_id: str) -> None:
    """Transition open findings to repair_attempted before repair run."""
    db.execute(
        "UPDATE review_findings SET status='repair_attempted' WHERE mission_id=? AND status='open'",
        (mission_id,),
    )


def unverified_findings(db: Database, mission_id: str) -> list[dict[str, Any]]:
    """Findings repaired but never verified fixed (omitted by later reviews).

    These no longer block the mission loop (bounded cycles preserved) but
    MUST gate product-level acceptance for requirement/correctness content.
    Includes inherited repair_attempted lineage so a retry cannot wash them.
    """
    local = db.query(
        "SELECT * FROM review_findings WHERE mission_id=? AND status='repair_attempted'",
        (mission_id,),
    )
    try:
        inherited = [
            r for r in inherited_open_findings(db, mission_id) if str(r.get("status") or "") == "repair_attempted"
        ]
    except Exception:
        inherited = []
    seen = {str(r.get("fingerprint") or r.get("id")) for r in local if r.get("fingerprint") or r.get("id")}
    out = list(local)
    for r in inherited:
        key = str(r.get("fingerprint") or r.get("id"))
        if key and key not in seen:
            seen.add(key)
            out.append(r)
    return out


def resolve_repaired_findings(db: Database, mission_id: str) -> None:
    """REMOVED semantics (F-LIFE-02): blanket resolution on re-review omission.

    Kept as a no-op for backward compatibility; findings now resolve only via
    explicit reviewer verification (VERIFIED_FIXED_JSON) applied in
    persist_findings. A subsequent review that omits a finding proves nothing.
    """
    _ = (db, mission_id)
    return


def resolve_open_findings(db: Database, mission_id: str) -> None:
    """Backward-compatible helper: marks open findings repair_attempted."""
    mark_findings_repair_attempted(db, mission_id)
