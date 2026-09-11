from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from conftest import make_orchestrator
from orchestrator import git_ops
from orchestrator.models import EventType, MissionStatus
from orchestrator.providers.fake import FakeAdapter


def _commit_fixture(workspace: Path) -> None:
    """Commit fixture files so the mission starts clean (F-PROV-03): these
    tests pin checkpoint-exhaustion behavior, not the dirty-start gate —
    a dirty start must gate, which would mask the exhaustion path."""
    subprocess.run(["git", "add", "-A"], cwd=workspace, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "fixture"], cwd=workspace, check=True, capture_output=True)


def _seed_project(orch, workspace: Path) -> None:
    orch.db.insert(
        "projects",
        {"id": "p1", "name": "w", "path": str(workspace), "detected_type": "node", "created_at": "2024-01-01"},
    )


def test_checkpoint_exhaustion_prevents_completed(tmp_path: Path, workspace: Path) -> None:
    async def scenario() -> None:
        await git_ops.init_repo(workspace)
        _commit_fixture(workspace)
        providers = {"fake-a": FakeAdapter("fake-a", ["ok"])}
        orch = make_orchestrator(tmp_path, providers)
        _seed_project(orch, workspace)

        mission = orch.create_mission("p1", "exhaustion", "prompt", "AUTONOMOUS", "balanced")
        mission_id = mission["id"]

        events_received = []
        orch.events.add_sync_subscriber(lambda e: events_received.append(e))

        original_checkpoint = git_ops.checkpoint
        try:
            async def broken_checkpoint(root, msg, **kwargs):
                raise git_ops.GitError("simulated git error")

            git_ops.checkpoint = broken_checkpoint

            await orch.start()
            orch.start_mission(mission_id)

            for _ in range(60):
                row = orch.db.get("missions", mission_id)
                if row["status"] in ("COMPLETED", "FAILED", "UNVERIFIED"):
                    break
                await asyncio.sleep(0.05)

            row = orch.db.get("missions", mission_id)
            assert row["status"] == MissionStatus.UNVERIFIED.value
            assert "git checkpoint exhausted" in (row.get("blocking_issue") or "")
            assert any(e.type == EventType.GIT_CHECKPOINT_FAILED.value for e in events_received)
        finally:
            git_ops.checkpoint = original_checkpoint
            await orch.shutdown()

    asyncio.run(scenario())


def test_transient_checkpoint_failure_allows_completion(tmp_path: Path, workspace: Path) -> None:
    async def scenario() -> None:
        await git_ops.init_repo(workspace)
        _commit_fixture(workspace)
        providers = {"fake-a": FakeAdapter("fake-a", ["ok"])}
        orch = make_orchestrator(tmp_path, providers)
        _seed_project(orch, workspace)

        mission = orch.create_mission("p1", "transient", "prompt", "AUTONOMOUS", "balanced")
        mission_id = mission["id"]

        original_checkpoint = git_ops.checkpoint
        fail_count = 0

        try:
            async def transient_checkpoint(root, msg, **kwargs):
                nonlocal fail_count
                if fail_count < 1:
                    fail_count += 1
                    raise git_ops.GitError("simulated transient error")
                return await original_checkpoint(root, msg, **kwargs)

            git_ops.checkpoint = transient_checkpoint

            await orch.start()
            orch.start_mission(mission_id)

            for _ in range(60):
                row = orch.db.get("missions", mission_id)
                if row["status"] in ("COMPLETED", "FAILED", "UNVERIFIED"):
                    break
                await asyncio.sleep(0.05)

            row = orch.db.get("missions", mission_id)
            assert row["status"] == MissionStatus.COMPLETED.value
            assert fail_count == 1
        finally:
            git_ops.checkpoint = original_checkpoint
            await orch.shutdown()

    asyncio.run(scenario())
