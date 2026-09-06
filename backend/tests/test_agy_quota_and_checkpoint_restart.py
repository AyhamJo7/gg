"""AGY quota classification and checkpoint failure durability across restart."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

from conftest import make_orchestrator
from orchestrator import git_ops
from orchestrator.models import FailureClass, MissionStatus
from orchestrator.providers.classify import classify_output
from orchestrator.providers.fake import FakeAdapter

# ===========================================================================
# AGY quota classification
# ===========================================================================


def test_agy_quota_exhausted_classified():
    """Real AGY output: 'Individual quota reached' → QUOTA_EXHAUSTED."""
    output = json.dumps({
        "event": "result",
        "result": {
            "status": "ERROR",
            "error": "Individual quota reached. Please upgrade your subscription to increase your limits. Resets in 142h10m35s.",
        },
    })
    result = classify_output(1, output, timed_out=False, cancelled=False)
    assert result == FailureClass.QUOTA_EXHAUSTED, f"Expected QUOTA_EXHAUSTED, got {result}"


def test_agy_quota_phrase_in_source_code_exit_zero_is_none():
    """Generated source mentioning quota with exit 0 must remain NONE."""
    source = 'def check():\n    if "Individual quota reached":\n        return "retry"'
    result = classify_output(0, source, timed_out=False, cancelled=False)
    assert result == FailureClass.NONE, f"Exit 0 misclassified as {result}"


# ===========================================================================
# Checkpoint failure durability across restart
# ===========================================================================


def test_checkpoint_failure_survives_restart(tmp_path: Path, workspace: Path):
    async def scenario():
        await git_ops.init_repo(workspace)
        providers = {"fake-a": FakeAdapter("fake-a", ["ok"])}
        orch = make_orchestrator(tmp_path, providers)
        orch.db.insert("projects", {
            "id": "p1", "name": "w", "path": str(workspace), "detected_type": "node", "created_at": "2024-01-01",
        })

        mission = orch.create_mission("p1", "restart-test", "prompt", "AUTONOMOUS", "balanced")
        mission_id = mission["id"]

        # Simulate one checkpoint failure persisted in DB
        orch.db.update("missions", mission_id, {"checkpoint_failures": 1})

        # Restart with fresh orchestrator (same DB)
        orch2 = make_orchestrator(tmp_path, providers)
        await orch2.start()

        # Second checkpoint failure should trigger exhaustion immediately
        with patch.object(git_ops, "checkpoint", side_effect=git_ops.GitError("disk full")):
            orch2.start_mission(mission_id)
            for _ in range(80):
                row = orch2.db.get("missions", mission_id)
                if row["status"] in ("COMPLETED", "FAILED", "UNVERIFIED"):
                    break
                await asyncio.sleep(0.05)

        row = orch2.db.get("missions", mission_id)
        assert row["status"] == MissionStatus.UNVERIFIED.value
        assert "git checkpoint failed repeatedly" in (row.get("blocking_issue") or "")
        await orch2.shutdown()

    asyncio.run(scenario())


def test_checkpoint_success_resets_failure_count(tmp_path: Path, workspace: Path):
    async def scenario():
        await git_ops.init_repo(workspace)
        providers = {"fake-a": FakeAdapter("fake-a", ["ok"])}
        orch = make_orchestrator(tmp_path, providers)
        orch.db.insert("projects", {
            "id": "p1", "name": "w", "path": str(workspace), "detected_type": "node", "created_at": "2024-01-01",
        })

        mission = orch.create_mission("p1", "reset-test", "prompt", "AUTONOMOUS", "balanced")
        mission_id = mission["id"]

        # Pre-seed one failure
        orch.db.update("missions", mission_id, {"checkpoint_failures": 1})

        # Start and complete mission (checkpoints should succeed and reset count)
        await orch.start()
        orch.start_mission(mission_id)
        for _ in range(80):
            row = orch.db.get("missions", mission_id)
            if row["status"] in ("COMPLETED", "FAILED", "UNVERIFIED"):
                break
            await asyncio.sleep(0.05)

        row = orch.db.get("missions", mission_id)
        # After successful checkpoints, count should be reset to 0
        assert row.get("checkpoint_failures", 0) == 0
        await orch.shutdown()

    asyncio.run(scenario())
