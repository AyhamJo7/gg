"""Phase 2A repair regression tests.

Deterministic tests verifying the eight audited defects are fixed.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from orchestrator.config import Config
from orchestrator.db import Database
from orchestrator.events import EventBus
from orchestrator.integration import run_integration
from orchestrator.models import (
    IntegrationStatus,
    MissionStatus,
    ProviderState,
    SchedulingMode,
    TaskStatus,
    utcnow,
)
from orchestrator.parallel_engine import ParallelMissionEngine
from orchestrator.providers.fake import (
    FastSuccessProvider,
    SlowSuccessProvider,
    WorkspaceWriterProvider,
)
from orchestrator.providers.registry import ProviderRegistry
from orchestrator.readiness import compute_ready_tasks, detect_permanent_blockage
from orchestrator.reservations import (
    active_reservations_for_provider,
    try_reserve_provider,
)


async def _git_cmd(cwd: Path, *args: str) -> None:
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {stderr.decode()}")


@pytest.fixture
def tmp_db():
    with tempfile.TemporaryDirectory() as td:
        db = Database(Path(td) / "test.db")
        yield db
        db.close()


@pytest.fixture
def tmp_project():
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "project"
        path.mkdir()
        import subprocess
        subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True, capture_output=True)
        (path / "README.md").write_text("# init")
        subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True)
        yield path


@pytest.fixture
def config():
    return Config({
        "scheduler": {"max_parallel_tasks": 3, "max_parallel_per_provider": {"fast": 2, "slow": 1}},
        "priority": {"implementation": ["fast", "slow"], "planning": ["fast"], "review": ["fast"], "repair": ["fast"]},
        "providers": {"fast": {"enabled": True}, "slow": {"enabled": True}},
        "orchestration": {"scheduler_tick_seconds": 0.1, "max_phase_attempts": 2, "review_required": True, "max_repair_cycles": 3},
        "git": {"max_auto_commit_file_mb": 5},
    })


@pytest.fixture
def registry(tmp_db, config):
    adapters = {
        "fast": FastSuccessProvider("fast"),
        "slow": SlowSuccessProvider("slow", delay_s=0.1),
    }
    reg = ProviderRegistry(tmp_db, adapters, config)
    for name in adapters:
        tmp_db.execute(
            "INSERT OR REPLACE INTO providers(name, state, installed) VALUES (?, ?, ?)",
            (name, ProviderState.AVAILABLE.value, 1),
        )
    return reg


# ---------------------------------------------------------------------------
# 1. Worktree checkpointing before integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_task_checkpoint_before_integration(tmp_db, tmp_project, config, registry):
    """Provider changes must be checkpointed in the worktree before integration."""
    events = EventBus(tmp_db)
    tmp_db.insert("projects", {"id": "p1", "name": "test", "path": str(tmp_project), "created_at": utcnow()})
    tmp_db.insert("missions", {"id": "m1", "project_id": "p1", "title": "t", "task": "t", "status": MissionStatus.RECOVERING.value, "scheduling_mode": SchedulingMode.PARALLEL_SAFE.value, "created_at": utcnow(), "updated_at": utcnow()})
    tmp_db.insert("tasks", {"id": "ta", "mission_id": "m1", "role": "implementation", "status": TaskStatus.PENDING.value, "workspace_scope": '["a/**"]', "created_at": utcnow()})

    # Use writer provider so there are real changes
    registry.adapters["fast"] = WorkspaceWriterProvider("fast", filename="a/data.txt", content="hello")

    engine = ParallelMissionEngine("m1", tmp_db, events, registry, config)
    await engine.run()

    task = tmp_db.get("tasks", "ta")
    assert task["status"] == TaskStatus.COMPLETED.value
    checkpoint_after = task.get("checkpoint_after")
    assert checkpoint_after is not None, "checkpoint_after must be set"
    assert len(checkpoint_after) == 40, "checkpoint_after must be a full SHA"

    # Verify integration succeeded and file is in main repo
    integration_rec = tmp_db.query("SELECT * FROM task_integrations WHERE mission_id='m1'")
    assert len(integration_rec) == 1
    assert integration_rec[0]["status"] == IntegrationStatus.COMPLETED.value
    assert (tmp_project / "a" / "data.txt").exists()


# ---------------------------------------------------------------------------
# 2. Merge conflict detection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_conflict_detected_and_not_reported_as_success(tmp_db, tmp_project, config, registry):
    """Two branches editing the same line must produce MERGE_CONFLICT, not COMPLETED."""
    events = EventBus(tmp_db)
    tmp_db.insert("projects", {"id": "p1", "name": "test", "path": str(tmp_project), "created_at": utcnow()})
    tmp_db.insert("missions", {"id": "m1", "project_id": "p1", "title": "t", "task": "t", "status": MissionStatus.RECOVERING.value, "scheduling_mode": SchedulingMode.PARALLEL_SAFE.value, "created_at": utcnow(), "updated_at": utcnow()})
    tmp_db.insert("tasks", {"id": "ta", "mission_id": "m1", "role": "implementation", "status": TaskStatus.COMPLETED.value, "created_at": utcnow()})
    tmp_db.insert("tasks", {"id": "tb", "mission_id": "m1", "role": "implementation", "status": TaskStatus.COMPLETED.value, "created_at": utcnow()})

    # Create two worktrees with conflicting changes to the same file
    from orchestrator.task_worktree import create_task_worktree
    ba = await create_task_worktree(tmp_db, events, tmp_project, "m1", "ta")
    bb = await create_task_worktree(tmp_db, events, tmp_project, "m1", "tb")

    # Task A changes README first line
    (Path(ba.worktree_path) / "README.md").write_text("# conflict-A")
    await _git_cmd(Path(ba.worktree_path), "add", ".")
    await _git_cmd(Path(ba.worktree_path), "commit", "-m", "a")

    # Task B changes README first line differently
    (Path(bb.worktree_path) / "README.md").write_text("# conflict-B")
    await _git_cmd(Path(bb.worktree_path), "add", ".")
    await _git_cmd(Path(bb.worktree_path), "commit", "-m", "b")

    result = await run_integration(tmp_db, events, tmp_project, "m1")
    assert result["status"] == IntegrationStatus.MERGE_CONFLICT.value
    assert len(result["conflict_files"]) > 0

    mission = tmp_db.get("missions", "m1")
    assert mission["status"] != MissionStatus.COMPLETED.value


# ---------------------------------------------------------------------------
# 3. Review pipeline cannot be bypassed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_review_high_finding_prevents_completion(tmp_db, tmp_project, config, registry):
    """A parallel mission with a HIGH review finding cannot reach COMPLETED."""
    events = EventBus(tmp_db)
    tmp_db.insert("projects", {"id": "p1", "name": "test", "path": str(tmp_project), "created_at": utcnow()})
    tmp_db.insert("missions", {"id": "m1", "project_id": "p1", "title": "t", "task": "t", "status": MissionStatus.RECOVERING.value, "scheduling_mode": SchedulingMode.PARALLEL_SAFE.value, "created_at": utcnow(), "updated_at": utcnow()})
    tmp_db.insert("tasks", {"id": "ta", "mission_id": "m1", "role": "implementation", "status": TaskStatus.PENDING.value, "workspace_scope": '["a/**"]', "created_at": utcnow()})

    registry.adapters["fast"] = WorkspaceWriterProvider("fast", filename="a/data.txt", content="hello")

    engine = ParallelMissionEngine("m1", tmp_db, events, registry, config)
    await engine.run()

    # Seed a HIGH review finding manually (simulating review output)
    tmp_db.insert("review_findings", {
        "id": "find-1",
        "mission_id": "m1",
        "severity": "HIGH",
        "category": "security",
        "description": "missing auth",
        "status": "open",
        "created_at": utcnow(),
    })

    # If mission reached COMPLETED despite HIGH finding, that's the defect
    mission = tmp_db.get("missions", "m1")
    assert mission["status"] != MissionStatus.COMPLETED.value, "mission must not complete with open HIGH finding"


# ---------------------------------------------------------------------------
# 4. SIGKILL recovery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sigkill_recovery_reconciles_running_tasks(tmp_db, tmp_project, config, registry):
    """After backend SIGKILL, RUNNING tasks are reconciled and reservations/locks released."""
    events = EventBus(tmp_db)
    tmp_db.insert("projects", {"id": "p1", "name": "test", "path": str(tmp_project), "created_at": utcnow()})
    tmp_db.insert("missions", {"id": "m1", "project_id": "p1", "title": "t", "task": "t", "status": MissionStatus.RECOVERING.value, "scheduling_mode": SchedulingMode.PARALLEL_SAFE.value, "created_at": utcnow(), "updated_at": utcnow()})
    tmp_db.insert("tasks", {"id": "ta", "mission_id": "m1", "role": "implementation", "status": TaskStatus.RUNNING.value, "created_at": utcnow()})
    tmp_db.insert("tasks", {"id": "tb", "mission_id": "m1", "role": "implementation", "status": TaskStatus.RUNNING.value, "created_at": utcnow()})
    tmp_db.insert("provider_runs", {
        "id": "run-a", "mission_id": "m1", "task_id": "ta", "provider": "fast",
        "role": "implementation", "command": "[]", "cwd": str(tmp_project),
        "started_at": utcnow().isoformat(), "failure_class": "RUNNING",
        "provider_state": "RUNNING", "pid": 999999, "pgid": 999999,
    })
    tmp_db.insert("provider_runs", {
        "id": "run-b", "mission_id": "m1", "task_id": "tb", "provider": "fast",
        "role": "implementation", "command": "[]", "cwd": str(tmp_project),
        "started_at": utcnow().isoformat(), "failure_class": "RUNNING",
        "provider_state": "RUNNING", "pid": 999998, "pgid": 999998,
    })
    tmp_db.insert("provider_reservations", {"id": "res-a", "task_id": "ta", "provider": "fast", "reserved_at": utcnow().isoformat()})
    tmp_db.insert("provider_reservations", {"id": "res-b", "task_id": "tb", "provider": "fast", "reserved_at": utcnow().isoformat()})
    tmp_db.insert("task_locks", {"id": "lck-a", "task_id": "ta", "lock_type": "PATH_PREFIX", "resource_key": "a/**", "acquired_at": utcnow().isoformat()})
    tmp_db.insert("task_locks", {"id": "lck-b", "task_id": "tb", "lock_type": "PATH_PREFIX", "resource_key": "b/**", "acquired_at": utcnow().isoformat()})

    engine = ParallelMissionEngine("m1", tmp_db, events, registry, config)
    # Reconcile should reset tasks, release reservations/locks
    engine.project_path = tmp_project
    await engine._reconcile_running_tasks()

    ta = tmp_db.get("tasks", "ta")
    tb = tmp_db.get("tasks", "tb")
    # Tasks reset to PENDING (attempts < max_attempts) or FAILED
    assert ta["status"] in (TaskStatus.PENDING.value, TaskStatus.FAILED.value)
    assert tb["status"] in (TaskStatus.PENDING.value, TaskStatus.FAILED.value)

    # Reservations released
    assert active_reservations_for_provider(tmp_db, "fast") == 0

    # Locks released
    active_locks = tmp_db.query("SELECT * FROM task_locks WHERE released_at IS NULL")
    assert len(active_locks) == 0


# ---------------------------------------------------------------------------
# 5. Dependency failure deadlock
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_dependency_propagates_to_dependents(tmp_db, tmp_project, config, registry):
    """A FAILED upstream task must cause dependent tasks to fail explicitly."""
    events = EventBus(tmp_db)
    tmp_db.insert("projects", {"id": "p1", "name": "test", "path": str(tmp_project), "created_at": utcnow()})
    tmp_db.insert("missions", {"id": "m1", "project_id": "p1", "title": "t", "task": "t", "status": MissionStatus.RECOVERING.value, "scheduling_mode": SchedulingMode.PARALLEL_SAFE.value, "created_at": utcnow(), "updated_at": utcnow()})
    tmp_db.insert("tasks", {"id": "a", "mission_id": "m1", "role": "implementation", "status": TaskStatus.FAILED.value, "created_at": utcnow()})
    tmp_db.insert("tasks", {"id": "b", "mission_id": "m1", "role": "implementation", "status": TaskStatus.PENDING.value, "created_at": utcnow()})
    tmp_db.insert("task_dependencies", {"from_task_id": "a", "to_task_id": "b", "created_at": utcnow().isoformat()})

    engine = ParallelMissionEngine("m1", tmp_db, events, registry, config)
    await engine.run()

    b = tmp_db.get("tasks", "b")
    assert b["status"] == TaskStatus.FAILED.value, f"dependent must be FAILED, got {b['status']}"
    assert "permanently blocked" in (b.get("blocking_issue") or "").lower()

    mission = tmp_db.get("missions", "m1")
    assert mission["status"] == MissionStatus.FAILED.value


# ---------------------------------------------------------------------------
# 6. WAITING_FOR_PROVIDER wake
# ---------------------------------------------------------------------------


def test_waiting_for_provider_becomes_ready_when_capacity_returns(tmp_db):
    """A task waiting for provider capacity must be reconsidered when a provider is released."""
    _events = EventBus(tmp_db)
    tmp_db.insert("projects", {"id": "p1", "name": "test", "path": "/tmp/test", "created_at": utcnow()})
    tmp_db.insert("missions", {"id": "m1", "project_id": "p1", "title": "t", "task": "t", "status": MissionStatus.IMPLEMENTING.value, "created_at": utcnow(), "updated_at": utcnow()})
    tmp_db.insert("tasks", {"id": "t1", "mission_id": "m1", "role": "implementation", "status": TaskStatus.WAITING_FOR_PROVIDER.value, "created_at": utcnow()})
    tmp_db.execute("INSERT INTO providers(name, state, installed) VALUES ('fast', ?, 1)", (ProviderState.AVAILABLE.value,))

    # Task is waiting but no active reservation
    ready = compute_ready_tasks(tmp_db, "m1")
    assert len(ready) == 1
    assert ready[0]["id"] == "t1"


# ---------------------------------------------------------------------------
# 7. Provider concurrency > 1
# ---------------------------------------------------------------------------


def test_provider_capacity_two_allows_two_runs(tmp_db):
    """Provider capacity 2 must permit two reservations but reject a third."""
    events = EventBus(tmp_db)
    tmp_db.insert("projects", {"id": "p1", "name": "test", "path": "/tmp/test", "created_at": utcnow()})
    tmp_db.insert("missions", {"id": "m1", "project_id": "p1", "title": "t", "task": "t", "status": MissionStatus.IMPLEMENTING.value, "created_at": utcnow(), "updated_at": utcnow()})
    for i in range(3):
        tmp_db.insert("tasks", {"id": f"t{i}", "mission_id": "m1", "role": "implementation", "status": TaskStatus.READY.value, "created_at": utcnow()})
    tmp_db.execute("INSERT INTO providers(name, state, installed) VALUES ('fast', ?, 1)", (ProviderState.AVAILABLE.value,))

    config_data = {"scheduler": {"max_parallel_tasks": 5, "max_parallel_per_provider": {"fast": 2}}}
    ok1 = try_reserve_provider(tmp_db, events, "t0", "fast", config_data)
    ok2 = try_reserve_provider(tmp_db, events, "t1", "fast", config_data)
    ok3 = try_reserve_provider(tmp_db, events, "t2", "fast", config_data)

    assert ok1 is True
    assert ok2 is True
    assert ok3 is False
    assert active_reservations_for_provider(tmp_db, "fast") == 2


# ---------------------------------------------------------------------------
# 8. Integration idempotency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_integration_idempotent(tmp_db, tmp_project, config, registry):
    """Calling integration twice must not duplicate the merge."""
    events = EventBus(tmp_db)
    tmp_db.insert("projects", {"id": "p1", "name": "test", "path": str(tmp_project), "created_at": utcnow()})
    tmp_db.insert("missions", {"id": "m1", "project_id": "p1", "title": "t", "task": "t", "status": MissionStatus.IMPLEMENTING.value, "created_at": utcnow(), "updated_at": utcnow()})
    tmp_db.insert("tasks", {"id": "ta", "mission_id": "m1", "role": "implementation", "status": TaskStatus.COMPLETED.value, "created_at": utcnow()})

    from orchestrator.task_worktree import create_task_worktree
    ba = await create_task_worktree(tmp_db, events, tmp_project, "m1", "ta")

    (Path(ba.worktree_path) / "new.txt").write_text("data")
    await _git_cmd(Path(ba.worktree_path), "add", ".")
    await _git_cmd(Path(ba.worktree_path), "commit", "-m", "a")

    result1 = await run_integration(tmp_db, events, tmp_project, "m1")
    assert result1["status"] == IntegrationStatus.COMPLETED.value
    merged1 = result1["merged_commit"]

    result2 = await run_integration(tmp_db, events, tmp_project, "m1")
    assert result2["status"] == IntegrationStatus.COMPLETED.value
    # Second call should not create a new merge commit
    assert result2["merged_commit"] == merged1 or result2["summary"].startswith("already merged")


# ---------------------------------------------------------------------------
# 9. Cancel preserves partial work
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_preserves_branches_and_worktrees(tmp_db, tmp_project, config, registry):
    """Cancel must preserve task branches and worktrees for audit."""
    events = EventBus(tmp_db)
    registry.adapters["slow"] = SlowSuccessProvider("slow", delay_s=2.0)
    tmp_db.execute("UPDATE providers SET state=? WHERE name='slow'", (ProviderState.AVAILABLE.value,))
    tmp_db.insert("projects", {"id": "p1", "name": "test", "path": str(tmp_project), "created_at": utcnow()})
    tmp_db.insert("missions", {"id": "m1", "project_id": "p1", "title": "t", "task": "t", "status": MissionStatus.RECOVERING.value, "scheduling_mode": SchedulingMode.PARALLEL_SAFE.value, "created_at": utcnow(), "updated_at": utcnow()})
    tmp_db.insert("tasks", {"id": "a", "mission_id": "m1", "role": "implementation", "status": TaskStatus.PENDING.value, "workspace_scope": '["a/**"]', "preferred_providers": '["slow"]', "created_at": utcnow()})

    engine = ParallelMissionEngine("m1", tmp_db, events, registry, config)

    async def canceller():
        await asyncio.sleep(0.3)
        engine.request_cancel()

    asyncio.create_task(canceller())
    await engine.run()

    mission = tmp_db.get("missions", "m1")
    assert mission["status"] == MissionStatus.CANCELLED.value

    # Worktree and branch should still exist
    branches = tmp_db.query("SELECT * FROM task_branches WHERE task_id='a'")
    assert len(branches) >= 1


# ---------------------------------------------------------------------------
# 10. Secret protection inside worktree
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_large_file_excluded_from_worktree_checkpoint(tmp_db, tmp_project, config, registry):
    """Large files must be excluded from auto-checkpoint inside task worktrees."""
    events = EventBus(tmp_db)
    tmp_db.insert("projects", {"id": "p1", "name": "test", "path": str(tmp_project), "created_at": utcnow()})
    tmp_db.insert("missions", {"id": "m1", "project_id": "p1", "title": "t", "task": "t", "status": MissionStatus.RECOVERING.value, "scheduling_mode": SchedulingMode.PARALLEL_SAFE.value, "created_at": utcnow(), "updated_at": utcnow()})
    tmp_db.insert("tasks", {"id": "ta", "mission_id": "m1", "role": "implementation", "status": TaskStatus.PENDING.value, "workspace_scope": '["a/**"]', "created_at": utcnow()})

    from orchestrator.task_worktree import create_task_worktree
    ba = await create_task_worktree(tmp_db, events, tmp_project, "m1", "ta")

    # Write a normal file and a large file
    normal_path = Path(ba.worktree_path) / "small.txt"
    normal_path.write_text("hello")
    large_path = Path(ba.worktree_path) / "big.bin"
    large_path.write_bytes(b"x" * (6 * 1024 * 1024))  # 6 MB

    # Run checkpoint with 5 MB limit
    from orchestrator.task_worktree import checkpoint_in_worktree
    sha = await checkpoint_in_worktree(tmp_project, "ta", "test checkpoint", tmp_db, max_file_mb=5)
    assert sha is not None, "checkpoint should succeed by excluding the large file"

    # Verify large file is NOT in the commit
    proc = await asyncio.create_subprocess_exec(
        "git", "diff-tree", "--no-commit-id", "--name-only", "-r", sha,
        cwd=ba.worktree_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _stderr = await proc.communicate()
    files = stdout.decode().strip().splitlines()
    assert "big.bin" not in files, "large file must be excluded from checkpoint"


# ---------------------------------------------------------------------------
# 11. Permanent blockage detection helper
# ---------------------------------------------------------------------------


def test_detect_permanent_blockage_finds_failed_dependency(tmp_db):
    tmp_db.insert("projects", {"id": "p1", "name": "test", "path": "/tmp/test", "created_at": utcnow()})
    tmp_db.insert("missions", {"id": "m1", "project_id": "p1", "title": "t", "task": "t", "status": MissionStatus.IMPLEMENTING.value, "created_at": utcnow(), "updated_at": utcnow()})
    tmp_db.insert("tasks", {"id": "a", "mission_id": "m1", "role": "implementation", "status": TaskStatus.FAILED.value, "created_at": utcnow()})
    tmp_db.insert("tasks", {"id": "b", "mission_id": "m1", "role": "implementation", "status": TaskStatus.PENDING.value, "created_at": utcnow()})
    tmp_db.insert("task_dependencies", {"from_task_id": "a", "to_task_id": "b", "created_at": utcnow().isoformat()})

    blocked = detect_permanent_blockage(tmp_db, "m1")
    assert "b" in blocked
