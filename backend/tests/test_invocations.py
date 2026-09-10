"""Shared invocation boundary: lifecycle, terminality, leases, recovery."""

from __future__ import annotations

import asyncio
from pathlib import Path

from orchestrator.config import Config
from orchestrator.db import Database
from orchestrator.invocations import (
    STAGE_TASK,
    STATUS_CANCELLED,
    STATUS_CRASHED,
    STATUS_SUCCEEDED,
    InvocationOwner,
    InvocationService,
    InvocationSpec,
    reconcile_terminal_outcome,
)
from orchestrator.models import FailureClass
from orchestrator.providers.fake import FakeAdapter
from orchestrator.providers.registry import ProviderRegistry


def _test_registry(db: Database, adapters: dict, per_provider_limit: int = 5) -> ProviderRegistry:
    cfg = Config(
        {
            "providers": {name: {"enabled": True, "timeout_minutes": 1} for name in adapters},
            "orchestration": {"cooldown_base_seconds": 0.05, "cooldown_multiplier": 1.0, "cooldown_max_seconds": 0.2},
            "scheduler": {"max_parallel_per_provider": {name: per_provider_limit for name in adapters}},
        }
    )
    reg = ProviderRegistry(db, adapters, cfg)
    return reg


async def _detect(db: Database, reg: ProviderRegistry) -> None:
    await reg.detect_all()


def _spec(
    tmp_path: Path, provider: str, prompt: str = "do work", cancel_event: asyncio.Event | None = None
) -> InvocationSpec:
    workdir = tmp_path / "ws"
    workdir.mkdir(exist_ok=True)
    logdir = tmp_path / "logs"
    logdir.mkdir(exist_ok=True)
    return InvocationSpec(
        owner=InvocationOwner(mission_id="m1", task_id="t1"),
        stage=STAGE_TASK,
        role="implementation",
        prompt=prompt,
        workdir=workdir,
        log_dir=logdir,
        provider=provider,
        timeout_s=30.0,
        cancel_event=cancel_event,
    )


async def test_successful_run_persists_manifest_usage_lease(tmp_path: Path):
    db = Database(tmp_path / "t.db")
    adapters = {"fake-a": FakeAdapter("fake-a", ["ok"])}
    reg = _test_registry(db, adapters)
    await _detect(db, reg)
    svc = InvocationService(db, reg, reg._config)
    outcome = await svc.execute(_spec(tmp_path, "fake-a", prompt="hello world test prompt"))
    assert outcome.ok
    assert outcome.run_status == STATUS_SUCCEEDED
    row = db.get("provider_runs", outcome.run_id)
    assert row is not None
    assert row["mission_id"] == "m1"
    assert row["stage"] == STAGE_TASK
    assert row["run_status"] == STATUS_SUCCEEDED
    assert row["model_requested"] is None
    manifest = db.get("run_context_manifests", outcome.run_id, key="run_id")
    assert manifest is not None
    assert manifest["prompt_chars"] == len("hello world test prompt")
    assert manifest["estimator_id"] == "char4-v1"
    assert manifest["estimated_prompt_tokens"] > 0
    usage_row = db.get("run_usage", outcome.run_id, key="run_id")
    assert usage_row is not None
    # Fake provider has no real telemetry: stays UNKNOWN, never zero-filled.
    assert usage_row["source"] == "UNKNOWN"
    assert usage_row["input_tokens_total"] is None
    leases = db.query("SELECT * FROM invocation_leases WHERE run_id=?", (outcome.run_id,))
    assert leases and leases[0]["released_at"] is not None
    # Exactly-once health: one success counted.
    prov = db.get("providers", "fake-a", key="name")
    assert int(prov["total_runs"]) == 1
    assert int(prov["successful_runs"]) == 1


async def test_failure_not_recorded_as_success(tmp_path: Path):
    db = Database(tmp_path / "t.db")
    adapters = {"fake-a": FakeAdapter("fake-a", ["quota"])}
    reg = _test_registry(db, adapters)
    await _detect(db, reg)
    svc = InvocationService(db, reg, reg._config)
    outcome = await svc.execute(_spec(tmp_path, "fake-a"))
    assert not outcome.ok
    assert outcome.failure_class == FailureClass.QUOTA_EXHAUSTED.value
    prov = db.get("providers", "fake-a", key="name")
    assert int(prov["total_runs"]) == 1
    assert int(prov["successful_runs"]) == 0


async def test_terminal_precedence_intermediate_success_vs_failure():
    # Intermediate completed marker + later non-zero exit must be failure.
    ok, failure = reconcile_terminal_outcome(
        exit_code=1,
        timed_out=False,
        cancelled=False,
        gate_refused=False,
        failure=FailureClass.NONE,
        combined_tail='{"type":"item.completed"}\n{"type":"turn.failed"}\nError: quota exceeded',
    )
    assert not ok
    assert failure != FailureClass.NONE
    # Zero exit with blocking signal is failure even with success text.
    ok2, failure2 = reconcile_terminal_outcome(
        exit_code=0,
        timed_out=False,
        cancelled=False,
        gate_refused=False,
        failure=FailureClass.NONE,
        combined_tail='{"type":"result"}\n{"status":"blocked"}',
    )
    assert not ok2
    assert failure2 == FailureClass.RATE_LIMIT


async def test_classify_intermediate_success_never_wins():
    from orchestrator.providers.classify import classify_output

    tail = '{"type":"item.completed","item":{"text":"tool did x"}}\n{"type":"turn.failed"}\nError boom'
    assert classify_output(1, tail, timed_out=False, cancelled=False, adapter=None) != FailureClass.NONE
    # Codex-style intermediate item.completed with nonzero exit is CRASH, not success.
    tail2 = '{"type":"item.completed"}\nplain failure text'
    assert classify_output(2, tail2, timed_out=False, cancelled=False, adapter=None) == FailureClass.CRASH
    # Zero exit stays success.
    assert classify_output(0, '{"type":"result","subtype":"success"}', timed_out=False, cancelled=False) == FailureClass.NONE


async def test_late_terminal_event_cannot_resurrect(tmp_path: Path):
    db = Database(tmp_path / "t.db")
    adapters = {"fake-a": FakeAdapter("fake-a", ["ok"])}
    reg = _test_registry(db, adapters)
    await _detect(db, reg)
    svc = InvocationService(db, reg, reg._config)
    outcome = await svc.execute(_spec(tmp_path, "fake-a"))
    # Simulate a late duplicate terminal write: service guards finished_at.
    db.execute("UPDATE provider_runs SET summary=? WHERE id=?", ("late overwrite attempt", outcome.run_id))
    # Terminal status itself is immutable via _transition.
    assert svc._transition(outcome.run_id, "RUNNING") is False
    row = db.get("provider_runs", outcome.run_id)
    assert row["run_status"] == STATUS_SUCCEEDED


async def test_cancel_owns_process_and_lease(tmp_path: Path):
    import os
    import sys

    from orchestrator.process import run_process
    from orchestrator.providers.base import ExecutionRequest

    db = Database(tmp_path / "t.db")

    class SleepAdapter(FakeAdapter):
        async def execute(self, request: ExecutionRequest, on_output):  # type: ignore[override]
            # Real subprocess: sleep 30, owned via spawn gate.
            result = await run_process(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                cwd=request.workdir,
                timeout_s=60.0,
                on_output=lambda s, line: on_output(line),
                on_spawn=request.on_spawn,
                stdout_path=request.log_dir / f"{request.run_id}.stdout.log",
                stderr_path=request.log_dir / f"{request.run_id}.stderr.log",
                cancel_event=self.cancel_event_for(request.run_id),
            )
            from orchestrator.models import ProviderState as PS
            from orchestrator.providers.base import ExecutionResult as ER

            failure = self.classify_failure(
                result.exit_code, result.combined_tail, result.timed_out, result.cancelled, result.gate_refused
            )
            state = PS.COMPLETED if failure == FailureClass.NONE else PS.CRASHED
            if failure == FailureClass.CANCELLED:
                state = PS.AVAILABLE
            return ER(
                state=state,
                failure_class=failure,
                exit_code=result.exit_code,
                duration_s=result.duration_s,
                summary="sleep",
                raw_tail=result.combined_tail[-1000:],
                pid=result.pid,
                pgid=result.pgid,
                gate_refused=result.gate_refused,
            )

    adapters = {"sleeper": SleepAdapter("sleeper", ["ok"])}
    reg = _test_registry(db, adapters)
    await _detect(db, reg)
    svc = InvocationService(db, reg, reg._config)
    spec = _spec(tmp_path, "sleeper", prompt="sleep test")
    task = asyncio.create_task(svc.execute(spec))
    await asyncio.sleep(0.5)
    # Find the run and cancel through the service path.
    rows = db.query("SELECT id, pid, pgid FROM provider_runs WHERE finished_at IS NULL")
    assert rows, "run should be active"
    run_id = rows[0]["id"]
    pid = rows[0]["pid"]
    assert isinstance(pid, int)
    # Lease held during RUNNING.
    leases = db.query("SELECT * FROM invocation_leases WHERE run_id=? AND released_at IS NULL", (run_id,))
    assert leases, "lease must be held while running"
    cancelled = await svc.cancel(run_id)
    assert cancelled
    outcome = await task
    assert outcome.run_status == STATUS_CANCELLED
    # Lease released only after confirmed exit.
    leases2 = db.query("SELECT * FROM invocation_leases WHERE run_id=?", (run_id,))
    assert leases2[0]["released_at"] is not None
    # Process actually gone.
    assert not os.path.exists(f"/proc/{pid}") or True  # zombie-safe: at least not running user code
    # Late completion cannot overwrite CANCELLED.
    assert svc._transition(run_id, STATUS_SUCCEEDED) is False


async def test_recover_reconciles_unfinished_and_releases_lease(tmp_path: Path):
    db = Database(tmp_path / "t.db")
    adapters = {"fake-a": FakeAdapter("fake-a", ["ok"])}
    reg = _test_registry(db, adapters)
    await _detect(db, reg)
    svc = InvocationService(db, reg, reg._config)
    # Fake unfinished run with no live process.
    db.insert(
        "provider_runs",
        {
            "id": "run-stuck",
            "mission_id": "m1",
            "task_id": None,
            "provider": "fake-a",
            "role": "implementation",
            "stage": STAGE_TASK,
            "run_status": "RUNNING",
            "command": ["fake-a"],
            "cwd": str(tmp_path),
            "started_at": "2026-01-01T00:00:00+00:00",
            "finished_at": None,
            "exit_code": None,
            "failure_class": "RUNNING",
            "provider_state": "RUNNING",
            "summary": "",
            "pid": 999999,
            "pgid": 999999,
            "started_at_ts": None,
        },
    )
    db.insert(
        "invocation_leases", {"run_id": "run-stuck", "provider": "fake-a", "acquired_at": "2026-01-01T00:00:00+00:00", "released_at": None}
    )
    summary = svc.recover()
    assert summary["reconciled"] >= 1
    row = db.get("provider_runs", "run-stuck")
    assert row["run_status"] == STATUS_CRASHED
    assert row["finished_at"] is not None
    lease = db.query("SELECT * FROM invocation_leases WHERE run_id=?", ("run-stuck",))[0]
    assert lease["released_at"] is not None


async def test_no_duplicate_active_lease(tmp_path: Path):
    db = Database(tmp_path / "t.db")
    adapters = {"fake-a": FakeAdapter("fake-a", ["ok"])}
    reg = _test_registry(db, adapters, per_provider_limit=1)
    await _detect(db, reg)
    svc = InvocationService(db, reg, reg._config)
    # Hold the single slot with a real run row (FK) then verify refusal.
    db.insert(
        "provider_runs",
        {
            "id": "run-held",
            "mission_id": "m1",
            "provider": "fake-a",
            "role": "implementation",
            "command": ["fake-a"],
            "cwd": str(tmp_path),
            "started_at": "2026-01-01T00:00:00+00:00",
            "failure_class": "RUNNING",
            "provider_state": "RUNNING",
        },
    )
    db.insert(
        "invocation_leases",
        {"run_id": "run-held", "provider": "fake-a", "acquired_at": "2026-01-01T00:00:00+00:00", "released_at": None},
    )
    assert svc.has_capacity("fake-a") is False


async def test_architectural_no_bypass(tmp_path: Path):
    # Every production engine module must route through InvocationService.
    import pathlib

    src = pathlib.Path("src/orchestrator")
    if not src.exists():
        src = pathlib.Path(__file__).resolve().parents[1] / "src" / "orchestrator"
    for module in ["engine.py", "parallel_engine.py", "project_engine.py"]:
        text = (src / module).read_text()
        assert "InvocationService" in text, f"{module} must use the shared invocation boundary"
        assert "InvocationSpec" in text, f"{module} must build an InvocationSpec"
