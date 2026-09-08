"""Post-mission resource recovery: no leaked reservations, locks, tasks,
busy providers, or missing branches/worktrees after a full parallel run."""

from __future__ import annotations

import asyncio
import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from orchestrator.config import Config
from orchestrator.db import Database
from orchestrator.events import EventBus
from orchestrator.models import MissionStatus, ProviderState, SchedulingMode, TaskStatus, utcnow
from orchestrator.parallel_engine import ParallelMissionEngine
from orchestrator.providers.base import ExecutionResult
from orchestrator.providers.fake import FakeAdapter
from orchestrator.providers.registry import ProviderRegistry


class ScopedWorkFake(FakeAdapter):
    """Fake that writes a per-instance file so parallel branches merge cleanly."""

    def __init__(self, name: str, filename: str):
        super().__init__(name, ["work"])
        self.filename = filename

    async def execute(self, request: Any, on_output: Any) -> ExecutionResult:
        if request.role in ("implementation", "repair", "testing"):
            (request.workdir / self.filename).write_text(f"{self.name}\n")
            on_output(f"[{self.name}] wrote {self.filename}")
        tail = "REVIEW_FINDINGS_JSON: []" if request.role == "review" else "ok"
        from orchestrator.models import FailureClass, ProviderState

        return ExecutionResult(
            state=ProviderState.COMPLETED,
            failure_class=FailureClass.NONE,
            exit_code=0,
            duration_s=0.01,
            summary=f"fake {self.name} {request.role}",
            raw_tail=tail,
            assistant_text=tail,
        )


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_full_mission_leaves_no_leaks():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        db = Database(base / "test.db")
        proj = base / "proj"
        proj.mkdir()
        _git(proj, "init")
        _git(proj, "config", "user.email", "t@t.t")
        _git(proj, "config", "user.name", "t")
        (proj / "package.json").write_text(
            json.dumps({"name": "t", "scripts": {"test": "node -e \"process.exit(0)\""}})
        )
        (proj / "README.md").write_text("# t\n")
        _git(proj, "add", ".")
        _git(proj, "commit", "-m", "init")

        config = Config(
            {
                "scheduler": {"max_parallel_tasks": 3},
                "priority": {
                    "planning": ["wa"],
                    "implementation": ["wa", "wb", "wc"],
                    "testing": ["wa"],
                    "review": ["wa"],
                    "repair": ["wa"],
                },
                "providers": {n: {"enabled": True} for n in ("wa", "wb", "wc")},
                "orchestration": {"scheduler_tick_seconds": 0.05, "max_phase_attempts": 2},
            }
        )
        adapters = {n: ScopedWorkFake(n, f"{n}.txt") for n in ("wa", "wb", "wc")}
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
        for tid, prov in (("a", "wa"), ("b", "wb"), ("c", "wc")):
            db.insert(
                "tasks",
                {
                    "id": tid,
                    "mission_id": "m1",
                    "role": "implementation",
                    "status": TaskStatus.PENDING.value,
                    "preferred_providers": f'["{prov}"]',
                    "workspace_scope": f'["{prov}.txt"]',
                    "created_at": utcnow(),
                },
            )

        async def main() -> None:
            engine = ParallelMissionEngine("m1", db, EventBus(db), registry, config)
            engine.project_path = proj
            await engine.run()

        asyncio.run(main())

        assert db.get("missions", "m1")["status"] == MissionStatus.COMPLETED.value
        # no non-terminal tasks
        assert db.query(
            "SELECT * FROM tasks WHERE mission_id='m1' AND status NOT IN "
            "('COMPLETED','FAILED','CANCELLED','UNVERIFIED')"
        ) == []
        # no unreleased reservations or locks
        assert db.query("SELECT * FROM provider_reservations WHERE released_at IS NULL") == []
        assert db.query("SELECT * FROM task_locks WHERE released_at IS NULL") == []
        # providers back to AVAILABLE
        for n in ("wa", "wb", "wc"):
            assert db.get("providers", n, key="name")["state"] == ProviderState.AVAILABLE.value
        # branches preserved for recovery, worktree dirs intact (never deleted)
        branches = db.query("SELECT * FROM task_branches WHERE removed_at IS NULL")
        assert len(branches) == 3
        for b in branches:
            assert Path(b["worktree_path"]).is_dir()
        db.close()
