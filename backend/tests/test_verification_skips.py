"""Skipped-test accounting (dogfood 2026-09-12: 30 skipped security tests logged as PASS)."""

from __future__ import annotations

from pathlib import Path

from orchestrator.db import Database
from orchestrator.mission_summary import mission_trust
from orchestrator.verify import CommandResult, VerificationReport, _persist_verification_attempt, count_skipped

SHA = "a" * 40


def test_counts_real_runner_summaries() -> None:
    assert count_skipped("========= 324 passed, 30 skipped in 5.31s =========") == 30
    assert count_skipped(" Tests  100 passed | 4 skipped (104)") == 4
    assert count_skipped("Tests:       2 skipped, 10 passed, 12 total") == 2
    assert count_skipped("test result: ok. 12 passed; 0 failed; 3 ignored; 0 measured") == 3


def test_unreported_is_none_not_zero() -> None:
    assert count_skipped("All checks passed!") is None
    assert count_skipped("") is None
    # Prose outside a runner summary line is not counted.
    assert count_skipped("note: 5 skipped steps in the README") is None


def test_report_summary_and_persistence(tmp_path: Path) -> None:
    db = Database(tmp_path / "v.db")
    report = VerificationReport(
        results=[
            CommandResult("uv run pytest -q", 0, True, 5.3, skipped=30),
            CommandResult("uv run ruff check .", 0, True, 0.1),
        ]
    )
    assert report.all_passed
    assert report.skipped_total == 30
    assert "[PASS] uv run pytest -q (exit=0, 5.3s, 30 skipped)" in report.summary()
    db.insert("projects", {"id": "p", "name": "p", "path": "/tmp/p-skip", "created_at": "t"})
    mission = {
        "id": "m",
        "project_id": "p",
        "title": "m",
        "task": "t",
        "status": "COMPLETED",
        "created_at": "t",
        "updated_at": "t",
    }
    db.insert("missions", mission)
    _persist_verification_attempt(
        db,
        mission_id="m",
        product_project_id=None,
        task_id=None,
        sha=SHA,
        repo_key="",
        kind="toolchain",
        report=report,
        started_at="2026-09-12T01:00:00",
    )
    verification = mission_trust(db, mission)["verification"]
    assert verification == {
        "status": "passed",
        "sha": SHA,
        "skipped_tests": 30,
        "finished_at": verification["finished_at"],
    }


def test_no_verification_is_none(tmp_path: Path) -> None:
    db = Database(tmp_path / "v.db")
    db.insert("projects", {"id": "p", "name": "p", "path": "/tmp/p-skip2", "created_at": "t"})
    mission = {"id": "m", "project_id": "p", "title": "m", "task": "t", "created_at": "t", "updated_at": "t"}
    db.insert("missions", mission)
    assert mission_trust(db, mission)["verification"] is None


def test_implausible_skip_count_is_unknown() -> None:
    """Security round 7: a 20-digit count must not break persistence."""
    assert count_skipped("99999999999999999999 skipped, 1 failed") is None


def test_row_survives_an_unstorable_skip_field(tmp_path: Path) -> None:
    db = Database(tmp_path / "v.db")
    report = VerificationReport(results=[CommandResult("pytest", 1, False, 1.0, skipped=10**20)])
    attempt = _persist_verification_attempt(
        db,
        mission_id="m",
        product_project_id=None,
        task_id=None,
        sha=SHA,
        repo_key="",
        kind="toolchain",
        report=report,
        started_at="t",
    )
    assert attempt is not None
    row = db.get("verification_attempts", attempt)
    assert row is not None
    assert row["status"] == "failed"
    assert row["skipped_tests"] is None
