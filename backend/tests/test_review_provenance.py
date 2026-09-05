"""F-003 regression suite: review provenance and self-review disclosure."""

from __future__ import annotations

import asyncio
from pathlib import Path

from orchestrator.models import MissionStatus
from orchestrator.providers.fake import FakeAdapter

from conftest import make_config, make_orchestrator

ALL_ROLES = ("planning", "implementation", "testing", "review", "repair")


def _seed(orch, workspace: Path) -> None:  # type: ignore[no-untyped-def]
    orch.db.insert(
        "projects",
        {"id": "p1", "name": "w", "path": str(workspace), "detected_type": "node", "created_at": "2024-01-01"},
    )


async def _run(orch, mission_id: str) -> None:  # type: ignore[no-untyped-def]
    orch.start_mission(mission_id)
    await asyncio.wait_for(orch._engine_tasks[mission_id], timeout=60)


def test_independent_review_recorded_when_two_providers(tmp_path: Path, workspace: Path):
    async def main() -> None:
        names = ["fake-a", "fake-b"]
        adapters = {n: FakeAdapter(n, ["ok"]) for n in names}
        config = make_config(priority={r: names for r in ALL_ROLES}, providers=names)
        orch = make_orchestrator(tmp_path, adapters, config)
        await orch.registry.detect_all()
        _seed(orch, workspace)
        mission = orch.create_mission("p1", "m", "t", "AUTONOMOUS", "balanced")
        await _run(orch, mission["id"])
        assert orch.db.get("missions", mission["id"])["status"] == MissionStatus.COMPLETED.value
        reviews = orch.db.query("SELECT * FROM reviews WHERE mission_id=?", (mission["id"],))
        assert reviews, "no review provenance persisted"
        latest = reviews[-1]
        assert latest["independent"] == 1
        assert latest["implementation_provider"] != latest["review_provider"]
        assert latest["degradation_reason"] is None
        await orch.shutdown()

    asyncio.run(main())


def test_self_review_recorded_and_disclosed_when_single_provider(tmp_path: Path, workspace: Path):
    async def main() -> None:
        names = ["fake-a"]
        adapters = {n: FakeAdapter(n, ["ok"]) for n in names}
        config = make_config(priority={r: names for r in ALL_ROLES}, providers=names)
        orch = make_orchestrator(tmp_path, adapters, config)
        await orch.registry.detect_all()
        _seed(orch, workspace)
        mission = orch.create_mission("p1", "m", "t", "AUTONOMOUS", "balanced")
        await _run(orch, mission["id"])
        # V1 semantics: self-review may complete, but degraded review is recorded
        assert orch.db.get("missions", mission["id"])["status"] == MissionStatus.COMPLETED.value
        reviews = orch.db.query("SELECT * FROM reviews WHERE mission_id=?", (mission["id"],))
        assert reviews
        latest = reviews[-1]
        assert latest["independent"] == 0
        assert latest["implementation_provider"] == "fake-a"
        assert latest["review_provider"] == "fake-a"
        assert "self-review" in latest["degradation_reason"]
        # the provenance event was emitted for the UI
        events = orch.db.query(
            "SELECT payload FROM events WHERE mission_id=? AND type='REVIEW_RECORDED'", (mission["id"],)
        )
        assert events
        import json

        payload = json.loads(events[-1]["payload"])
        assert payload["independent"] is False
        await orch.shutdown()

    asyncio.run(main())
