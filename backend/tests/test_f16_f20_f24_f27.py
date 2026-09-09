"""Regression tests for audit findings F-19, F-20, F-24, and F-27."""

from __future__ import annotations

import logging
from pathlib import Path

import httpx
import pytest

from conftest import make_config, make_orchestrator
from orchestrator.api.app import create_app
from orchestrator.config import Config
from orchestrator.db import Database
from orchestrator.git_ops import checkpoint, init_repo
from orchestrator.models import MissionStatus
from orchestrator.providers.fake import FakeAdapter
from orchestrator.review import (
    finding_fingerprint,
    mark_findings_repair_attempted,
    open_blockers,
    persist_findings,
    resolve_repaired_findings,
    unverified_findings,
)


@pytest.fixture()
async def client(tmp_path: Path, workspace: Path):
    orch = make_orchestrator(tmp_path, {"fake-a": FakeAdapter("fake-a", ["ok"])})
    await orch.registry.detect_all()
    app = create_app(tmp_path / "api.db", make_config(providers=["fake-a"]), orch)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        c.orchestrator = orch  # type: ignore[attr-defined]
        yield c
    await orch.shutdown()


@pytest.mark.asyncio
async def test_f19_review_repair_lifecycle(tmp_path: Path) -> None:
    """F-19/F-LIFE-02: omission never resolves; only re-flag or explicit verification changes state."""
    db_path = tmp_path / "test.db"
    db = Database(db_path)
    proj_id = "proj-f19"
    mission_id = "test-mission-f19"

    db.insert("projects", {"id": proj_id, "name": "test-p", "path": str(tmp_path), "created_at": "2026-09-05T00:00:00Z"})
    db.insert(
        "missions",
        {
            "id": mission_id,
            "project_id": proj_id,
            "title": "t",
            "task": "k",
            "status": MissionStatus.REVIEWING.value,
            "created_at": "2026-09-05T00:00:00Z",
            "updated_at": "2026-09-05T00:00:00Z",
        },
    )

    # Seed an unverified review with one blocker
    raw_review = 'REVIEW_FINDINGS_JSON: [{"severity": "BLOCKER", "category": "bug", "description": "Null pointer", "recommended_fix": "Add check"}]'
    parsed_ok, findings = persist_findings(db, mission_id, raw_review)
    assert parsed_ok is True
    assert len(findings) == 1
    assert findings[0].id
    assert open_blockers(db, mission_id)

    # When repair phase starts, open findings transition to repair_attempted
    mark_findings_repair_attempted(db, mission_id)
    # They are no longer 'open', but preserved with status='repair_attempted'
    assert len(open_blockers(db, mission_id)) == 0
    stored = db.query("SELECT status FROM review_findings WHERE mission_id=?", (mission_id,))
    assert stored[0]["status"] == "repair_attempted"

    # A later review that OMITS the finding resolves nothing (F-LIFE-02).
    parsed_ok, _ = persist_findings(db, mission_id, "REVIEW_FINDINGS_JSON: []")
    assert parsed_ok is True
    resolve_repaired_findings(db, mission_id)  # legacy no-op
    assert db.query("SELECT status FROM review_findings WHERE mission_id=?", (mission_id,))[0]["status"] == (
        "repair_attempted"
    )
    assert len(unverified_findings(db, mission_id)) == 1

    # A re-flagged defect reopens the same row (stable identity, no duplicate).
    parsed_ok, findings = persist_findings(db, mission_id, raw_review)
    assert parsed_ok is True
    assert len(db.query("SELECT id FROM review_findings WHERE mission_id=?", (mission_id,))) == 1
    assert open_blockers(db, mission_id)

    # Explicit verification with evidence resolves it.
    mark_findings_repair_attempted(db, mission_id)
    fid = db.query("SELECT id FROM review_findings WHERE mission_id=?", (mission_id,))[0]["id"]
    verified = (
        "REVIEW_FINDINGS_JSON: []\n"
        f'VERIFIED_FIXED_JSON: [{{"finding_id": "{fid}", "evidence": "added null check, tests/edge passes"}}]'
    )
    persist_findings(db, mission_id, verified)
    row = db.query("SELECT status, verified_by FROM review_findings WHERE mission_id=?", (mission_id,))[0]
    assert row["status"] == "resolved"
    assert "null check" in (row["verified_by"] or "")
    assert finding_fingerprint("BLOCKER", "bug", None, "Null pointer")
    assert finding_fingerprint("BLOCKER", "bug", None, "Null pointer") == finding_fingerprint(
        "blocker", "BUG", None, "  null   pointer "
    )


@pytest.mark.asyncio
async def test_f20_large_file_checkpoint_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """F-20: git checkpoint warns when large files (>5MB) are staged."""
    await init_repo(tmp_path)
    large_file = tmp_path / "large_payload.bin"
    # Write 5.5 MB file
    large_file.write_bytes(b"0" * (5500 * 1024))

    with caplog.at_level(logging.WARNING):
        sha = await checkpoint(tmp_path, "add large file")
        assert sha is None
        assert any("Excluding large file from auto-checkpoint" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_f24_checkpoint_before_provider_switch_config() -> None:
    """F-24: Checkpoint before provider switch can be enabled or disabled via config."""
    cfg = Config({"orchestration": {"checkpoint_before_provider_switch": False}})
    assert cfg.get("orchestration.checkpoint_before_provider_switch") is False

    cfg_default = Config({})
    assert cfg_default.get("orchestration.checkpoint_before_provider_switch", True) is True


@pytest.mark.asyncio
async def test_f27_mission_retry_endpoint(client: httpx.AsyncClient, workspace: Path) -> None:
    """F-27: Failed/unverified missions can be retried via /api/missions/{id}/retry, creating a new mission."""
    orch = client.orchestrator  # type: ignore[attr-defined]

    # 1. Create a project
    proj_resp = await client.post("/api/projects", json={"path": str(workspace)})
    assert proj_resp.status_code == 201
    project_id = proj_resp.json()["id"]

    # 2. Create a mission and set it to terminal FAILED state
    mission_row = orch.create_mission(
        project_id=project_id,
        title="Flaky task",
        task="run something that fails",
        autonomy="semi-autonomous",
        profile="balanced",
    )
    mid = mission_row["id"]
    orch.db.update("missions", mid, {"status": MissionStatus.FAILED.value})

    # 3. Call retry endpoint
    retry_resp = await client.post(f"/api/missions/{mid}/retry")
    assert retry_resp.status_code == 200
    retried_mission = retry_resp.json()

    assert retried_mission["id"] != mid
    assert retried_mission["project_id"] == project_id
    assert retried_mission["title"] == "Retry: Flaky task"
    # New mission is active / not failed
    assert retried_mission["status"] in (
        MissionStatus.CREATED.value,
        MissionStatus.ANALYZING.value,
        MissionStatus.RECOVERING.value,
    )
