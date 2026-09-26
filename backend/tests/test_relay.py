"""Agent Relay: persisted provider hand-to-hand chain, bounded and redacted."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import make_config
from orchestrator.api.app import create_app
from orchestrator.api.auth import load_or_create_token
from orchestrator.db import Database
from orchestrator.orchestrator import Orchestrator
from orchestrator.providers.fake import FakeAdapter
from orchestrator.relay import (
    HANDOFF_PREVIEW_CHARS,
    LOST_WORK_MIN_MS,
    THIN_EVIDENCE_BLOCK_CHARS,
    handoff_content,
    mission_relay,
)

SECRET = "sk-ant-api03-" + "A" * 40


def _seed_mission(db: Database, mid: str = "m1") -> None:
    if not db.get("projects", "proj"):
        db.insert("projects", {"id": "proj", "name": "p", "path": "/tmp/proj-relay", "created_at": "t"})
    db.insert(
        "missions",
        {"id": mid, "project_id": "proj", "title": "t", "task": "t", "created_at": "t", "updated_at": "t"},
    )


def _run(db: Database, rid: str, provider: str, role: str, at: str, **extra: object) -> None:
    row: dict[str, object] = {
        "id": rid,
        "mission_id": "m1",
        "provider": provider,
        "role": role,
        "stage": role,
        "started_at": at,
        "finished_at": at,
        "run_status": "SUCCEEDED",
        "duration_ms": 1000,
    }
    row.update(extra)
    db.insert("provider_runs", row)


def _manifest(db: Database, rid: str, blocks: list[dict[str, object]], warnings: list[str] | None = None) -> None:
    db.insert(
        "run_context_manifests",
        {
            "run_id": rid,
            "prompt_chars": 1000,
            "estimated_prompt_tokens": 250,
            "blocks_json": json.dumps(blocks),
            "warnings_json": json.dumps(warnings or []),
            "created_at": "t",
        },
    )


def _block(block_type: str, chars: int, original: int | None = None, priority: str = "MANDATORY") -> dict[str, object]:
    return {
        "block_type": block_type,
        "block_id": block_type.lower(),
        "priority": priority,
        "included": True,
        "included_chars": chars,
        "original_chars": original if original is not None else chars,
    }


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "relay.db")
    _seed_mission(database)
    return database


def test_replays_dogfood_review_on_thin_diff(db: Database) -> None:
    _run(db, "r-review", "agy", "review", "2026-09-12T01:00:00")
    _manifest(
        db,
        "r-review",
        [
            _block("SYSTEM_INSTRUCTIONS", 232),
            _block("TASK_OBJECTIVE", 25160),
            _block("GIT_DIFF", 131),
            _block("OUTPUT_CONTRACT", 120),
        ],
    )
    run = mission_relay(db, "m1")["timeline"][0]
    assert "THIN_REQUIRED_EVIDENCE" in run["flags"]
    thin = run["context"]["thin_evidence_blocks"]
    # Framing blocks are short by design and never flagged.
    assert [b["block_type"] for b in thin] == ["GIT_DIFF"]
    assert thin[0]["included_chars"] == 131 < THIN_EVIDENCE_BLOCK_CHARS


def test_truncated_and_optional_blocks(db: Database) -> None:
    _run(db, "r1", "claude", "implementation", "2026-09-12T01:00:00")
    _manifest(db, "r1", [_block("PHASE_CONTEXT", 900, 5000), _block("NOTES", 10, priority="PREFERRED")], ["X"])
    run = mission_relay(db, "m1")["timeline"][0]
    assert run["flags"] == ["CONTEXT_TRUNCATED"]
    assert run["context"]["warnings"] == ["X"]


def test_lost_work_and_uncaptured_context(db: Database) -> None:
    _run(
        db,
        "r1",
        "codex",
        "testing",
        "2026-09-12T01:00:00",
        run_status="FAILED",
        failure_class="RATE_LIMITED",
        duration_ms=LOST_WORK_MIN_MS + 1,
    )
    _run(db, "r2", "codex", "review", "2026-09-12T02:00:00", run_status="FAILED", duration_ms=5)
    relay = mission_relay(db, "m1")
    first, second = relay["timeline"]
    assert "LOST_WORK" in first["flags"]
    assert "CONTEXT_NOT_CAPTURED" in first["flags"]
    assert "LOST_WORK" not in second["flags"]
    codex = relay["providers"][0]
    assert codex["not_succeeded"] == 2
    assert codex["roles"] == ["testing", "review"]


def test_unknown_duration_is_not_zero(db: Database) -> None:
    _run(
        db, "r1", "claude", "planning", "2026-09-12T01:00:00", duration_ms=None, run_status="RUNNING", finished_at=None
    )
    relay = mission_relay(db, "m1")
    assert relay["timeline"][0]["duration_ms"] is None
    totals = relay["providers"][0]
    assert totals["unknown_duration_runs"] == 1
    assert totals["in_flight"] == 1


def test_legacy_run_outcome_is_labelled(db: Database) -> None:
    _run(db, "r1", "claude", "repair", "2026-09-12T01:00:00", run_status="", failure_class="NONE")
    run = mission_relay(db, "m1")["timeline"][0]
    assert run["outcome"] == "SUCCEEDED"
    assert run["outcome_source"] == "legacy"


def test_commit_change_needs_both_shas(db: Database) -> None:
    _run(db, "r1", "opencode", "implementation", "a1", git_commit_before="aaa", git_commit_after="bbb")
    _run(db, "r2", "claude", "review", "a2", git_commit_before="bbb", git_commit_after="bbb")
    _run(db, "r3", "claude", "review", "a3", git_commit_before="bbb")
    changed = [e["changed_commit"] for e in mission_relay(db, "m1")["timeline"]]
    assert changed == [True, False, None]


def test_handoffs_are_bounded_and_redacted(db: Database) -> None:
    content = f"token {SECRET} " + "x" * (HANDOFF_PREVIEW_CHARS * 3)
    db.insert(
        "handoffs",
        {
            "id": "h1",
            "mission_id": "m1",
            "from_provider": "codex",
            "to_provider": "agy",
            "role": "review",
            "content": content,
            "created_at": "2026-09-12T01:00:00",
        },
    )
    entry = mission_relay(db, "m1")["timeline"][0]
    assert entry["kind"] == "handoff"
    assert entry["preview_truncated"] is True
    assert len(entry["preview"]) == HANDOFF_PREVIEW_CHARS
    assert SECRET not in entry["preview"]
    full = handoff_content(db, "m1", "h1")
    assert full is not None
    assert SECRET not in full["content"]
    assert full["content_chars"] == len(full["content"])
    # A handoff is only served within its own mission.
    _seed_mission(db, "m2")
    assert handoff_content(db, "m2", "h1") is None


def test_timeline_orders_runs_handoffs_reviews(db: Database) -> None:
    at = "2026-09-12T01:00:00"
    db.insert("handoffs", {"id": "h1", "mission_id": "m1", "role": "review", "content": "c", "created_at": at})
    _run(db, "r1", "agy", "review", at)
    db.insert(
        "reviews",
        {"id": "rv", "mission_id": "m1", "review_provider": "agy", "independent": 1, "created_at": at},
    )
    assert [e["kind"] for e in mission_relay(db, "m1")["timeline"]] == ["run", "handoff", "review"]


def test_finding_lineage_fields(db: Database) -> None:
    db.insert(
        "review_findings",
        {
            "id": "f1",
            "mission_id": "m1",
            "severity": "HIGH",
            "description": "d " + SECRET,
            "status": "resolved",
            "origin_review_id": "rv1",
            "resolved_review_id": "rv2",
            "verified_by": "codex",
            "created_at": "t",
        },
    )
    finding = mission_relay(db, "m1")["findings"][0]
    assert finding["origin_review_id"] == "rv1"
    assert finding["resolved_review_id"] == "rv2"
    assert SECRET not in finding["description"]


def test_api_relay_and_handoff_routes(tmp_path: Path) -> None:
    database = Database(tmp_path / "api.db")
    _seed_mission(database)
    database.insert("handoffs", {"id": "h1", "mission_id": "m1", "role": "r", "content": "full", "created_at": "t"})
    orch = Orchestrator(database, make_config(), {"fake-a": FakeAdapter("fake-a", ["ok"])})
    app = create_app(tmp_path / "api.db", make_config(), orch)
    client = TestClient(app, headers={"Authorization": f"Bearer {load_or_create_token(tmp_path)}"})
    assert client.get("/api/missions/m1/relay").json()["timeline"][0]["id"] == "h1"
    assert client.get("/api/missions/nope/relay").status_code == 404
    assert client.get("/api/missions/m1/handoffs/h1").json()["content"] == "full"
    assert client.get("/api/missions/m1/handoffs/missing").status_code == 404
    unauth = TestClient(app)
    assert unauth.get("/api/missions/m1/relay").status_code == 401


def test_relay_lists_inherited_unresolved_findings(db: Database) -> None:
    _seed_mission(db, "parent")
    db.execute("UPDATE missions SET retry_of_mission_id='parent' WHERE id='m1'")
    db.insert(
        "review_findings",
        {
            "id": "pf",
            "mission_id": "parent",
            "severity": "MEDIUM",
            "description": "inherited",
            "status": "repair_attempted",
            "fingerprint": "fp-1",
            "created_at": "t",
        },
    )
    relay = mission_relay(db, "m1")
    assert relay["inherited_findings_available"] is True
    [finding] = relay["findings"]
    assert finding["inherited_from_mission_id"] == "parent"
    assert finding["status"] == "repair_attempted"
