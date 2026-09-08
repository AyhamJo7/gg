"""Leak-audit script adversarial tests: dirty trees must fail, ignored
runtime metadata must not, missing evidence must fail."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "leak-audit.py"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _repo(base: Path) -> Path:
    ws = base / "ws"
    ws.mkdir()
    (ws / ".gitignore").write_text(".orchestrator/\nnode_modules/\n")
    (ws / "app.py").write_text("print('hi')\n")
    _git(ws, "init")
    _git(ws, "config", "user.email", "t@t.t")
    _git(ws, "config", "user.name", "t")
    _git(ws, "add", ".")
    _git(ws, "commit", "-m", "init")
    return ws


def _db(base: Path) -> Path:
    from orchestrator.db import Database

    p = base / "audit.db"
    Database(p).close()
    return p


def _run(db: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(db), *args],
        capture_output=True, text=True, check=False,
    )


def test_clean_repo_passes(tmp_path: Path):
    ws = _repo(tmp_path)
    db = _db(tmp_path)
    r = _run(db, "--repo", str(ws))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "clean" in r.stdout


def test_modified_tracked_file_fails(tmp_path: Path):
    ws = _repo(tmp_path)
    db = _db(tmp_path)
    (ws / "app.py").write_text("print('changed')\n")
    r = _run(db, "--repo", str(ws))
    assert r.returncode == 1
    assert "uncommitted change" in r.stdout and "app.py" in r.stdout


def test_untracked_product_file_fails(tmp_path: Path):
    ws = _repo(tmp_path)
    db = _db(tmp_path)
    (ws / "notes.txt").write_text("todo\n")
    r = _run(db, "--repo", str(ws))
    assert r.returncode == 1
    assert "uncommitted new file" in r.stdout


def test_ignored_runtime_metadata_passes(tmp_path: Path):
    ws = _repo(tmp_path)
    db = _db(tmp_path)
    logs = ws / ".orchestrator" / "logs"
    logs.mkdir(parents=True)
    (logs / "run-1.stdout.log").write_text("x" * 100)
    (ws / "node_modules" / "dep").mkdir(parents=True)
    (ws / "node_modules" / "dep" / "index.js").write_text("y")
    r = _run(db, "--repo", str(ws))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "repo_ignored=2" in r.stdout


def test_completed_mission_missing_evidence_fails(tmp_path: Path):
    _repo(tmp_path)
    db = _db(tmp_path)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO missions(id, project_id, title, task, status, created_at, updated_at)"
        " VALUES ('m1','p1','t','t','COMPLETED','2024-01-01','2024-01-01')"
    )
    conn.commit()
    conn.close()
    r = _run(db, "--mission", "m1")
    assert r.returncode == 1
    assert "missing integration evidence" in r.stdout


def test_completed_mission_with_evidence_passes(tmp_path: Path):
    _repo(tmp_path)
    db = _db(tmp_path)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO missions(id, project_id, title, task, status, created_at, updated_at)"
        " VALUES ('m1','p1','t','t','COMPLETED','2024-01-01','2024-01-01')"
    )
    conn.execute(
        "INSERT INTO task_integrations(id, mission_id, status, created_at)"
        " VALUES ('i1','m1','COMPLETED','2024-01-01')"
    )
    conn.execute(
        "INSERT INTO reviews(id, mission_id, implementation_provider, review_provider, created_at)"
        " VALUES ('r1','m1','a','b','2024-01-01')"
    )
    conn.execute(
        "INSERT INTO checkpoints(id, mission_id, project_id, commit_sha, message, created_at)"
        " VALUES ('c1','m1','p1','abc','m','2024-01-01')"
    )
    conn.commit()
    conn.close()
    r = _run(db, "--mission", "m1")
    assert r.returncode == 0, r.stdout + r.stderr


def test_unreleased_reservation_fails(tmp_path: Path):
    _repo(tmp_path)
    db = _db(tmp_path)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO provider_reservations(task_id, provider, reserved_at)"
        " VALUES ('t1','agy','2024-01-01')"
    )
    conn.commit()
    conn.close()
    r = _run(db)
    assert r.returncode == 1
    assert "unreleased_reservations=1" in r.stdout


def test_output_never_contains_log_contents(tmp_path: Path):
    ws = _repo(tmp_path)
    db = _db(tmp_path)
    logs = ws / ".orchestrator" / "logs"
    logs.mkdir(parents=True)
    (logs / "run-1.stdout.log").write_text("sk-ant-FAKEFAKEFAKE1234567890abcdef\n")
    r = _run(db, "--repo", str(ws))
    assert "FAKEFAKEFAKE" not in r.stdout
    assert json.dumps(r.stdout)  # valid JSON-serializable text
