"""F-04 regression suite: human-gate resolution across backend restart does not deadlock or duplicate gates.

SAFE autonomy missions create an approval gate before IMPLEMENTATION.
Across a backend restart, resolving that gate must allow the mission to proceed
and complete without livelocking in a duplicate gate creation loop.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from conftest import make_orchestrator
from orchestrator.models import MissionStatus
from orchestrator.orchestrator import Orchestrator
from orchestrator.providers.fake import FakeAdapter


def _seed_project(orch: Orchestrator, workspace: Path) -> None:
    orch.db.insert(
        "projects",
        {"id": "p1", "name": "w", "path": str(workspace), "detected_type": "node", "created_at": "2024-01-01"},
    )


def test_f04_safe_mode_gate_resolution_after_restart(tmp_path: Path, workspace: Path) -> None:
    """A SAFE-autonomy mission hits the approval gate. The backend restarts.
    The user approves the gate post-restart. The mission must complete cleanly
    with exactly 1 gate created in total, never livelocking in a duplicate gate loop.
    """
    providers = {"fake-a": FakeAdapter("fake-a", ["ok"])}

    async def scenario() -> None:
        # 1. Start initial orchestrator
        orch1 = make_orchestrator(tmp_path, providers)
        _seed_project(orch1, workspace)

        mission = orch1.create_mission("p1", "safe mission", "task prompt", "SAFE", "balanced")
        mission_id = mission["id"]

        await orch1.start()
        orch1.start_mission(mission_id)

        # Wait until mission reaches WAITING_FOR_HUMAN
        gate_id = None
        for _ in range(60):
            row = orch1.db.get("missions", mission_id)
            if row["status"] == MissionStatus.WAITING_FOR_HUMAN.value:
                gates = orch1.db.query(
                    "SELECT id FROM human_gates WHERE mission_id=? AND status='open'",
                    (mission_id,),
                )
                if gates:
                    gate_id = gates[0]["id"]
                    break
            await asyncio.sleep(0.05)

        assert gate_id is not None, "Mission never created an open human gate!"
        assert orch1.db.get("missions", mission_id)["status"] == MissionStatus.WAITING_FOR_HUMAN.value

        # 2. Simulate backend restart: shutdown orch1 cleanly
        await orch1.shutdown()

        # 3. Spin up fresh orchestrator instance against the same database
        orch2 = make_orchestrator(tmp_path, providers)
        await orch2.start()

        # Verify post-restart state before resolution
        row_post_restart = orch2.db.get("missions", mission_id)
        assert row_post_restart["status"] == MissionStatus.WAITING_FOR_HUMAN.value

        # Exactly 1 gate before resolution
        all_gates = orch2.db.query("SELECT id, status FROM human_gates WHERE mission_id=?", (mission_id,))
        assert len(all_gates) == 1
        assert all_gates[0]["status"] == "open"

        # 4. User approves the gate
        orch2.resolve_gate(gate_id, "Approve")

        # 5. Mission must recover and proceed to COMPLETED
        for _ in range(80):
            st = orch2.db.get("missions", mission_id)["status"]
            if st == MissionStatus.COMPLETED.value:
                break
            await asyncio.sleep(0.05)

        final_status = orch2.db.get("missions", mission_id)["status"]
        assert final_status == MissionStatus.COMPLETED.value, f"Mission stuck in {final_status}!"

        # Verify exactly 1 gate exists and it is resolved
        final_gates = orch2.db.query("SELECT id, status, resolution FROM human_gates WHERE mission_id=?", (mission_id,))
        assert len(final_gates) == 1, f"Expected 1 gate, found duplicate gates: {final_gates}"
        assert final_gates[0]["status"] == "resolved"
        assert final_gates[0]["resolution"] == "Approve"

        await orch2.shutdown()

    asyncio.run(scenario())
