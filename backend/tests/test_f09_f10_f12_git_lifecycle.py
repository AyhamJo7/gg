"""Regression tests for F-09, F-10, and F-12 git lifecycle findings.

- F-09: Repeated git checkpoint failures emit GIT_CHECKPOINT_FAILED events and set blocking_issue.
- F-10: Detached HEAD is detected and checked out to a dedicated gg/mission-<id> branch.
- F-12: Mission completion records the fresh post-checkpoint git_head instead of a stale pre-checkpoint SHA.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from conftest import make_orchestrator
from orchestrator import git_ops
from orchestrator.models import EventType, MissionStatus
from orchestrator.orchestrator import Orchestrator
from orchestrator.providers.fake import FakeAdapter


def _seed_project(orch: Orchestrator, workspace: Path) -> None:
    orch.db.insert(
        "projects",
        {"id": "p1", "name": "w", "path": str(workspace), "detected_type": "node", "created_at": "2024-01-01"},
    )


def test_f10_detached_head_checks_out_mission_branch(tmp_path: Path, workspace: Path) -> None:
    async def scenario() -> None:
        # Initialize repo and detach HEAD
        await git_ops.init_repo(workspace)
        (workspace / "initial.txt").write_text("commit 1")
        await git_ops.checkpoint(workspace, "initial commit")
        await git_ops._git(workspace, "checkout", "--detach", "HEAD")

        # Verify repo is detached
        st = await git_ops.status(workspace)
        assert st.branch == "", "Repo should be in detached HEAD state"

        # Run mission
        providers = {"fake-a": FakeAdapter("fake-a", ["ok"])}
        orch = make_orchestrator(tmp_path, providers)
        _seed_project(orch, workspace)

        mission = orch.create_mission("p1", "detached test", "prompt", "AUTONOMOUS", "balanced")
        mission_id = mission["id"]

        await orch.start()
        orch.start_mission(mission_id)

        for _ in range(60):
            row = orch.db.get("missions", mission_id)
            if row["status"] in ("COMPLETED", "FAILED", "UNVERIFIED"):
                break
            await asyncio.sleep(0.05)

        assert orch.db.get("missions", mission_id)["status"] == MissionStatus.COMPLETED.value

        # Verify branch is now the dedicated mission branch
        st_after = await git_ops.status(workspace)
        expected_branch = f"gg/mission-{mission_id[:8]}"
        assert st_after.branch == expected_branch, f"Expected branch {expected_branch}, got {st_after.branch}"

        await orch.shutdown()

    asyncio.run(scenario())


def test_f12_final_checkpoint_updates_git_head_truthfully(tmp_path: Path, workspace: Path) -> None:
    async def scenario() -> None:
        await git_ops.init_repo(workspace)
        (workspace / "initial.txt").write_text("start")
        await git_ops.checkpoint(workspace, "start commit")

        providers = {"fake-a": FakeAdapter("fake-a", ["work"])}
        orch = make_orchestrator(tmp_path, providers)
        _seed_project(orch, workspace)

        mission = orch.create_mission("p1", "test head", "prompt", "AUTONOMOUS", "balanced")
        mission_id = mission["id"]

        await orch.start()
        orch.start_mission(mission_id)

        for _ in range(60):
            row = orch.db.get("missions", mission_id)
            if row["status"] == MissionStatus.COMPLETED.value:
                break
            await asyncio.sleep(0.05)

        final_mission = orch.db.get("missions", mission_id)
        assert final_mission["status"] == MissionStatus.COMPLETED.value

        # The recorded git_head on the mission row must match HEAD SHA exactly
        real_head = await git_ops.head_sha(workspace)
        assert final_mission["git_head"] == real_head
        assert real_head is not None

        # Verify that checkpoints table records this final commit
        checkpoints = orch.db.query("SELECT * FROM checkpoints WHERE mission_id=?", (mission_id,))
        assert any(c["commit_sha"] == real_head for c in checkpoints)

        await orch.shutdown()

    asyncio.run(scenario())


def test_f09_repeated_checkpoint_failures_emit_event_and_record_blocking_issue(tmp_path: Path, workspace: Path) -> None:
    async def scenario() -> None:
        await git_ops.init_repo(workspace)
        providers = {"fake-a": FakeAdapter("fake-a", ["ok"])}
        orch = make_orchestrator(tmp_path, providers)
        _seed_project(orch, workspace)

        mission = orch.create_mission("p1", "checkpoint fail test", "prompt", "AUTONOMOUS", "balanced")
        mission_id = mission["id"]

        engine = orch._create_engine(mission_id) if hasattr(orch, "_create_engine") else None
        if not engine:
            from orchestrator.engine import MissionEngine
            engine = MissionEngine(mission_id, orch.db, orch.events, orch.registry, orch.config, orch.locks)

        engine.project_path = workspace
        engine.workspace = await orch.inspect_workspace(workspace) if hasattr(orch, "inspect_workspace") else None
        if not engine.workspace:
            from orchestrator.workspace import inspect_workspace
            engine.workspace = await inspect_workspace(workspace)

        events_received = []
        orch.events.add_sync_subscriber(lambda e: events_received.append(e))

        # Make checkpoint fail by mocking or breaking git
        original_checkpoint = git_ops.checkpoint
        try:
            async def broken_checkpoint(root, msg):
                raise git_ops.GitError("simulated nested repo submodule failure")

            git_ops.checkpoint = broken_checkpoint

            # 1st failure
            res1 = await engine._checkpoint("attempt 1")
            assert res1 is None
            assert any(e.type == EventType.GIT_CHECKPOINT_FAILED.value for e in events_received)

            # 2nd failure (should trigger blocking_issue)
            res2 = await engine._checkpoint("attempt 2")
            assert res2 is None

            row = orch.db.get("missions", mission_id)
            assert "git checkpoint failed repeatedly" in (row.get("blocking_issue") or "")
        finally:
            git_ops.checkpoint = original_checkpoint

    asyncio.run(scenario())
