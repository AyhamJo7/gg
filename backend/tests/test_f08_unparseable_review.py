"""F-08 regression suite: unparseable review does not silently pass missions.

When a reviewer emits unstructured prose without REVIEW_FINDINGS_JSON, the system
synthesizes a finding, marks review_parsed=0 on the reviews row, and transitions
to UNVERIFIED after retries instead of silently completing.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from conftest import make_orchestrator
from orchestrator.models import MissionStatus
from orchestrator.orchestrator import Orchestrator
from orchestrator.providers.fake import FakeAdapter
from orchestrator.review import parse_findings, parse_review_output, persist_findings


def _seed_project(orch: Orchestrator, workspace: Path) -> None:
    orch.db.insert(
        "projects",
        {"id": "p1", "name": "w", "path": str(workspace), "detected_type": "node", "created_at": "2024-01-01"},
    )


def test_parse_review_output_unstructured_prose_detected():
    prose = "Everything is complete. All tests pass. No issues found. LGTM."
    parsed_ok, findings = parse_review_output(prose)
    assert not parsed_ok
    assert len(findings) == 1
    assert findings[0]["severity"] == "MEDIUM"
    assert "unstructured" in findings[0]["description"].lower()

    # Legacy helper returns empty list for raw prose
    assert parse_findings(prose) == []


def test_persist_findings_records_synthesized_finding(tmp_path: Path):
    orch = make_orchestrator(tmp_path, {})
    orch.db.insert(
        "projects",
        {"id": "p1", "name": "w", "path": str(tmp_path), "detected_type": "node", "created_at": "2024-01-01"},
    )
    mission = orch.create_mission("p1", "test", "prompt", "AUTONOMOUS", "balanced")
    mission_id = mission["id"]
    prose = "LGTM. Checked all functions."
    parsed_ok, findings = persist_findings(orch.db, mission_id, prose)
    assert not parsed_ok
    assert len(findings) == 1

    stored = orch.db.query("SELECT * FROM review_findings WHERE mission_id=?", (mission_id,))
    assert len(stored) == 1
    assert stored[0]["severity"] == "MEDIUM"


def test_f08_unparseable_review_transitions_to_unverified(tmp_path: Path, workspace: Path):
    """When a reviewer continually emits prose with no structured findings block,
    the mission must transition to UNVERIFIED and record review_parsed=0 in reviews table.
    """
    # Create fake reviewer that only emits unstructured prose
    class ProseReviewer(FakeAdapter):
        async def execute(self, request, on_output):
            on_output("Looks great! No bugs found, tests pass.")
            from orchestrator.models import FailureClass, ProviderState
            from orchestrator.providers.base import ExecutionResult
            return ExecutionResult(
                state=ProviderState.COMPLETED,
                failure_class=FailureClass.NONE,
                exit_code=0,
                duration_s=0.05,
                summary="Looks great! No bugs found, tests pass.",
                raw_tail="Looks great! No bugs found, tests pass.",
            )

    providers = {
        "fake-impl": FakeAdapter("fake-impl", ["ok"]),
        "fake-prose-reviewer": ProseReviewer("fake-prose-reviewer"),
    }

    async def scenario():
        orch = make_orchestrator(tmp_path, providers)
        _seed_project(orch, workspace)

        mission = orch.create_mission("p1", "test unparseable", "prompt", "AUTONOMOUS", "balanced")
        mission_id = mission["id"]

        await orch.start()
        orch.start_mission(mission_id)

        # Wait for terminal state
        for _ in range(60):
            st = orch.db.get("missions", mission_id)["status"]
            if st in ("UNVERIFIED", "COMPLETED", "FAILED"):
                break
            await asyncio.sleep(0.05)

        final_mission = orch.db.get("missions", mission_id)
        assert final_mission["status"] == MissionStatus.UNVERIFIED.value, (
            f"Expected UNVERIFIED for unparseable review, got {final_mission['status']}"
        )
        assert "unparseable" in (final_mission["blocking_issue"] or "").lower()

        # Check reviews row has review_parsed=0
        reviews = orch.db.query("SELECT * FROM reviews WHERE mission_id=?", (mission_id,))
        assert len(reviews) >= 1
        assert any(r["review_parsed"] == 0 for r in reviews)

        await orch.shutdown()

    asyncio.run(scenario())
