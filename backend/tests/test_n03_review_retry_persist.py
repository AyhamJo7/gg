"""N-03 regression suite: Unparseable review retry count is persisted."""

import asyncio
from pathlib import Path

from conftest import make_orchestrator
from orchestrator.engine import MissionEngine
from orchestrator.models import MissionStatus
from orchestrator.providers.fake import FakeAdapter


def _seed_project(orch, workspace: Path) -> None:
    orch.db.insert(
        "projects",
        {"id": "p1", "name": "w", "path": str(workspace), "detected_type": "node", "created_at": "2024-01-01"},
    )


def test_unparseable_count_survives_restart_simulation(tmp_path: Path, workspace: Path):
    """Verify that _unparseable_review_count fetches the correct count from DB."""
    orch = make_orchestrator(tmp_path, {})
    _seed_project(orch, workspace)
    
    mission = orch.create_mission("p1", "test", "prompt", "AUTONOMOUS", "balanced")
    mission_id = mission["id"]

    # Insert a few review records with review_parsed=0
    for i in range(3):
        orch.db.insert(
            "reviews",
            {
                "id": f"rev_{i}",
                "mission_id": mission_id,
                "review_provider": "fake",
                "created_at": "2024-01-01",
                "review_parsed": 0,
            }
        )
    # Insert a parseable review just in case
    orch.db.insert(
        "reviews",
        {
            "id": "rev_3",
            "mission_id": mission_id,
            "review_provider": "fake",
            "created_at": "2024-01-01",
            "review_parsed": 1,
        }
    )

    engine = MissionEngine(mission_id, orch.db, orch.events, orch.registry, orch.config, orch.locks)

    count = engine._unparseable_review_count()
    assert count == 3


def test_unparseable_ceiling_applies_across_restarts(tmp_path: Path, workspace: Path):
    """Verify that after restarting, the engine uses the DB count for retry ceiling."""
    class ProseReviewer(FakeAdapter):
        async def execute(self, request, on_output):
            on_output("Looks great! No bugs found, tests pass.")
            from orchestrator.models import FailureClass, ProviderState
            from orchestrator.providers.base import ExecutionResult
            return ExecutionResult(
                state=ProviderState.COMPLETED,
                failure_class=FailureClass.NONE,
                exit_code=0,
                duration_s=0.05,
                summary="Looks great! No bugs found, tests pass.",
                raw_tail="Looks great! No bugs found, tests pass.",
            )

    providers = {
        "fake-impl": FakeAdapter("fake-impl", ["ok"]),
        "fake-prose-reviewer": ProseReviewer("fake-prose-reviewer"),
    }

    async def scenario():
        orch = make_orchestrator(tmp_path, providers)
        _seed_project(orch, workspace)

        mission = orch.create_mission("p1", "test unparseable", "prompt", "AUTONOMOUS", "balanced")
        mission_id = mission["id"]

        # Insert 1 unparseable review record
        orch.db.insert(
            "reviews",
            {
                "id": "rev_manual_1",
                "mission_id": mission_id,
                "review_provider": "fake",
                "created_at": "2024-01-01",
                "review_parsed": 0,
            }
        )

        # Set max attempts to 2 via config update
        orch.config._data["orchestration"] = orch.config._data.get("orchestration", {})
        orch.config._data["orchestration"]["max_unparseable_review_attempts"] = 2
        
        # We simulate restart by just using the orchestrator with the pre-inserted record
        await orch.start()
        # Since mission status is INITIAL, start_mission will move it to CODING then REVIEWING
        # But maybe we should just start the engine review loop directly or let it run
        orch.start_mission(mission_id)

        # Wait for terminal state
        for _ in range(60):
            st = orch.db.get("missions", mission_id)["status"]
            if st in ("UNVERIFIED", "COMPLETED", "FAILED"):
                break
            await asyncio.sleep(0.05)

        final_mission = orch.db.get("missions", mission_id)
        
        # After ONE more unparseable review (since we mocked ProseReviewer),
        # total count becomes 2 (DB has 1 from manual insert + 1 from run)
        # It should hit the ceiling and become UNVERIFIED.
        assert final_mission["status"] == MissionStatus.UNVERIFIED.value
        
        reviews = orch.db.query("SELECT * FROM reviews WHERE mission_id=?", (mission_id,))
        # 1 from our manual insert, 1 from the actual engine run = 2
        assert len([r for r in reviews if r["review_parsed"] == 0]) == 2
        
        await orch.shutdown()

    asyncio.run(scenario())
