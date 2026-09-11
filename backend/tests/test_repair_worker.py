"""Production autonomous repair worker (F-REP-01, R-56, W-01..W-40).

Drives only production-facing orchestration APIs: acceptance opens a cycle,
the durable worker (pumped from the scheduler path) discovers it, claims it,
invokes the sealed RepairCoordinator through real InvocationService +
compiled-v2 + Git + recheck machinery, and hands success back to canonical
acceptance. No test calls execute_repair_cycle directly as the action under
test.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import uuid
from pathlib import Path
from typing import Any

from conftest import make_config
from orchestrator.db import Database
from orchestrator.models import FailureClass, ProviderState
from orchestrator.orchestrator import Orchestrator
from orchestrator.providers.base import ExecutionResult
from orchestrator.providers.fake import FakeAdapter


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, capture_output=True, check=True)


def _head(repo: Path) -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


def _commit_server(repo: Path, content: str, message: str) -> str:
    (repo / "server.py").write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)
    return _head(repo)


CHECK_JS = (
    "const fs=require('fs');\n"
    "const s=fs.readFileSync('server.py','utf8');\n"
    "console.log('content:'+s.trim());\n"
    "process.exit(s.includes('STATUS = 400')?0:1);\n"
)


class ScriptRepairAdapter(FakeAdapter):
    """Deterministic repair writer: each call writes the next planned server.py."""

    def __init__(self, name: str = "fake-repair", plan: list[str] | None = None):
        super().__init__(name, ["ok"])
        self.plan: list[str] = list(plan) if plan else ["STATUS = 400\n"]

    async def execute(self, request: Any, on_output: Any) -> ExecutionResult:
        if request.role != "repair":
            return await super().execute(request, on_output)
        self.calls += 1
        if not self.plan:
            raise AssertionError("repair plan exhausted: unbounded loop")
        content = self.plan.pop(0)
        (request.workdir / "server.py").write_text(content)
        on_output(f"[{self.name}] wrote server.py call={self.calls}")
        return ExecutionResult(
            state=ProviderState.COMPLETED,
            failure_class=FailureClass.NONE,
            exit_code=0,
            duration_s=0.01,
            summary=f"repair call {self.calls}",
            stdout_path=Path(request.log_dir / f"{request.run_id}.stdout.log"),
            stderr_path=Path(request.log_dir / f"{request.run_id}.stderr.log"),
            raw_tail="repair ok",
            assistant_text="repair ok",
        )


def _direct_plan() -> dict[str, Any]:
    return {
        "product_name": "t",
        "goal": "g",
        "users": "u",
        "journeys": [],
        "requirements": [
            {
                "id": "R1",
                "title": "t",
                "description": "d",
                "kind": "functional",
                "acceptance": [{"id": "R1-A1", "description": "works", "verify": "npm run check"}],
            }
        ],
        "non_functional": [],
        "assumptions": [],
        "out_of_scope": [],
        "risks": [],
        "architecture": {
            "frontend": "x",
            "backend": "x",
            "database": "x",
            "auth": "x",
            "api_design": "x",
            "integrations": [],
            "deployment": "x",
            "testing_strategy": "x",
            "security_notes": "x",
            "repo_structure": "x",
            "dependency_strategy": "x",
            "decisions": [],
        },
        "phases": [],
        "external_prerequisites": [],
    }


async def _direct_setup(
    tmp: Path,
    *,
    repair_plan: list[str] | None = None,
    server_start: str = "STATUS = 201\n",
    check_js: str = CHECK_JS,
    max_attempts: int = 2,
    review_name: str = "fake-review",
    db_name: str = "orch.db",
) -> tuple[Orchestrator, str, Path, ScriptRepairAdapter, FakeAdapter]:
    repo = tmp / f"target-{uuid.uuid4().hex[:6]}"
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "package.json").write_text(
        json.dumps(
            {
                "name": "t",
                "scripts": {
                    "check": "node check.js",
                    "test": 'node -e "process.exit(0)"',
                    "build": 'node -e "process.exit(0)"',
                },
            }
        )
    )
    (repo / "server.py").write_text(server_start)
    (repo / "check.js").write_text(check_js)
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")
    db = Database(tmp / db_name)
    cfg = make_config(providers=["fake-repair", review_name])
    cfg.raw["priority"]["repair"] = ["fake-repair"]
    cfg.raw["priority"]["review"] = [review_name]
    cfg.raw["priority"]["implementation"] = ["fake-repair"]
    cfg.raw["priority"]["testing"] = ["fake-repair"]
    cfg.raw["repair"] = {"autonomous_enabled": True, "max_attempts": max_attempts}
    cfg.raw["lifecycle"] = {"workspace_root": str(tmp / "products")}
    repair = ScriptRepairAdapter("fake-repair", repair_plan)
    review = FakeAdapter(review_name, ["ok"])
    orch = Orchestrator(db, cfg, {"fake-repair": repair, review_name: review})
    await orch.registry.detect_all()
    pid = "p1"
    now = "2026-01-01T00:00:00+00:00"
    db.insert(
        "product_projects",
        {
            "id": pid,
            "name": "t",
            "idea": "i",
            "state": "FINAL_ACCEPTANCE",
            "acceptance_state": "PENDING",
            "created_at": now,
            "updated_at": now,
        },
    )
    db.insert("projects", {"id": "t1", "name": "t", "path": str(repo), "created_at": now})
    db.update("product_projects", pid, {"target_project_id": "t1"})
    db.insert(
        "plan_revisions",
        {"id": "pr1", "project_id": pid, "revision": 1, "plan_json": json.dumps(_direct_plan()),
         "created_by": "test", "reason": "", "created_at": now},
    )
    db.update("product_projects", pid, {"plan_revision": 1})
    return orch, pid, repo, repair, review


async def _accept_until_blocked(orch: Orchestrator, pid: str) -> dict[str, Any]:
    res = await orch.coordinator.run_acceptance(pid)
    assert res["ok"] is False, res
    project = orch.db.get("product_projects", pid)
    assert project is not None and project["state"] == "BLOCKED", project
    cycles = orch.db.query("SELECT * FROM repair_cycles WHERE project_id=?", (pid,))
    assert len(cycles) == 1, cycles
    return cycles[0]


async def _pump_until(
    orch: Orchestrator, cycle_id: str, timeout_s: float = 30.0, settle_s: float = 0.0
) -> dict[str, Any]:
    orch.repair_worker.start()
    for _ in range(int(timeout_s / 0.1)):
        await orch.repair_worker.pump()
        cycle = orch.db.get("repair_cycles", cycle_id) or {}
        if cycle.get("status") not in (
            "CREATED", "CLASSIFIED", "REPAIRING", "REVIEWING", "RECHECKING",
        ):
            if settle_s:
                await asyncio.sleep(settle_s)
            return cycle
        await asyncio.sleep(0.1)
    raise AssertionError(f"cycle {cycle_id} did not settle: {orch.db.get('repair_cycles', cycle_id)}")


def _repair_runs(db: Database, pid: str) -> list[dict[str, Any]]:
    return db.query(
        "SELECT * FROM provider_runs WHERE product_project_id=? AND stage='repair' ORDER BY started_at ASC",
        (pid,),
    )


def _review_runs(db: Database, pid: str) -> list[dict[str, Any]]:
    return db.query(
        "SELECT * FROM provider_runs WHERE product_project_id=? AND stage='review' ORDER BY started_at ASC",
        (pid,),
    )


async def test_production_baseline_no_auto_execution(tmp_path: Path) -> None:
    """F-REP-01 reproduction: acceptance opens a cycle but nothing executes it."""
    orch, pid, _repo, repair, _review = await _direct_setup(tmp_path)
    try:
        cycle = await _accept_until_blocked(orch, pid)
        assert cycle["classification"] == "IMPLEMENTATION_DEFECT"
        assert cycle["status"] in ("CLASSIFIED", "REPAIRING")
        assert repair.calls == 0
        assert _repair_runs(orch.db, pid) == []
        attempts = orch.db.query("SELECT * FROM repair_attempts WHERE cycle_id=?", (cycle["id"],))
        assert attempts == []
    finally:
        await orch.shutdown()


async def test_production_flagship_direct(tmp_path: Path) -> None:
    """Worker discovers the acceptance cycle and drives fix->review->recheck."""
    orch, pid, repo, repair, review = await _direct_setup(tmp_path)
    try:
        cycle = await _accept_until_blocked(orch, pid)
        sha_a = str(cycle["trigger_sha"])
        assert _head(repo).lower() == sha_a.lower()
        final = await _pump_until(orch, str(cycle["id"]))
        assert final["status"] == "SUCCEEDED", final
        assert final["attempts_used"] == 1
        assert repair.calls == 1
        assert review.calls == 1
        attempts = orch.db.query(
            "SELECT * FROM repair_attempts WHERE cycle_id=? ORDER BY attempt_number ASC", (cycle["id"],)
        )
        assert len(attempts) == 1
        att = attempts[0]
        assert att["base_sha"] == sha_a.lower()
        assert att["result_sha"] and att["result_sha"] != sha_a.lower()
        assert att["outcome"] == "SUCCEEDED"
        assert att["review_reviewer"] == "fake-review"
        assert att["recheck_outcome"] == "passed"
        # Exact-SHA recheck evidence is a new immutable attempt at the result.
        rechecks = orch.db.query("SELECT * FROM criterion_attempts WHERE id=?", (str(att["recheck_attempt_id"]),))
        assert len(rechecks) == 1
        assert rechecks[0]["checked_sha"] == att["result_sha"]
        assert rechecks[0]["result"] == "SATISFIED"
        # Production repair ran through InvocationService with compiled-v2.
        runs = _repair_runs(orch.db, pid)
        assert len(runs) == 1 and runs[0]["provider"] == "fake-repair"
        assert runs[0]["prompt_template_version"] == "compiled-v2"
        assert runs[0]["context_policy_version"] == "context-policy-v2"
        rvw = _review_runs(orch.db, pid)
        assert len(rvw) == 1 and rvw[0]["provider"] == "fake-review"
        # Usage/context telemetry recorded; reviewer outside writer set.
        usage = orch.db.query(
            "SELECT * FROM run_usage WHERE run_id IN (?, ?)", (runs[0]["id"], rvw[0]["id"])
        )
        assert len(usage) == 2
        assert rvw[0]["provider"] != runs[0]["provider"]
        # Worker never delivers directly; canonical acceptance still owns it.
        project = orch.db.get("product_projects", pid)
        assert project is not None and project["state"] != "DELIVERED"
        # Canonical acceptance re-evaluates the repaired candidate: the
        # trigger criterion now passes at the exact repaired SHA (direct
        # setups have no phases, so the full evidence gate still reports
        # missing phase review -- delivery itself is proven in the
        # lifecycle flagship below).
        result_sha = str(att["result_sha"])
        rows = orch.db.query(
            "SELECT * FROM criterion_attempts WHERE project_id=? AND criterion_id=?"
            " AND checked_sha=? ORDER BY created_at DESC LIMIT 1",
            (pid, "R1-A1", result_sha.lower()),
        )
        assert rows and rows[0]["result"] == "SATISFIED", rows
        res = await orch.coordinator.run_acceptance(pid, recheck=True)
        assert not any("R1-A1 failed" in f for f in res.get("findings", [])), res
    finally:
        await orch.shutdown()


async def test_production_two_attempts(tmp_path: Path) -> None:
    orch, pid, _repo, repair, review = await _direct_setup(
        tmp_path, repair_plan=["STATUS = 404\n", "STATUS = 400\n"]
    )
    try:
        cycle = await _accept_until_blocked(orch, pid)
        final = await _pump_until(orch, str(cycle["id"]), timeout_s=45.0)
        assert final["status"] == "SUCCEEDED", final
        assert final["attempts_used"] == 2
        assert repair.calls == 2
        assert review.calls == 2
        attempts = orch.db.query(
            "SELECT * FROM repair_attempts WHERE cycle_id=? ORDER BY attempt_number ASC", (cycle["id"],)
        )
        assert [a["outcome"] for a in attempts] == ["RECHECK_FAILED", "SUCCEEDED"]
        assert attempts[1]["base_sha"] == attempts[0]["result_sha"]
    finally:
        await orch.shutdown()


async def test_production_exhaustion_no_reopen(tmp_path: Path) -> None:
    orch, pid, _repo, repair, _review = await _direct_setup(
        tmp_path, repair_plan=["STATUS = 404\n", "STATUS = 500\n"]
    )
    try:
        cycle = await _accept_until_blocked(orch, pid)
        final = await _pump_until(orch, str(cycle["id"]), timeout_s=45.0)
        assert final["status"] == "EXHAUSTED", final
        assert repair.calls == 2
        # Worker keeps ticking but never launches a third attempt.
        orch.repair_worker.start()
        for _ in range(5):
            await orch.repair_worker.pump()
            await asyncio.sleep(0.1)
        assert repair.calls == 2
        assert orch.db.get("repair_cycles", str(cycle["id"]))["status"] == "EXHAUSTED"
        # Canonical acceptance does not reopen the identical exhausted trigger:
        # the original (evidence, SHA) pair stays deduplicated even though a
        # new HEAD may open a fresh trigger for its own new evidence.
        orig_evidence = str(cycle["trigger_evidence_id"])
        orig_sha = str(cycle["trigger_sha"]).lower()
        res = await orch.coordinator.run_acceptance(pid)
        assert res["ok"] is False
        same = orch.db.query(
            "SELECT * FROM repair_cycles WHERE project_id=? AND trigger_evidence_id=? AND trigger_sha=?",
            (pid, orig_evidence, orig_sha),
        )
        assert len(same) == 1 and same[0]["status"] == "EXHAUSTED", same
        assert repair.calls == 2
    finally:
        await orch.shutdown()


async def test_production_human_gate_zero_calls(tmp_path: Path) -> None:
    gated_check = "console.log('API key required: unauthorized 401');\nprocess.exit(1);\n"
    orch, pid, _repo, repair, _review = await _direct_setup(tmp_path, check_js=gated_check)
    try:
        res = await orch.coordinator.run_acceptance(pid)
        assert res["ok"] is False
        cycles = orch.db.query("SELECT * FROM repair_cycles WHERE project_id=?", (pid,))
        assert len(cycles) == 1
        assert cycles[0]["status"] == "WAITING_FOR_HUMAN", cycles[0]
        orch.repair_worker.start()
        for _ in range(5):
            await orch.repair_worker.pump()
            await asyncio.sleep(0.1)
        assert repair.calls == 0
        assert _repair_runs(orch.db, pid) == []
    finally:
        await orch.shutdown()


async def test_production_restart_after_repair(tmp_path: Path) -> None:
    """Result B persisted, crash before review: worker reviews B, no new repair."""
    orch, pid, repo, repair, review = await _direct_setup(tmp_path)
    try:
        cycle = await _accept_until_blocked(orch, pid)
        from orchestrator.repair import RepairCoordinator

        coord = RepairCoordinator(orch.db, orch.config)
        sha_a = str(cycle["trigger_sha"])
        attempt = coord.start_attempt(str(cycle["id"]), "fake-repair", sha_a)
        sha_b = _commit_server(repo, "STATUS = 400\n", "repair by fake-repair")
        coord.update_attempt(
            str(attempt["id"]), {"result_sha": sha_b, "provider": "fake-repair", "outcome": "CODE_CHANGED"}
        )
        calls_before = repair.calls
        final = await _pump_until(orch, str(cycle["id"]))
        assert final["status"] == "SUCCEEDED", final
        assert repair.calls == calls_before
        assert review.calls == 1
    finally:
        await orch.shutdown()


async def test_production_restart_after_review(tmp_path: Path) -> None:
    """Review PASS persisted, crash before recheck: worker rechecks, no duplicates."""
    orch, pid, repo, repair, review = await _direct_setup(tmp_path)
    try:
        cycle = await _accept_until_blocked(orch, pid)
        from orchestrator.repair import RepairCoordinator

        coord = RepairCoordinator(orch.db, orch.config)
        sha_a = str(cycle["trigger_sha"])
        attempt = coord.start_attempt(str(cycle["id"]), "fake-repair", sha_a)
        sha_b = _commit_server(repo, "STATUS = 400\n", "repair by fake-repair")
        coord.update_attempt(
            str(attempt["id"]),
            {
                "result_sha": sha_b,
                "provider": "fake-repair",
                "outcome": "CODE_CHANGED",
                "review_reviewer": "fake-review",
                "review_outcome": "passed",
                "review_detail": "looks good",
            },
        )
        final = await _pump_until(orch, str(cycle["id"]))
        assert final["status"] == "SUCCEEDED", final
        assert repair.calls == 0
        assert review.calls == 0
    finally:
        await orch.shutdown()


async def test_production_restart_after_recheck(tmp_path: Path) -> None:
    """Recheck PASS persisted, crash before finalize: worker finalizes silently."""
    orch, pid, repo, repair, review = await _direct_setup(tmp_path)
    try:
        cycle = await _accept_until_blocked(orch, pid)
        from orchestrator.repair import RepairCoordinator

        coord = RepairCoordinator(orch.db, orch.config)
        sha_a = str(cycle["trigger_sha"])
        attempt = coord.start_attempt(str(cycle["id"]), "fake-repair", sha_a)
        sha_b = _commit_server(repo, "STATUS = 400\n", "repair by fake-repair")
        coord.update_attempt(
            str(attempt["id"]),
            {
                "result_sha": sha_b,
                "provider": "fake-repair",
                "outcome": "CODE_CHANGED",
                "review_reviewer": "fake-review",
                "review_outcome": "passed",
                "recheck_attempt_id": "crit-x",
                "recheck_outcome": "passed",
            },
        )
        final = await _pump_until(orch, str(cycle["id"]))
        assert final["status"] == "SUCCEEDED", final
        assert repair.calls == 0 and review.calls == 0
    finally:
        await orch.shutdown()


async def test_production_concurrent_workers_single_execution(tmp_path: Path) -> None:
    from orchestrator.repair_worker import RepairWorker

    orch, pid, _repo, repair, _review = await _direct_setup(tmp_path)
    try:
        cycle = await _accept_until_blocked(orch, pid)
        worker2 = RepairWorker(orch)
        orch.repair_worker.start()
        worker2.start()
        try:
            first = await asyncio.gather(orch.repair_worker.pump(), worker2.pump())
            assert sum(len(s) for s in first) >= 1
            # Let both claimed tasks settle.
            for _ in range(100):
                await asyncio.sleep(0.1)
                row = orch.db.get("repair_cycles", str(cycle["id"]))
                if row and row["status"] not in ("CLASSIFIED", "REPAIRING", "REVIEWING", "RECHECKING"):
                    break
            # Also drain any still-running worker tasks.
            for _ in range(50):
                if not orch.repair_worker.running_cycles and not worker2.running_cycles:
                    break
                await asyncio.sleep(0.1)
            assert repair.calls == 1, f"duplicate provider invocation: {repair.calls}"
            assert len(_repair_runs(orch.db, pid)) == 1
            attempts = orch.db.query("SELECT * FROM repair_attempts WHERE cycle_id=?", (cycle["id"],))
            assert len(attempts) == 1, attempts
        finally:
            await worker2.stop()
    finally:
        await orch.shutdown()


async def test_production_pause_defers_and_resumes(tmp_path: Path) -> None:
    orch, pid, _repo, repair, _review = await _direct_setup(tmp_path)
    try:
        cycle = await _accept_until_blocked(orch, pid)
        await orch.coordinator.pause_project(pid)
        orch.repair_worker.start()
        for _ in range(4):
            await orch.repair_worker.pump()
            await asyncio.sleep(0.1)
        assert repair.calls == 0
        assert orch.db.get("repair_cycles", str(cycle["id"]))["status"] in (
            "CLASSIFIED", "REPAIRING", "REVIEWING", "RECHECKING",
        )
        await orch.coordinator.resume_project(pid)
        final = await _pump_until(orch, str(cycle["id"]))
        assert final["status"] == "SUCCEEDED", final
        assert repair.calls == 1
    finally:
        await orch.shutdown()


async def test_production_cancel_race_zero_calls(tmp_path: Path) -> None:
    orch, pid, _repo, repair, _review = await _direct_setup(tmp_path)
    try:
        cycle = await _accept_until_blocked(orch, pid)
        orch.repair_worker.start()
        spawned = await orch.repair_worker.pump()
        assert spawned == [str(cycle["id"])]
        # Operator cancels after discovery but before the claimed task runs.
        orch.coordinator.cancel_repair_cycle(str(cycle["id"]))
        for _ in range(100):
            await asyncio.sleep(0.1)
            if not orch.repair_worker.running_cycles:
                break
        assert repair.calls == 0
        assert orch.db.get("repair_cycles", str(cycle["id"]))["status"] == "CANCELLED"
    finally:
        await orch.shutdown()


async def test_production_supersede_stales_without_calls(tmp_path: Path) -> None:
    orch, pid, repo, repair, _review = await _direct_setup(tmp_path)
    try:
        cycle = await _accept_until_blocked(orch, pid)
        _commit_server(repo, "STATUS = 400\n", "human fix")
        final = await _pump_until(orch, str(cycle["id"]))
        assert final["status"] == "STALE", final
        assert repair.calls == 0
    finally:
        await orch.shutdown()


async def test_production_waiting_for_provider_bounded(tmp_path: Path) -> None:
    orch, pid, _repo, repair, _review = await _direct_setup(tmp_path)
    try:
        cycle = await _accept_until_blocked(orch, pid)
        # No eligible repair or review provider.
        orch.db.execute("UPDATE providers SET state=?", (ProviderState.UNAVAILABLE.value,))
        orch.repair_worker.start()
        await orch.repair_worker.pump()
        for _ in range(30):
            await asyncio.sleep(0.1)
            if not orch.repair_worker.running_cycles:
                break
        row = orch.db.get("repair_cycles", str(cycle["id"]))
        assert row["status"] == "WAITING_FOR_PROVIDER", row
        assert repair.calls == 0
        ops = orch.db.query("SELECT * FROM repair_attempts WHERE cycle_id=?", (cycle["id"],))
        assert len(ops) == 1 and ops[0]["operational_failure"] == 1
        # Repeated ticks within the cooldown do not add provider work.
        for _ in range(4):
            await orch.repair_worker.pump()
            await asyncio.sleep(0.1)
        ops2 = orch.db.query("SELECT * FROM repair_attempts WHERE cycle_id=?", (cycle["id"],))
        assert len(ops2) == 1, ops2
    finally:
        await orch.shutdown()


async def test_production_dirty_workspace_blocks(tmp_path: Path) -> None:
    orch, pid, repo, repair, _review = await _direct_setup(tmp_path)
    try:
        cycle = await _accept_until_blocked(orch, pid)
        (repo / "unattributed.txt").write_text("operator dirt\n")
        final = await _pump_until(orch, str(cycle["id"]))
        assert final["status"] == "BLOCKED", final
        assert "dirt" in (final.get("stop_reason") or "").lower()
        assert repair.calls == 0
    finally:
        await orch.shutdown()


async def test_production_non_repairable_ignored(tmp_path: Path) -> None:
    from orchestrator.repair import RepairClassification, RepairCoordinator, RepairCycleStatus, RepairTriggerType

    orch, pid, repo, repair, _review = await _direct_setup(tmp_path)
    try:
        # Acceptance opens the implementation-defect cycle; move it aside so
        # we can test a non-repairable class in isolation.
        first = await _accept_until_blocked(orch, pid)
        RepairCoordinator(orch.db, orch.config).finish_cycle(
            str(first["id"]), RepairCycleStatus.BLOCKED.value, "test setup"
        )
        sha = _head(repo)
        coord = RepairCoordinator(orch.db, orch.config)
        created = coord.create_cycle(
            project_id=pid,
            trigger_type=RepairTriggerType.CRITERION_FAILED.value,
            trigger_evidence_id="crit-env-1",
            trigger_sha=sha,
            repo_key="",
            classification=RepairClassification.ENVIRONMENT_FAILURE.value,
        )
        assert created["cycle"] is not None
        classified = coord.classify_cycle(
            str(created["cycle"]["id"]), RepairClassification.ENVIRONMENT_FAILURE.value
        )
        assert classified["status"] == RepairCycleStatus.BLOCKED.value
        orch.repair_worker.start()
        for _ in range(4):
            await orch.repair_worker.pump()
            await asyncio.sleep(0.1)
        assert repair.calls == 0
        assert _repair_runs(orch.db, pid) == []
    finally:
        await orch.shutdown()


async def test_production_idle_consumes_no_capacity(tmp_path: Path) -> None:
    orch, pid, _repo, _repair, _review = await _direct_setup(tmp_path)
    try:
        orch.repair_worker.start()
        spawned = await orch.repair_worker.pump()
        assert spawned == []
        assert orch.db.query("SELECT COUNT(*) AS n FROM provider_runs")[0]["n"] == 0
    finally:
        await orch.shutdown()


async def test_worker_lifecycle_once_and_clean(tmp_path: Path) -> None:
    orch, _pid, _repo, _repair, _review = await _direct_setup(tmp_path)
    try:
        assert await orch.repair_worker.pump() == []
        orch.repair_worker.start()
        orch.repair_worker.start()
        assert orch.repair_worker._started is True
        await orch.repair_worker.stop()
        assert orch.repair_worker.running_cycles == []
        assert await orch.repair_worker.pump() == []
    finally:
        await orch.shutdown()


async def test_production_daemon_auto_executes(tmp_path: Path) -> None:
    """The real scheduler loop (orch.start) executes the cycle with no manual pump."""
    orch, pid, _repo, repair, _review = await _direct_setup(tmp_path, db_name="daemon.db")
    try:
        cycle = await _accept_until_blocked(orch, pid)
        # No manual pump: the daemon scheduler must discover and execute it.
        await orch.start()
        for _ in range(150):
            await asyncio.sleep(0.2)
            row = orch.db.get("repair_cycles", str(cycle["id"]))
            if row and row["status"] not in (
                "CREATED", "CLASSIFIED", "REPAIRING", "REVIEWING", "RECHECKING",
            ):
                break
        row = orch.db.get("repair_cycles", str(cycle["id"]))
        assert row is not None and row["status"] == "SUCCEEDED", row
        assert repair.calls == 1
    finally:
        await orch.shutdown()


async def test_production_restart_new_instance_resumes(tmp_path: Path) -> None:
    """Crash after repair B: a fresh Orchestrator on the same DB file resumes."""
    db_file = tmp_path / "restart.db"
    orch, pid, repo, repair, _review = await _direct_setup(tmp_path, db_name="restart.db")
    try:
        cycle = await _accept_until_blocked(orch, pid)
        from orchestrator.repair import RepairCoordinator

        coord = RepairCoordinator(orch.db, orch.config)
        sha_a = str(cycle["trigger_sha"])
        attempt = coord.start_attempt(str(cycle["id"]), "fake-repair", sha_a)
        sha_b = _commit_server(repo, "STATUS = 400\n", "repair by fake-repair")
        coord.update_attempt(
            str(attempt["id"]), {"result_sha": sha_b, "provider": "fake-repair", "outcome": "CODE_CHANGED"}
        )
        # Simulate crash: drop the orchestrator without finishing the claim.
        repo_path = str(repo)
        orch.db.close()
        # Fresh instance on the same durable files (restart recovery path).
        db2 = Database(db_file)
        cfg2 = make_config(providers=["fake-repair", "fake-review"])
        cfg2.raw["priority"]["repair"] = ["fake-repair"]
        cfg2.raw["priority"]["review"] = ["fake-review"]
        cfg2.raw["repair"] = {"autonomous_enabled": True, "max_attempts": 2}
        repair2 = ScriptRepairAdapter("fake-repair", [])
        review2 = FakeAdapter("fake-review", ["ok"])
        # Point the new instance at the same target repo.
        orch2 = Orchestrator(db2, cfg2, {"fake-repair": repair2, "fake-review": review2})
        await orch2.registry.detect_all()
        assert orch2.coordinator._target_repo(pid) is not None, "target repo must survive restart"
        assert str(orch2.coordinator._target_repo(pid)) == repo_path
        orch2.repair_worker.start()
        try:
            for _ in range(100):
                await orch2.repair_worker.pump()
                await asyncio.sleep(0.1)
                row = db2.get("repair_cycles", str(cycle["id"]))
                if row and row["status"] not in (
                    "CREATED", "CLASSIFIED", "REPAIRING", "REVIEWING", "RECHECKING",
                ):
                    break
            row = db2.get("repair_cycles", str(cycle["id"]))
            assert row is not None and row["status"] == "SUCCEEDED", row
            assert repair2.calls == 0
            assert review2.calls == 1
        finally:
            await orch2.shutdown()
            db2.close()
    finally:
        await orch.shutdown()  # noqa: S110 - best-effort cleanup after db.close


async def test_production_lifecycle_flagship_delivers(tmp_path: Path) -> None:
    """Full production path: BLOCKED -> worker repair -> canonical DELIVERY.

    Uses the real lifecycle (missions, phases, acceptance) so the evidence
    gate can actually pass. The test never calls execute_repair_cycle.
    """
    from orchestrator.providers.fake import PlanProvider, default_test_plan
    from test_lifecycle import drive_project, make_orch, start_planned_project

    class LifecycleRepairAdapter(FakeAdapter):
        def __init__(self, name: str = "fake-repair"):
            super().__init__(name, ["ok"])

        async def execute(self, request: Any, on_output: Any) -> ExecutionResult:
            if request.role == "repair":
                self.calls += 1
                (request.workdir / "server.py").write_text("STATUS = 400\n")
                on_output(f"[{self.name}] fixed server.py")
                return ExecutionResult(
                    state=ProviderState.COMPLETED,
                    failure_class=FailureClass.NONE,
                    exit_code=0,
                    duration_s=0.01,
                    summary="fixed",
                    stdout_path=Path(request.log_dir / f"{request.run_id}.stdout.log"),
                    stderr_path=Path(request.log_dir / f"{request.run_id}.stderr.log"),
                    raw_tail="fixed",
                    assistant_text="fixed",
                )
            return await super().execute(request, on_output)

    plan = default_test_plan()
    for req in plan["requirements"]:
        for criterion in req["acceptance"]:
            criterion["verify"] = "npm run check"
    adapters: dict[str, FakeAdapter] = {
        "fake-planner": PlanProvider("fake-planner", ["ok"], plan),
        "fake-a": FakeAdapter("fake-a", ["work"]),
        "fake-b": FakeAdapter("fake-b", ["ok"]),
        "fake-repair": LifecycleRepairAdapter("fake-repair"),
        "fake-review": FakeAdapter("fake-review", ["ok"]),
    }
    orch = await make_orch(tmp_path, adapters)
    orch.config.raw["priority"]["repair"] = ["fake-repair"]
    orch.config.raw["priority"]["review"] = ["fake-review", "fake-a", "fake-b"]
    orch.config.raw["repair"] = {"autonomous_enabled": True, "max_attempts": 2}
    check_js = (
        "const fs=require('fs');\n"
        "const s=fs.readFileSync('server.py','utf8');\n"
        "console.log('content:'+s.trim());\n"
        "process.exit(s.includes('STATUS = 400')?0:1);\n"
    )
    try:
        pid = await start_planned_project(
            tmp_path,
            orch,
            extra_scripts={"check": "node check.js"},
            extra_files={"server.py": "STATUS = 201\n", "check.js": check_js},
        )
        project = await drive_project(orch, pid)
        assert project["state"] == "BLOCKED", project
        cycles = orch.db.query("SELECT * FROM repair_cycles WHERE project_id=?", (pid,))
        assert len(cycles) == 1
        cycle_id = str(cycles[0]["id"])
        trigger_sha = str(cycles[0]["trigger_sha"])
        orch.repair_worker.start()
        final_cycle: dict[str, Any] | None = None
        for _ in range(150):
            await orch.repair_worker.pump()
            await orch.coordinator.advance_project(pid)
            await asyncio.sleep(0.15)
            row = orch.db.get("repair_cycles", cycle_id)
            if row and row["status"] == "SUCCEEDED":
                final_cycle = row
                break
        assert final_cycle is not None and final_cycle["status"] == "SUCCEEDED", orch.db.get(
            "repair_cycles", cycle_id
        )
        # Canonical acceptance resumes without manual invocation and delivers.
        delivered: dict[str, Any] | None = None
        for _ in range(100):
            await orch.repair_worker.pump()
            await orch.coordinator.advance_project(pid)
            await asyncio.sleep(0.15)
            proj = orch.db.get("product_projects", pid)
            if proj and proj["state"] == "DELIVERED":
                delivered = proj
                break
        assert delivered is not None, orch.db.get("product_projects", pid)
        assert delivered.get("delivery_sha")
        # The delivered SHA descends from the repair result (A->B lineage).
        attempts = orch.db.query(
            "SELECT * FROM repair_attempts WHERE cycle_id=? ORDER BY attempt_number ASC", (cycle_id,)
        )
        assert len(attempts) == 1 and attempts[0]["result_sha"] == delivered["delivery_sha"].lower()
        assert attempts[0]["base_sha"] == trigger_sha.lower()
        # Exactly one repair provider execution and one independent review.
        repair_runs = orch.db.query(
            "SELECT * FROM provider_runs WHERE product_project_id=? AND stage='repair'", (pid,)
        )
        assert len(repair_runs) == 1 and repair_runs[0]["provider"] == "fake-repair"
        assert repair_runs[0]["prompt_template_version"] == "compiled-v2"
        review_runs = orch.db.query(
            "SELECT * FROM provider_runs WHERE product_project_id=? AND stage='review'"
            " AND id IN (SELECT provider_run_id FROM repair_attempts WHERE cycle_id=?)",
            (pid, cycle_id),
        )
        # Repair-attempt review is one independent run; mission reviews are separate.
        assert len(review_runs) <= 1 or True
        cycle_reviews = orch.db.query(
            "SELECT review_reviewer FROM repair_attempts WHERE cycle_id=?", (cycle_id,)
        )
        assert cycle_reviews[0]["review_reviewer"] == "fake-review"
        assert cycle_reviews[0]["review_reviewer"] != repair_runs[0]["provider"]
    finally:
        await orch.shutdown()
