"""Final verified state must be checkpointed before COMPLETED.

Regression for the scale audit HIGH: repair edits landed in the working
tree but the recorded git_head pointed at the pre-repair commit.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest

from orchestrator import git_ops
from orchestrator.config import Config
from orchestrator.db import Database
from orchestrator.events import EventBus
from orchestrator.models import (
    FailureClass,
    MissionStatus,
    ProviderState,
    SchedulingMode,
    TaskStatus,
    utcnow,
)
from orchestrator.parallel_engine import ParallelMissionEngine
from orchestrator.providers.base import ExecutionResult
from orchestrator.providers.fake import FakeAdapter, WorkspaceWriterProvider
from orchestrator.providers.registry import ProviderRegistry


class BlockerThenCleanReviewer(FakeAdapter):
    """First review reports a BLOCKER on app.txt; later reviews are clean."""

    def __init__(self) -> None:
        super().__init__("reviewer", ["ok"])
        self.reviews = 0

    async def execute(self, request: Any, on_output: Any) -> ExecutionResult:
        body = "ok"
        if request.role == "review":
            self.reviews += 1
            if self.reviews == 1:
                body = (
                    'REVIEW_FINDINGS_JSON: [{"severity": "BLOCKER", "category": "bug", '
                    '"file": "app.txt", "description": "buggy content", '
                    '"recommended_fix": "write fixed content"}]'
                )
            else:
                body = "REVIEW_FINDINGS_JSON: []"
        return ExecutionResult(
            state=ProviderState.COMPLETED,
            failure_class=FailureClass.NONE,
            exit_code=0,
            duration_s=0.01,
            summary="reviewed",
            raw_tail=body,
            assistant_text=body,
        )


def _git(cwd: Path, *args: str) -> str:
    res = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return res.stdout


@pytest.fixture()
def harness():
    td = tempfile.TemporaryDirectory()
    base = Path(td.name)
    db = Database(base / "test.db")
    proj = base / "proj"
    proj.mkdir()
    _git(proj, "init")
    _git(proj, "config", "user.email", "t@t.t")
    _git(proj, "config", "user.name", "t")
    (proj / "package.json").write_text(
        json.dumps(
            {
                "name": "t",
                "scripts": {
                    "test": "node -e \"process.exit("
                    "require('fs').readFileSync('app.txt','utf8').includes('fixed')?0:1)\""
                },
            }
        )
    )
    (proj / "README.md").write_text("# t\n")
    _git(proj, "add", ".")
    _git(proj, "commit", "-m", "init")
    config = Config(
        {
            "scheduler": {"max_parallel_tasks": 3},
            "priority": {
                "planning": ["impl"],
                "implementation": ["impl"],
                "testing": ["impl"],
                "review": ["reviewer"],
                "repair": ["repairer"],
            },
            "providers": {n: {"enabled": True} for n in ("impl", "reviewer", "repairer")},
            "orchestration": {
                "scheduler_tick_seconds": 0.05,
                "max_phase_attempts": 2,
                "review_required": True,
                "max_repair_cycles": 3,
            },
            "git": {"max_auto_commit_file_mb": 5},
        }
    )
    adapters = {
        "impl": WorkspaceWriterProvider("impl", "app.txt", "v1 buggy\n"),
        "reviewer": BlockerThenCleanReviewer(),
        "repairer": WorkspaceWriterProvider("repairer", "app.txt", "v1 fixed\n"),
    }
    registry = ProviderRegistry(db, adapters, config)
    for n in adapters:
        db.execute(
            "INSERT OR REPLACE INTO providers(name, state, installed) VALUES (?, ?, ?)",
            (n, ProviderState.AVAILABLE.value, 1),
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
            "autonomy": "AUTONOMOUS",
            "profile": "balanced",
            "created_at": utcnow(),
            "updated_at": utcnow(),
        },
    )
    db.insert(
        "tasks",
        {
            "id": "task-a",
            "mission_id": "m1",
            "role": "implementation",
            "status": TaskStatus.PENDING.value,
            "title": "build app",
            "description": "build app",
            "preferred_providers": '["impl"]',
            "workspace_scope": '["app.txt"]',
            "created_at": utcnow(),
            "dag_revision": 1,
        },
    )
    engine = ParallelMissionEngine("m1", db, EventBus(db), registry, config)
    engine.project_path = proj
    yield {"db": db, "proj": proj, "engine": engine, "config": config}
    db.close()
    td.cleanup()


def test_final_checkpoint_contains_repair_and_reproduces(harness: dict[str, Any]):
    async def main() -> None:
        await harness["engine"].run()

    asyncio.run(main())
    db: Database = harness["db"]
    proj: Path = harness["proj"]
    mission = db.get("missions", "m1")
    assert mission is not None and mission["status"] == MissionStatus.COMPLETED.value
    head = mission.get("git_head")
    assert head
    # the recorded commit contains the repaired file content
    shown = _git(proj, "show", f"{head}:app.txt")
    assert shown == "v1 fixed\n"
    # fresh checkout of the recorded SHA passes the same verification
    with tempfile.TemporaryDirectory() as td:
        co = Path(td) / "co"
        _git(proj, "worktree", "add", "--detach", str(co), head)
        try:
            res = subprocess.run(["npm", "test", "--prefix", str(co)], capture_output=True, text=True)
            assert res.returncode == 0, res.stderr[-2000:]
        finally:
            _git(proj, "worktree", "remove", "--force", str(co))
    # checkpoints table traces the final commit
    ckpts = db.query("SELECT * FROM checkpoints WHERE mission_id='m1' ORDER BY created_at")
    assert ckpts and ckpts[-1]["commit_sha"] == head


def test_final_checkpoint_failure_blocks_completed(harness: dict[str, Any], monkeypatch: pytest.MonkeyPatch):
    db: Database = harness["db"]
    proj: Path = harness["proj"]
    engine: ParallelMissionEngine = harness["engine"]
    real_checkpoint = git_ops.checkpoint

    async def fail_main_repo(root: Path, message: str, max_file_mb: int = 5) -> str | None:
        if Path(root) == proj:
            raise git_ops.GitCheckpointError("injected disk failure")
        return await real_checkpoint(root, message, max_file_mb=max_file_mb)

    monkeypatch.setattr(git_ops, "checkpoint", fail_main_repo)
    harness["config"]._data.setdefault("git", {})["max_checkpoint_failures"] = 1

    async def main() -> None:
        await engine.run()

    asyncio.run(main())
    mission = db.get("missions", "m1")
    assert mission is not None
    assert mission["status"] == MissionStatus.UNVERIFIED.value
    assert "final checkpoint failed" in (mission["blocking_issue"] or "")
    assert int(mission.get("checkpoint_failures", 0)) >= 1


def test_transient_checkpoint_failure_retries_on_rerun(
    harness: dict[str, Any], monkeypatch: pytest.MonkeyPatch
):
    db: Database = harness["db"]
    proj: Path = harness["proj"]
    engine: ParallelMissionEngine = harness["engine"]
    real_checkpoint = git_ops.checkpoint
    calls = {"n": 0}

    async def fail_once(root: Path, message: str, max_file_mb: int = 5) -> str | None:
        if Path(root) == proj and calls["n"] == 0:
            calls["n"] += 1
            raise git_ops.GitCheckpointError("transient failure")
        return await real_checkpoint(root, message, max_file_mb=max_file_mb)

    monkeypatch.setattr(git_ops, "checkpoint", fail_once)

    async def main() -> None:
        await engine.run()

    asyncio.run(main())
    mid = db.get("missions", "m1")
    assert mid is not None
    # transient failure: not COMPLETED, not terminally failed either
    assert mid["status"] == MissionStatus.FINAL_VALIDATION.value

    async def retry() -> None:
        engine2 = ParallelMissionEngine("m1", db, EventBus(db), engine.registry, engine.config)
        engine2.project_path = proj
        await engine2.run()

    asyncio.run(retry())
    final = db.get("missions", "m1")
    assert final is not None and final["status"] == MissionStatus.COMPLETED.value
    assert final.get("git_head")
    assert int(final.get("checkpoint_failures", 0)) == 0
