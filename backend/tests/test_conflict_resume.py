"""Merge-conflict operator path: conflict blocks honestly, manual merge plus
resume completes the mission. Deterministic — fake providers, real git."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any

from conftest import make_orchestrator
from orchestrator.config import Config
from orchestrator.models import (
    FailureClass,
    IntegrationStatus,
    MissionStatus,
    ProviderState,
    SchedulingMode,
)
from orchestrator.providers.base import ExecutionResult
from orchestrator.providers.fake import FakeAdapter


class ConflictFake(FakeAdapter):
    """Writes fixed conflicting content to shared.txt for implementation."""

    def __init__(self, name: str, content: str):
        super().__init__(name, ["ok"])
        self.content = content

    async def execute(self, request: Any, on_output: Any) -> ExecutionResult:
        self.calls += 1
        if request.role == "implementation":
            (request.workdir / "shared.txt").write_text(self.content)
        tail = "REVIEW_FINDINGS_JSON: []" if request.role == "review" else "ok"
        return ExecutionResult(
            state=ProviderState.COMPLETED,
            failure_class=FailureClass.NONE,
            exit_code=0,
            duration_s=0.01,
            summary=f"fake {self.name} {request.role}",
            raw_tail=tail,
            assistant_text=tail,
        )


def _conflict_config() -> Config:
    return Config(
        {
            "scheduler": {"max_parallel_tasks": 3},
            "priority": {
                "planning": ["impl-a"],
                "implementation": ["impl-a", "impl-b"],
                "testing": ["impl-a"],
                "review": ["impl-a"],
                "repair": ["impl-a"],
            },
            "providers": {"impl-a": {"enabled": True}, "impl-b": {"enabled": True}},
            "orchestration": {
                "scheduler_tick_seconds": 0.05,
                "max_phase_attempts": 4,
                "max_provider_wait_seconds": 5,
            },
        }
    )


def test_conflict_blocks_then_resume_completes(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "package.json").write_text(
        json.dumps({"name": "t", "scripts": {"test": "node -e \"process.exit(0)\""}})
    )
    (ws / "shared.txt").write_text("base line\n")
    subprocess.run(["git", "init"], cwd=ws, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=ws, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=ws, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=ws, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=ws, check=True, capture_output=True)

    async def main() -> None:
        orch = make_orchestrator(
            tmp_path,
            {
                "impl-a": ConflictFake("impl-a", "base line\nside A\n"),
                "impl-b": ConflictFake("impl-b", "base line\nside B\n"),
            },
            _conflict_config(),
        )
        await orch.registry.detect_all()
        orch.db.insert(
            "projects",
            {"id": "p1", "name": "w", "path": str(ws), "detected_type": "node", "created_at": "2024-01-01"},
        )
        mission = orch.create_mission("p1", "conflict mission", "edit shared", "AUTONOMOUS", "balanced")
        mid = mission["id"]
        orch.db.update("missions", mid, {"scheduling_mode": SchedulingMode.PARALLEL_SAFE.value})
        for tid, pref in (("task-a", "impl-a"), ("task-b", "impl-b")):
            orch.db.insert(
                "tasks",
                {
                    "id": f"{mid[:8]}-{tid}",
                    "mission_id": mid,
                    "role": "implementation",
                    "status": "PENDING",
                    "title": tid,
                    "description": tid,
                    "preferred_providers": json.dumps([pref]),
                    "workspace_scope": json.dumps(["shared.txt"]),
                    "created_at": "2024-01-01T00:00:00+00:00",
                    "dag_revision": 1,
                },
            )
        orch.start_mission(mid)
        await asyncio.wait_for(orch._engine_tasks[mid], timeout=120)

        blocked = orch.db.get("missions", mid)
        assert blocked is not None
        assert blocked["status"] == MissionStatus.WAITING_FOR_HUMAN.value
        assert "merge conflict" in (blocked["blocking_issue"] or "")
        conflicts = orch.db.query(
            "SELECT * FROM task_integrations WHERE mission_id=? ORDER BY created_at DESC LIMIT 1", (mid,)
        )
        assert conflicts and conflicts[0]["status"] == IntegrationStatus.MERGE_CONFLICT.value
        conflict_files = json.loads(conflicts[0]["conflict_files"])
        assert conflict_files == ["shared.txt"]
        branches = json.loads(conflicts[0]["branch_names"])
        assert len(branches) == 2

        # Operator resolves in the repo: merge remaining branches manually.
        async def git(*args: str) -> subprocess.CompletedProcess[str]:
            return await asyncio.to_thread(
                subprocess.run,
                ["git", *args],
                cwd=ws,
                check=True,
                capture_output=True,
                text=True,
            )

        merged = (
            await asyncio.to_thread(
                subprocess.run,
                ["git", "branch", "--merged", "HEAD", "--list"],
                cwd=ws,
                check=True,
                capture_output=True,
                text=True,
            )
        ).stdout
        for branch in branches:
            if branch.strip() and branch.strip() not in merged:
                res = await asyncio.to_thread(
                    subprocess.run,
                    ["git", "merge", "--no-commit", "--no-ff", branch.strip()],
                    cwd=ws,
                    capture_output=True,
                    text=True,
                )
                if res.returncode != 0:
                    await git("checkout", "--theirs", "--", "shared.txt")
                    await git("add", "shared.txt")
                await git("commit", "-m", f"operator: resolve {branch.strip()}", "--allow-empty")
        assert (ws / "shared.txt").read_text() in ("base line\nside A\n", "base line\nside B\n")

        # Resume re-runs integration; already-merged branches are skipped.
        orch.resume_mission(mid)
        await asyncio.wait_for(orch._engine_tasks[mid], timeout=180)
        final = orch.db.get("missions", mid)
        assert final is not None
        assert final["status"] == MissionStatus.COMPLETED.value, final.get("blocking_issue")
        done = orch.db.query(
            "SELECT * FROM task_integrations WHERE mission_id=? AND status='COMPLETED'", (mid,)
        )
        # Operator-merged branches are skipped as already-merged; success is
        # the COMPLETED row, not necessarily a new merge commit.
        assert done
        await orch.shutdown()

    asyncio.run(main())
