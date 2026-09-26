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
from orchestrator.models import EventType
from orchestrator.orchestrator import Orchestrator
from orchestrator.providers.fake import FakeAdapter
from orchestrator.relay import (
    HANDOFF_MAX_CHARS,
    HANDOFF_PREVIEW_CHARS,
    LOST_WORK_MIN_MS,
    RELAY_ITEM_LIMIT,
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
        "representation": "COMPACT" if original is not None and original > chars else "FULL",
        "reason": "BUDGET" if original is not None and original > chars else "",
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
    run = mission_relay(db, db.get("missions", "m1"))["timeline"][0]
    assert "THIN_REQUIRED_EVIDENCE" in run["flags"]
    thin = run["context"]["thin_evidence_blocks"]
    # Framing blocks are short by design and never flagged.
    assert [b["block_type"] for b in thin] == ["GIT_DIFF"]
    assert thin[0]["included_chars"] == 131 < THIN_EVIDENCE_BLOCK_CHARS


def test_truncated_and_optional_blocks(db: Database) -> None:
    _run(db, "r1", "claude", "implementation", "2026-09-12T01:00:00")
    _manifest(db, "r1", [_block("PHASE_CONTEXT", 900, 5000), _block("NOTES", 10, priority="PREFERRED")], ["X"])
    run = mission_relay(db, db.get("missions", "m1"))["timeline"][0]
    assert run["flags"] == ["CONTEXT_REDUCED"]
    reduced = run["context"]["reduced_blocks"][0]
    assert reduced["representation"] == "COMPACT"
    assert reduced["reason"] == "BUDGET"
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
    relay = mission_relay(db, db.get("missions", "m1"))
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
    relay = mission_relay(db, db.get("missions", "m1"))
    assert relay["timeline"][0]["duration_ms"] is None
    totals = relay["providers"][0]
    assert totals["unknown_duration_runs"] == 1
    assert totals["in_flight"] == 1


def test_unfinished_legacy_run_is_unrecorded_not_in_flight(db: Database) -> None:
    _run(db, "r1", "claude", "repair", "a", run_status="", finished_at=None, failure_class="NONE")
    relay = mission_relay(db, db.get("missions", "m1"))
    assert relay["timeline"][0]["outcome"] == "UNKNOWN"
    assert relay["providers"][0]["outcome_unknown"] == 1
    assert relay["providers"][0]["in_flight"] == 0


def test_legacy_run_outcome_is_labelled(db: Database) -> None:
    _run(db, "r1", "claude", "repair", "2026-09-12T01:00:00", run_status="", failure_class="NONE")
    run = mission_relay(db, db.get("missions", "m1"))["timeline"][0]
    assert run["outcome"] == "SUCCEEDED"
    assert run["outcome_source"] == "legacy"


def test_commit_change_needs_both_shas(db: Database) -> None:
    _run(db, "r1", "opencode", "implementation", "a1", git_commit_before="aaa", git_commit_after="bbb")
    _run(db, "r2", "claude", "review", "a2", git_commit_before="bbb", git_commit_after="bbb")
    _run(db, "r3", "claude", "review", "a3", git_commit_before="bbb")
    changed = [e["changed_commit"] for e in mission_relay(db, db.get("missions", "m1"))["timeline"]]
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
    entry = mission_relay(db, db.get("missions", "m1"))["timeline"][0]
    assert entry["kind"] == "handoff"
    assert entry["preview_truncated"] is True
    assert len(entry["preview"]) <= HANDOFF_PREVIEW_CHARS
    assert entry["stored_chars"] == len(content)
    assert SECRET not in entry["preview"]
    full = handoff_content(db, "m1", "h1")
    assert full is not None
    assert SECRET not in full["content"]
    assert full["truncated"] is False
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
    assert [e["kind"] for e in mission_relay(db, db.get("missions", "m1"))["timeline"]] == ["run", "handoff", "review"]


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
    finding = mission_relay(db, db.get("missions", "m1"))["findings"][0]
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
    relay = mission_relay(db, db.get("missions", "m1"))
    assert relay["inherited_findings_available"] is True
    [finding] = relay["findings"]
    assert finding["inherited_from_mission_id"] == "parent"
    assert finding["status"] == "repair_attempted"


def test_recent_events_across_missions(tmp_path: Path) -> None:
    database = Database(tmp_path / "api.db")
    _seed_mission(database)
    orch = Orchestrator(database, make_config(), {"fake-a": FakeAdapter("fake-a", ["ok"])})
    orch.events.publish(EventType.MISSION_CREATED, "m1", note=f"key {SECRET}")
    orch.events.publish(EventType.PROVIDER_OUTPUT, "m1", line="transient")
    orch.events.publish(EventType.MISSION_COMPLETED, "m1")
    app = create_app(tmp_path / "api.db", make_config(), orch)
    client = TestClient(app, headers={"Authorization": f"Bearer {load_or_create_token(tmp_path)}"})
    events = client.get("/api/events/recent?limit=5").json()
    assert [e["type"] for e in events] == ["MISSION_COMPLETED", "MISSION_CREATED"]
    assert events[0]["mission_title"] == "t"
    assert SECRET not in json.dumps(events)
    assert len(client.get("/api/events/recent?limit=100000").json()) == 2


def test_short_objectives_and_criteria_are_never_thin_evidence(db: Database) -> None:
    """Architecture review H1: only evidence blocks can be thin."""
    _run(db, "r1", "claude", "implementation", "2026-09-12T01:00:00")
    _manifest(
        db,
        "r1",
        [
            _block("SYSTEM_INSTRUCTIONS", 200),
            _block("TASK_OBJECTIVE", 18),
            _block("ACCEPTANCE_CRITERION", 40),
            _block("ACCEPTANCE_CRITERION", 35),
            _block("ARCHITECTURE_DECISION", 90),
            _block("PHASE_CONTEXT", 120),
            _block("OUTPUT_CONTRACT", 98),
        ],
    )
    run = mission_relay(db, db.get("missions", "m1"))["timeline"][0]
    assert run["flags"] == []
    assert run["context"]["thin_evidence_blocks"] == []


def test_optional_small_diff_is_not_flagged(db: Database) -> None:
    _run(db, "r1", "agy", "review", "a")
    _manifest(db, "r1", [_block("GIT_DIFF", 50, priority="PREFERRED")])
    assert mission_relay(db, db.get("missions", "m1"))["timeline"][0]["flags"] == []


def test_relay_bounds_items_and_flags_truncation(db: Database) -> None:
    for i in range(RELAY_ITEM_LIMIT + 2):
        db.insert(
            "handoffs",
            {"id": f"h{i:04d}", "mission_id": "m1", "role": "r", "content": "c", "created_at": f"t{i:04d}"},
        )
    relay = mission_relay(db, db.get("missions", "m1"))
    assert relay["handoffs_truncated"] is True
    assert relay["reviews_truncated"] is False
    assert sum(1 for e in relay["timeline"] if e["kind"] == "handoff") == RELAY_ITEM_LIMIT


def test_full_handoff_is_capped(db: Database) -> None:
    db.insert(
        "handoffs",
        {"id": "big", "mission_id": "m1", "role": "r", "content": "y" * (HANDOFF_MAX_CHARS + 10), "created_at": "t"},
    )
    full = handoff_content(db, "m1", "big")
    assert full is not None
    assert full["truncated"] is True
    assert len(full["content"]) <= HANDOFF_MAX_CHARS
    assert full["stored_chars"] == HANDOFF_MAX_CHARS + 10


def test_secret_straddling_preview_boundary_is_redacted(db: Database) -> None:
    content = "x" * (HANDOFF_PREVIEW_CHARS - 10) + SECRET
    db.insert("handoffs", {"id": "h", "mission_id": "m1", "role": "r", "content": content, "created_at": "t"})
    entry = mission_relay(db, db.get("missions", "m1"))["timeline"][0]
    assert SECRET[:20] not in entry["preview"]


def test_finding_file_is_redacted(db: Database) -> None:
    db.insert(
        "review_findings",
        {
            "id": "f",
            "mission_id": "m1",
            "severity": "LOW",
            "file": f"cfg/{SECRET}.py",
            "description": "d",
            "status": "open",
            "created_at": "t",
        },
    )
    assert SECRET not in mission_relay(db, db.get("missions", "m1"))["findings"][0]["file"]


def test_secret_split_by_preview_cut_is_not_leaked(db: Database) -> None:
    """Security round 2 M1: a key straddling the SQL prefix cut."""
    from orchestrator.relay import REDACTION_MARGIN_CHARS

    cut = HANDOFF_PREVIEW_CHARS + REDACTION_MARGIN_CHARS
    jwt = "eyJ" + "a" * 600 + "." + "eyJ" + "b" * 300 + "." + "c" * 300
    key = "sk-ant-" + "Q" * 40
    prefix = f"token {jwt} "
    content = prefix + "p" * (cut - len(prefix) - 12) + key + " tail"
    db.insert("handoffs", {"id": "h", "mission_id": "m1", "role": "r", "content": content, "created_at": "t"})
    preview = mission_relay(db, db.get("missions", "m1"))["timeline"][0]["preview"]
    assert "sk-ant-" not in preview


def test_secret_split_by_full_cap_is_not_leaked(db: Database) -> None:
    key = "sk-ant-" + "Q" * 40
    content = "y" * (HANDOFF_MAX_CHARS - 12) + key
    db.insert("handoffs", {"id": "big", "mission_id": "m1", "role": "r", "content": content, "created_at": "t"})
    full = handoff_content(db, "m1", "big")
    assert full is not None
    assert full["truncated"] is True
    assert "sk-ant-" not in full["content"]


def test_long_jwt_cut_by_prefix_leaves_no_fragment(db: Database) -> None:
    """Architecture round 2 L2: token longer than the redaction margin."""
    from orchestrator.relay import REDACTION_MARGIN_CHARS

    cut = HANDOFF_PREVIEW_CHARS + REDACTION_MARGIN_CHARS
    start = HANDOFF_PREVIEW_CHARS - 50
    jwt = "eyJhbGciOiJIUzI1NiJ9" + "a" * 700 + "." + "eyJzdWIi" + "b" * 50 + ".sig"
    content = "p" * (start - 1) + " " + jwt + " tail"
    assert start + len(jwt) > cut
    db.insert("handoffs", {"id": "h", "mission_id": "m1", "role": "r", "content": content, "created_at": "t"})
    preview = mission_relay(db, db.get("missions", "m1"))["timeline"][0]["preview"]
    assert "eyJ" not in preview


def test_recent_events_skip_routine_bookkeeping(tmp_path: Path) -> None:
    database = Database(tmp_path / "ev.db")
    _seed_mission(database)
    orch = Orchestrator(database, make_config(), {"fake-a": FakeAdapter("fake-a", ["ok"])})
    orch.events.publish(EventType.HUMAN_GATE_CREATED, "m1", reason="decide")
    for _ in range(5):
        orch.events.publish(EventType.LOCK_ACQUIRED, "m1")
    assert [e["type"] for e in orch.events.recent(3)] == ["HUMAN_GATE_CREATED"]
