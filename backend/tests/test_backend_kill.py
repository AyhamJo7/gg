"""Subprocess kill/restart: real backend SIGKILL during parallel execution.

Spawns an isolated uvicorn backend (fake parking providers, isolated DB),
drives a 2-task mission over HTTP, SIGKILLs only the verified backend PID,
restarts against the same DB, and proves recovery without duplicates.
Linux-only (/proc supervision).
"""

from __future__ import annotations

import json
import logging
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

logger = logging.getLogger(__name__)

BACKEND_ROOT = Path(__file__).resolve().parents[1]
HELPER = Path(__file__).resolve().parent / "helpers" / "isolated_server.py"

pytestmark = pytest.mark.skipif(os.name != "posix", reason="requires /proc + SIGKILL")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _spawn(db: Path, port: int, log: Path, home: Path) -> subprocess.Popen[bytes]:
    env = dict(os.environ, PYTHONPATH=str(BACKEND_ROOT / "src"))
    fh = open(log, "ab")
    venv_python = BACKEND_ROOT / ".venv" / "bin" / "python"
    binary = str(venv_python) if venv_python.exists() else sys.executable
    return subprocess.Popen(
        [binary, str(HELPER), str(db), str(port), str(home)],
        env=env,
        stdout=fh,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )


def _wait_health(port: int, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"http://127.0.0.1:{port}/api/health", timeout=2.0)
            if r.status_code == 200:
                return
        except Exception:
            time.sleep(0.3)
    raise TimeoutError("backend never became healthy")


def _q(db: Path, sql: str, params: tuple = ()) -> list[dict]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()


def _alive(pid: int | None) -> bool:
    return bool(pid) and Path(f"/proc/{pid}").exists()


def _auth_headers(db: Path) -> dict[str, str]:
    token = (db.parent / "auth_token").read_text().strip()
    return {"Authorization": f"Bearer {token}"}


def test_backend_sigkill_mid_run_recovers_without_duplicates(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "package.json").write_text(json.dumps({"name": "t", "scripts": {"test": 'node -e "process.exit(0)"'}}))
    subprocess.run(["git", "init"], cwd=ws, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=ws, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=ws, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=ws, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=ws, check=True, capture_output=True)

    db = tmp_path / "kill.db"
    port = _free_port()
    evidence: dict = {}
    server = _spawn(db, port, tmp_path / "server1.log", tmp_path / "srvhome1")
    try:
        _wait_health(port)
        base = f"http://127.0.0.1:{port}"
        auth = _auth_headers(db)
        project = httpx.post(f"{base}/api/projects", json={"path": str(ws)}, headers=auth, timeout=10).json()
        mission = httpx.post(
            f"{base}/api/missions",
            json={
                "project_id": project["id"],
                "title": "kill me",
                "task": "park twice",
                "autonomy": "AUTONOMOUS",
                "scheduling_mode": "PARALLEL_SAFE",
                "start": False,
            },
            headers=auth,
            timeout=10,
        ).json()
        mid = mission["id"]
        dag = {
            "tasks": [
                {
                    "id": "k-a",
                    "role": "implementation",
                    "title": "park a",
                    "description": "park",
                    "preferred_providers": '["slow-a"]',
                    "workspace_scope": '["a.txt"]',
                    "priority": 0,
                },
                {
                    "id": "k-b",
                    "role": "implementation",
                    "title": "park b",
                    "description": "park",
                    "preferred_providers": '["slow-b"]',
                    "workspace_scope": '["b.txt"]',
                    "priority": 0,
                },
            ],
            "dependencies": [],
        }
        assert httpx.post(f"{base}/api/missions/{mid}/dag", json=dag, headers=auth, timeout=10).status_code == 200
        assert httpx.post(f"{base}/api/missions/{mid}/start", headers=auth, timeout=10).status_code in (200, 201)

        # wait for two genuinely RUNNING tasks with live provider PIDs
        deadline = time.monotonic() + 60
        live_runs: list[dict] = []
        while time.monotonic() < deadline:
            tasks = _q(db, "SELECT id, status FROM tasks WHERE mission_id=?", (mid,))
            if sum(1 for t in tasks if t["status"] == "RUNNING") >= 2:
                live_runs = _q(
                    db,
                    "SELECT id, task_id, provider, pid, pgid FROM provider_runs"
                    " WHERE mission_id=? AND finished_at IS NULL",
                    (mid,),
                )
                if len(live_runs) >= 2 and all(_alive(r["pid"]) for r in live_runs):
                    break
            time.sleep(0.5)
        assert len(live_runs) >= 2, "never reached 2 live RUNNING tasks"
        evidence = {
            "backend_pid": server.pid,
            "backend_pgid": os.getpgid(server.pid),
            "runs": [(r["task_id"], r["provider"], r["pid"], r["pgid"]) for r in live_runs],
        }
        (tmp_path / "evidence.json").write_text(json.dumps(evidence, indent=1))

        # precise SIGKILL of only the verified backend process
        os.kill(server.pid, signal.SIGKILL)
        _, status = os.waitpid(server.pid, 0)
        assert os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL

        # kill was isolated: provider sleepers must still be alive
        for _, _, pid, _ in evidence["runs"]:
            assert _alive(pid), f"provider pid {pid} died with backend — kill not isolated"

        # restart same instance against the same DB
        server2 = _spawn(db, port, tmp_path / "server2.log", tmp_path / "srvhome2")
        try:
            _wait_health(port)
            base2 = base
            # orphans must be reaped promptly
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if not any(_alive(pid) for _, _, pid, _ in evidence["runs"]):
                    break
                time.sleep(0.5)
            for _, _, pid, _ in evidence["runs"]:
                assert not _alive(pid), f"orphan {pid} survived restart"

            # tasks reset with recorded attempts, then rescheduled — never duplicated
            deadline = time.monotonic() + 60
            relaunched = False
            while time.monotonic() < deadline:
                tasks = {
                    t["id"]: t for t in _q(db, "SELECT id, status, attempts FROM tasks WHERE mission_id=?", (mid,))
                }
                attempts = [int(tasks[f"{mid[:8]}-k-{s}"]["attempts"]) for s in ("a", "b")]
                if all(a >= 1 for a in attempts):
                    relaunched = True
                    break
                time.sleep(0.5)
            assert relaunched, f"tasks never rescheduled: {tasks}"
            unfinished = _q(
                db,
                "SELECT task_id, COUNT(*) n FROM provider_runs WHERE mission_id=? AND finished_at IS NULL"
                " GROUP BY task_id",
                (mid,),
            )
            assert all(r["n"] <= 1 for r in unfinished), f"duplicate active runs: {unfinished}"

            m = httpx.get(f"{base2}/api/missions/{mid}", timeout=10).json()
            assert m["status"] not in ("COMPLETED", "FAILED"), m["status"]
            httpx.post(f"{base2}/api/missions/{mid}/cancel", headers=auth, timeout=10)
        finally:
            server2.terminate()
            try:
                server2.wait(timeout=15)
            except subprocess.TimeoutExpired:
                server2.kill()
    finally:
        # hygiene: only PIDs recorded in THIS test's DB may be signaled
        try:
            for r in _q(db, "SELECT pid FROM provider_runs WHERE pid IS NOT NULL"):
                if _alive(r["pid"]):
                    try:
                        os.kill(r["pid"], signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
        except Exception as exc:
            logger.warning("test cleanup query failed: %s", exc)
        if server.poll() is None:
            server.kill()
