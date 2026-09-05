"""Review engine: parse reviewer output into structured findings.

The reviewer provider is asked to emit a JSON array after a
`REVIEW_FINDINGS_JSON:` marker. Parsing is tolerant: if no valid block is
found, a single LOW finding records that review output was unstructured.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .db import Database
from .models import ReviewFinding, Severity

MARKER = "REVIEW_FINDINGS_JSON:"

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
""".strip()


def parse_findings(raw_output: str) -> list[dict[str, Any]]:
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
                if isinstance(item, dict) and item.get("description"):
                    findings.append(item)
            return findings
    return []


def persist_findings(db: Database, mission_id: str, raw_output: str) -> list[ReviewFinding]:
    parsed = parse_findings(raw_output)
    findings: list[ReviewFinding] = []
    for item in parsed:
        try:
            severity = Severity(str(item.get("severity", "LOW")).upper())
        except ValueError:
            severity = Severity.LOW
        finding = ReviewFinding(
            mission_id=mission_id,
            severity=severity,
            category=str(item.get("category", "general")),
            file=item.get("file") if item.get("file") else None,
            description=str(item["description"])[:2000],
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
                "created_at": finding.created_at,
            },
        )
    return findings


def open_blockers(db: Database, mission_id: str) -> list[dict[str, Any]]:
    return db.query(
        "SELECT * FROM review_findings WHERE mission_id=? AND status='open' AND severity IN ('BLOCKER','HIGH')",
        (mission_id,),
    )


def resolve_open_findings(db: Database, mission_id: str) -> None:
    db.execute("UPDATE review_findings SET status='resolved' WHERE mission_id=? AND status='open'", (mission_id,))
