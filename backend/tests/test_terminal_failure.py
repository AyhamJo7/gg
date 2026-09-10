"""F-001 regression suite: terminal-state durability.

Invariant: a terminal event always corresponds to a durable terminal mission
state. Recovery must never resurrect a terminal mission, and restarts after
exhaustion must never re-spend provider quota.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from conftest import make_config, make_orchestrator
from orchestrator.models import MissionStatus
from orchestrator.orchestrator import IllegalMissionTransitionError, Orchestrator
from orchestrator.providers.fake import FakeAdapter

ALL_ROLES = ("planning", "implementation", "testing", "review", "repair")


def _seed_project(orch: Orchestrator, workspace: Path) -> None:
    orch.db.insert(
        "projects",
        {"id": "p1", "name": "w", "path": str(workspace), "detected_type": "node", "created_at": "2024-01-01"},
    )


def _failing_orch(tmp_path: Path, behavior: str = "ratelimit", providers: list[str] | None = None) -> Orchestrator:
    names = providers or ["fake-a"]
    adapters = {n: FakeAdapter(n, [behavior]) for n in names}
    config = make_config(
        priority={r: names for r in ALL_ROLES},
        providers=names,
    )
    return make_orchestrator(tmp_path, adapters, config)


async def _run(orch: Orchestrator, mission_id: str, timeout: float = 60) -> None:
    orch.start_mission(mission_id)
    await asyncio.wait_for(orch._engine_tasks[mission_id], timeout=timeout)


def test_exhaustion_persists_failed_not_active(tmp_path: Path, workspace: Path):
    """All providers fail at PLANNING → durable FAILED, truthful blocking_issue."""

    async def main() -> None:
        orch = _failing_orch(tmp_path)
        await orch.registry.detect_all()
        _seed_project(orch, workspace)
        mission = orch.create_mission("p1", "m", "t", "AUTONOMOUS", "balanced")
        await _run(orch, mission["id"])
        final = orch.db.get("missions", mission["id"])
        assert final["status"] == MissionStatus.FAILED.value
        assert final["blocking_issue"]
        assert "exhausted" in final["blocking_issue"]
        assert final["finished_at"] is not None
        # terminal event exists
        events = orch.db.query("SELECT * FROM events WHERE mission_id=? AND type='MISSION_FAILED'", (mission["id"],))
        assert events
        await orch.shutdown()

    asyncio.run(main())


def test_gate_refused_failure_does_not_penalize_provider(tmp_path: Path, workspace: Path):
    """A spawn-handshake refusal is orchestrator-internal (identity persistence
    failed before exec, see _spawn_gate.py) — not evidence the provider itself
    is unhealthy. It must not cost the provider a reliability cooldown the way
    a genuine crash/timeout does, unlike an ordinary crash (contrasted below)."""

    async def main() -> None:
        orch = _failing_orch(tmp_path, behavior="gate_refused")
        await orch.registry.detect_all()
        _seed_project(orch, workspace)
        mission = orch.create_mission("p1", "m", "t", "AUTONOMOUS", "balanced")
        await _run(orch, mission["id"])
        final = orch.db.get("missions", mission["id"])
        assert final["status"] == MissionStatus.FAILED.value  # still fails the mission — no free pass on the task

        row = orch.db.get("providers", "fake-a", key="name")
        assert row["consecutive_failures"] == 0
        # A short fixed cooldown is applied (distinct from the exponential
        # reliability cooldown below) — not zero, but bounded and unrelated
        # to failure count, so it never compounds like a real penalty would.
        assert row["cooldown_until"] is not None
        assert row["state"] != "CRASHED"
        await orch.shutdown()

    asyncio.run(main())


def test_gate_refused_cooldown_is_short_and_expires(tmp_path: Path, workspace: Path):
    """The gate_refused exemption still applies a small fixed cooldown (not
    the exponential reliability one) so a persistent internal fault can't
    cause an immediate zero-delay respawn loop. Verify it directly against
    the registry: not eligible right after, eligible again once it elapses."""

    async def main() -> None:
        orch = _failing_orch(tmp_path, behavior="gate_refused")
        await orch.registry.detect_all()
        orch.registry.clear_busy_without_penalty("fake-a")
        assert orch.registry.is_eligible("fake-a") is False
        await asyncio.sleep(0.1)  # > the 0.05s test-config gate_refused_cooldown_seconds
        assert orch.registry.is_eligible("fake-a") is True
        # is_eligible's lazy reconcile clears the now-expired cooldown_until
        # out of the DB too (not just eligibility-correct) — otherwise
        # health()'s dashboard payload shows a perpetually-stale timestamp.
        row = orch.db.get("providers", "fake-a", key="name")
        assert row["cooldown_until"] is None
        await orch.shutdown()

    asyncio.run(main())


def test_ordinary_crash_still_penalizes_provider(tmp_path: Path, workspace: Path):
    """Contrast case for the gate_refused exemption above: an ordinary crash
    (gate_refused=False) must still record a real reliability penalty."""

    async def main() -> None:
        orch = _failing_orch(tmp_path, behavior="crash")
        await orch.registry.detect_all()
        _seed_project(orch, workspace)
        mission = orch.create_mission("p1", "m", "t", "AUTONOMOUS", "balanced")
        await _run(orch, mission["id"])

        row = orch.db.get("providers", "fake-a", key="name")
        assert row["consecutive_failures"] > 0
        assert row["cooldown_until"] is not None
        await orch.shutdown()

    asyncio.run(main())


def test_exhaustion_at_every_provider_phase(tmp_path: Path):
    """Exhaustion must persist FAILED regardless of which phase it occurs in."""

    async def phase_case(phase_marker: str, tmp: Path) -> None:
        ws = tmp / f"ws-{phase_marker}"
        ws.mkdir()
        if phase_marker != "planning":
            # toolchain so ANALYZING and later phases work
            (ws / "package.json").write_text('{"name":"x","scripts":{"test":"node -e 0","build":"node -e 0"}}')
        names = ["fake-good", "fake-bad"]
        adapters = {
            "fake-good": FakeAdapter("fake-good", ["ok"]),
            "fake-bad": FakeAdapter("fake-bad", ["ratelimit"]),
        }
        succeeding = {r: ["fake-good"] for r in ALL_ROLES}
        priority = dict(succeeding)
        priority[phase_marker] = ["fake-bad"]
        # review needs implementer history for separation; keep it simple:
        config = make_config(priority=priority, providers=names)
        orch = make_orchestrator(tmp, adapters, config)
        await orch.registry.detect_all()
        _seed_project(orch, ws)
        mission = orch.create_mission("p1", f"m-{phase_marker}", "t", "AUTONOMOUS", "balanced")
        await _run(orch, mission["id"])
        final = orch.db.get("missions", mission["id"])
        assert final["status"] == MissionStatus.FAILED.value, f"{phase_marker}: {final['status']}"
        assert final["blocking_issue"]
        await orch.shutdown()

    async def main() -> None:
        for i, phase in enumerate(("planning", "implementation", "testing", "review")):
            sub = tmp_path / f"case{i}"
            sub.mkdir()
            await phase_case(phase, sub)

    asyncio.run(main())


def test_restart_never_recovers_failed_mission(tmp_path: Path, workspace: Path):
    """FAILED → 10 restarts → zero additional provider invocations."""

    async def main() -> None:
        names = ["fake-a"]
        adapters = {n: FakeAdapter(n, ["ratelimit"]) for n in names}
        config = make_config(priority={r: names for r in ALL_ROLES}, providers=names)
        orch = make_orchestrator(tmp_path, adapters, config)
        await orch.registry.detect_all()
        _seed_project(orch, workspace)
        mission = orch.create_mission("p1", "m", "t", "AUTONOMOUS", "balanced")
        await _run(orch, mission["id"])
        assert orch.db.get("missions", mission["id"])["status"] == MissionStatus.FAILED.value
        calls_after_failure = adapters["fake-a"].calls
        assert calls_after_failure > 0
        runs_after_failure = len(orch.db.query("SELECT id FROM provider_runs WHERE mission_id=?", (mission["id"],)))
        await orch.shutdown()

        for i in range(10):
            adapters2 = {n: FakeAdapter(n, ["ratelimit"]) for n in names}
            orch2 = make_orchestrator(tmp_path, adapters2, config)
            await orch2.start()  # full recovery path
            await asyncio.sleep(0.1)
            # no engine should be running for the failed mission
            assert mission["id"] not in orch2._engines
            for a in adapters2.values():
                assert a.calls == 0, f"restart {i}: provider was invoked!"
            assert (
                len(orch2.db.query("SELECT id FROM provider_runs WHERE mission_id=?", (mission["id"],)))
                == runs_after_failure
            )
            assert orch2.db.get("missions", mission["id"])["status"] == MissionStatus.FAILED.value
            await orch2.shutdown()

    asyncio.run(main())


def test_recovery_filters_all_terminal_states(tmp_path: Path, workspace: Path):
    """FAILED, COMPLETED, CANCELLED, UNVERIFIED are never recovered."""

    async def main() -> None:
        names = ["fake-a"]
        adapters = {n: FakeAdapter(n, ["ok"]) for n in names}
        config = make_config(priority={r: names for r in ALL_ROLES}, providers=names)
        orch = make_orchestrator(tmp_path, adapters, config)
        await orch.registry.detect_all()
        _seed_project(orch, workspace)
        for i, status in enumerate(("FAILED", "COMPLETED", "CANCELLED", "UNVERIFIED")):
            orch.db.insert(
                "missions",
                {
                    "id": f"m{i}",
                    "project_id": "p1",
                    "title": "t",
                    "task": "t",
                    "status": status,
                    "autonomy": "AUTONOMOUS",
                    "profile": "balanced",
                    "created_at": "2024-01-01",
                    "updated_at": "2024-01-01",
                    "finished_at": "2024-01-01",
                },
            )
        await orch._recover_missions()
        assert len(orch._engines) == 0, f"terminal missions were recovered: {list(orch._engines)}"
        for a in adapters.values():
            assert a.calls == 0
        await orch.shutdown()

    asyncio.run(main())


def test_terminal_event_ordering_crash_between_persist_and_publish(tmp_path: Path, workspace: Path):
    """If MISSION_FAILED publication raises after FAILED was persisted,
    the mission must still be terminal after restart (DB is authoritative)."""

    async def main() -> None:
        names = ["fake-a"]
        adapters = {n: FakeAdapter(n, ["ratelimit"]) for n in names}
        config = make_config(priority={r: names for r in ALL_ROLES}, providers=names)
        orch = make_orchestrator(tmp_path, adapters, config)
        await orch.registry.detect_all()
        _seed_project(orch, workspace)

        # Sabotage: publishing MISSION_FAILED raises (simulating event-bus crash
        # after the durable update inside _set_status has already committed).
        from orchestrator.models import EventType

        original_publish = orch.events.publish

        def exploding_publish(event_type, mission_id=None, **payload):  # type: ignore[no-untyped-def]
            if event_type == EventType.MISSION_FAILED:
                raise RuntimeError("simulated event bus crash")
            return original_publish(event_type, mission_id, **payload)

        orch.events.publish = exploding_publish  # type: ignore[method-assign]

        mission = orch.create_mission("p1", "m", "t", "AUTONOMOUS", "balanced")
        await _run(orch, mission["id"])

        # despite the event-bus crash, the mission row is durably FAILED
        assert orch.db.get("missions", mission["id"])["status"] == MissionStatus.FAILED.value
        await orch.shutdown()

        # restart: no recovery, no spend
        adapters2 = {n: FakeAdapter(n, ["ratelimit"]) for n in names}
        orch2 = make_orchestrator(tmp_path, adapters2, config)
        await orch2.start()
        await asyncio.sleep(0.1)
        assert mission["id"] not in orch2._engines
        assert all(a.calls == 0 for a in adapters2.values())
        await orch2.shutdown()

    asyncio.run(main())


def test_all_providers_disabled_fails_fast_durably(tmp_path: Path, workspace: Path):
    """When no provider can ever become eligible, mission fails durably
    instead of waiting forever — with zero provider invocations."""

    async def main() -> None:
        names = ["fake-a", "fake-b"]
        adapters = {n: FakeAdapter(n, ["ok"]) for n in names}
        config = make_config(priority={r: names for r in ALL_ROLES}, providers=names)
        orch = make_orchestrator(tmp_path, adapters, config)
        await orch.registry.detect_all()
        for n in names:
            orch.registry.set_enabled(n, False)
        _seed_project(orch, workspace)
        mission = orch.create_mission("p1", "m", "t", "AUTONOMOUS", "balanced")
        await _run(orch, mission["id"], timeout=20)
        final = orch.db.get("missions", mission["id"])
        assert final["status"] == MissionStatus.FAILED.value
        assert "disabled or uninstalled" in final["blocking_issue"]
        assert all(a.calls == 0 for a in adapters.values())  # zero spend
        await orch.shutdown()

        # restart: stays failed, still zero spend
        adapters2 = {n: FakeAdapter(n, ["ok"]) for n in names}
        orch2 = make_orchestrator(tmp_path, adapters2, config)
        await orch2.start()
        await asyncio.sleep(0.1)
        assert all(a.calls == 0 for a in adapters2.values())
        assert orch2.db.get("missions", mission["id"])["status"] == MissionStatus.FAILED.value
        await orch2.shutdown()

    asyncio.run(main())


def test_f02_terminal_state_transitions_rejected_domain(tmp_path: Path, workspace: Path):
    """F-02: terminal states (COMPLETED, CANCELLED, FAILED, UNVERIFIED) reject start/resume/pause."""
    import pytest

    async def main() -> None:
        orch = _failing_orch(tmp_path, "ok")
        await orch.registry.detect_all()
        _seed_project(orch, workspace)

        for terminal_status in (
            MissionStatus.COMPLETED,
            MissionStatus.CANCELLED,
            MissionStatus.FAILED,
            MissionStatus.UNVERIFIED,
        ):
            m = orch.create_mission("p1", f"m-{terminal_status.value}", "t", "AUTONOMOUS", "balanced")
            orch.db.update("missions", m["id"], {"status": terminal_status.value})

            # start rejected
            with pytest.raises(IllegalMissionTransitionError, match="terminal state"):
                orch.start_mission(m["id"])

            # resume rejected
            with pytest.raises(IllegalMissionTransitionError, match="terminal state"):
                orch.resume_mission(m["id"])

            # pause rejected
            with pytest.raises(IllegalMissionTransitionError, match="terminal state"):
                orch.pause_mission(m["id"])

            # cancel is idempotent no-op (leaves status unchanged)
            orch.cancel_mission(m["id"])
            assert orch.db.get("missions", m["id"])["status"] == terminal_status.value

        # Non-existent mission raises KeyError
        with pytest.raises(KeyError):
            orch.start_mission("nonexistent-mission")
        with pytest.raises(KeyError):
            orch.pause_mission("nonexistent-mission")
        with pytest.raises(KeyError):
            orch.resume_mission("nonexistent-mission")
        with pytest.raises(KeyError):
            orch.cancel_mission("nonexistent-mission")

    asyncio.run(main())
