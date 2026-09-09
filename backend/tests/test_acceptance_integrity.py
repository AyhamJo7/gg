"""F-LIFE-01/F-LIFE-02 regression tests: acceptance integrity.

Mission COMPLETED must never auto-satisfy requirements; only executed
criterion checks (or authorized waivers) satisfy criteria; findings resolve
only via explicit verification; waivers are versioned and auditable.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from orchestrator.db import Database
from orchestrator.orchestrator import Orchestrator
from orchestrator.project_engine import ProductValidationError
from orchestrator.providers.fake import FakeAdapter, FindingsProvider, default_test_plan
from orchestrator.review import finding_fingerprint
from test_lifecycle import (
    drive_project,
    lifecycle_config,
    make_orch,
    standard_adapters,
    start_planned_project,
)


def _plan_with_verify(verify: str) -> dict[str, Any]:
    plan = default_test_plan()
    for req in plan["requirements"]:
        for criterion in req["acceptance"]:
            criterion["verify"] = verify
    return plan


async def _run_with_plan(
    tmp_path: Path, plan: dict[str, Any], extra_adapters: dict[str, FakeAdapter] | None = None
) -> tuple[Orchestrator, str]:
    adapters = standard_adapters(plan)
    if extra_adapters:
        adapters.update(extra_adapters)
    orch = await make_orch(tmp_path, adapters)
    pid = await start_planned_project(tmp_path, orch)
    return orch, pid


async def test_completion_marks_work_not_satisfied(tmp_path: Path):
    """Scenario 1: phase COMPLETED records WORK_COMPLETED, never SATISFIED."""
    orch = await make_orch(tmp_path, standard_adapters())
    pid = await start_planned_project(tmp_path, orch)
    await orch.coordinator.advance_project(pid)
    phase = orch.db.query(
        "SELECT * FROM project_phases WHERE project_id=? AND phase_key='foundation'", (pid,)
    )[0]
    assert phase["mission_id"]
    from test_lifecycle import drive_mission

    mission = await drive_mission(orch.db, phase["mission_id"])
    assert mission["status"] == "COMPLETED"
    await orch.coordinator.advance_project(pid)
    rows = {
        r["requirement_id"]: r["status"]
        for r in orch.db.query("SELECT * FROM requirement_evidence WHERE project_id=?", (pid,))
    }
    assert rows.get("R1") == "WORK_COMPLETED"
    assert "SATISFIED" not in rows.values()
    await orch.shutdown()


async def test_missing_target_blocks_delivery(tmp_path: Path):
    """Scenario 2: acceptance without a target repository cannot deliver."""
    orch, pid = await _run_with_plan(tmp_path, default_test_plan())
    project = await drive_project(orch, pid)
    assert project["state"] == "DELIVERED"
    import shutil

    shutil.rmtree(project["target_repo_path"])
    orch.db.update("product_projects", pid, {"state": "FINAL_ACCEPTANCE", "acceptance_state": "PENDING"})
    result = await orch.coordinator.run_acceptance(pid)
    assert result["ok"] is False
    assert orch.db.get("product_projects", pid)["state"] != "DELIVERED"
    await orch.shutdown()


async def test_failing_criterion_blocks_despite_green_suite(tmp_path: Path):
    """Scenarios 3+4: generic toolchain passes but a failing criterion blocks."""
    plan = _plan_with_verify("node -e \"process.exit(1)\"")
    orch, pid = await _run_with_plan(tmp_path, plan)
    project = await drive_project(orch, pid)
    assert project["state"] == "BLOCKED"
    assert project["state"] != "DELIVERED"
    assert project["acceptance_state"] == "UNVERIFIED"
    reason = project.get("blocking_reason") or ""
    assert "R1-A1" in reason  # criterion failure, not toolchain failure
    rows = orch.db.query("SELECT * FROM criterion_results WHERE project_id=?", (pid,))
    assert all(r["status"] == "FAILED" and r["exit_code"] == 1 for r in rows)
    await orch.shutdown()


async def test_open_medium_requirement_finding_blocks(tmp_path: Path):
    """Scenario 5: an open MEDIUM requirements finding blocks delivery."""
    finding = {
        "severity": "MEDIUM",
        "category": "requirements",
        "file": "src/app.js",
        "description": "Whitespace-only titles accepted by createIssue",
        "recommended_fix": "Trim and reject blank titles",
    }
    reviewer = FindingsProvider("fake-reviewer", [[finding], [finding]])
    adapters = standard_adapters()
    adapters["fake-reviewer"] = reviewer
    names = list(adapters.keys())
    db = Database(tmp_path / "orch.db")

    cfg = lifecycle_config(tmp_path, names)
    for role in ("implementation", "testing", "review", "repair"):
        cfg.raw["priority"][role] = ["fake-a", "fake-b", "fake-reviewer"]
    cfg.raw["priority"]["review"] = ["fake-reviewer"]
    orch = Orchestrator(db, cfg, adapters)
    await orch.registry.detect_all()
    pid = await start_planned_project(tmp_path, orch)
    project = await drive_project(orch, pid)
    # Mission completes (MEDIUM never blocks the mission loop) but delivery is refused.
    assert project["state"] == "BLOCKED", project.get("blocking_reason")
    assert "finding" in (project.get("blocking_reason") or "").lower()
    rows = orch.db.query(
        "SELECT * FROM review_findings WHERE description LIKE '%Whitespace-only%'"
    )
    assert rows and rows[0]["status"] == "open"
    await orch.shutdown()
    # Scenario 8 setup continues in the waiver test below (fresh project there).


async def test_partial_repair_keeps_unverified_open(tmp_path: Path):
    """Scenario 6: two findings, one re-flagged, one omitted -> open + unverified."""
    from orchestrator.db import Database as _Database

    db = _Database(tmp_path / "t.db")
    db.insert("projects", {"id": "p", "name": "p", "path": str(tmp_path), "created_at": "2026-01-01T00:00:00"})
    db.insert(
        "missions",
        {"id": "m", "project_id": "p", "title": "t", "task": "k", "status": "REVIEWING",
         "created_at": "2026-01-01T00:00:00", "updated_at": "2026-01-01T00:00:00"},
    )
    from orchestrator.review import mark_findings_repair_attempted, persist_findings

    first = (
        'REVIEW_FINDINGS_JSON: [{"severity": "HIGH", "category": "correctness", "description": "Broken query", '
        '"recommended_fix": "fix"}, {"severity": "MEDIUM", "category": "requirements", "description": "Blank titles", '
        '"recommended_fix": "trim"}]'
    )
    _, found = persist_findings(db, "m", first)
    assert len(found) == 2
    mark_findings_repair_attempted(db, "m")
    second = (
        'REVIEW_FINDINGS_JSON: [{"severity": "HIGH", "category": "correctness", "description": "Broken query", '
        '"recommended_fix": "fix"}]'
    )
    _, found2 = persist_findings(db, "m", second)
    rows = {r["description"]: r["status"] for r in db.query("SELECT * FROM review_findings WHERE mission_id='m'")}
    assert rows["Broken query"] == "open"  # re-flagged reopens the same row
    assert rows["Blank titles"] == "repair_attempted"  # omitted stays unverified
    assert len(db.query("SELECT id FROM review_findings WHERE mission_id='m'")) == 2  # no duplicates


async def test_verified_blocker_repair_permits_delivery(tmp_path: Path):
    """Scenario 7: BLOCKER repaired + explicitly verified -> resolved -> DELIVERED."""
    fp = finding_fingerprint("HIGH", "correctness", "src/db.js", "Broken query crashes")
    reviewer = FindingsProvider(
        "fake-reviewer",
        findings_script=[
            [{"severity": "HIGH", "category": "correctness", "file": "src/db.js",
              "description": "Broken query crashes", "recommended_fix": "guard inputs"}],
            [],
        ],
        verified_script=[[], [{"fingerprint": fp, "evidence": "added guard, edge tests pass"}]],
    )
    adapters = standard_adapters()
    adapters["fake-reviewer"] = reviewer
    names = list(adapters.keys())
    db = Database(tmp_path / "orch.db")
    cfg = lifecycle_config(tmp_path, names)
    for role in ("implementation", "testing", "review", "repair"):
        cfg.raw["priority"][role] = ["fake-a", "fake-b", "fake-reviewer"]
    cfg.raw["priority"]["review"] = ["fake-reviewer"]
    orch = Orchestrator(db, cfg, adapters)
    await orch.registry.detect_all()
    pid = await start_planned_project(tmp_path, orch)
    project = await drive_project(orch, pid)
    assert project["state"] == "DELIVERED", project.get("blocking_reason")
    rows = orch.db.query("SELECT status, verified_by FROM review_findings")
    assert rows and all(r["status"] == "resolved" for r in rows)
    assert all("guard" in (r["verified_by"] or "") for r in rows)
    await orch.shutdown()


async def test_waiver_is_versioned_and_respected(tmp_path: Path):
    """Scenario 8: authorized waiver records revision/reason and unblocks."""
    finding = {
        "severity": "MEDIUM",
        "category": "requirements",
        "file": "src/app.js",
        "description": "Whitespace-only titles accepted",
        "recommended_fix": "Trim titles",
    }
    reviewer = FindingsProvider("fake-reviewer", [[finding]])
    adapters = standard_adapters()
    adapters["fake-reviewer"] = reviewer
    names = list(adapters.keys())
    db = Database(tmp_path / "orch.db")
    cfg = lifecycle_config(tmp_path, names)
    for role in ("implementation", "testing", "review", "repair"):
        cfg.raw["priority"][role] = ["fake-a", "fake-b", "fake-reviewer"]
    cfg.raw["priority"]["review"] = ["fake-reviewer"]
    orch = Orchestrator(db, cfg, adapters)
    await orch.registry.detect_all()
    pid = await start_planned_project(tmp_path, orch)
    project = await drive_project(orch, pid)
    assert project["state"] == "BLOCKED"
    fid_rows = orch.db.query("SELECT id FROM review_findings")
    assert fid_rows, "expected blocking findings"
    fid = fid_rows[0]["id"]
    # Unknown targets and empty reasons are rejected.
    for kind, tid, reason in [("nope", fid, "x"), ("finding", "missing", "x"), ("finding", fid, "  ")]:
        with pytest.raises((ProductValidationError, ValueError)):
            orch.coordinator.create_waiver(pid, kind, tid, reason)
    assert orch.db.query("SELECT id FROM acceptance_waivers WHERE project_id=?", (pid,)) == []
    for fr in fid_rows:
        waiver = orch.coordinator.create_waiver(pid, "finding", fr["id"], "accepted for MVP; tracked in R1 follow-up")
        assert waiver["ok"] is True
    rows = orch.db.query("SELECT * FROM acceptance_waivers WHERE project_id=?", (pid,))
    assert len(rows) == len(fid_rows) == 2  # one waiver per finding row (one per phase mission)
    assert rows[0]["reason"] and rows[0]["actor"] == "human" and rows[0]["plan_revision"] == 1
    orch.db.update("product_projects", pid, {"state": "FINAL_ACCEPTANCE"})
    result = await orch.coordinator.run_acceptance(pid)
    assert result["ok"] is True, result
    assert orch.db.get("product_projects", pid)["state"] == "DELIVERED"
    await orch.shutdown()


async def test_criterion_waiver_unblocks(tmp_path: Path):
    """Waiving a failing criterion (with reason) permits delivery."""
    plan = _plan_with_verify("node -e \"process.exit(1)\"")
    orch, pid = await _run_with_plan(tmp_path, plan)
    project = await drive_project(orch, pid)
    assert project["state"] == "BLOCKED"
    orch.coordinator.create_waiver(pid, "criterion", "R1-A1", "covered manually this once")
    orch.coordinator.create_waiver(pid, "criterion", "R2-A1", "covered manually this once")
    orch.db.update("product_projects", pid, {"state": "FINAL_ACCEPTANCE"})
    result = await orch.coordinator.run_acceptance(pid)
    assert result["ok"] is True, result
    report = orch.coordinator.get_project(pid)
    assert report is not None
    delivery = report["delivery_report"]
    delivery = json.loads(delivery) if isinstance(delivery, str) else delivery
    assert len(delivery["waivers"]) == 2
    await orch.shutdown()


async def test_restart_reuses_criterion_results(tmp_path: Path):
    """Scenario 12: restart neither duplicates check runs nor skips criteria."""
    counter = tmp_path / "probe-count.txt"
    probe = "node -e 'require(\"fs\").appendFileSync(" + json.dumps(str(counter)) + ",\"x\")'"
    plan = _plan_with_verify(probe)
    orch, pid = await _run_with_plan(tmp_path, plan)
    project = await drive_project(orch, pid)
    assert project["state"] == "DELIVERED", project.get("blocking_reason")
    assert counter.read_text() == "xx"  # one run per criterion
    first_checked = {
        r["criterion_id"]: r["checked_at"]
        for r in orch.db.query("SELECT * FROM criterion_results WHERE project_id=?", (pid,))
    }
    await orch.shutdown()
    # Simulate backend restart on the same database.
    orch2 = await make_orch(tmp_path, standard_adapters(plan), db_name="orch.db")
    orch2.db.update("product_projects", pid, {"state": "FINAL_ACCEPTANCE"})
    result = await orch2.coordinator.run_acceptance(pid)
    assert result["ok"] is True, result
    assert counter.read_text() == "xx"  # no duplicate executions
    second_checked = {
        r["criterion_id"]: r["checked_at"]
        for r in orch2.db.query("SELECT * FROM criterion_results WHERE project_id=?", (pid,))
    }
    assert second_checked == first_checked  # rows reused, criteria not skipped
    # Every criterion still has a recorded verdict after restart.
    assert set(second_checked) == {"R1-A1", "R2-A1"}
    await orch2.shutdown()
