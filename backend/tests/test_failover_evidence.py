"""Failover carries the failed attempt's partial report forward (dogfood 2026-09-12)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from conftest import make_orchestrator
from orchestrator.context_compiler import ContextCompileSpec, build_candidate_blocks
from orchestrator.engine import FAILOVER_EVIDENCE_CHARS, _failover_evidence
from orchestrator.models import FailureClass, MissionStatus, ProviderState
from orchestrator.orchestrator import Orchestrator
from orchestrator.providers.base import ExecutionResult
from orchestrator.providers.fake import FakeAdapter

SECRET = "sk-ant-api03-" + "C" * 40


def _result(summary: str, failure: FailureClass = FailureClass.RATE_LIMIT) -> ExecutionResult:
    return ExecutionResult(
        state=ProviderState.RATE_LIMITED, failure_class=failure, exit_code=1, duration_s=407.2, summary=summary
    )


def test_note_is_labelled_bounded_and_redacted() -> None:
    note = _failover_evidence("codex", _result(f"build passes; key {SECRET} " + "x" * 5000))
    assert "codex stopped with RATE_LIMIT after 407s" in note
    assert "partial and unverified" in note
    assert SECRET not in note
    assert len(note) < FAILOVER_EVIDENCE_CHARS + 300


def test_empty_report_adds_nothing() -> None:
    assert _failover_evidence("codex", _result("   ")) == ""


async def _run(orch: Orchestrator, mission_id: str) -> None:
    orch.start_mission(mission_id)
    await asyncio.wait_for(orch._engine_tasks[mission_id], timeout=30)


def test_failover_prompt_differs_and_carries_failure_evidence(tmp_path: Path, workspace: Path) -> None:
    async def main() -> None:
        flaky = FakeAdapter("fake-a", ["ratelimit", "ok"])
        worker = FakeAdapter("fake-b", ["ok"])
        orch = make_orchestrator(tmp_path, {"fake-a": flaky, "fake-b": worker})
        await orch.registry.detect_all()
        orch.db.insert(
            "projects",
            {"id": "p1", "name": "w", "path": str(workspace), "detected_type": "node", "created_at": "2024-01-01"},
        )
        mission = orch.create_mission("p1", "m", "t", "AUTONOMOUS", "balanced")
        await _run(orch, mission["id"])
        assert orch.db.get("missions", mission["id"])["status"] == MissionStatus.COMPLETED.value
        runs = orch.db.query(
            "SELECT r.id, r.provider, r.failure_class, m.prompt_hash, m.blocks_json FROM provider_runs r "
            "JOIN run_context_manifests m ON m.run_id = r.id WHERE r.mission_id=? AND r.role='planning' "
            "ORDER BY r.started_at, r.rowid",
            (mission["id"],),
        )
        assert runs[0]["failure_class"] == "RATE_LIMIT"
        retry = runs[1]
        assert retry["prompt_hash"] != runs[0]["prompt_hash"]
        blocks = json.loads(retry["blocks_json"])
        assert any(b.get("block_id") == "failover" for b in blocks)
        await orch.shutdown()

    asyncio.run(main())


def _repair_spec(**over: object) -> ContextCompileSpec:
    base: dict[str, object] = {
        "role": "repairer",
        "stage": "repair",
        "mission_id": "m",
        "provider": "claude",
        "task_objective": "fix it",
        "failure_text": "## Verification failures to fix\n" + "E" * 1900 + "\nFAILED test_vat",
        "failover_text": "codex stopped with RATE_LIMIT: I think the fix is " + "p" * 1500,
    }
    base.update(over)
    return ContextCompileSpec(**base)  # type: ignore[arg-type]


def _blocks(spec: ContextCompileSpec) -> dict[str, str]:
    blocks, _aux, _warnings = build_candidate_blocks(spec, None)
    return {b.id: b.content for b in blocks}


def test_repair_failover_keeps_verification_evidence_separate() -> None:
    """Round 4 H1: the note never displaces observed-vs-expected evidence."""
    blocks = _blocks(_repair_spec())
    assert "FAILED test_vat" in blocks["failure"]
    assert "codex stopped" not in blocks["failure"]
    assert blocks["failover"].startswith("Previous attempt (partial, unverified")


def test_reviewer_never_receives_failover_text() -> None:
    blocks = _blocks(_repair_spec(role="reviewer", stage="review", failure_text=""))
    assert "failover" not in blocks


def test_failover_skipped_after_gate_refusal(tmp_path: Path, workspace: Path) -> None:
    async def main() -> None:
        orch = make_orchestrator(
            tmp_path, {"fake-a": FakeAdapter("fake-a", ["gate_refused", "ok"]), "fake-b": FakeAdapter("fake-b", ["ok"])}
        )
        await orch.registry.detect_all()
        orch.db.insert(
            "projects",
            {"id": "p1", "name": "w", "path": str(workspace), "detected_type": "node", "created_at": "2024-01-01"},
        )
        mission = orch.create_mission("p1", "m", "t", "AUTONOMOUS", "balanced")
        await _run(orch, mission["id"])
        manifests = orch.db.query(
            "SELECT m.blocks_json FROM provider_runs r JOIN run_context_manifests m ON m.run_id = r.id "
            "WHERE r.mission_id=?",
            (mission["id"],),
        )
        assert not any('"failover"' in row["blocks_json"] for row in manifests)
        await orch.shutdown()

    asyncio.run(main())
