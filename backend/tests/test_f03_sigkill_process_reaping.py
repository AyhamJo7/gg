"""F-03 regression suite: abnormal backend termination / SIGKILL does not orphan provider process trees.

Durable process ownership: pgid is persisted at spawn. On startup, orphaned process groups
are reaped before recovery, preventing concurrent writers to the workspace.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

from conftest import make_orchestrator
from orchestrator.models import MissionStatus
from orchestrator.orchestrator import Orchestrator
from orchestrator.providers.fake import FakeAdapter

ALL_ROLES = ("planning", "implementation", "testing", "review", "repair")


def _seed_project(orch: Orchestrator, workspace: Path) -> None:
    orch.db.insert(
        "projects",
        {"id": "p1", "name": "w", "path": str(workspace), "detected_type": "node", "created_at": "2024-01-01"},
    )


def test_startup_reaps_orphaned_process_tree(tmp_path: Path, workspace: Path):
    """Spawns a real process tree in its own process group, registers it in provider_runs,
    and asserts that Orchestrator.start() terminates both the parent and child processes."""
    # Spawn a detached parent script that spawns a child, mimicking start_new_session
    parent_script = (
        "import subprocess, sys, time, os\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'])\n"
        "sys.stdout.write(f'{child.pid}\\n')\n"
        "sys.stdout.flush()\n"
        "time.sleep(300)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", parent_script],
        stdout=subprocess.PIPE,
        start_new_session=True,  # new process group
    )
    assert proc.stdout is not None
    child_pid_line = proc.stdout.readline().decode().strip()
    child_pid = int(child_pid_line)
    parent_pid = proc.pid
    parent_pgid = os.getpgid(parent_pid)

    # Verify both parent and child are running
    assert os.path.exists(f"/proc/{parent_pid}")
    assert os.path.exists(f"/proc/{child_pid}")

    # Set up orchestrator with a DB recording this active run.
    # Provider name is "python" so the cmdline ownership check matches the
    # actual Python process command line (reaping verification must identify
    # the process as ours before sending signals).
    orch = make_orchestrator(tmp_path, {"python": FakeAdapter("python", ["ok"])})
    _seed_project(orch, workspace)
    mission = orch.create_mission("p1", "m", "t", "AUTONOMOUS", "balanced")
    orch.db.update("missions", mission["id"], {"status": MissionStatus.IMPLEMENTING.value, "current_phase": "IMPLEMENTING"})
    run_id = "test-orphan-run"
    orch.db.insert(
        "provider_runs",
        {
            "id": run_id,
            "mission_id": mission["id"],
            "task_id": "t1",
            "provider": "python",
            "role": "implementation",
            "command": "['python']",
            "cwd": str(workspace),
            "started_at": "2026-01-01T00:00:00Z",
            "finished_at": None,
            "failure_class": "RUNNING",
            "provider_state": "RUNNING",
            "stdout_path": None,
            "stderr_path": None,
            "git_commit_before": None,
            "git_commit_after": None,
            "summary": "running",
            "pid": parent_pid,
            "pgid": parent_pgid,
            "started_at_ts": time.time(),
        },
    )

    async def main() -> None:
        # Start orchestrator: must reap the orphan process tree
        await orch.start()
        await asyncio.sleep(0.3)

        # Allow parent process to be reaped by parent python test process
        exit_code = proc.poll()
        assert exit_code is not None, f"Parent process {parent_pid} was not terminated!"

        # Both parent and child must be dead
        # Poll briefly for child to exit / be reaped
        child_dead = False
        for _ in range(30):
            try:
                os.kill(child_pid, 0)
                if os.path.exists(f"/proc/{child_pid}/status"):
                    with open(f"/proc/{child_pid}/status") as f:  # noqa: ASYNC230
                        if "Z (zombie)" in f.read():
                            child_dead = True
                            break
                await asyncio.sleep(0.05)
            except OSError:
                child_dead = True
                break
        assert child_dead, f"Child process {child_pid} was not reaped!"

        # The run must be marked as crashed
        run_row = orch.db.get("provider_runs", run_id)
        assert run_row["finished_at"] is not None
        assert run_row["failure_class"] == "CRASH"

        # The mission recovered and completed cleanly with a single new run
        for _ in range(60):
            st = orch.db.get("missions", mission["id"])["status"]
            if st == MissionStatus.COMPLETED.value:
                break
            await asyncio.sleep(0.1)
        assert orch.db.get("missions", mission["id"])["status"] == MissionStatus.COMPLETED.value
        await orch.shutdown()

    asyncio.run(main())
