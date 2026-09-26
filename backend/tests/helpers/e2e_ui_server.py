"""Deterministic backend for browser e2e of operator surfaces.

Real FastAPI app + real Orchestrator with fake adapters only (no provider CLI
can ever run), seeded with the RechnungsRadar dogfood shape: a COMPLETED
mission with an open MEDIUM finding, an uncertified review, a reviewer that
received a 131-character GIT_DIFF, a rate-limited testing run, a handoff, a
verification with skipped tests, and recorded events.

Usage: e2e_ui_server.py <state_dir> <port>   (PYTHONPATH must include src)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import uvicorn

from orchestrator.api.app import create_app
from orchestrator.config import Config
from orchestrator.db import Database
from orchestrator.models import EventType
from orchestrator.orchestrator import Orchestrator
from orchestrator.providers.fake import FakeAdapter

MISSION_ID = "e2e-rr"
FIXTURE_TS = "2026-09-12T01:00:00+00:00"
REVIEWER_DIFF_CHARS = 131
LOST_RUN_MS = 438_392
SKIPPED_TESTS = 30


def _config() -> Config:
    names = ["fake-a"]
    return Config(
        {
            "providers": {n: {"enabled": True, "timeout_minutes": 1} for n in names},
            "orchestration": {"scheduler_tick_seconds": 0.2},
            "priority": {r: names for r in ("planning", "implementation", "testing", "review", "repair")},
        }
    )


def _block(block_type: str, chars: int) -> dict[str, object]:
    return {
        "block_type": block_type,
        "block_id": block_type.lower(),
        "priority": "MANDATORY",
        "included": True,
        "included_chars": chars,
        "original_chars": chars,
        "representation": "FULL",
        "reason": "",
    }


def seed(db: Database, repo: Path) -> None:
    db.insert("projects", {"id": "e2e-proj", "name": "rr", "path": str(repo), "created_at": FIXTURE_TS})
    db.insert(
        "missions",
        {
            "id": MISSION_ID,
            "project_id": "e2e-proj",
            "title": "E2E RechnungsRadar retry",
            "task": "market launch readiness",
            "status": "COMPLETED",
            "created_at": FIXTURE_TS,
            "updated_at": FIXTURE_TS,
            "finished_at": FIXTURE_TS,
        },
    )
    runs = [
        ("run-plan", "claude", "planning", "SUCCEEDED", "NONE", 642_998, None),
        ("run-test", "codex", "testing", "FAILED", "RATE_LIMIT", LOST_RUN_MS, None),
        ("run-review", "agy", "review", "SUCCEEDED", "NONE", 462_767, REVIEWER_DIFF_CHARS),
    ]
    for i, (rid, provider, role, status, failure, duration, diff) in enumerate(runs):
        at = f"2026-09-12T00:0{i}:00+00:00"
        db.insert(
            "provider_runs",
            {
                "id": rid,
                "mission_id": MISSION_ID,
                "provider": provider,
                "role": role,
                "stage": role,
                "run_status": status,
                "failure_class": failure,
                "duration_ms": duration,
                "started_at": at,
                "finished_at": at,
                "summary": f"{provider} {role} report",
            },
        )
        blocks = [_block("SYSTEM_INSTRUCTIONS", 230), _block("TASK_OBJECTIVE", 25_160)]
        if diff is not None:
            blocks.append(_block("GIT_DIFF", diff))
        db.insert(
            "run_context_manifests",
            {"run_id": rid, "prompt_chars": 26_000, "blocks_json": json.dumps(blocks), "created_at": at},
        )
    db.insert(
        "handoffs",
        {
            "id": "h-review",
            "mission_id": MISSION_ID,
            "from_provider": "codex",
            "to_provider": "agy",
            "role": "review",
            "content": "Review the candidate range.",
            "created_at": "2026-09-12T00:01:30+00:00",
        },
    )
    db.insert(
        "reviews",
        {
            "id": "rev-e2e",
            "mission_id": MISSION_ID,
            "implementation_provider": "claude",
            "review_provider": "agy",
            "independent": 0,
            "degradation_reason": "writer provenance incomplete (cannot certify independence)",
            "writer_set_json": json.dumps(["claude", "opencode"]),
            "reviewed_base_sha": "2a20ca71" + "0" * 32,
            "reviewed_head_sha": "bf39469b" + "0" * 32,
            "created_at": "2026-09-12T00:03:00+00:00",
        },
    )
    db.insert(
        "review_findings",
        {
            "id": "f-e2e",
            "mission_id": MISSION_ID,
            "severity": "MEDIUM",
            "category": "security",
            "file": "services/ingest-api/pipeline.py",
            "description": "Capture upload bypasses rate limiting",
            "status": "open",
            "fingerprint": "fp-e2e",
            "created_at": "2026-09-12T00:03:00+00:00",
        },
    )
    db.insert(
        "verification_attempts",
        {
            "id": "ver-e2e",
            "mission_id": MISSION_ID,
            "sha": "bf39469b" + "0" * 32,
            "kind": "toolchain",
            "status": "passed",
            "started_at": "2026-09-12T00:04:00+00:00",
            "finished_at": "2026-09-12T00:04:10+00:00",
            "skipped_tests": SKIPPED_TESTS,
        },
    )


def main() -> None:
    state_dir, port = Path(sys.argv[1]), int(sys.argv[2])
    repo = state_dir / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    db_path = state_dir / "orchestrator.db"
    db = Database(db_path)
    seed(db, repo)
    config = _config()
    orch = Orchestrator(db, config, {"fake-a": FakeAdapter("fake-a", ["ok"])})
    orch.events.publish(EventType.MISSION_COMPLETED, MISSION_ID)
    uvicorn.run(create_app(db_path, config, orch), host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
