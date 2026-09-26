"""Skipped-test accounting (dogfood 2026-09-12: 30 skipped security tests logged as PASS)."""

from __future__ import annotations

from pathlib import Path

from orchestrator.db import Database
from orchestrator.mission_summary import mission_trust
from orchestrator.verify import CommandResult, VerificationReport, _persist_verification_attempt, count_skipped

SHA = "a" * 40


def test_counts_real_runner_summaries() -> None:
    assert count_skipped("324 passed, 30 skipped in 5.31s") == 30
    assert count_skipped("========= 324 passed, 30 skipped in 5.31s =========") == 30
    assert count_skipped("===== 1 failed, 3 passed, 2 skipped, 1 warning in 0.40s =====") == 2
    assert count_skipped("test result: ok. 12 passed; 0 failed; 3 ignored; 0 measured") == 3


def test_vitest_and_jest_count_tests_not_files_or_suites() -> None:
    """Architecture round 7 M1: real output, file/suite lines excluded."""
    vitest = " Test Files  25 passed | 1 skipped (26)\n      Tests  137 passed | 2 skipped (139)"
    assert count_skipped(vitest) == 2
    jest = "Test Suites: 1 skipped, 3 passed, 4 of 4 total\nTests:       5 skipped, 10 passed, 15 total"
    assert count_skipped(jest) == 5
    colored = "\x1b[2m      Tests \x1b[22m \x1b[1m\x1b[32m9 passed\x1b[39m\x1b[22m | \x1b[33m1 skipped\x1b[39m"
    assert count_skipped(colored) == 1


def test_unreported_is_none_not_zero() -> None:
    assert count_skipped("All checks passed!") is None
    assert count_skipped("") is None
    assert count_skipped("INFO sync: 12 skipped, 40 passed validation") is None
    # Mocha is not claimed: its pending count is not read.
    assert count_skipped("  10 passing (20ms)\n  2 pending") is None


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


def test_summary_parsing_is_fast_on_long_lines() -> None:
    import time

    hostile = "1 passed, " * 150 + "x"
    started = time.perf_counter()
    count_skipped("\n".join([hostile] * 10))
    assert time.perf_counter() - started < 0.5


def test_fallback_insert_works_without_the_skip_column(tmp_path: Path) -> None:
    """Round 8 L1: the fallback must not name the column it is recovering from."""
    db = Database(tmp_path / "v.db")
    db.execute("ALTER TABLE verification_attempts DROP COLUMN skipped_tests")
    report = VerificationReport(results=[CommandResult("pytest", 0, True, 1.0, skipped=3)])
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
    assert db.get("verification_attempts", attempt)["status"] == "passed"
