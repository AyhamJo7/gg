"""Fail-closed context compilation (Increment 2 seal: F-01/F-02, C-02/C-03/C-07/C-19/C-24).

The compiler was audited correct; these tests pin the CALLERS: in strict
compiled mode a compilation failure must never execute a provider, acquire
a lease, touch provider health, or fabricate a provider_run. The owning
mission/task/operation records an honest structured failure instead.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from conftest import make_config, make_orchestrator
from orchestrator import operations as ops
from orchestrator.config import Config
from orchestrator.context_compiler import (
    ContextCompileError,
    ContextCompiler,
    ContextCompileSpec,
    mapped_requirements,
    prepare_invocation_context,
)
from orchestrator.db import Database
from orchestrator.events import EventBus
from orchestrator.models import MissionStatus, ProviderState, SchedulingMode, TaskStatus, utcnow
from orchestrator.orchestrator import Orchestrator
from orchestrator.parallel_engine import ParallelMissionEngine
from orchestrator.providers.fake import FakeAdapter, PlanProvider
from orchestrator.providers.registry import ProviderRegistry


def _tiny_config(names: list[str], tiny_roles: dict[str, int]) -> Config:
    cfg = make_config(providers=names)
    cfg.raw["context"] = {"mode": "compiled", "roles": dict(tiny_roles)}
    return cfg


def _no_provider_side_effects(db: Database, adapters: dict[str, FakeAdapter]) -> None:
    assert all(a.calls == 0 for a in adapters.values()), "no provider execute() may run"
    assert db.query("SELECT COUNT(*) as n FROM provider_runs")[0]["n"] == 0, "no provider_run may exist"
    assert db.query("SELECT COUNT(*) as n FROM invocation_leases")[0]["n"] == 0, "no lease may be acquired"


async def _run_mission(orch: Orchestrator, mission_id: str, timeout: float = 60) -> None:
    orch.start_mission(mission_id)
    await asyncio.wait_for(orch._engine_tasks[mission_id], timeout=timeout)


def _git_project(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True, capture_output=True)
    (path / "README.md").write_text("# init")
    (path / "package.json").write_text('{"scripts": {"test": "true"}}')
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True)


# -- sequential engine --------------------------------------------------------


def test_sequential_overflow_zero_executions(tmp_path: Path, workspace: Path):
    """F-01/C-07/C-19: tiny budget -> MANDATORY_CONTEXT_OVERFLOW, nothing runs."""

    async def main() -> None:
        adapters = {"fake-a": FakeAdapter("fake-a", ["ok"])}
        cfg = _tiny_config(
            ["fake-a"],
            {
                "planner": 10,
                "planning": 10,
                "implementer": 10,
                "implementation": 10,
                "testing": 10,
                "review": 10,
                "repair": 10,
            },
        )
        orch = make_orchestrator(tmp_path, adapters, config=cfg)
        await orch.registry.detect_all()
        orch.db.insert(
            "projects",
            {"id": "p1", "name": "w", "path": str(workspace), "detected_type": "node", "created_at": "2024-01-01"},
        )
        mission = orch.create_mission("p1", "Build thing", "Create a file", "AUTONOMOUS", "balanced")
        await _run_mission(orch, mission["id"])
        final = orch.db.get("missions", mission["id"])
        assert final["status"] == MissionStatus.FAILED.value
        assert "MANDATORY_CONTEXT_OVERFLOW" in (final.get("blocking_issue") or "")
        assert "NOT invoked" in (final.get("blocking_issue") or "")
        _no_provider_side_effects(orch.db, adapters)
        await orch.shutdown()

    asyncio.run(main())


def test_sequential_always_raise_compiler_no_silent_fallback(tmp_path: Path, workspace: Path, monkeypatch):
    """C-19/C-24 architectural: even a healthy compiler-shaped error cannot fall back."""

    async def main() -> None:
        adapters = {"fake-a": FakeAdapter("fake-a", ["ok"]), "fake-b": FakeAdapter("fake-b", ["ok"])}
        orch = make_orchestrator(tmp_path, adapters)
        await orch.registry.detect_all()

        def _boom(self, spec):  # noqa: ANN001, ANN202
            raise ContextCompileError("MANDATORY_CONTEXT_OVERFLOW", "injected")

        monkeypatch.setattr(ContextCompiler, "compile", _boom)
        orch.db.insert(
            "projects",
            {"id": "p1", "name": "w", "path": str(workspace), "detected_type": "node", "created_at": "2024-01-01"},
        )
        mission = orch.create_mission("p1", "Build thing", "Create a file", "AUTONOMOUS", "balanced")
        await _run_mission(orch, mission["id"])
        final = orch.db.get("missions", mission["id"])
        assert final["status"] == MissionStatus.FAILED.value
        assert "MANDATORY_CONTEXT_OVERFLOW" in (final.get("blocking_issue") or "")
        _no_provider_side_effects(orch.db, adapters)
        await orch.shutdown()

    asyncio.run(main())


def test_sequential_review_overflow_keeps_prior_runs_honest(tmp_path: Path, workspace: Path):
    """Review compile failure blocks the mission; earlier compiled runs stand."""

    async def main() -> None:
        adapters = {"fake-a": FakeAdapter("fake-a", ["ok"]), "fake-b": FakeAdapter("fake-b", ["ok"])}
        cfg = make_config(providers=["fake-a", "fake-b"])
        cfg.raw["context"] = {"mode": "compiled", "roles": {"review": 10, "reviewer": 10}}
        orch = make_orchestrator(tmp_path, adapters, config=cfg)
        await orch.registry.detect_all()
        orch.db.insert(
            "projects",
            {"id": "p1", "name": "w", "path": str(workspace), "detected_type": "node", "created_at": "2024-01-01"},
        )
        mission = orch.create_mission("p1", "Build thing", "Create a file", "AUTONOMOUS", "balanced")
        await _run_mission(orch, mission["id"])
        final = orch.db.get("missions", mission["id"])
        assert final["status"] == MissionStatus.FAILED.value
        assert "OVERFLOW" in (final.get("blocking_issue") or "")
        runs = orch.db.query("SELECT role, prompt_template_version FROM provider_runs")
        roles = [r["role"] for r in runs]
        assert "review" not in roles, f"reviewer must not execute, got {roles}"
        assert set(roles) == {"planning", "implementation", "testing"}
        assert all(r["prompt_template_version"] == "compiled-v2" for r in runs)
        await orch.shutdown()

    asyncio.run(main())


def test_sequential_missing_requirement_drift_fails_closed(tmp_path: Path, workspace: Path):
    """F-02/C-02/C-03: phase references a requirement the current plan revision
    no longer defines -> MISSING_REQUIREMENT_MAPPING, provider never launches."""

    async def main() -> None:
        adapters = {"fake-a": FakeAdapter("fake-a", ["ok"])}
        orch = make_orchestrator(tmp_path, adapters)
        await orch.registry.detect_all()
        db = orch.db
        db.insert(
            "projects",
            {"id": "p1", "name": "w", "path": str(workspace), "detected_type": "node", "created_at": "2024-01-01"},
        )
        pid = "prod-drift"
        db.insert(
            "product_projects",
            {
                "id": pid,
                "name": "P",
                "idea": "drift",
                "constraints_text": "",
                "state": "EXECUTING",
                "acceptance_state": "",
                "auto_execute": 0,
                "require_plan_approval": 0,
                "target_project_id": None,
                "target_repo_path": "",
                "plan_revision": 2,
                "created_at": "t",
                "updated_at": "t",
            },
        )
        # Current revision keeps the phase key but dropped requirement R1.
        plan2 = {
            "product_name": "P",
            "goal": "g",
            "users": "u",
            "requirements": [],
            "architecture": {"backend": "py", "frontend": "js", "database": "sqlite", "decisions": []},
            "phases": [
                {
                    "key": "foundation",
                    "title": "F",
                    "goal": "g",
                    "deliverables": [],
                    "tasks": [],
                    "depends_on": [],
                    "workspace_scopes": ["backend"],
                    "suggested_providers": [],
                    "acceptance": [{"id": "f-A1", "description": "d", "verify": "t"}],
                    "requirement_ids": ["R1"],
                    "verify_commands": [],
                    "human_prerequisites": [],
                    "effort": "S",
                }
            ],
        }
        db.insert(
            "plan_revisions",
            {
                "id": "rev2",
                "project_id": pid,
                "revision": 2,
                "plan_json": plan2,
                "created_by": "t",
                "reason": "t",
                "created_at": "t",
            },
        )
        mission = orch.create_mission("p1", "Foundation", "auth backend", "AUTONOMOUS", "balanced")
        db.insert(
            "project_phases",
            {
                "id": "phase-f",
                "project_id": pid,
                "phase_key": "foundation",
                "title": "F",
                "goal": "g",
                "status": "RUNNING",
                "mission_id": mission["id"],
                "depends_on": "[]",
                "acceptance_json": "[]",
                "evidence_json": "{}",
                "attempts": 1,
                "created_at": "t",
                "updated_at": "t",
            },
        )
        await _run_mission(orch, mission["id"])
        final = db.get("missions", mission["id"])
        assert final["status"] == MissionStatus.FAILED.value
        assert "MISSING_REQUIREMENT_MAPPING" in (final.get("blocking_issue") or "")
        # Planning phase (no requirement contract) ran; implementation never did.
        runs = db.query("SELECT role FROM provider_runs")
        assert [r["role"] for r in runs] == ["planning"]
        await orch.shutdown()

    asyncio.run(main())


# -- parallel engine ----------------------------------------------------------


def _parallel_setup(tmp_path: Path, context: dict) -> tuple[Database, Config, ProviderRegistry, dict]:
    db = Database(tmp_path / "p.db")
    cfg = Config(
        {
            "scheduler": {"max_parallel_tasks": 2},
            "priority": {"implementation": ["fast"], "planning": ["fast"], "review": ["fast"], "repair": ["fast"]},
            "providers": {"fast": {"enabled": True, "timeout_minutes": 1}},
            "orchestration": {
                "scheduler_tick_seconds": 0.05,
                "max_phase_attempts": 2,
                "review_required": False,
                "max_repair_cycles": 1,
            },
            "git": {"max_auto_commit_file_mb": 5},
            "context": context,
        }
    )
    adapters = {"fast": FakeAdapter("fast", ["ok"])}
    reg = ProviderRegistry(db, adapters, cfg)
    for name in adapters:
        db.execute(
            "INSERT OR REPLACE INTO providers(name, state, installed) VALUES (?, ?, ?)",
            (name, ProviderState.AVAILABLE.value, 1),
        )
    proj = tmp_path / "project"
    _git_project(proj)
    db.insert("projects", {"id": "p1", "name": "t", "path": str(proj), "created_at": utcnow()})
    return db, cfg, reg, adapters


def test_parallel_task_overflow_fails_task_without_execution(tmp_path: Path):
    """F-01 on DAG tasks: task FAILED + TASK_FAILED event, dependents blocked, zero runs."""

    async def main() -> None:
        db, cfg, reg, adapters = _parallel_setup(
            tmp_path, {"mode": "compiled", "roles": {"implementer": 10, "implementation": 10}}
        )
        events = EventBus(db)
        db.insert(
            "missions",
            {
                "id": "m1",
                "project_id": "p1",
                "title": "t",
                "task": "t",
                "status": MissionStatus.RECOVERING.value,
                "scheduling_mode": SchedulingMode.PARALLEL_SAFE.value,
                "created_at": utcnow(),
                "updated_at": utcnow(),
            },
        )
        db.insert(
            "tasks",
            {
                "id": "ta",
                "mission_id": "m1",
                "role": "implementation",
                "title": "A",
                "description": "do a",
                "status": TaskStatus.PENDING.value,
                "workspace_scope": '["a/**"]',
                "created_at": utcnow(),
            },
        )
        engine = ParallelMissionEngine("m1", db, events, reg, cfg)
        await engine.run()
        task = db.get("tasks", "ta")
        assert task["status"] == TaskStatus.FAILED.value
        assert "MANDATORY_CONTEXT_OVERFLOW" in (task.get("blocking_issue") or "")
        failed_events = db.query("SELECT * FROM events WHERE mission_id=? AND type='TASK_FAILED'", ("m1",))
        assert failed_events, "TASK_FAILED must be observable"
        _no_provider_side_effects(db, adapters)

    asyncio.run(main())


def test_parallel_planner_overflow_blocks_mission(tmp_path: Path):
    """F-01 on DAG planning: planner provider never invoked, mission FAILED."""

    async def main() -> None:
        db, cfg, reg, adapters = _parallel_setup(
            tmp_path, {"mode": "compiled", "roles": {"planner": 10, "planning": 10}}
        )
        events = EventBus(db)
        db.insert(
            "missions",
            {
                "id": "m1",
                "project_id": "p1",
                "title": "t",
                "task": "t",
                "status": MissionStatus.RECOVERING.value,
                "scheduling_mode": SchedulingMode.PARALLEL_SAFE.value,
                "created_at": utcnow(),
                "updated_at": utcnow(),
            },
        )
        engine = ParallelMissionEngine("m1", db, events, reg, cfg)
        await engine.run()
        mission = db.get("missions", "m1")
        assert mission["status"] == MissionStatus.FAILED.value
        assert "OVERFLOW" in (mission.get("blocking_issue") or "")
        _no_provider_side_effects(db, adapters)

    asyncio.run(main())


# -- product planning ---------------------------------------------------------


def _planning_orch(tmp_path: Path, context: dict) -> tuple[Orchestrator, PlanProvider]:
    db = Database(tmp_path / "o.db")
    cfg = Config(
        {
            "providers": {"fake-planner": {"enabled": True, "timeout_minutes": 1}},
            "orchestration": {"cooldown_base_seconds": 0.05},
            "priority": {"planning": ["fake-planner"]},
            "git": {"auto_checkpoint": False},
            "lifecycle": {"workspace_root": str(tmp_path / "products")},
            "context": context,
        }
    )
    planner = PlanProvider("fake-planner", ["ok"])
    return Orchestrator(db, cfg, {"fake-planner": planner}), planner


def test_product_planner_overflow_fails_operation_without_run(tmp_path: Path):
    """F-01 on product planning: op FAILED with compiler code, no run, no plan."""

    async def main() -> None:
        orch, planner = _planning_orch(tmp_path, {"mode": "compiled", "roles": {"planner": 10, "planning": 10}})
        await orch.registry.detect_all()
        project = orch.coordinator.create_project("P", "build a tiny thing " * 50, "", False, True, "")
        pid = project["id"]
        with pytest.raises(ContextCompileError) as exc:
            await orch.coordinator.generate_plan(pid)
        assert exc.value.code == "MANDATORY_CONTEXT_OVERFLOW"
        assert planner.calls == 0
        assert orch.db.query("SELECT COUNT(*) as n FROM provider_runs")[0]["n"] == 0
        assert orch.db.query("SELECT COUNT(*) as n FROM plan_revisions WHERE project_id=?", (pid,))[0]["n"] == 0
        op = orch.db.query(
            "SELECT * FROM orchestration_operations WHERE product_project_id=? ORDER BY created_at DESC LIMIT 1", (pid,)
        )[0]
        assert op["state"] == ops.STATE_FAILED
        assert op["error_code"] == "MANDATORY_CONTEXT_OVERFLOW"
        fresh = orch.db.get("product_projects", pid)
        assert fresh["state"] == "BLOCKED"
        await orch.shutdown()

    asyncio.run(main())


# -- mode matrix --------------------------------------------------------------


def test_prepare_modes_matrix(tmp_path: Path):
    """C-24: legacy/shadow/compiled mode contract at the choke point."""
    db = Database(tmp_path / "m.db")
    spec = ContextCompileSpec(
        role="implementer",
        stage="implementation",
        mission_id="m-1",
        task_title="T",
        task_description="D",
        requirement_ids=["R-NOPE"],
    )
    # Compiled: strict raise.
    with pytest.raises(ContextCompileError) as exc:
        prepare_invocation_context(
            legacy_prompt="LEG", spec=spec, db=db, config=Config({"context": {"mode": "compiled"}})
        )
    assert exc.value.code == "MISSING_REQUIREMENT_MAPPING"
    # Shadow failure: legacy executes once-worth, failure observable, no fake success.
    prompt, meta = prepare_invocation_context(
        legacy_prompt="LEG", spec=spec, db=db, config=Config({"context": {"mode": "shadow"}})
    )
    assert prompt == "LEG"
    assert meta["prompt_template_version"] == "legacy-v1"
    warnings = meta["context_warnings_json"]
    assert "SHADOW_COMPILATION_FAILED" in warnings
    assert "MISSING_REQUIREMENT_MAPPING" in warnings
    assert "SHADOW_MODE_LEGACY_EXECUTED" in warnings
    assert meta["context_blocks_json"] is None
    # Legacy: compiler irrelevant, legacy recorded.
    prompt2, meta2 = prepare_invocation_context(
        legacy_prompt="LEG", spec=spec, db=db, config=Config({"context": {"mode": "legacy"}})
    )
    assert prompt2 == "LEG"
    assert meta2["prompt_template_version"] == "legacy-v1"


def test_shadow_success_executes_single_legacy_run(tmp_path: Path, workspace: Path):
    """Shadow with a healthy compiler: mission completes on legacy runs with shadow markers."""

    async def main() -> None:
        adapters = {"fake-a": FakeAdapter("fake-a", ["ok"]), "fake-b": FakeAdapter("fake-b", ["ok"])}
        cfg = make_config(providers=["fake-a", "fake-b"])
        cfg.raw["context"] = {"mode": "shadow"}
        orch = make_orchestrator(tmp_path, adapters, config=cfg)
        await orch.registry.detect_all()
        orch.db.insert(
            "projects",
            {"id": "p1", "name": "w", "path": str(workspace), "detected_type": "node", "created_at": "2024-01-01"},
        )
        mission = orch.create_mission("p1", "Build thing", "Create a file", "AUTONOMOUS", "balanced")
        await _run_mission(orch, mission["id"])
        final = orch.db.get("missions", mission["id"])
        assert final["status"] == MissionStatus.COMPLETED.value
        runs = orch.db.query("SELECT prompt_template_version FROM provider_runs")
        assert runs and all(r["prompt_template_version"] == "legacy-v1" for r in runs)
        manifests = orch.db.query("SELECT warnings_json FROM run_context_manifests")
        assert any("SHADOW_MODE_LEGACY_EXECUTED" in (m["warnings_json"] or "") for m in manifests)
        await orch.shutdown()

    asyncio.run(main())


def test_legacy_mode_ignores_broken_compiler(tmp_path: Path, workspace: Path, monkeypatch):
    """Legacy mode never compiles: mission completes on legacy-v1 even if compile would raise."""

    async def main() -> None:
        adapters = {"fake-a": FakeAdapter("fake-a", ["ok"]), "fake-b": FakeAdapter("fake-b", ["ok"])}
        cfg = make_config(providers=["fake-a", "fake-b"])
        cfg.raw["context"] = {"mode": "legacy"}
        orch = make_orchestrator(tmp_path, adapters, config=cfg)
        await orch.registry.detect_all()

        def _boom(self, spec):  # noqa: ANN001, ANN202
            raise ContextCompileError("MANDATORY_CONTEXT_OVERFLOW", "injected")

        monkeypatch.setattr(ContextCompiler, "compile", _boom)
        orch.db.insert(
            "projects",
            {"id": "p1", "name": "w", "path": str(workspace), "detected_type": "node", "created_at": "2024-01-01"},
        )
        mission = orch.create_mission("p1", "Build thing", "Create a file", "AUTONOMOUS", "balanced")
        await _run_mission(orch, mission["id"])
        final = orch.db.get("missions", mission["id"])
        assert final["status"] == MissionStatus.COMPLETED.value
        runs = orch.db.query("SELECT prompt_template_version FROM provider_runs")
        assert runs and all(r["prompt_template_version"] == "legacy-v1" for r in runs)
        await orch.shutdown()

    asyncio.run(main())


# -- F-04 dedup -----------------------------------------------------------------


def test_requirement_ids_deduplicated(tmp_path: Path):
    """F-04: duplicate IDs budget/render once, in stable input order."""
    db = Database(tmp_path / "d.db")
    db.insert(
        "projects",
        {
            "id": "proj-1",
            "name": "p",
            "path": "/tmp/x",
            "detected_type": "node",  # noqa: S108
            "created_at": "t",
        },
    )
    reqs = [
        {
            "id": rid,
            "title": f"T{rid}",
            "description": f"d{rid}",
            "kind": "functional",
            "acceptance": [{"id": f"{rid}-A1", "description": f"a{rid}", "verify": "t"}],
        }
        for rid in ("R1", "R2", "R3")
    ]
    plan = {
        "product_name": "P",
        "goal": "g",
        "users": "u",
        "requirements": reqs,
        "architecture": {"backend": "b", "frontend": "f", "database": "d", "decisions": []},
        "phases": [],
    }
    db.insert(
        "product_projects",
        {
            "id": "prod-1",
            "name": "P",
            "idea": "i",
            "constraints_text": "",
            "state": "EXECUTING",
            "acceptance_state": "",
            "auto_execute": 0,
            "require_plan_approval": 0,
            "target_project_id": None,
            "target_repo_path": "",
            "plan_revision": 1,
            "created_at": "t",
            "updated_at": "t",
        },
    )
    db.insert(
        "plan_revisions",
        {
            "id": "r1",
            "project_id": "prod-1",
            "revision": 1,
            "plan_json": plan,
            "created_by": "t",
            "reason": "t",
            "created_at": "t",
        },
    )
    mapped, missing, _ = mapped_requirements(db, "prod-1", ["R2", "R1", "R2", "R3", "R1"])
    assert [r["id"] for r in mapped] == ["R2", "R1", "R3"]
    assert missing == []
    cc = ContextCompiler(db, Config({"context": {"mode": "compiled"}}))
    out = cc.compile(
        ContextCompileSpec(
            role="implementer",
            stage="task",
            product_project_id="prod-1",
            mission_id="m-1",
            task_title="T",
            requirement_ids=["R2", "R1", "R2", "R3", "R1"],
        )
    )
    req_blocks = [b for b in out.blocks if b.type == "PRODUCT_REQUIREMENT" and b.included]
    assert sorted(b.block_id for b in req_blocks) == ["req-R1", "req-R2", "req-R3"]


def test_unknown_requirement_raises_for_contract_roles(tmp_path: Path):
    """F-02/C-02/C-03 unit: unresolvable explicit references fail closed."""
    db = Database(tmp_path / "u.db")
    cc = ContextCompiler(db, Config({"context": {"mode": "compiled"}}))
    for role in ("implementer", "reviewer", "repairer", "testing"):
        with pytest.raises(ContextCompileError) as exc:
            cc.compile(
                ContextCompileSpec(
                    role=role, stage="task", mission_id="m-1", task_title="T", requirement_ids=["R-GHOST"]
                )
            )
        assert exc.value.code == "MISSING_REQUIREMENT_MAPPING"
