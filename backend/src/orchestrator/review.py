"""Review engine: parse reviewer output into structured findings.

The reviewer provider is asked to emit a JSON array after a
`REVIEW_FINDINGS_JSON:` marker. Parsing is tolerant: if no valid block is
found, a single LOW finding records that review output was unstructured.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .db import Database
from .models import ReviewFinding, Severity, utcnow

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


def parse_verified_fixed(raw_output: str) -> list[dict[str, Any]]:
    """Parse the optional explicit resolution mapping from reviewer output."""
    for line in raw_output.splitlines():
        stripped = line.strip()
        if not stripped.startswith(VERIFIED_FIXED_MARKER):
            continue
        payload = stripped[len(VERIFIED_FIXED_MARKER):].strip()
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
        payload = stripped[len(MARKER):].strip()
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
        payload = stripped[len(MARKER):].strip()
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
    # nothing.
    for item in parse_verified_fixed(raw_output):
        evidence = str(item.get("evidence", ""))[:2000]
        fid = str(item.get("finding_id", "") or "")
        fpr = str(item.get("fingerprint", "") or "")
        if fid:
            db.execute(
                "UPDATE review_findings SET status='resolved', verified_by=?, resolved_at=? "
                "WHERE id=? AND mission_id=? AND status='repair_attempted'",
                (evidence, utcnow().isoformat(), fid, mission_id),
            )
        elif fpr:
            db.execute(
                "UPDATE review_findings SET status='resolved', verified_by=?, resolved_at=? "
                "WHERE fingerprint=? AND mission_id=? AND status='repair_attempted'",
                (evidence, utcnow().isoformat(), fpr, mission_id),
            )
    return parsed_ok, findings


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
    return db.query(
        "SELECT * FROM review_findings WHERE mission_id=? AND status='open' AND severity IN ('BLOCKER','HIGH')",
        (mission_id,),
    )


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
    """
    return db.query(
        "SELECT * FROM review_findings WHERE mission_id=? AND status='repair_attempted'",
        (mission_id,),
    )


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

