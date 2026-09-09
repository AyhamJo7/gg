"""Regression: resolve_gate must not hold _advance_lock across the (up to
120s) external validation command — that would stall advance_all()'s ~2s
scheduler tick for every other active project for the full validation span.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from test_lifecycle import make_orch, standard_adapters, start_planned_project


async def _open_action_gate(orch, project_id: str, validation: str) -> str:
    gate_id = uuid.uuid4().hex[:16]
    orch.db.insert(
        "project_gates",
        {
            "id": gate_id,
            "project_id": project_id,
            "gate_type": "action",
            "title": "run smoke check",
            "what_required": "confirm the smoke check passes",
            "why_required": "test fixture",
            "blocked_ref": "prereq:smoke",
            "completed_so_far": "project plan approved",
            "human_action": "click resolve",
            "where_to_provide": "",
            "validation": validation,
            "after_resolve": "blocked phases resume automatically",
            "required_vars": [],
            "status": "open",
            "created_at": "2026-01-01T00:00:00+00:00",
        },
    )
    return gate_id


@pytest.mark.asyncio
async def test_resolve_gate_releases_lock_during_external_validation(tmp_path: Path, monkeypatch):
    orch = await make_orch(tmp_path, standard_adapters())
    pid = await start_planned_project(tmp_path, orch)
    gate_id = await _open_action_gate(orch, pid, "run npm test")

    observed_locked_during_validation: list[bool] = []

    async def fake_run_gate_validation(self, project_id: str, command: str) -> bool:
        # The whole point of the fix: the coordinator-wide lock must be free
        # while this (stand-in for a slow subprocess) is in flight, so other
        # projects' advance_all ticks and other resolve_gate/pause/cancel
        # calls are never stalled behind one project's validation command.
        observed_locked_during_validation.append(orch.coordinator._advance_lock.locked())
        return True

    monkeypatch.setattr(type(orch.coordinator), "_run_gate_validation", fake_run_gate_validation, raising=True)

    result = await orch.coordinator.resolve_gate(pid, gate_id, "confirmed")

    assert result == {"ok": True, "gate_id": gate_id}
    assert observed_locked_during_validation == [False]
    await orch.shutdown()


@pytest.mark.asyncio
async def test_resolve_gate_rejects_stale_state_after_unlocked_validation(tmp_path: Path, monkeypatch):
    """If the gate is no longer open by the time validation finishes (e.g. a
    racing call already resolved it while the lock was released), the result
    must not be silently applied on top of that changed state."""
    orch = await make_orch(tmp_path, standard_adapters())
    pid = await start_planned_project(tmp_path, orch)
    gate_id = await _open_action_gate(orch, pid, "run npm test")

    async def fake_run_gate_validation_that_races(self, project_id: str, command: str) -> bool:
        # Simulate the gate having been resolved by someone else while this
        # validation was running unlocked.
        orch.db.update("project_gates", gate_id, {"status": "resolved", "resolution": "raced"})
        return True

    monkeypatch.setattr(
        type(orch.coordinator), "_run_gate_validation", fake_run_gate_validation_that_races, raising=True
    )

    result = await orch.coordinator.resolve_gate(pid, gate_id, "confirmed")

    assert result["ok"] is False
    assert "no longer open" in result["error"]
    await orch.shutdown()
