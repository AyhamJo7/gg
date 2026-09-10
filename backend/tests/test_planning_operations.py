"""Product planning integrity: durable ops, attempt truth, stale rejection."""

from __future__ import annotations

from pathlib import Path

from orchestrator import operations as ops
from orchestrator.config import Config
from orchestrator.db import Database
from orchestrator.orchestrator import Orchestrator
from orchestrator.providers.fake import PlanProvider


def _cfg(names: list[str]) -> Config:
    return Config(
        {
            "providers": {n: {"enabled": True, "timeout_minutes": 1} for n in names},
            "orchestration": {
                "cooldown_base_seconds": 0.05,
                "cooldown_multiplier": 1.0,
                "cooldown_max_seconds": 0.2,
                "gate_refused_cooldown_seconds": 0.05,
            },
            "priority": {"planning": names},
            "git": {"auto_checkpoint": False},
            "lifecycle": {"workspace_root": "/tmp/gg-test-products"},
        }
    )


def _orch(tmp_path: Path, planner: PlanProvider) -> Orchestrator:
    db = Database(tmp_path / "o.db")
    adapters = {"fake-planner": planner}
    return Orchestrator(db, _cfg(["fake-planner"]), adapters)


async def _create_product(orch: Orchestrator) -> str:
    project = orch.coordinator.create_project("P", "build a tiny thing", "", False, True, "")
    return project["id"]


async def test_every_attempt_persisted_and_malformed_is_not_success(tmp_path: Path):
    planner = PlanProvider("fake-planner", ["malformed", "ok"])
    orch = _orch(tmp_path, planner)
    await orch.registry.detect_all()
    pid = await _create_product(orch)
    result = await orch.coordinator.generate_plan(pid)
    assert result["ok"] is True
    runs = orch.db.query("SELECT * FROM provider_runs WHERE product_project_id=? ORDER BY started_at", (pid,))
    assert len(runs) == 2, f"both attempts must be durable, got {len(runs)}"
    assert all(r["stage"] == "product_plan" for r in runs)
    assert all(r["operation_id"] for r in runs)
    assert all(r["run_status"] == "SUCCEEDED" for r in runs), "provider protocol succeeded; plan validity is separate"
    # Operation finished exactly once.
    op_row = orch.db.get("orchestration_operations", result["operation_id"])
    assert op_row["state"] == "SUCCEEDED"
    assert int(op_row["attempts"]) == 2


async def test_quota_failure_is_not_health_success(tmp_path: Path):
    # Two planners: first quotas, second succeeds. Proves a failed
    # invocation is never counted as provider success and each attempt is
    # a distinct durable run.
    from orchestrator.models import FailureClass, ProviderState
    from orchestrator.providers.base import ExecutionResult
    from orchestrator.providers.fake import FakeAdapter

    class QuotaPlanner(FakeAdapter):
        async def execute(self, request, on_output):  # type: ignore[override]
            self.calls += 1
            on_output("Error: quota exceeded")
            return ExecutionResult(
                state=ProviderState.RATE_LIMITED,
                failure_class=FailureClass.QUOTA_EXHAUSTED,
                exit_code=1,
                duration_s=0.01,
                summary="quota",
                raw_tail="quota exceeded",
                assistant_text="quota exceeded",
            )

    quota = QuotaPlanner("quota-planner", ["quota"])
    good = PlanProvider("good-planner", ["ok"])
    db = Database(tmp_path / "o.db")
    cfg = Config(
        {
            "providers": {
                "quota-planner": {"enabled": True, "timeout_minutes": 1},
                "good-planner": {"enabled": True, "timeout_minutes": 1},
            },
            "orchestration": {
                "cooldown_base_seconds": 0.05,
                "cooldown_multiplier": 1.0,
                "cooldown_max_seconds": 0.2,
                "gate_refused_cooldown_seconds": 0.05,
                "quota_cooldown_seconds": 0.05,
            },
            "priority": {"planning": ["quota-planner", "good-planner"]},
            "git": {"auto_checkpoint": False},
            "lifecycle": {"workspace_root": "/tmp/gg-test-products"},
        }
    )
    from orchestrator.orchestrator import Orchestrator as _Orch

    orch = _Orch(db, cfg, {"quota-planner": quota, "good-planner": good})
    await orch.registry.detect_all()
    pid = await _create_product(orch)
    result = await orch.coordinator.generate_plan(pid)
    assert result["ok"] is True
    quota_row = orch.db.get("providers", "quota-planner", key="name")
    good_row = orch.db.get("providers", "good-planner", key="name")
    assert int(quota_row["successful_runs"]) == 0
    assert int(quota_row["total_runs"]) == 1
    assert int(good_row["successful_runs"]) == 1
    runs = orch.db.query("SELECT * FROM provider_runs WHERE product_project_id=? ORDER BY started_at", (pid,))
    assert len(runs) == 2
    assert runs[0]["provider"] == "quota-planner"
    assert runs[0]["failure_class"] == "QUOTA_EXHAUSTED"
    assert runs[1]["provider"] == "good-planner"
    assert runs[1]["run_status"] == "SUCCEEDED"


async def test_duplicate_planning_request_controlled(tmp_path: Path):
    planner = PlanProvider("fake-planner", ["ok"])
    orch = _orch(tmp_path, planner)
    await orch.registry.detect_all()
    pid = await _create_product(orch)
    op, created = ops.create_or_attach_operation(orch.db, pid)
    assert created is True
    op2, created2 = ops.create_or_attach_operation(orch.db, pid)
    assert created2 is False
    assert op2.id == op.id
    # Second generate_plan while an operation is active must refuse, not race.
    try:
        await orch.coordinator.generate_plan(pid)
        raise AssertionError("expected duplicate planning to be rejected")
    except ValueError as exc:
        assert "already in progress" in str(exc)


async def test_stale_result_rejected(tmp_path: Path):
    planner = PlanProvider("fake-planner", ["ok"])
    orch = _orch(tmp_path, planner)
    await orch.registry.detect_all()
    pid = await _create_product(orch)
    op, _ = ops.create_or_attach_operation(orch.db, pid)
    # Simulate a newer revision arriving before the planner commits.
    orch.db.execute("UPDATE product_projects SET plan_revision=5 WHERE id=?", (pid,))
    assert ops.is_stale_result(orch.db, op.id, pid) is True


async def test_cancel_terminates_operation(tmp_path: Path):
    planner = PlanProvider("fake-planner", ["ok"])
    orch = _orch(tmp_path, planner)
    await orch.registry.detect_all()
    pid = await _create_product(orch)
    op, _ = ops.create_or_attach_operation(orch.db, pid)
    assert ops.request_cancel(orch.db, op.id) is True
    row = orch.db.get("orchestration_operations", op.id)
    assert int(row["cancel_requested"]) == 1


async def test_product_plan_rejects_duplicate_criterion_ids():
    from orchestrator.product_plan import validate_product_plan
    from orchestrator.providers.fake import default_test_plan

    plan = default_test_plan()
    # Duplicate R1-A1 across two requirements.
    plan["requirements"][1]["acceptance"] = [{"id": "R1-A1", "description": "dup", "verify": "npm run test"}]
    errors = validate_product_plan(plan)
    assert any("duplicate criterion id" in e for e in errors)
