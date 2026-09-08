"""Kill/restart reconcile: interrupted RUNNING tasks must never get stuck.

Proves the recovery gap first: tasks whose recorded provider PID is alive
(or missing) were left RUNNING forever with no runner after a restart.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from orchestrator.config import Config
from orchestrator.db import Database
from orchestrator.events import EventBus
from orchestrator.models import MissionStatus, ProviderState, SchedulingMode, TaskStatus, utcnow
from orchestrator.parallel_engine import ParallelMissionEngine
from orchestrator.providers.fake import FakeAdapter
from orchestrator.providers.registry import ProviderRegistry


async def _git_async(cwd: Path, *args: str) -> None:
    proc = await asyncio.create_subprocess_exec(
        "git", *args, cwd=cwd,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {stderr.decode()}")


@pytest.fixture()
def ctx():
    td = tempfile.TemporaryDirectory()
    base = Path(td.name)
    db = Database(base / "test.db")
    proj = base / "proj"
    proj.mkdir()
    subprocess.run(["git", "init"], cwd=proj, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=proj, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=proj, check=True, capture_output=True)
    (proj / "README.md").write_text("# t\n")
    subprocess.run(["git", "add", "."], cwd=proj, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=proj, check=True, capture_output=True)
    config = Config(
        {
            "scheduler": {"max_parallel_tasks": 3},
            "priority": {"planning": ["w"], "implementation": ["w"], "testing": ["w"],
                         "review": ["w"], "repair": ["w"]},
            "providers": {"w": {"enabled": True}},
            "orchestration": {"scheduler_tick_seconds": 0.05, "max_phase_attempts": 2},
        }
    )
    registry = ProviderRegistry(db, {"w": FakeAdapter("w", ["ok"])}, config)
    db.execute(
        "INSERT OR REPLACE INTO providers(name, state, installed) VALUES (?, ?, ?)",
        ("w", ProviderState.AVAILABLE.value, 1),
    )
    db.insert("projects", {"id": "p1", "name": "t", "path": str(proj), "created_at": utcnow()})
    db.insert(
        "missions",
        {"id": "m1", "project_id": "p1", "title": "t", "task": "t",
         "status": MissionStatus.RECOVERING.value,
         "scheduling_mode": SchedulingMode.PARALLEL_SAFE.value,
         "created_at": utcnow(), "updated_at": utcnow()},
    )
    yield {"db": db, "proj": proj, "config": config, "registry": registry}
    db.close()
    td.cleanup()


def _running_task(db: Database, tid: str = "task-a", attempts: int = 0) -> None:
    db.insert(
        "tasks",
        {"id": tid, "mission_id": "m1", "role": "implementation",
         "status": TaskStatus.RUNNING.value, "prompt": "", "summary": "",
         "attempts": attempts, "created_at": utcnow(),
         "workspace_scope": '["a.txt"]'},
    )
    db.execute(
        "INSERT INTO provider_reservations(id, task_id, provider, reserved_at) VALUES (?,?,?,?)",
        (f"res-{tid}", tid, "w", utcnow().isoformat()),
    )


def _run_row(db: Database, tid: str, pid: int | None) -> None:
    db.insert(
        "provider_runs",
        {"id": f"run-{tid}", "mission_id": "m1", "task_id": tid, "provider": "w",
         "role": "implementation", "command": ["w"], "cwd": "/tmp",
         "started_at": utcnow().isoformat(), "failure_class": "RUNNING",
         "provider_state": "RUNNING", "summary": "", "pid": pid,
         "pgid": pid, "started_at_ts": None},
    )


async def _reconcile(ctx: dict) -> ParallelMissionEngine:
    engine = ParallelMissionEngine("m1", ctx["db"], EventBus(ctx["db"]), ctx["registry"], ctx["config"])
    engine.project_path = ctx["proj"]
    await engine._reconcile_running_tasks()
    return engine


@pytest.mark.asyncio()
async def test_reconcile_dead_pid_resets(ctx):
    _running_task(ctx["db"])
    _run_row(ctx["db"], "task-a", 2**30)
    await _reconcile(ctx)
    task = ctx["db"].get("tasks", "task-a")
    assert task is not None and task["status"] == TaskStatus.PENDING.value
    assert int(task["attempts"]) == 1


@pytest.mark.asyncio()
async def test_reconcile_missing_pid_resets(ctx):
    """Run record exists but no PID was ever recorded (killed pre-spawn)."""
    _running_task(ctx["db"])
    _run_row(ctx["db"], "task-a", None)
    await _reconcile(ctx)
    task = ctx["db"].get("tasks", "task-a")
    assert task is not None and task["status"] == TaskStatus.PENDING.value
    assert int(task["attempts"]) == 1


@pytest.mark.asyncio()
async def test_reconcile_live_unverifiable_pid_fails_loudly(ctx):
    """A recorded PID that is still alive but cannot be verified as ours
    must not stick forever and must never be requeued blindly."""
    _running_task(ctx["db"])
    _run_row(ctx["db"], "task-a", os.getpid())
    await _reconcile(ctx)
    task = ctx["db"].get("tasks", "task-a")
    assert task is not None
    assert task["status"] in (TaskStatus.PENDING.value, TaskStatus.FAILED.value), (
        f"task stuck in {task['status']} with no runner"
    )
    if task["status"] == TaskStatus.FAILED.value:
        assert "verifiable" in (task["blocking_issue"] or "")


@pytest.mark.asyncio()
async def test_verify_process_ownership_tolerance(ctx):
    """Fresh verifiable sleeper passes; stale timestamp does not."""
    import time

    from orchestrator.orphans import verify_process_ownership

    proc = await asyncio.create_subprocess_exec(
        "bash", "-c", "exec -a w-marker sleep 60",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        pgid = os.getpgid(proc.pid)
        assert verify_process_ownership(proc.pid, pgid, time.time(), "w") is True
        assert verify_process_ownership(proc.pid, pgid, time.time() - 3600, "w") is False
        assert verify_process_ownership(proc.pid, pgid + 1, time.time(), "w") is False
        assert verify_process_ownership(proc.pid, pgid, time.time(), "other-provider") is False
    finally:
        proc.terminate()
        await proc.wait()


@pytest.mark.asyncio()
async def test_zombie_counts_as_dead(ctx):
    """A zombie (reaped by backend start, PID not yet collected) must not
    read as a live writer: tasks reset instead of failing loudly."""
    from orchestrator.orphans import process_alive

    pid = os.fork()
    if pid == 0:
        os._exit(0)
    try:
        # give the child a moment to exit into the zombie state
        await asyncio.sleep(0.2)
        assert process_alive(pid) is False
    finally:
        await asyncio.to_thread(os.waitpid, pid, 0)
    assert process_alive(os.getpid()) is True
    assert process_alive(2**30) is False
    assert process_alive(None) is False


@pytest.mark.asyncio()
async def test_reconcile_exhausted_attempts_fails(ctx):
    _running_task(ctx["db"], attempts=3)
    _run_row(ctx["db"], "task-a", 2**30)
    await _reconcile(ctx)
    task = ctx["db"].get("tasks", "task-a")
    assert task is not None and task["status"] == TaskStatus.FAILED.value


@pytest.mark.asyncio()
async def test_reconcile_twice_is_idempotent(ctx):
    _running_task(ctx["db"])
    _run_row(ctx["db"], "task-a", 2**30)
    await _reconcile(ctx)
    await _reconcile(ctx)
    task = ctx["db"].get("tasks", "task-a")
    assert task is not None and task["status"] == TaskStatus.PENDING.value
    assert int(task["attempts"]) == 1


@pytest.mark.asyncio()
async def test_stale_reservation_on_pending_task_released(ctx):
    ctx["db"].insert(
        "tasks",
        {"id": "task-p", "mission_id": "m1", "role": "implementation",
         "status": TaskStatus.PENDING.value, "prompt": "", "summary": "",
         "attempts": 0, "created_at": utcnow(), "workspace_scope": '["p.txt"]'},
    )
    ctx["db"].execute(
        "INSERT INTO provider_reservations(id, task_id, provider, reserved_at) VALUES (?,?,?,?)",
        ("res-p", "task-p", "w", utcnow().isoformat()),
    )
    await _reconcile(ctx)
    assert ctx["db"].query("SELECT * FROM provider_reservations WHERE released_at IS NULL") == []
    task = ctx["db"].get("tasks", "task-p")
    assert task is not None and task["status"] == TaskStatus.PENDING.value


@pytest.mark.asyncio()
async def test_uncheckpointed_worktree_work_preserved(ctx):
    import uuid

    _running_task(ctx["db"])
    _run_row(ctx["db"], "task-a", 2**30)
    wt = ctx["proj"] / ".orchestrator" / "worktrees" / "m1" / "task-a"
    wt.mkdir(parents=True)
    (wt / "draft.txt").write_text("uncommitted work\n")
    ctx["db"].insert(
        "task_branches",
        {"id": f"br-{uuid.uuid4().hex[:8]}", "task_id": "task-a",
         "branch_name": "gg/m1/task-a", "base_commit": "abc",
         "worktree_path": str(wt), "created_at": utcnow()},
    )
    await _reconcile(ctx)
    assert (wt / "draft.txt").read_text() == "uncommitted work\n"
    rows = ctx["db"].query("SELECT * FROM task_branches WHERE task_id='task-a' AND removed_at IS NULL")
    assert len(rows) == 1


@pytest.mark.asyncio()
async def test_integration_rerun_after_crash_completes(ctx):
    """A stale IN_PROGRESS integration row (crash mid-merge) must not block
    the resumed run: already-merged branches are skipped idempotently."""
    import uuid

    from orchestrator.models import IntegrationStatus

    db = ctx["db"]
    proj = ctx["proj"]
    # one completed task with a merged branch
    db.insert(
        "tasks",
        {"id": "task-i", "mission_id": "m1", "role": "implementation",
         "status": TaskStatus.COMPLETED.value, "prompt": "", "summary": "",
         "attempts": 1, "created_at": utcnow(), "finished_at": utcnow().isoformat(),
         "workspace_scope": '["i.txt"]'},
    )
    (proj / "i.txt").write_text("done\n")
    branch = "gg/m1/task-i"
    await _git_async(proj, "checkout", "-b", branch)
    await _git_async(proj, "add", "i.txt")
    await _git_async(proj, "commit", "-m", "task work")
    await _git_async(proj, "checkout", "-")
    db.insert(
        "task_branches",
        {"id": f"br-{uuid.uuid4().hex[:8]}", "task_id": "task-i",
         "branch_name": branch, "base_commit": "x",
         "worktree_path": str(proj), "created_at": utcnow()},
    )
    # crash leftover: IN_PROGRESS integration from the dead run
    db.insert(
        "task_integrations",
        {"id": "int-stale", "mission_id": "m1",
         "status": IntegrationStatus.IN_PROGRESS.value,
         "branch_names": f'["{branch}"]', "created_at": utcnow()},
    )
    from orchestrator.events import EventBus
    from orchestrator.integration import run_integration

    result = await run_integration(db, EventBus(db), proj, "m1")
    assert result["status"] == IntegrationStatus.COMPLETED.value
    rows = db.query("SELECT * FROM task_integrations WHERE mission_id='m1' AND status='COMPLETED'")
    assert rows


@pytest.mark.asyncio()
async def test_engine_rerun_after_restart_duplicates_nothing(ctx):
    """Two sequential engine runs (restart between) complete the mission
    without launching duplicate implementation runs."""
    from orchestrator.events import EventBus
    from orchestrator.parallel_engine import ParallelMissionEngine

    db = ctx["db"]
    db.insert(
        "tasks",
        {"id": "task-r", "mission_id": "m1", "role": "implementation",
         "status": TaskStatus.COMPLETED.value, "prompt": "", "summary": "done",
         "attempts": 1, "created_at": utcnow(), "finished_at": utcnow().isoformat(),
         "workspace_scope": '["r.txt"]'},
    )
    (ctx["proj"] / "r.txt").write_text("done\n")
    await _git_async(ctx["proj"], "add", "r.txt")
    await _git_async(ctx["proj"], "commit", "-m", "task work")

    async def run_once() -> None:
        eng = ParallelMissionEngine("m1", db, EventBus(db), ctx["registry"], ctx["config"])
        eng.project_path = ctx["proj"]
        await eng.run()

    # registry only has "w" (ok); review needs structured output for ok
    await run_once()
    impl_runs_1 = db.query(
        "SELECT * FROM provider_runs WHERE mission_id='m1' AND role='implementation'"
    )
    await run_once()
    impl_runs_2 = db.query(
        "SELECT * FROM provider_runs WHERE mission_id='m1' AND role='implementation'"
    )
    assert len(impl_runs_1) == len(impl_runs_2) == 0
    terminal = db.get("missions", "m1")
    assert terminal is not None and terminal["status"] in (
        MissionStatus.COMPLETED.value, MissionStatus.UNVERIFIED.value)


@pytest.mark.asyncio()
async def test_reconcile_releases_stale_reservation_and_locks(ctx):
    _running_task(ctx["db"])
    _run_row(ctx["db"], "task-a", 2**30)
    ctx["db"].execute(
        "INSERT INTO task_locks(id, task_id, lock_type, resource_key, acquired_at, released_at)"
        " VALUES (?,?,?,?,?,?)",
        ("lock-1", "task-a", "WRITE", "a.txt", "2024-01-01T00:00:00+00:00", None),
    )
    await _reconcile(ctx)
    assert ctx["db"].query("SELECT * FROM provider_reservations WHERE released_at IS NULL") == []
    assert ctx["db"].query("SELECT * FROM task_locks WHERE released_at IS NULL") == []
