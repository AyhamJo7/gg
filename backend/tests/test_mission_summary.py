"""Mission trust summaries: caveats derived from persisted rows only."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import make_config
from orchestrator.api.app import create_app
from orchestrator.api.auth import load_or_create_token
from orchestrator.db import Database
from orchestrator.mission_summary import BULK_CHUNK, mission_trust, mission_trust_bulk, review_summary
from orchestrator.orchestrator import Orchestrator
from orchestrator.providers.fake import FakeAdapter

TS = "2026-09-12T02:00:00+00:00"


def _mission(db: Database, mid: str, status: str = "COMPLETED", retry_of: str | None = None) -> dict[str, object]:
    if not db.get("projects", "proj"):
        db.insert("projects", {"id": "proj", "name": "p", "path": "/tmp/proj-trust", "created_at": TS})
    row = {
        "id": mid,
        "project_id": "proj",
        "title": mid,
        "task": "t",
        "status": status,
        "created_at": TS,
        "updated_at": TS,
        "retry_of_mission_id": retry_of,
    }
    db.insert("missions", row)
    return row


def _finding(db: Database, fid: str, mid: str, severity: str, status: str, fp: str = "", ts: str = TS) -> None:
    db.insert(
        "review_findings",
        {
            "id": fid,
            "mission_id": mid,
            "severity": severity,
            "description": "d",
            "status": status,
            "fingerprint": fp or fid,
            "created_at": ts,
        },
    )


def _review(
    db: Database, rid: str, mid: str, reviewer: str, independent: bool, reason: str | None, writers: list[str], ts: str
) -> None:
    db.insert(
        "reviews",
        {
            "id": rid,
            "mission_id": mid,
            "implementation_provider": "claude",
            "review_provider": reviewer,
            "independent": int(independent),
            "degradation_reason": reason,
            "writer_set_json": json.dumps(writers),
            "created_at": ts,
        },
    )


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    return Database(tmp_path / "trust.db")


def test_completed_mission_with_open_medium_and_uncertified_review(db: Database) -> None:
    """Replays the RechnungsRadar shape: COMPLETED is not clean."""
    m = _mission(db, "m2")
    _finding(db, "f1", "m2", "MEDIUM", "open")
    _finding(db, "f2", "m2", "HIGH", "resolved")
    _review(
        db,
        "r1",
        "m2",
        "agy",
        False,
        "writer provenance incomplete (cannot certify independence)",
        ["claude", "opencode"],
        TS,
    )
    trust = mission_trust(db, m)
    assert trust["unresolved_findings"] == {"BLOCKER": 0, "HIGH": 0, "MEDIUM": 1, "LOW": 0}
    review = trust["review"]
    assert review["independent"] is False
    # The reviewer wrote nothing: no self-review claim may be derived.
    assert review["reviewer_in_writer_set"] is False
    assert review["degradation_reason"].startswith("writer provenance incomplete")


def test_self_review_is_evidence_based(db: Database) -> None:
    m = _mission(db, "m1")
    _review(
        db, "r1", "m1", "claude", False, "reviewer claude is in the candidate writer set (self-review)", ["claude"], TS
    )
    assert mission_trust(db, m)["review"]["reviewer_in_writer_set"] is True


def test_no_review_stays_none_not_clean(db: Database) -> None:
    m = _mission(db, "m1")
    trust = mission_trust(db, m)
    assert trust["review"] is None
    assert trust["review_count"] == 0


def test_latest_review_wins_and_counts_all(db: Database) -> None:
    m = _mission(db, "m1")
    _review(db, "r-old", "m1", "claude", False, "x", ["claude"], "2026-09-12T01:00:00+00:00")
    _review(db, "r-new", "m1", "codex", True, None, ["claude"], "2026-09-12T03:00:00+00:00")
    trust = mission_trust(db, m)
    assert trust["review"]["id"] == "r-new"
    assert trust["review"]["independent"] is True
    assert trust["review_count"] == 2


def test_repair_claims_count_as_unverified(db: Database) -> None:
    m = _mission(db, "m1")
    _finding(db, "f1", "m1", "HIGH", "repair_attempted")
    trust = mission_trust(db, m)
    assert trust["unresolved_findings"]["HIGH"] == 1
    assert trust["unverified_repairs"] == 1


def test_retry_inherits_unresolved_ancestor_findings(db: Database) -> None:
    _mission(db, "parent", status="UNVERIFIED")
    _finding(db, "pf1", "parent", "MEDIUM", "open", fp="fp-a")
    child = _mission(db, "child", retry_of="parent")
    trust = mission_trust(db, child)
    assert trust["unresolved_findings"]["MEDIUM"] == 1
    assert trust["inherited_unresolved"] == 1


def test_local_resolution_shadows_inherited(db: Database) -> None:
    _mission(db, "parent", status="UNVERIFIED")
    _finding(db, "pf1", "parent", "MEDIUM", "open", fp="fp-a")
    child = _mission(db, "child", retry_of="parent")
    _finding(db, "cf1", "child", "MEDIUM", "resolved", fp="fp-a")
    assert mission_trust(db, child)["unresolved_findings"]["MEDIUM"] == 0


def test_unknown_severity_is_counted_not_dropped(db: Database) -> None:
    m = _mission(db, "m1")
    _finding(db, "f1", "m1", "CRITICAL", "open")
    trust = mission_trust(db, m)
    assert trust["unresolved_other"] == 1
    assert sum(trust["unresolved_findings"].values()) == 0


def test_bulk_chunks_large_listings(db: Database) -> None:
    missions = [_mission(db, f"m{i}") for i in range(BULK_CHUNK + 3)]
    _finding(db, "f-last", missions[-1]["id"], "LOW", "open")  # type: ignore[arg-type]
    out = mission_trust_bulk(db, missions)
    assert len(out) == BULK_CHUNK + 3
    assert out[str(missions[-1]["id"])]["unresolved_findings"]["LOW"] == 1


def test_review_summary_handles_legacy_rows() -> None:
    assert review_summary(None) is None
    legacy = review_summary({"review_provider": "agy", "independent": 0, "writer_set_json": "not-json"})
    assert legacy is not None
    assert legacy["writer_set"] == []
    assert legacy["reviewer_in_writer_set"] is False
    assert legacy["parsed"] is None


def test_api_exposes_trust_on_list_and_detail(tmp_path: Path) -> None:
    database = Database(tmp_path / "api.db")
    orch = Orchestrator(database, make_config(), {"fake-a": FakeAdapter("fake-a", ["ok"])})
    app = create_app(tmp_path / "api.db", make_config(), orch)
    client = TestClient(app, headers={"Authorization": f"Bearer {load_or_create_token(tmp_path)}"})
    _mission(database, "m1")
    _finding(database, "f1", "m1", "MEDIUM", "open")
    listed = client.get("/api/missions").json()
    assert listed[0]["trust"]["unresolved_findings"]["MEDIUM"] == 1
    detail = client.get("/api/missions/m1").json()
    assert detail["trust"]["review"] is None
