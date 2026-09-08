"""Planner failover: a failed primary planner must not kill the mission.

Deterministic — fake providers only, no AI quota consumed.
Covers RATE_LIMIT, QUOTA_EXHAUSTED, CRASH (result + exception), successful
fallback, and all-providers-unavailable, plus one full parallel mission
proving planner A fails, planner B succeeds, and the mission runs through
integration, structured review, and final verification.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest

from conftest import make_orchestrator
from orchestrator.config import Config
from orchestrator.db import Database
from orchestrator.events import EventBus
from orchestrator.models import (
    FailureClass,
    MissionStatus,
    ProviderState,
    SchedulingMode,
    utcnow,
)
from orchestrator.parallel_engine import ParallelMissionEngine
from orchestrator.providers.base import ExecutionResult
from orchestrator.providers.fake import FakeAdapter
from orchestrator.providers.registry import ProviderRegistry

DAG = {
    "tasks": [
        {
            "id": "impl-1",
            "title": "implement thing",
            "role": "implementation",
            "depends_on": [],
            "workspace_scope": ["work.txt"],
            "preferred_providers": ["impl"],
        }
    ]
}


class DagPlannerFake(FakeAdapter):
    """Planner that always returns a valid DAG as structured output."""

    def __init__(self, name: str = "plan-b"):
        super().__init__(name, ["ok"])
        self.dag: dict[str, Any] = DAG

    async def execute(self, request: Any, on_output: Any) -> ExecutionResult:
        self.calls += 1
        body = json.dumps(self.dag)
        on_output(f"[{self.name}] planning ok")
        return ExecutionResult(
            state=ProviderState.COMPLETED,
            failure_class=FailureClass.NONE,
            exit_code=0,
            duration_s=0.01,
            summary="planned 1 task",
            raw_tail=f"here is the plan:\n```json\n{body}\n```",
            assistant_text=f"here is the plan:\n```json\n{body}\n```",
        )


@pytest.fixture()
def tmp_db():
    with tempfile.TemporaryDirectory() as td:
        db = Database(Path(td) / "test.db")
        yield db
        db.close()


@pytest.fixture()
def tmp_project():
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "project"
        path.mkdir()
        subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True, capture_output=True)
        (path / "README.md").write_text("# init\n")
        (path / "package.json").write_text(
            json.dumps({"name": "t", "scripts": {"test": "node -e \"process.exit(0)\"" }})
        )
        subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True)
        yield path


def _engine_config() -> Config:
    return Config(
        {
            "scheduler": {"max_parallel_tasks": 3},
            "priority": {
                "planning": ["plan-a", "plan-b"],
                "implementation": ["impl"],
                "testing": ["impl"],
                "review": ["impl"],
                "repair": ["impl"],
            },
            "providers": {
                "plan-a": {"enabled": True},
                "plan-b": {"enabled": True},
                "impl": {"enabled": True},
            },
            "orchestration": {
                "scheduler_tick_seconds": 0.05,
                "max_phase_attempts": 4,
                "max_provider_wait_seconds": 1,
                "cooldown_base_seconds": 60,
                "cooldown_multiplier": 1.0,
                "cooldown_max_seconds": 60,
            },
        }
    )


def _registry(tmp_db: Database, config: Config, adapters: dict[str, FakeAdapter]) -> ProviderRegistry:
    reg = ProviderRegistry(tmp_db, adapters, config)
    for name in adapters:
        tmp_db.execute(
            "INSERT OR REPLACE INTO providers(name, state, installed) VALUES (?, ?, ?)",
            (name, ProviderState.AVAILABLE.value, 1),
        )
    return reg


def _seed_mission(tmp_db: Database, tmp_project: Path, mid: str = "m1") -> None:
    tmp_db.insert(
        "projects", {"id": "p1", "name": "t", "path": str(tmp_project), "created_at": utcnow()}
    )
    tmp_db.insert(
        "missions",
        {
            "id": mid,
            "project_id": "p1",
            "title": "t",
            "task": "build thing",
            "status": MissionStatus.RECOVERING.value,
            "scheduling_mode": SchedulingMode.PARALLEL_SAFE.value,
            "autonomy": "AUTONOMOUS",
            "profile": "balanced",
            "created_at": utcnow(),
            "updated_at": utcnow(),
        },
    )


def _make_engine(
    tmp_db: Database,
    tmp_project: Path,
    primary_script: list[str],
    mid: str = "m1",
) -> ParallelMissionEngine:
    config = _engine_config()
    adapters = {"plan-a": FakeAdapter("plan-a", primary_script), "plan-b": DagPlannerFake()}
    registry = _registry(tmp_db, config, adapters)
    _seed_mission(tmp_db, tmp_project, mid)
    engine = ParallelMissionEngine(mid, tmp_db, EventBus(tmp_db), registry, config)
    engine.project_path = tmp_project
    return engine


@pytest.mark.asyncio()
async def test_failover_on_rate_limit(tmp_db: Database, tmp_project: Path):
    engine = _make_engine(tmp_db, tmp_project, ["ratelimit"])
    assert await engine._run_planning_phase() is True
    tasks = tmp_db.query("SELECT * FROM tasks WHERE mission_id='m1'")
    assert [t["id"] for t in tasks] == ["m1-impl-1"]
    runs = tmp_db.query("SELECT provider, failure_class FROM provider_runs WHERE mission_id='m1' ORDER BY started_at")
    assert (runs[0]["provider"], runs[0]["failure_class"]) == ("plan-a", FailureClass.RATE_LIMIT.value)
    assert (runs[1]["provider"], runs[1]["failure_class"]) == ("plan-b", FailureClass.NONE.value)
    prov_a = tmp_db.get("providers", "plan-a", key="name")
    assert prov_a is not None and prov_a["state"] == ProviderState.RATE_LIMITED.value
    assert prov_a["cooldown_until"] is not None
    mission = tmp_db.get("missions", "m1")
    assert mission is not None and "plan-a" in json.loads(mission["providers_failed"] or "[]")


@pytest.mark.asyncio()
async def test_failover_on_quota_exhausted(tmp_db: Database, tmp_project: Path):
    engine = _make_engine(tmp_db, tmp_project, ["quota"])
    assert await engine._run_planning_phase() is True
    runs = tmp_db.query("SELECT provider, failure_class FROM provider_runs WHERE mission_id='m1' ORDER BY started_at")
    assert runs[0]["failure_class"] == FailureClass.QUOTA_EXHAUSTED.value
    assert runs[1]["provider"] == "plan-b"
    prov_a = tmp_db.get("providers", "plan-a", key="name")
    assert prov_a is not None and prov_a["state"] == ProviderState.RATE_LIMITED.value
    assert prov_a["cooldown_until"] is not None


@pytest.mark.asyncio()
async def test_failover_on_crash_result(tmp_db: Database, tmp_project: Path):
    engine = _make_engine(tmp_db, tmp_project, ["crash"])
    assert await engine._run_planning_phase() is True
    runs = tmp_db.query("SELECT provider, failure_class FROM provider_runs WHERE mission_id='m1' ORDER BY started_at")
    assert runs[0]["failure_class"] == FailureClass.CRASH.value
    assert runs[1]["provider"] == "plan-b"


@pytest.mark.asyncio()
async def test_failover_on_adapter_exception(tmp_db: Database, tmp_project: Path):
    engine = _make_engine(tmp_db, tmp_project, ["explode"])
    assert await engine._run_planning_phase() is True
    runs = tmp_db.query("SELECT provider, failure_class FROM provider_runs WHERE mission_id='m1' ORDER BY started_at")
    assert runs[0]["failure_class"] == FailureClass.CRASH.value
    assert runs[1]["provider"] == "plan-b"


@pytest.mark.asyncio()
async def test_all_planners_unavailable_fails_fast(tmp_db: Database, tmp_project: Path):
    config = _engine_config()
    adapters: dict[str, FakeAdapter] = {"plan-a": FakeAdapter("plan-a", ["ratelimit"])}
    registry = _registry(tmp_db, config, adapters)
    _seed_mission(tmp_db, tmp_project)
    engine = ParallelMissionEngine("m1", tmp_db, EventBus(tmp_db), registry, config)
    engine.project_path = tmp_project
    # disable the only planner: no execution possible, must fail immediately
    tmp_db.execute("UPDATE providers SET state=?, installed=0 WHERE name='plan-a'", ("DISABLED",))
    assert await engine._run_planning_phase() is False
    mission = tmp_db.get("missions", "m1")
    assert mission is not None and mission["status"] == MissionStatus.FAILED.value
    assert "no provider available for planning" in (mission["blocking_issue"] or "")


@pytest.mark.asyncio()
async def test_attempts_bounded_when_every_planner_fails(tmp_db: Database, tmp_project: Path):
    config = _engine_config()
    config._data["orchestration"]["max_phase_attempts"] = 2
    adapters = {
        "plan-a": FakeAdapter("plan-a", ["ratelimit", "ratelimit"]),
        "plan-b": FakeAdapter("plan-b", ["crash", "crash"]),
    }
    registry = _registry(tmp_db, config, adapters)
    _seed_mission(tmp_db, tmp_project)
    engine = ParallelMissionEngine("m1", tmp_db, EventBus(tmp_db), registry, config)
    engine.project_path = tmp_project
    assert await engine._run_planning_phase() is False
    mission = tmp_db.get("missions", "m1")
    assert mission is not None and mission["status"] == MissionStatus.FAILED.value
    assert "exhausted after" in (mission["blocking_issue"] or "")
    runs = tmp_db.query("SELECT * FROM provider_runs WHERE mission_id='m1'")
    assert len(runs) <= 4


def test_full_parallel_mission_with_planner_failover(tmp_path: Path):
    """Planner A rate-limits, planner B plans; mission completes end to end."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "package.json").write_text(
        json.dumps({"name": "t", "scripts": {"test": "node -e \"process.exit(0)\""}})
    )
    subprocess.run(["git", "init"], cwd=ws, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=ws, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=ws, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=ws, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=ws, check=True, capture_output=True)

    async def main() -> None:
        orch = make_orchestrator(
            tmp_path,
            {
                "plan-a": FakeAdapter("plan-a", ["ratelimit"]),
                "plan-b": DagPlannerFake("plan-b"),
                "impl": FakeAdapter("impl", ["work"]),
            },
            Config(
                {
                    "scheduler": {"max_parallel_tasks": 3},
                    "priority": {
                        "planning": ["plan-a", "plan-b"],
                        "implementation": ["impl"],
                        "testing": ["impl"],
                        "review": ["impl"],
                        "repair": ["impl"],
                    },
                    "providers": {
                        "plan-a": {"enabled": True},
                        "plan-b": {"enabled": True},
                        "impl": {"enabled": True},
                    },
                    "orchestration": {
                        "scheduler_tick_seconds": 0.05,
                        "max_phase_attempts": 4,
                        "max_provider_wait_seconds": 5,
                        "cooldown_base_seconds": 60,
                        "cooldown_multiplier": 1.0,
                        "cooldown_max_seconds": 60,
                    },
                }
            ),
        )
        await orch.registry.detect_all()
        orch.db.insert(
            "projects",
            {"id": "p1", "name": "w", "path": str(ws), "detected_type": "node", "created_at": "2024-01-01"},
        )
        mission = orch.create_mission("p1", "failover mission", "build thing", "AUTONOMOUS", "balanced")
        orch.db.update("missions", mission["id"], {"scheduling_mode": SchedulingMode.PARALLEL_SAFE.value})
        orch.start_mission(mission["id"])
        await asyncio.wait_for(orch._engine_tasks[mission["id"]], timeout=120)
        final = orch.db.get("missions", mission["id"])
        assert final is not None
        assert final["status"] == MissionStatus.COMPLETED.value, final.get("blocking_issue")
        plan_runs = orch.db.query(
            "SELECT provider, failure_class FROM provider_runs WHERE mission_id=? AND role='planning' ORDER BY started_at",
            (mission["id"],),
        )
        assert [r["provider"] for r in plan_runs] == ["plan-a", "plan-b"]
        assert plan_runs[0]["failure_class"] == FailureClass.RATE_LIMIT.value
        integrations = orch.db.query(
            "SELECT * FROM task_integrations WHERE mission_id=?", (mission["id"],)
        )
        assert integrations and integrations[0]["status"] == "COMPLETED"
        reviews = orch.db.query("SELECT * FROM reviews WHERE mission_id=?", (mission["id"],))
        assert reviews
        await orch.shutdown()

    asyncio.run(main())
