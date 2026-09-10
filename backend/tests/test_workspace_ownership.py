"""F-007 regression suite: same-workspace mission exclusion.

Durable ownership derived from the missions table: two missions must never
mutate the same canonical workspace concurrently. Queued missions launch
when ownership frees; ownership reconciles correctly across restarts.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from conftest import make_config, make_orchestrator
from orchestrator.providers.fake import FakeAdapter

ALL_ROLES = ("planning", "implementation", "testing", "review", "repair")


def _seed(orch, workspace: Path, pid: str = "p1") -> None:  # type: ignore[no-untyped-def]
    orch.db.insert(
        "projects",
        {"id": pid, "name": pid, "path": str(workspace), "detected_type": "node", "created_at": "2024-01-01"},
    )


async def _wait_status(orch, mission_id: str, want: set[str], timeout: float = 10) -> str:  # type: ignore[no-untyped-def]
    for _ in range(int(timeout / 0.05)):
        status = orch.db.get("missions", mission_id)["status"]
        if status in want:
            return status
        await asyncio.sleep(0.05)
    raise AssertionError(f"mission {mission_id} never reached {want}; last={status}")


def test_second_mission_same_workspace_queued_not_concurrent(tmp_path: Path, workspace: Path):
    async def main() -> None:
        # fake-a blocks on first use (mission A planning), succeeds after
        slow_then_ok = FakeAdapter("fake-a", ["slow", "ok"])
        config = make_config(priority={r: ["fake-a"] for r in ALL_ROLES}, providers=["fake-a"])
        orch = make_orchestrator(tmp_path, {"fake-a": slow_then_ok}, config)
        await orch.registry.detect_all()
        await orch.start()  # scheduler loop runs (tick 0.05s)
        _seed(orch, workspace)

        mission_a = orch.create_mission("p1", "A", "t", "AUTONOMOUS", "balanced")
        orch.start_mission(mission_a["id"])
        await _wait_status(orch, mission_a["id"], {"PLANNING"})

        mission_b = orch.create_mission("p1", "B", "t", "AUTONOMOUS", "balanced")
        orch.start_mission(mission_b["id"])
        # B must be queued, never launched while A owns the workspace
        await _wait_status(orch, mission_b["id"], {"WAITING_FOR_WORKSPACE"})
        await asyncio.sleep(0.3)
        assert mission_b["id"] not in orch._engines
        assert slow_then_ok.calls == 1  # only A is running

        # cancel A → ownership frees → scheduler launches B → completes
        orch.cancel_mission(mission_a["id"])
        await _wait_status(orch, mission_a["id"], {"CANCELLED"})
        await _wait_status(orch, mission_b["id"], {"COMPLETED"}, timeout=30)
        # B did real work via the provider
        runs = orch.db.query("SELECT * FROM provider_runs WHERE mission_id=?", (mission_b["id"],))
        assert runs
        await orch.shutdown()

    asyncio.run(main())


def test_queued_missions_do_not_deadlock_each_other(tmp_path: Path, workspace: Path):
    async def main() -> None:
        slow_then_ok = FakeAdapter("fake-a", ["slow", "ok"])
        config = make_config(priority={r: ["fake-a"] for r in ALL_ROLES}, providers=["fake-a"])
        orch = make_orchestrator(tmp_path, {"fake-a": slow_then_ok}, config)
        await orch.registry.detect_all()
        await orch.start()
        _seed(orch, workspace)

        a = orch.create_mission("p1", "A", "t", "AUTONOMOUS", "balanced")
        orch.start_mission(a["id"])
        await _wait_status(orch, a["id"], {"PLANNING"})
        b = orch.create_mission("p1", "B", "t", "AUTONOMOUS", "balanced")
        c = orch.create_mission("p1", "C", "t", "AUTONOMOUS", "balanced")
        orch.start_mission(b["id"])
        orch.start_mission(c["id"])
        await _wait_status(orch, b["id"], {"WAITING_FOR_WORKSPACE"})
        await _wait_status(orch, c["id"], {"WAITING_FOR_WORKSPACE"})

        orch.cancel_mission(a["id"])
        # B launches (FIFO), C still queued — no deadlock between queued missions
        await _wait_status(orch, b["id"], {"COMPLETED"}, timeout=30)
        await _wait_status(orch, c["id"], {"COMPLETED"}, timeout=30)
        await orch.shutdown()

    asyncio.run(main())


def test_ownership_survives_restart(tmp_path: Path, workspace: Path):
    async def main() -> None:
        slow = FakeAdapter("fake-a", ["slow"])
        config = make_config(priority={r: ["fake-a"] for r in ALL_ROLES}, providers=["fake-a"])
        orch = make_orchestrator(tmp_path, {"fake-a": slow}, config)
        await orch.registry.detect_all()
        await orch.start()
        _seed(orch, workspace)
        a = orch.create_mission("p1", "A", "t", "AUTONOMOUS", "balanced")
        orch.start_mission(a["id"])
        await _wait_status(orch, a["id"], {"PLANNING"})
        b = orch.create_mission("p1", "B", "t", "AUTONOMOUS", "balanced")
        orch.start_mission(b["id"])
        await _wait_status(orch, b["id"], {"WAITING_FOR_WORKSPACE"})

        # hard crash: stop the original scheduler first so only the restarted
        # instance drives recovery (no dual-scheduler race on the same rows)
        orch._engine_tasks[a["id"]].cancel()
        try:
            await orch._engine_tasks[a["id"]]
        except asyncio.CancelledError:
            pass
        await orch.shutdown()

        # restart: A recovers and owns; B must remain queued (no double writer)
        orch2 = make_orchestrator(tmp_path, {"fake-a": FakeAdapter("fake-a", ["ok"])}, config)
        await orch2.start()
        for _ in range(int(10 / 0.05)):
            if a["id"] in orch2._engines:
                break
            await asyncio.sleep(0.05)
        assert a["id"] in orch2._engines, "active mission A was not recovered"
        assert b["id"] not in orch2._engines or orch2.db.get("missions", b["id"])["status"] == "WAITING_FOR_WORKSPACE"
        # A completes → B releases
        await _wait_status(orch2, a["id"], {"COMPLETED"}, timeout=30)
        await _wait_status(orch2, b["id"], {"COMPLETED"}, timeout=30)
        await orch2.shutdown()

    asyncio.run(main())


def test_different_workspaces_run_concurrently(tmp_path: Path, workspace: Path):
    async def main() -> None:
        ws2 = tmp_path / "ws2"
        ws2.mkdir()
        (ws2 / "package.json").write_text('{"name":"y","scripts":{"test":"node -e 0","build":"node -e 0"}}')
        adapters = {"fake-a": FakeAdapter("fake-a", ["ok"]), "fake-b": FakeAdapter("fake-b", ["ok"])}
        config = make_config(
            priority={r: ["fake-a", "fake-b"] for r in ALL_ROLES},
            providers=["fake-a", "fake-b"],
        )
        orch = make_orchestrator(tmp_path, adapters, config)
        await orch.registry.detect_all()
        await orch.start()
        _seed(orch, workspace, "p1")
        _seed(orch, ws2, "p2")
        a = orch.create_mission("p1", "A", "t", "AUTONOMOUS", "balanced")
        b = orch.create_mission("p2", "B", "t", "AUTONOMOUS", "balanced")
        orch.start_mission(a["id"])
        orch.start_mission(b["id"])
        # both must launch (different workspaces); B must NOT be queued
        await asyncio.sleep(0.3)
        assert a["id"] in orch._engines
        assert b["id"] in orch._engines
        await _wait_status(orch, a["id"], {"COMPLETED"}, timeout=30)
        await _wait_status(orch, b["id"], {"COMPLETED"}, timeout=30)
        await orch.shutdown()

    asyncio.run(main())


def test_terminal_mission_releases_workspace(tmp_path: Path, workspace: Path):
    async def main() -> None:
        # A fails terminally (4 ratelimit attempts exhaust) → workspace frees for B
        flaky = FakeAdapter("fake-a", ["ratelimit"] * 6 + ["ok"])
        config = make_config(priority={r: ["fake-a"] for r in ALL_ROLES}, providers=["fake-a"])
        orch = make_orchestrator(tmp_path, {"fake-a": flaky}, config)
        await orch.registry.detect_all()
        await orch.start()
        _seed(orch, workspace)
        a = orch.create_mission("p1", "A", "t", "AUTONOMOUS", "balanced")
        orch.start_mission(a["id"])
        await _wait_status(orch, a["id"], {"FAILED"}, timeout=30)
        b = orch.create_mission("p1", "B", "t", "AUTONOMOUS", "balanced")
        orch.start_mission(b["id"])
        # B must launch immediately — FAILED mission owns nothing
        await _wait_status(orch, b["id"], {"COMPLETED"}, timeout=30)
        await orch.shutdown()

    asyncio.run(main())
