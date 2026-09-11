"""Migration 0014: additive dependency artifacts, legacy rows stay UNKNOWN."""

from __future__ import annotations

import sqlite3
from pathlib import Path


def test_migration_0014_additive_and_legacy_unknown(tmp_path: Path):
    from orchestrator.db import Database

    db_path = tmp_path / "mig.db"
    db = Database(db_path)
    tables = {r[0] for r in sqlite3.connect(db_path).execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "task_dependency_inputs" in tables
    task_cols = {r[1] for r in sqlite3.connect(db_path).execute("PRAGMA table_info(tasks)")}
    assert {"input_sha", "result_sha"} <= task_cols
    mission_cols = {r[1] for r in sqlite3.connect(db_path).execute("PRAGMA table_info(missions)")}
    assert "dag_base_sha" in mission_cols
    # Legacy-style task without artifacts remains readable; SHAs UNKNOWN.
    db.insert(
        "projects",
        {"id": "p-old", "name": "p", "path": str(tmp_path), "detected_type": "node",
         "created_at": "2026-09-01T00:00:00+00:00"},
    )
    db.insert(
        "missions",
        {"id": "m-old", "project_id": "p-old", "title": "t", "task": "t", "status": "COMPLETED",
         "created_at": "2026-09-01T00:00:00+00:00", "updated_at": "2026-09-01T00:00:00+00:00"},
    )
    db.insert(
        "tasks",
        {"id": "t-old", "mission_id": "m-old", "role": "implementation",
         "status": "COMPLETED", "prompt": "", "summary": "done", "attempts": 1,
         "created_at": "2026-09-01T00:00:00+00:00"},
    )
    row = db.get("tasks", "t-old")
    assert row is not None
    assert row.get("input_sha") is None
    assert row.get("result_sha") is None
    assert db.get("missions", "m-old") is None or db.get("missions", "m-old").get("dag_base_sha") is None
    # Migration is idempotent: reopening does not duplicate or fail.
    db2 = Database(db_path)
    assert db2.get("tasks", "t-old") is not None
    db.close()
    db2.close()
