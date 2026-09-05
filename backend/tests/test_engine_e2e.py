"""End-to-end engine tests with fake providers: full lifecycle, failover,
pause/resume, review/repair loop, and backend-restart recovery."""

from __future__ import annotations

import asyncio
from pathlib import Path

from conftest import make_config, make_orchestrator
from orchestrator.models import MissionStatus
from orchestrator.orchestrator import Orchestrator
from orchestrator.providers.fake import FakeAdapter


async def run_mission(orch: Orchestrator, mission_id: str, timeout: float = 30) -> None:
    orch.start_mission(mission_id)
    task = orch._engine_tasks[mission_id]
    await asyncio.wait_for(task, timeout=timeout)


def test_full_lifecycle_completes(tmp_path: Path, workspace: Path):
    async def main() -> None:
        orch = make_orchestrator(tmp_path, {
            "fake-a": FakeAdapter("fake-a", ["ok"]),
            "fake-b": FakeAdapter("fake-b", ["ok"]),
        })
        await orch.registry.detect_all()
        orch.db.insert("projects", {"id": "p1", "name": "w", "path": str(workspace), "detected_type": "node", "created_at": "2024-01-01"})
        mission = orch.create_mission("p1", "Build thing", "Create a file and verify", "AUTONOMOUS", "balanced")
        await run_mission(orch, mission["id"])
        final = orch.db.get("missions", mission["id"])
        assert final["status"] == MissionStatus.COMPLETED.value, final.get("blocking_issue")
        # git checkpoints exist and the workspace has real commits
        checkpoints = orch.db.query("SELECT * FROM checkpoints WHERE mission_id=?", (mission["id"],))
        assert checkpoints
        # handoffs were created and persisted to disk
        handoffs = orch.db.query("SELECT * FROM handoffs WHERE mission_id=?", (mission["id"],))
        assert handoffs
        assert Path(handoffs[0]["path"]).exists()
        # events persisted
        events = orch.db.query("SELECT * FROM events WHERE mission_id=? AND type='MISSION_COMPLETED'", (mission["id"],))
        assert events
        await orch.shutdown()

    asyncio.run(main())


def test_failover_rate_limit_to_next_provider(tmp_path: Path, workspace: Path):
    async def main() -> None:
        flaky = FakeAdapter("fake-a", ["ratelimit", "ok"])  # rate-limited on first planning attempt... but cooldown may expire
        worker = FakeAdapter("fake-b", ["ok"])
        orch = make_orchestrator(tmp_path, {"fake-a": flaky, "fake-b": worker})
        await orch.registry.detect_all()
        orch.db.insert("projects", {"id": "p1", "name": "w", "path": str(workspace), "detected_type": "node", "created_at": "2024-01-01"})
        mission = orch.create_mission("p1", "m", "t", "AUTONOMOUS", "balanced")
        await run_mission(orch, mission["id"])
        final = orch.db.get("missions", mission["id"])
        assert final["status"] == MissionStatus.COMPLETED.value
        # rate limit was recorded with cooldown
        row = orch.db.get("providers", "fake-a", key="name")
        assert row["rate_limit_events"] >= 1
        # a PROVIDER_RATE_LIMITED event exists
        rl = orch.db.query("SELECT * FROM events WHERE mission_id=? AND type='PROVIDER_RATE_LIMITED'", (mission["id"],))
        assert rl
        await orch.shutdown()

    asyncio.run(main())


def test_separation_of_duties_review_differs_from_implementer(tmp_path: Path, workspace: Path):
    async def main() -> None:
        a = FakeAdapter("fake-a", ["ok"])
        b = FakeAdapter("fake-b", ["ok"])
        # implementation priority puts fake-a first; review priority also fake-a first
        config = make_config(
            priority={
                "planning": ["fake-a", "fake-b"],
                "implementation": ["fake-a", "fake-b"],
                "testing": ["fake-a", "fake-b"],
                "review": ["fake-a", "fake-b"],
                "repair": ["fake-a", "fake-b"],
            },
            providers=["fake-a", "fake-b"],
        )
        orch = make_orchestrator(tmp_path, {"fake-a": a, "fake-b": b}, config)
        await orch.registry.detect_all()
        orch.db.insert("projects", {"id": "p1", "name": "w", "path": str(workspace), "detected_type": "node", "created_at": "2024-01-01"})
        mission = orch.create_mission("p1", "m", "t", "AUTONOMOUS", "balanced")
        await run_mission(orch, mission["id"])
        runs = orch.db.query(
            "SELECT role, provider FROM provider_runs WHERE mission_id=? AND failure_class='NONE'", (mission["id"],)
        )
        impl = next(r for r in runs if r["role"] == "implementation")
        review = next(r for r in runs if r["role"] == "review")
        assert impl["provider"] != review["provider"], f"separation violated: {runs}"
        await orch.shutdown()

    asyncio.run(main())


def test_pause_and_resume(tmp_path: Path, workspace: Path):
    async def main() -> None:
        slow = FakeAdapter("fake-a", ["slow"])
        ok = FakeAdapter("fake-b", ["ok"])
        config = make_config(
            priority={r: ["fake-a", "fake-b"] for r in ("planning", "implementation", "testing", "review", "repair")},
            providers=["fake-a", "fake-b"],
        )
        orch = make_orchestrator(tmp_path, {"fake-a": slow, "fake-b": ok}, config)
        await orch.registry.detect_all()
        orch.db.insert("projects", {"id": "p1", "name": "w", "path": str(workspace), "detected_type": "node", "created_at": "2024-01-01"})
        mission = orch.create_mission("p1", "m", "t", "AUTONOMOUS", "balanced")
        orch.start_mission(mission["id"])
        # wait until planning actually starts with the slow provider
        for _ in range(100):
            row = orch.db.get("missions", mission["id"])
            if row["current_provider"] == "fake-a":
                break
            await asyncio.sleep(0.02)
        orch.pause_mission(mission["id"])
        task = orch._engine_tasks[mission["id"]]
        await asyncio.wait_for(task, timeout=10)
        assert orch.db.get("missions", mission["id"])["status"] == MissionStatus.PAUSED.value
        await orch.shutdown()

        # resume with a fresh orchestrator whose fake-a no longer stalls
        adapters = {"fake-a": FakeAdapter("fake-a", ["ok"]), "fake-b": FakeAdapter("fake-b", ["ok"])}
        orch2 = make_orchestrator(tmp_path, adapters, config)
        await orch2.registry.detect_all()
        orch2.resume_mission(mission["id"])
        task2 = orch2._engine_tasks[mission["id"]]
        await asyncio.wait_for(task2, timeout=30)
        assert orch2.db.get("missions", mission["id"])["status"] == MissionStatus.COMPLETED.value
        await orch2.shutdown()

    asyncio.run(main())


def test_recovery_after_backend_crash_mid_flight(tmp_path: Path, workspace: Path):
    async def main() -> None:
        slow = FakeAdapter("fake-a", ["slow"])
        config = make_config(
            priority={r: ["fake-a"] for r in ("planning", "implementation", "testing", "review", "repair")},
            providers=["fake-a"],
        )
        orch = make_orchestrator(tmp_path, {"fake-a": slow}, config)
        await orch.registry.detect_all()
        orch.db.insert("projects", {"id": "p1", "name": "w", "path": str(workspace), "detected_type": "node", "created_at": "2024-01-01"})
        mission = orch.create_mission("p1", "m", "t", "AUTONOMOUS", "balanced")
        orch.start_mission(mission["id"])
        for _ in range(100):
            if orch.db.get("missions", mission["id"])["current_provider"] == "fake-a":
                break
            await asyncio.sleep(0.02)
        # simulate hard backend crash: cancel engine task without cleanup
        orch._engine_tasks[mission["id"]].cancel()
        try:
            await orch._engine_tasks[mission["id"]]
        except asyncio.CancelledError:
            pass
        # provider is left BUSY in the DB — recovery must reset it
        assert orch.db.get("providers", "fake-a", key="name")["state"] == "BUSY"

        orch2 = make_orchestrator(tmp_path, {"fake-a": FakeAdapter("fake-a", ["ok"])}, config)
        await orch2.start()  # recovery path
        assert orch2.db.get("providers", "fake-a", key="name")["state"] == "AVAILABLE"
        assert orch2.db.get("missions", mission["id"])["status"] in (
            MissionStatus.RECOVERING.value,
            MissionStatus.PLANNING.value,
        )
        task = orch2._engine_tasks[mission["id"]]
        await asyncio.wait_for(task, timeout=30)
        assert orch2.db.get("missions", mission["id"])["status"] == MissionStatus.COMPLETED.value
        await orch2.shutdown()

    asyncio.run(main())


def test_unverified_when_no_toolchain(tmp_path: Path):
    async def main() -> None:
        ws = tmp_path / "empty-ws"
        ws.mkdir()
        orch = make_orchestrator(tmp_path, {"fake-a": FakeAdapter("fake-a", ["ok"])})
        await orch.registry.detect_all()
        orch.db.insert("projects", {"id": "p1", "name": "w", "path": str(ws), "detected_type": "unknown", "created_at": "2024-01-01"})
        mission = orch.create_mission("p1", "m", "t", "AUTONOMOUS", "balanced")
        await run_mission(orch, mission["id"])
        final = orch.db.get("missions", mission["id"])
        # no test/build commands detectable → must not claim COMPLETED
        assert final["status"] == MissionStatus.UNVERIFIED.value
        await orch.shutdown()

    asyncio.run(main())
