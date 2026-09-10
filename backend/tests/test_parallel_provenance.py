"""Parallel writer provenance: multi-writer sets, integration preservation (P-23..P-25)."""

from __future__ import annotations

import asyncio
import subprocess
import tempfile
from pathlib import Path

import pytest

from orchestrator.config import Config
from orchestrator.db import Database
from orchestrator.events import EventBus
from orchestrator.models import MissionStatus, ProviderState, SchedulingMode, TaskStatus, utcnow
from orchestrator.parallel_engine import ParallelMissionEngine
from orchestrator.provenance import mission_provider_writers
from orchestrator.providers.fake import WorkspaceWriterProvider
from orchestrator.providers.registry import ProviderRegistry


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture()
def setup():
    td = tempfile.TemporaryDirectory()
    tmp = Path(td.name)
    proj = tmp / "project"
    proj.mkdir()
    _git(proj, "init")
    _git(proj, "config", "user.email", "t@t")
    _git(proj, "config", "user.name", "t")
    (proj / "README.md").write_text("# init")
    _git(proj, "add", ".")
    _git(proj, "commit", "-m", "init")
    db = Database(tmp / "test.db")
    cfg = Config(
        {
            "scheduler": {"max_parallel_tasks": 3},
            "priority": {
                "implementation": ["fast", "slow"],
                "planning": ["fast"],
                "review": ["fast"],
                "repair": ["fast"],
            },
            "providers": {"fast": {"enabled": True}, "slow": {"enabled": True}},
            "orchestration": {
                "scheduler_tick_seconds": 0.05,
                "max_phase_attempts": 2,
                "review_required": False,
                "max_repair_cycles": 1,
            },
            "git": {"max_auto_commit_file_mb": 5},
            "context": {"mode": "compiled"},
        }
    )
    adapters = {
        "fast": WorkspaceWriterProvider("fast", filename="a/data.txt", content="hello"),
        "slow": WorkspaceWriterProvider("slow", filename="b/data.txt", content="world"),
    }
    reg = ProviderRegistry(db, adapters, cfg)
    for name in adapters:
        db.execute(
            "INSERT OR REPLACE INTO providers(name, state, installed) VALUES (?, ?, ?)",
            (name, ProviderState.AVAILABLE.value, 1),
        )
    db.insert("projects", {"id": "p1", "name": "t", "path": str(proj), "created_at": utcnow()})
    db.insert(
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
    yield db, cfg, reg, proj
    db.close()


def _task(db: Database, tid: str, scope: str, providers: list[str] | None = None) -> None:
    import json as _json

    db.insert(
        "tasks",
        {
            "id": tid,
            "mission_id": "m1",
            "role": "implementation",
            "status": TaskStatus.PENDING.value,
            "workspace_scope": f'["{scope}"]',
            "preferred_providers": _json.dumps(providers or []),
            "title": tid,
            "description": tid,
            "created_at": utcnow(),
        },
    )


def test_two_writers_preserved_through_integration(setup):
    """P-23/P-25: OpenCode+Codex style multi-writer set survives the SYSTEM merge."""
    db, cfg, reg, proj = setup
    events = EventBus(db)
    _task(db, "ta", "a/**", ["fast"])
    _task(db, "tb", "b/**", ["slow"])

    async def main() -> None:
        engine = ParallelMissionEngine("m1", db, events, reg, cfg)
        await engine.run()
        assert db.get("tasks", "ta")["status"] == TaskStatus.COMPLETED.value
        assert db.get("tasks", "tb")["status"] == TaskStatus.COMPLETED.value

    asyncio.run(main())
    writers, complete = mission_provider_writers(db, "m1")
    assert writers == {"fast", "slow"}, f"both task writers must be attributed, got {writers}"
    assert complete
    # Integration merge is SYSTEM with ancestry intact — never "writer = GG".
    system_rows = db.query("SELECT * FROM write_provenance WHERE mission_id='m1' AND actor_type='SYSTEM'")
    assert system_rows, "integration merge must leave a SYSTEM row"
    for row in db.query("SELECT * FROM write_provenance WHERE mission_id='m1' AND actor_type='PROVIDER'"):
        assert row["provider"] in ("fast", "slow")


def test_same_provider_runs_dedup_set_preserve_runs(setup):
    """Two runs by one provider: set deduplicates, run rows are preserved."""
    db, cfg, reg, proj = setup
    events = EventBus(db)
    _task(db, "ta", "a/**")
    _task(db, "tb", "a2/**")

    async def main() -> None:
        # Force both tasks onto 'fast'.
        reg.adapters.pop("slow")
        engine = ParallelMissionEngine("m1", db, events, reg, cfg)
        await engine.run()

    asyncio.run(main())
    writers, _ = mission_provider_writers(db, "m1")
    assert writers == {"fast"}
    runs = db.query("SELECT id FROM provider_runs WHERE task_id IN ('ta','tb')")
    assert len(runs) == 2, "both attempt rows preserved"
    linked = db.query("SELECT COUNT(*) as n FROM write_provenance WHERE mission_id='m1' AND run_id IS NOT NULL")
    assert linked[0]["n"] >= 2


def test_task_dirty_worktree_blocked_without_execution(setup):
    """P-03 parallel: a dirty task worktree fails the task, runs nothing."""
    db, cfg, reg, proj = setup
    events = EventBus(db)

    async def main() -> None:
        from orchestrator import task_worktree

        _task(db, "ta", "a/**")
        orig = task_worktree.create_task_worktree

        async def _dirty_creator(*args, **kwargs):  # noqa: ANN001, ANN202
            record = await orig(*args, **kwargs)
            (Path(record.worktree_path) / "leftover.txt").write_text("someone else")
            return record

        task_worktree.create_task_worktree = _dirty_creator
        try:
            engine = ParallelMissionEngine("m1", db, events, reg, cfg)
            await engine.run()
        finally:
            task_worktree.create_task_worktree = orig
        task = db.get("tasks", "ta")
        assert task["status"] == TaskStatus.FAILED.value
        assert "unattributed changes" in (task.get("blocking_issue") or "")

    asyncio.run(main())
    assert db.query("SELECT COUNT(*) as n FROM provider_runs")[0]["n"] == 0
