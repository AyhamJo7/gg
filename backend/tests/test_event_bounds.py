"""F-004 regression suite: bounded event persistence.

Invariant: a noisy provider emitting unlimited stdout must not create
unlimited SQLite growth. Structured lifecycle events stay durable;
PROVIDER_OUTPUT is transient (bounded in-memory replay + live broadcast).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from conftest import make_orchestrator
from orchestrator.events import TRANSIENT_REPLAY_LIMIT, EventBus
from orchestrator.models import EventType, MissionStatus
from orchestrator.providers.fake import FakeAdapter

ALL_ROLES = ("planning", "implementation", "testing", "review", "repair")


def test_provider_output_never_persisted(db):  # type: ignore[no-untyped-def]
    bus = EventBus(db)
    for i in range(100_000):
        bus.publish(EventType.PROVIDER_OUTPUT, "m1", provider="noisy", line=f"line {i}")
    bus.publish(EventType.MISSION_CREATED, "m1", title="t")
    bus.publish(EventType.PHASE_STARTED, "m1", phase="PLANNING")

    rows = db.query("SELECT COUNT(*) AS n FROM events WHERE mission_id='m1'")
    assert rows[0]["n"] == 2, f"structured events must persist; output must not: {rows}"
    types = {r["type"] for r in db.query("SELECT DISTINCT type FROM events WHERE mission_id='m1'")}
    assert "PROVIDER_OUTPUT" not in types
    # transient replay buffer is bounded
    assert len(bus.transient_replay("m1")) == TRANSIENT_REPLAY_LIMIT
    # structured history still replayable
    history = bus.history("m1")
    assert {h["type"] for h in history} == {"MISSION_CREATED", "PHASE_STARTED"}


def test_transient_still_broadcasts_live(db):  # type: ignore[no-untyped-def]
    async def main() -> None:
        bus = EventBus(db)
        queue = bus.subscribe()
        bus.publish(EventType.PROVIDER_OUTPUT, "m1", provider="p", line="hello")
        event = await asyncio.wait_for(queue.get(), timeout=1)
        assert event.type == EventType.PROVIDER_OUTPUT
        assert event.payload["line"] == "hello"
        bus.unsubscribe(queue)

    asyncio.run(main())


def test_sqlite_file_growth_bounded_under_flood(tmp_path: Path):
    """Direct flood at the bus level: DB file size stays flat."""

    async def main() -> None:
        orch = make_orchestrator(tmp_path, {"fake-a": FakeAdapter("fake-a", ["ok"])})
        db = orch.db
        bus = orch.events
        db_path = tmp_path / "orch.db"
        bus.publish(EventType.MISSION_CREATED, "m1", title="t")
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        size_before = os.path.getsize(db_path)
        for i in range(200_000):
            bus.publish(EventType.PROVIDER_OUTPUT, "m1", provider="noisy", line=f"flood {i} " + "x" * 100)
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        size_after = os.path.getsize(db_path)
        growth = size_after - size_before
        assert growth < 1_000_000, f"DB grew by {growth} bytes under 200k-line flood"
        assert db.query("SELECT COUNT(*) AS n FROM events")[0]["n"] == 1
        await orch.shutdown()

    asyncio.run(main())


def test_mission_with_flooding_provider_completes_and_stays_bounded(tmp_path: Path, workspace: Path):
    """End-to-end: a provider flooding stdout during a real mission phase
    leaves the events table containing only structured rows."""

    async def main() -> None:
        flood = FakeAdapter("fake-a", ["ok"])
        flood.flood_lines = 50_000
        orch = make_orchestrator(tmp_path, {"fake-a": flood})
        await orch.registry.detect_all()
        orch.db.insert(
            "projects",
            {"id": "p1", "name": "w", "path": str(workspace), "detected_type": "node", "created_at": "2024-01-01"},
        )
        mission = orch.create_mission("p1", "m", "t", "AUTONOMOUS", "balanced")
        orch.start_mission(mission["id"])
        await asyncio.wait_for(orch._engine_tasks[mission["id"]], timeout=120)
        final = orch.db.get("missions", mission["id"])
        assert final["status"] == MissionStatus.COMPLETED.value, final.get("blocking_issue")
        # 4 roles × 50k flood lines happened, but no output row was persisted
        output_rows = orch.db.query("SELECT COUNT(*) AS n FROM events WHERE type='PROVIDER_OUTPUT'")
        assert output_rows[0]["n"] == 0
        # structured history is complete and replayable
        types = {r["type"] for r in orch.db.query("SELECT DISTINCT type FROM events WHERE mission_id=?", (mission["id"],))}
        assert "MISSION_COMPLETED" in types
        assert "PHASE_COMPLETED" in types
        # transient replay holds only the bounded tail
        assert len(orch.events.transient_replay(mission["id"])) <= TRANSIENT_REPLAY_LIMIT
        await orch.shutdown()

    asyncio.run(main())
