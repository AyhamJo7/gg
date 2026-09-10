"""FINAL_VALIDATION must not spend REPAIR-role attempts on a failure that
no code change could fix (real example: a toolchain trying to self-provision
over network inside a sandbox that deliberately has none — see sandbox.py
and verify.py's _is_environment_failure). Proven by a real gg-orch mission
run against an external project: FINAL_VALIDATION burned its repair budget
against a `getaddrinfo EAI_AGAIN registry.npmjs.org` failure before finally
blocking, when no repair attempt could ever have resolved a DNS lookup with
no network available.

This exercises MissionEngine._phase_final_validation() directly (not through
the full scheduler) with orchestrator.engine.run_verification monkeypatched
to a canned report — the scenario under test is the branch logic itself,
which doesn't depend on a real toolchain or sandbox being present.
"""

from __future__ import annotations

from pathlib import Path

from conftest import make_config
from orchestrator.db import Database
from orchestrator.engine import MissionEngine
from orchestrator.events import EventBus
from orchestrator.locks import ResourceLocks
from orchestrator.models import MissionStatus, utcnow
from orchestrator.providers.registry import ProviderRegistry
from orchestrator.verify import CommandResult, VerificationReport
from orchestrator.workspace import WorkspaceInfo


def _make_engine(tmp_path: Path) -> MissionEngine:
    db = Database(tmp_path / "test.db")
    db.insert(
        "projects",
        {"id": "p1", "name": "proj", "path": str(tmp_path), "detected_type": "node", "created_at": utcnow()},
    )
    db.insert(
        "missions",
        {
            "id": "m1",
            "project_id": "p1",
            "title": "t",
            "task": "t",
            "status": MissionStatus.FINAL_VALIDATION.value,
            "autonomy": "AUTONOMOUS",
            "profile": "balanced",
            "created_at": utcnow(),
            "updated_at": utcnow(),
        },
    )
    events = EventBus(db)
    config = make_config()
    registry = ProviderRegistry(db, {}, config)
    engine = MissionEngine("m1", db, events, registry, config, ResourceLocks())
    engine.project_path = tmp_path
    engine.workspace = WorkspaceInfo(path=tmp_path, is_git_repo=True, project_type="node")
    return engine


async def test_environment_failure_skips_repair_and_blocks_directly(tmp_path: Path, monkeypatch):
    engine = _make_engine(tmp_path)

    async def fake_inspect(*_a, **_kw):
        return engine.workspace

    async def fake_verify(*_a, **_kw):
        report = VerificationReport()
        report.results.append(
            CommandResult(
                command="pnpm run test",
                exit_code=1,
                passed=False,
                duration_s=0.1,
                tail="getaddrinfo EAI_AGAIN registry.npmjs.org",
                likely_environment_issue=True,
            )
        )
        return report

    async def repair_must_not_run(*_a, **_kw):
        raise AssertionError("REPAIR role must not run for an environment-classified failure")

    monkeypatch.setattr("orchestrator.engine.inspect_workspace", fake_inspect)
    monkeypatch.setattr("orchestrator.engine.run_verification", fake_verify)
    monkeypatch.setattr(engine, "_run_provider_phase", repair_must_not_run)

    ok = await engine._phase_final_validation()
    assert ok is False

    final = engine.db.get("missions", "m1")
    assert final["status"] == MissionStatus.UNVERIFIED.value
    assert final["repair_cycles"] == 0, "no repair cycle should have been spent"
    assert "environment/tooling issue" in final["blocking_issue"]
    assert "not a code defect" in final["blocking_issue"]


async def test_genuine_code_failure_still_triggers_repair(tmp_path: Path, monkeypatch):
    """Contrast case: a failure with no environment-failure signature must
    still go through the existing repair-cycle path unchanged."""
    engine = _make_engine(tmp_path)

    async def fake_inspect(*_a, **_kw):
        return engine.workspace

    async def fake_verify(*_a, **_kw):
        report = VerificationReport()
        report.results.append(
            CommandResult(
                command="pytest",
                exit_code=1,
                passed=False,
                duration_s=0.1,
                tail="AssertionError: expected 4, got 5",
                likely_environment_issue=False,
            )
        )
        return report

    repair_called = {"count": 0}

    async def fake_repair_phase(role, extra_context=""):
        repair_called["count"] += 1
        return None  # engine treats a None repair result as "stop here" — sufficient for this assertion

    monkeypatch.setattr("orchestrator.engine.inspect_workspace", fake_inspect)
    monkeypatch.setattr("orchestrator.engine.run_verification", fake_verify)
    monkeypatch.setattr(engine, "_run_provider_phase", fake_repair_phase)

    ok = await engine._phase_final_validation()
    assert ok is False
    assert repair_called["count"] == 1, "a genuine code failure must still trigger a repair attempt"

    final = engine.db.get("missions", "m1")
    assert final["repair_cycles"] == 1
