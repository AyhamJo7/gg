"""Migration 0010: additive, legacy preserved, no fictional telemetry."""

from __future__ import annotations

import sqlite3
from pathlib import Path


def test_migration_additive_and_legacy_unknown(tmp_path: Path):
    from orchestrator.db import Database

    db_path = tmp_path / "mig.db"
    db = Database(db_path)
    # New tables exist.
    tables = {r[0] for r in sqlite3.connect(db_path).execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for expected in ("run_context_manifests", "run_usage", "invocation_leases", "orchestration_operations"):
        assert expected in tables
    # Legacy-style run without new telemetry remains readable; usage UNKNOWN.
    db.insert(
        "provider_runs",
        {
            "id": "run-old",
            "mission_id": None,
            "provider": "claude",
            "role": "implementation",
            "command": ["claude"],
            "cwd": str(tmp_path),
            "started_at": "2026-09-01T00:00:00+00:00",
            "failure_class": "NONE",
            "provider_state": "COMPLETED",
        },
    )
    row = db.get("provider_runs", "run-old")
    assert row["run_status"] == ""
    assert row["stage"] == ""
    assert db.get("run_usage", "run-old", key="run_id") is None
    assert db.get("run_context_manifests", "run-old", key="run_id") is None
    # Migration is idempotent: reopening does not duplicate or fail.
    db2 = Database(db_path)
    assert db2.get("provider_runs", "run-old") is not None
