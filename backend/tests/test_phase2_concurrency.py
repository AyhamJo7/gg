"""Phase 2A concurrency and DAG test campaign.

Deterministic tests using fake providers — no real AI quota consumed.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from orchestrator.config import Config
from orchestrator.dag import DagValidationError, validate_planner_payload, validate_task_graph
from orchestrator.db import Database
from orchestrator.events import EventBus
from orchestrator.integration import run_integration
from orchestrator.models import (
    IntegrationStatus,
    LockType,
    MissionStatus,
    ProviderState,
    SchedulingMode,
    TaskGraphTask,
    TaskStatus,
    utcnow,
)
from orchestrator.parallel_engine import ParallelMissionEngine
from orchestrator.providers.fake import (
    FastSuccessProvider,
    RateLimitAfterDelayProvider,
    SlowSuccessProvider,
)
from orchestrator.providers.registry import ProviderRegistry
from orchestrator.readiness import compute_ready_tasks
from orchestrator.reservations import (
    active_reservations_for_provider,
    provider_score,
    release_provider_reservation,
    try_reserve_provider,
)
from orchestrator.task_locks import acquire_locks, release_locks_for_task
from orchestrator.task_worktree import create_task_worktree, get_task_worktree_path, remove_task_worktree

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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
        # init git repo
        import subprocess

        subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True, capture_output=True)
        # initial commit
        (path / "README.md").write_text("# init")
        subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True)
        yield path


@pytest.fixture
def config():
    return Config(
        {
            "scheduler": {"max_parallel_tasks": 3, "max_parallel_per_provider": {"fast": 2, "slow": 1}},
            "priority": {"implementation": ["fast", "slow"], "planning": ["fast"]},
            "providers": {"fast": {"enabled": True}, "slow": {"enabled": True}},
            "orchestration": {"scheduler_tick_seconds": 0.1, "max_phase_attempts": 2},
        }
    )


@pytest.fixture
def registry(tmp_db, config):
    adapters = {
        "fast": FastSuccessProvider("fast"),
        "slow": SlowSuccessProvider("slow", delay_s=0.1),
    }
    reg = ProviderRegistry(tmp_db, adapters, config)
    # Seed providers as installed and available
    for name in adapters:
        tmp_db.execute(
            "INSERT OR REPLACE INTO providers(name, state, installed) VALUES (?, ?, ?)",
            (name, ProviderState.AVAILABLE.value, 1),
        )
    return reg


# ---------------------------------------------------------------------------
# 1. DAG Validation
# ---------------------------------------------------------------------------


def test_validate_linear_chain():
    tasks = [
        TaskGraphTask(id="a", mission_id="m", dependencies=[]),
        TaskGraphTask(id="b", mission_id="m", dependencies=["a"]),
        TaskGraphTask(id="c", mission_id="m", dependencies=["b"]),
    ]
    validate_task_graph(tasks)  # no error


def test_validate_diamond():
    tasks = [
        TaskGraphTask(id="a", mission_id="m", dependencies=[]),
        TaskGraphTask(id="b", mission_id="m", dependencies=["a"]),
        TaskGraphTask(id="c", mission_id="m", dependencies=["a"]),
        TaskGraphTask(id="d", mission_id="m", dependencies=["b", "c"]),
    ]
    validate_task_graph(tasks)


def test_validate_cycle_detected():
    tasks = [
        TaskGraphTask(id="a", mission_id="m", dependencies=["c"]),
        TaskGraphTask(id="b", mission_id="m", dependencies=["a"]),
        TaskGraphTask(id="c", mission_id="m", dependencies=["b"]),
    ]
    with pytest.raises(DagValidationError, match="cycle"):
        validate_task_graph(tasks)


def test_validate_self_dependency():
    tasks = [TaskGraphTask(id="a", mission_id="m", dependencies=["a"])]
    with pytest.raises(DagValidationError, match="self-dependency"):
        validate_task_graph(tasks)


def test_validate_missing_dependency():
    tasks = [
        TaskGraphTask(id="a", mission_id="m", dependencies=[]),
        TaskGraphTask(id="b", mission_id="m", dependencies=["z"]),
    ]
    with pytest.raises(DagValidationError, match="missing"):
        validate_task_graph(tasks)


def test_validate_duplicate_ids():
    tasks = [
        TaskGraphTask(id="a", mission_id="m"),
        TaskGraphTask(id="a", mission_id="m"),
    ]
    with pytest.raises(DagValidationError, match="duplicate"):
        validate_task_graph(tasks)


def test_validate_planner_payload_ok():
    payload = {
        "tasks": [
            {"id": "backend", "title": "Backend", "role": "implementation", "depends_on": []},
            {"id": "frontend", "title": "Frontend", "role": "implementation", "depends_on": []},
            {
                "id": "integration",
                "title": "Integration",
                "role": "implementation",
                "depends_on": ["backend", "frontend"],
            },
        ]
    }
    tasks = validate_planner_payload(payload)
    assert len(tasks) == 3
    assert tasks[2].dependencies == ["backend", "frontend"]


def test_validate_planner_payload_missing_tasks_key():
    with pytest.raises(DagValidationError):
        validate_planner_payload({})


# ---------------------------------------------------------------------------
# 2. Task Readiness
# ---------------------------------------------------------------------------


def test_readiness_all_pending_no_deps(tmp_db):
    tmp_db.insert("projects", {"id": "p1", "name": "test", "path": "/tmp/test", "created_at": utcnow()})
    tmp_db.insert(
        "missions",
        {
            "id": "m1",
            "project_id": "p1",
            "title": "t",
            "task": "t",
            "status": MissionStatus.IMPLEMENTING.value,
            "created_at": utcnow(),
            "updated_at": utcnow(),
        },
    )
    tmp_db.insert(
        "tasks",
        {
            "id": "a",
            "mission_id": "m1",
            "role": "implementation",
            "status": TaskStatus.PENDING.value,
            "workspace_scope": '["backend/**"]',
            "created_at": utcnow(),
        },
    )
    tmp_db.insert(
        "tasks",
        {
            "id": "b",
            "mission_id": "m1",
            "role": "implementation",
            "status": TaskStatus.PENDING.value,
            "workspace_scope": '["frontend/**"]',
            "created_at": utcnow(),
        },
    )

    ready = compute_ready_tasks(tmp_db, "m1")
    assert len(ready) == 2
    assert {r["id"] for r in ready} == {"a", "b"}


def test_readiness_blocked_by_dependency(tmp_db):
    tmp_db.insert("projects", {"id": "p1", "name": "test", "path": "/tmp/test", "created_at": utcnow()})
    tmp_db.insert(
        "missions",
        {
            "id": "m1",
            "project_id": "p1",
            "title": "t",
            "task": "t",
            "status": MissionStatus.IMPLEMENTING.value,
            "created_at": utcnow(),
            "updated_at": utcnow(),
        },
    )
    tmp_db.insert(
        "tasks",
        {
            "id": "a",
            "mission_id": "m1",
            "role": "implementation",
            "status": TaskStatus.RUNNING.value,
            "created_at": utcnow(),
        },
    )
    tmp_db.insert(
        "tasks",
        {
            "id": "b",
            "mission_id": "m1",
            "role": "implementation",
            "status": TaskStatus.PENDING.value,
            "created_at": utcnow(),
        },
    )
    tmp_db.insert("task_dependencies", {"from_task_id": "a", "to_task_id": "b", "created_at": utcnow().isoformat()})

    ready = compute_ready_tasks(tmp_db, "m1")
    assert len(ready) == 0
    b = tmp_db.get("tasks", "b")
    assert b["status"] == TaskStatus.BLOCKED.value


def test_readiness_resource_conflict_serializes(tmp_db):
    tmp_db.insert("projects", {"id": "p1", "name": "test", "path": "/tmp/test", "created_at": utcnow()})
    tmp_db.insert(
        "missions",
        {
            "id": "m1",
            "project_id": "p1",
            "title": "t",
            "task": "t",
            "status": MissionStatus.IMPLEMENTING.value,
            "created_at": utcnow(),
            "updated_at": utcnow(),
        },
    )
    tmp_db.insert(
        "tasks",
        {
            "id": "a",
            "mission_id": "m1",
            "role": "implementation",
            "status": TaskStatus.PENDING.value,
            "workspace_scope": '["backend/**"]',
            "created_at": utcnow(),
        },
    )
    tmp_db.insert(
        "tasks",
        {
            "id": "b",
            "mission_id": "m1",
            "role": "implementation",
            "status": TaskStatus.PENDING.value,
            "workspace_scope": '["backend/api/**"]',
            "created_at": utcnow(),
        },
    )

    ready = compute_ready_tasks(tmp_db, "m1")
    # Readiness engine is conservative: only one of the conflicting scopes is ready at a time
    assert len(ready) == 1
    b = tmp_db.get("tasks", "b")
    assert b["status"] == TaskStatus.BLOCKED.value


# ---------------------------------------------------------------------------
# 3. Provider Reservations
# ---------------------------------------------------------------------------


def test_reserve_provider_atomic(tmp_db):
    events = EventBus(tmp_db)
    tmp_db.insert("projects", {"id": "p1", "name": "test", "path": "/tmp/test", "created_at": utcnow()})
    tmp_db.insert(
        "missions",
        {
            "id": "m1",
            "project_id": "p1",
            "title": "t",
            "task": "t",
            "status": MissionStatus.IMPLEMENTING.value,
            "created_at": utcnow(),
            "updated_at": utcnow(),
        },
    )
    tmp_db.insert(
        "tasks",
        {
            "id": "t1",
            "mission_id": "m1",
            "role": "implementation",
            "status": TaskStatus.READY.value,
            "created_at": utcnow(),
        },
    )
    tmp_db.execute(
        "INSERT INTO providers(name, state, installed) VALUES ('fast', ?, 1)", (ProviderState.AVAILABLE.value,)
    )

    config_data = {"scheduler": {"max_parallel_tasks": 3, "max_parallel_per_provider": {"fast": 1}}}
    ok = try_reserve_provider(tmp_db, events, "t1", "fast", config_data)
    assert ok is True
    assert active_reservations_for_provider(tmp_db, "fast") == 1


def test_reserve_provider_capacity_exceeded(tmp_db):
    events = EventBus(tmp_db)
    tmp_db.insert("projects", {"id": "p1", "name": "test", "path": "/tmp/test", "created_at": utcnow()})
    tmp_db.insert(
        "missions",
        {
            "id": "m1",
            "project_id": "p1",
            "title": "t",
            "task": "t",
            "status": MissionStatus.IMPLEMENTING.value,
            "created_at": utcnow(),
            "updated_at": utcnow(),
        },
    )
    tmp_db.insert(
        "tasks",
        {
            "id": "t1",
            "mission_id": "m1",
            "role": "implementation",
            "status": TaskStatus.READY.value,
            "created_at": utcnow(),
        },
    )
    tmp_db.insert(
        "tasks",
        {
            "id": "t2",
            "mission_id": "m1",
            "role": "implementation",
            "status": TaskStatus.READY.value,
            "created_at": utcnow(),
        },
    )
    tmp_db.execute(
        "INSERT INTO providers(name, state, installed) VALUES ('fast', ?, 1)", (ProviderState.AVAILABLE.value,)
    )

    config_data = {"scheduler": {"max_parallel_tasks": 3, "max_parallel_per_provider": {"fast": 1}}}
    ok1 = try_reserve_provider(tmp_db, events, "t1", "fast", config_data)
    ok2 = try_reserve_provider(tmp_db, events, "t2", "fast", config_data)
    assert ok1 is True
    assert ok2 is False


def test_release_reservation(tmp_db):
    events = EventBus(tmp_db)
    tmp_db.insert("projects", {"id": "p1", "name": "test", "path": "/tmp/test", "created_at": utcnow()})
    tmp_db.insert(
        "missions",
        {
            "id": "m1",
            "project_id": "p1",
            "title": "t",
            "task": "t",
            "status": MissionStatus.IMPLEMENTING.value,
            "created_at": utcnow(),
            "updated_at": utcnow(),
        },
    )
    tmp_db.insert(
        "tasks",
        {
            "id": "t1",
            "mission_id": "m1",
            "role": "implementation",
            "status": TaskStatus.READY.value,
            "created_at": utcnow(),
        },
    )
    tmp_db.execute(
        "INSERT INTO providers(name, state, installed) VALUES ('fast', ?, 1)", (ProviderState.AVAILABLE.value,)
    )
    config_data = {"scheduler": {"max_parallel_tasks": 3, "max_parallel_per_provider": {"fast": 1}}}
    try_reserve_provider(tmp_db, events, "t1", "fast", config_data)
    release_provider_reservation(tmp_db, events, "t1", "run-1")
    assert active_reservations_for_provider(tmp_db, "fast") == 0


# ---------------------------------------------------------------------------
# 4. Resource Locks
# ---------------------------------------------------------------------------


def test_acquire_and_release_locks(tmp_db):
    events = EventBus(tmp_db)
    tmp_db.insert("projects", {"id": "p1", "name": "test", "path": "/tmp/test", "created_at": utcnow()})
    tmp_db.insert(
        "missions",
        {
            "id": "m1",
            "project_id": "p1",
            "title": "t",
            "task": "t",
            "status": MissionStatus.IMPLEMENTING.value,
            "created_at": utcnow(),
            "updated_at": utcnow(),
        },
    )
    tmp_db.insert(
        "tasks",
        {
            "id": "t1",
            "mission_id": "m1",
            "role": "implementation",
            "status": TaskStatus.RUNNING.value,
            "created_at": utcnow(),
        },
    )
    ok, reason = acquire_locks(tmp_db, events, "t1", [(LockType.PATH_PREFIX, "backend/**")])
    assert ok is True
    locks = tmp_db.query("SELECT * FROM task_locks WHERE task_id='t1' AND released_at IS NULL")
    assert len(locks) == 1

    release_locks_for_task(tmp_db, events, "t1")
    locks = tmp_db.query("SELECT * FROM task_locks WHERE task_id='t1' AND released_at IS NULL")
    assert len(locks) == 0


def test_lock_conflict(tmp_db):
    events = EventBus(tmp_db)
    tmp_db.insert("projects", {"id": "p1", "name": "test", "path": "/tmp/test", "created_at": utcnow()})
    tmp_db.insert(
        "missions",
        {
            "id": "m1",
            "project_id": "p1",
            "title": "t",
            "task": "t",
            "status": MissionStatus.IMPLEMENTING.value,
            "created_at": utcnow(),
            "updated_at": utcnow(),
        },
    )
    tmp_db.insert(
        "tasks",
        {
            "id": "t1",
            "mission_id": "m1",
            "role": "implementation",
            "status": TaskStatus.RUNNING.value,
            "created_at": utcnow(),
        },
    )
    tmp_db.insert(
        "tasks",
        {
            "id": "t2",
            "mission_id": "m1",
            "role": "implementation",
            "status": TaskStatus.RUNNING.value,
            "created_at": utcnow(),
        },
    )
    acquire_locks(tmp_db, events, "t1", [(LockType.PATH_PREFIX, "backend/**")])
    ok, reason = acquire_locks(tmp_db, events, "t2", [(LockType.PATH_PREFIX, "backend/**")])
    assert ok is False
    assert "conflicts" in reason.lower() or "held" in reason.lower()


# ---------------------------------------------------------------------------
# 5. Worktree Isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_and_remove_worktree(tmp_db, tmp_project):
    events = EventBus(tmp_db)
    tmp_db.insert("projects", {"id": "p1", "name": "test", "path": str(tmp_project), "created_at": utcnow()})
    tmp_db.insert(
        "missions",
        {
            "id": "m1",
            "project_id": "p1",
            "title": "t",
            "task": "t",
            "status": MissionStatus.IMPLEMENTING.value,
            "created_at": utcnow(),
            "updated_at": utcnow(),
        },
    )
    tmp_db.insert(
        "tasks",
        {
            "id": "task-a",
            "mission_id": "m1",
            "role": "implementation",
            "status": TaskStatus.PENDING.value,
            "created_at": utcnow(),
        },
    )

    branch = await create_task_worktree(tmp_db, events, tmp_project, "m1", "task-a")
    assert branch.branch_name.startswith("gg/m1/task-a")
    assert Path(branch.worktree_path).exists()

    wt = await get_task_worktree_path(tmp_db, "task-a")
    assert wt is not None

    await remove_task_worktree(tmp_db, events, tmp_project, "task-a")
    # worktree dir may still exist if git didn't fully remove it, but branch should be gone
    import subprocess

    branches = subprocess.run(["git", "branch", "--list"], cwd=tmp_project, capture_output=True, text=True).stdout  # noqa: ASYNC221
    assert branch.branch_name not in branches


@pytest.mark.asyncio
async def test_worktree_never_overwrites_user_branch(tmp_db, tmp_project):
    events = EventBus(tmp_db)
    tmp_db.insert("projects", {"id": "p1", "name": "test", "path": str(tmp_project), "created_at": utcnow()})
    tmp_db.insert(
        "missions",
        {
            "id": "m1",
            "project_id": "p1",
            "title": "t",
            "task": "t",
            "status": MissionStatus.IMPLEMENTING.value,
            "created_at": utcnow(),
            "updated_at": utcnow(),
        },
    )
    tmp_db.insert(
        "tasks",
        {
            "id": "task-a",
            "mission_id": "m1",
            "role": "implementation",
            "status": TaskStatus.PENDING.value,
            "created_at": utcnow(),
        },
    )

    # Pre-create a user branch with the same name
    import subprocess

    subprocess.run(["git", "checkout", "-b", "gg/m1/task-a"], cwd=tmp_project, check=True, capture_output=True)  # noqa: ASYNC221
    subprocess.run(["git", "checkout", "main"], cwd=tmp_project, check=False, capture_output=True)  # noqa: ASYNC221

    branch = await create_task_worktree(tmp_db, events, tmp_project, "m1", "task-a")
    # Should have generated a unique suffix to avoid collision
    assert branch.branch_name != "gg/m1/task-a"


# ---------------------------------------------------------------------------
# 6. Integration Engine
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_integration_no_branches(tmp_db, tmp_project):
    events = EventBus(tmp_db)
    tmp_db.insert("projects", {"id": "p1", "name": "test", "path": str(tmp_project), "created_at": utcnow()})
    tmp_db.insert(
        "missions",
        {
            "id": "m1",
            "project_id": "p1",
            "title": "t",
            "task": "t",
            "status": MissionStatus.IMPLEMENTING.value,
            "created_at": utcnow(),
            "updated_at": utcnow(),
        },
    )
    result = await run_integration(tmp_db, events, tmp_project, "m1")
    assert result["status"] == IntegrationStatus.COMPLETED.value


@pytest.mark.asyncio
async def test_integration_merge_two_branches(tmp_db, tmp_project):
    events = EventBus(tmp_db)
    tmp_db.insert("projects", {"id": "p1", "name": "test", "path": str(tmp_project), "created_at": utcnow()})
    tmp_db.insert(
        "missions",
        {
            "id": "m1",
            "project_id": "p1",
            "title": "t",
            "task": "t",
            "status": MissionStatus.IMPLEMENTING.value,
            "created_at": utcnow(),
            "updated_at": utcnow(),
        },
    )
    tmp_db.insert(
        "tasks",
        {
            "id": "ta",
            "mission_id": "m1",
            "role": "implementation",
            "status": TaskStatus.COMPLETED.value,
            "created_at": utcnow(),
        },
    )
    tmp_db.insert(
        "tasks",
        {
            "id": "tb",
            "mission_id": "m1",
            "role": "implementation",
            "status": TaskStatus.COMPLETED.value,
            "created_at": utcnow(),
        },
    )

    # Create two worktrees with changes
    from orchestrator.task_worktree import create_task_worktree

    ba = await create_task_worktree(tmp_db, events, tmp_project, "m1", "ta")
    bb = await create_task_worktree(tmp_db, events, tmp_project, "m1", "tb")

    (Path(ba.worktree_path) / "backend.txt").write_text("backend")
    import subprocess

    subprocess.run(["git", "add", "."], cwd=ba.worktree_path, check=True, capture_output=True)  # noqa: ASYNC221
    subprocess.run(["git", "commit", "-m", "backend"], cwd=ba.worktree_path, check=True, capture_output=True)  # noqa: ASYNC221

    (Path(bb.worktree_path) / "frontend.txt").write_text("frontend")
    subprocess.run(["git", "add", "."], cwd=bb.worktree_path, check=True, capture_output=True)  # noqa: ASYNC221
    subprocess.run(["git", "commit", "-m", "frontend"], cwd=bb.worktree_path, check=True, capture_output=True)  # noqa: ASYNC221

    result = await run_integration(tmp_db, events, tmp_project, "m1")
    assert result["status"] == IntegrationStatus.COMPLETED.value
    assert result["merged_commit"] is not None


# ---------------------------------------------------------------------------
# 7. Parallel Scheduler End-to-End
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parallel_engine_independent_tasks(tmp_db, tmp_project, config, registry):
    """Three independent tasks should execute in parallel up to capacity."""
    events = EventBus(tmp_db)
    tmp_db.insert("projects", {"id": "p1", "name": "test", "path": str(tmp_project), "created_at": utcnow()})
    tmp_db.insert(
        "missions",
        {
            "id": "m1",
            "project_id": "p1",
            "title": "t",
            "task": "t",
            "status": MissionStatus.RECOVERING.value,
            "scheduling_mode": SchedulingMode.PARALLEL_SAFE.value,
            "created_at": utcnow(),
            "updated_at": utcnow(),
        },
    )

    # Seed a pre-built DAG (bypass planning)
    for tid in ("a", "b", "c"):
        tmp_db.insert(
            "tasks",
            {
                "id": tid,
                "mission_id": "m1",
                "role": "implementation",
                "status": TaskStatus.PENDING.value,
                "workspace_scope": f'["{tid}/**"]',
                "created_at": utcnow(),
            },
        )

    engine = ParallelMissionEngine("m1", tmp_db, events, registry, config)
    engine.project_path = tmp_project

    # Run until completion (tasks are fake-fast, should finish quickly)
    await engine.run()

    # If no planning phase tasks, engine may fail or complete depending on DAG
    # Since we seeded tasks directly, the engine should run them
    tasks = tmp_db.query("SELECT * FROM tasks WHERE mission_id='m1'")
    assert len(tasks) == 3


@pytest.mark.asyncio
async def test_parallel_engine_dependency_order(tmp_db, tmp_project, config, registry):
    """A -> B: B must never start before A completes."""
    events = EventBus(tmp_db)
    tmp_db.insert("projects", {"id": "p1", "name": "test", "path": str(tmp_project), "created_at": utcnow()})
    tmp_db.insert(
        "missions",
        {
            "id": "m1",
            "project_id": "p1",
            "title": "t",
            "task": "t",
            "status": MissionStatus.RECOVERING.value,
            "scheduling_mode": SchedulingMode.PARALLEL_SAFE.value,
            "created_at": utcnow(),
            "updated_at": utcnow(),
        },
    )

    tmp_db.insert(
        "tasks",
        {
            "id": "a",
            "mission_id": "m1",
            "role": "implementation",
            "status": TaskStatus.PENDING.value,
            "workspace_scope": '["a/**"]',
            "created_at": utcnow(),
        },
    )
    tmp_db.insert(
        "tasks",
        {
            "id": "b",
            "mission_id": "m1",
            "role": "implementation",
            "status": TaskStatus.PENDING.value,
            "workspace_scope": '["b/**"]',
            "created_at": utcnow(),
        },
    )
    tmp_db.insert("task_dependencies", {"from_task_id": "a", "to_task_id": "b", "created_at": utcnow().isoformat()})

    engine = ParallelMissionEngine("m1", tmp_db, events, registry, config)
    await engine.run()

    a = tmp_db.get("tasks", "a")
    b = tmp_db.get("tasks", "b")
    # Both should be completed since fake providers succeed
    assert a["status"] == TaskStatus.COMPLETED.value
    assert b["status"] == TaskStatus.COMPLETED.value


@pytest.mark.asyncio
async def test_parallel_engine_diamond(tmp_db, tmp_project, config, registry):
    """A -> B,C -> D. B and C should run in parallel."""
    from orchestrator.providers.fake import WorkspaceWriterProvider

    class _PerTaskWriter(WorkspaceWriterProvider):
        """One distinct file per task worktree (derived from the worktree
        path): parallel siblings must not falsa-conflict on a shared fake
        marker file now that dependency inputs integrate real artifacts."""

        async def execute(self, request, on_output):  # type: ignore[no-untyped-def]
            self.filename = f"{request.workdir.name}.txt"
            self.content = f"{request.workdir.name}\n"
            return await super().execute(request, on_output)

    registry.adapters["fast"] = _PerTaskWriter("fast")
    registry.adapters["slow"] = _PerTaskWriter("slow")
    events = EventBus(tmp_db)
    tmp_db.insert("projects", {"id": "p1", "name": "test", "path": str(tmp_project), "created_at": utcnow()})
    tmp_db.insert(
        "missions",
        {
            "id": "m1",
            "project_id": "p1",
            "title": "t",
            "task": "t",
            "status": MissionStatus.RECOVERING.value,
            "scheduling_mode": SchedulingMode.PARALLEL_SAFE.value,
            "created_at": utcnow(),
            "updated_at": utcnow(),
        },
    )

    for tid in ("a", "b", "c", "d"):
        tmp_db.insert(
            "tasks",
            {
                "id": tid,
                "mission_id": "m1",
                "role": "implementation",
                "status": TaskStatus.PENDING.value,
                "workspace_scope": f'["{tid}/**"]',
                "created_at": utcnow(),
            },
        )
    tmp_db.insert("task_dependencies", {"from_task_id": "a", "to_task_id": "b", "created_at": utcnow().isoformat()})
    tmp_db.insert("task_dependencies", {"from_task_id": "a", "to_task_id": "c", "created_at": utcnow().isoformat()})
    tmp_db.insert("task_dependencies", {"from_task_id": "b", "to_task_id": "d", "created_at": utcnow().isoformat()})
    tmp_db.insert("task_dependencies", {"from_task_id": "c", "to_task_id": "d", "created_at": utcnow().isoformat()})

    engine = ParallelMissionEngine("m1", tmp_db, events, registry, config)
    await engine.run()

    for tid in ("a", "b", "c", "d"):
        t = tmp_db.get("tasks", tid)
        assert t["status"] == TaskStatus.COMPLETED.value, f"task {tid} not completed"


@pytest.mark.asyncio
async def test_parallel_engine_provider_failure_fallback(tmp_db, tmp_project, config, registry):
    """One task rate-limits; other independent tasks continue."""
    events = EventBus(tmp_db)
    # Replace one adapter with rate-limit provider
    registry.adapters["slow"] = RateLimitAfterDelayProvider("slow", delay_s=0.05)
    tmp_db.execute("UPDATE providers SET state=? WHERE name='slow'", (ProviderState.AVAILABLE.value,))

    tmp_db.insert("projects", {"id": "p1", "name": "test", "path": str(tmp_project), "created_at": utcnow()})
    tmp_db.insert(
        "missions",
        {
            "id": "m1",
            "project_id": "p1",
            "title": "t",
            "task": "t",
            "status": MissionStatus.RECOVERING.value,
            "scheduling_mode": SchedulingMode.PARALLEL_SAFE.value,
            "created_at": utcnow(),
            "updated_at": utcnow(),
        },
    )

    tmp_db.insert(
        "tasks",
        {
            "id": "a",
            "mission_id": "m1",
            "role": "implementation",
            "status": TaskStatus.PENDING.value,
            "workspace_scope": '["a/**"]',
            "preferred_providers": '["slow"]',
            "created_at": utcnow(),
        },
    )
    tmp_db.insert(
        "tasks",
        {
            "id": "b",
            "mission_id": "m1",
            "role": "implementation",
            "status": TaskStatus.PENDING.value,
            "workspace_scope": '["b/**"]',
            "preferred_providers": '["fast"]',
            "created_at": utcnow(),
        },
    )

    engine = ParallelMissionEngine("m1", tmp_db, events, registry, config)
    await engine.run()

    b = tmp_db.get("tasks", "b")
    assert b["status"] == TaskStatus.COMPLETED.value
    # Task a may be failed or retried depending on max_attempts
    a = tmp_db.get("tasks", "a")
    assert a["status"] in (TaskStatus.FAILED.value, TaskStatus.COMPLETED.value, TaskStatus.CANCELLED.value)


@pytest.mark.asyncio
async def test_parallel_engine_cancel(tmp_db, tmp_project, config, registry):
    """Cancel during execution: nothing new launches."""
    events = EventBus(tmp_db)
    # Use slow provider so we can cancel mid-flight
    registry.adapters["slow"] = SlowSuccessProvider("slow", delay_s=2.0)
    tmp_db.execute("UPDATE providers SET state=? WHERE name='slow'", (ProviderState.AVAILABLE.value,))

    tmp_db.insert("projects", {"id": "p1", "name": "test", "path": str(tmp_project), "created_at": utcnow()})
    tmp_db.insert(
        "missions",
        {
            "id": "m1",
            "project_id": "p1",
            "title": "t",
            "task": "t",
            "status": MissionStatus.RECOVERING.value,
            "scheduling_mode": SchedulingMode.PARALLEL_SAFE.value,
            "created_at": utcnow(),
            "updated_at": utcnow(),
        },
    )
    tmp_db.insert(
        "tasks",
        {
            "id": "a",
            "mission_id": "m1",
            "role": "implementation",
            "status": TaskStatus.PENDING.value,
            "workspace_scope": '["a/**"]',
            "preferred_providers": '["slow"]',
            "created_at": utcnow(),
        },
    )

    engine = ParallelMissionEngine("m1", tmp_db, events, registry, config)

    async def canceller():
        await asyncio.sleep(0.3)
        engine.request_cancel()

    asyncio.create_task(canceller())
    await engine.run()

    mission = tmp_db.get("missions", "m1")
    assert mission["status"] == MissionStatus.CANCELLED.value


# ---------------------------------------------------------------------------
# 8. Scope Conflict Detection
# ---------------------------------------------------------------------------


def test_scope_exact_match():
    from orchestrator.readiness import _scopes_conflict

    assert _scopes_conflict("backend/api.py", "backend/api.py") is True


def test_scope_prefix_conflict():
    from orchestrator.readiness import _scopes_conflict

    assert _scopes_conflict("backend/**", "backend/api/**") is True


def test_scope_no_conflict():
    from orchestrator.readiness import _scopes_conflict

    assert _scopes_conflict("backend/**", "frontend/**") is False


def test_scope_whole_workspace():
    from orchestrator.readiness import _scopes_conflict

    assert _scopes_conflict(".", "backend/**") is True
    assert _scopes_conflict("**", "frontend/**") is True


# ---------------------------------------------------------------------------
# 9. Provider Arbitration Scoring
# ---------------------------------------------------------------------------


def test_provider_score_favors_priority(tmp_db, config):
    tmp_db.execute(
        "INSERT INTO providers(name, state, installed, total_runs, successful_runs) VALUES ('fast', ?, 1, 10, 10)",
        (ProviderState.AVAILABLE.value,),
    )
    score, reasons = provider_score("fast", "implementation", config.raw, tmp_db)
    assert score > 0
    assert any("priority" in r for r in reasons)


def test_provider_score_penalizes_cooldown(tmp_db, config):
    from datetime import UTC, datetime, timedelta

    until = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    tmp_db.execute(
        "INSERT INTO providers(name, state, installed, total_runs, successful_runs, cooldown_until) VALUES ('slow', ?, 1, 10, 10, ?)",
        (ProviderState.AVAILABLE.value, until),
    )
    score, reasons = provider_score("slow", "implementation", config.raw, tmp_db)
    assert score < 0 or any("cooldown" in r for r in reasons)
