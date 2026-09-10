"""Run inspection APIs: pagination, scoping, unknown handling, redaction."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from orchestrator.api.app import create_app
from orchestrator.config import Config
from orchestrator.db import Database
from orchestrator.orchestrator import Orchestrator
from orchestrator.providers.fake import FakeAdapter


def _app(tmp_path: Path) -> tuple[TestClient, Orchestrator]:
    db = Database(tmp_path / "api.db")
    cfg = Config(
        {
            "providers": {"fake-a": {"enabled": True, "timeout_minutes": 1}},
            "orchestration": {},
            "priority": {"planning": ["fake-a"]},
        }
    )
    orch = Orchestrator(db, cfg, {"fake-a": FakeAdapter("fake-a", ["ok"])})
    db.insert(
        "product_projects",
        {
            "id": "p1",
            "name": "P",
            "idea": "idea",
            "constraints_text": "",
            "state": "DRAFT",
            "acceptance_state": "",
            "auto_execute": 0,
            "require_plan_approval": 1,
            "target_repo_path": "",
            "plan_revision": 0,
            "created_at": "2026-09-10T09:00:00+00:00",
            "updated_at": "2026-09-10T09:00:00+00:00",
        },
    )
    # Seed runs: one modern with usage, one legacy without.
    db.insert(
        "provider_runs",
        {
            "id": "run-modern",
            "mission_id": "m1",
            "task_id": "t1",
            "product_project_id": "p1",
            "provider": "fake-a",
            "role": "implementation",
            "stage": "task",
            "run_status": "SUCCEEDED",
            "command": ["fake-a"],
            "cwd": str(tmp_path),
            "started_at": "2026-09-10T10:00:00+00:00",
            "finished_at": "2026-09-10T10:01:00+00:00",
            "exit_code": 0,
            "failure_class": "NONE",
            "provider_state": "COMPLETED",
            "summary": "did work token=should-redact? token=supersecretvalue12345678",
        },
    )
    db.insert(
        "run_context_manifests",
        {
            "run_id": "run-modern",
            "schema_version": "v1",
            "prompt_hash": "abc",
            "hash_basis": "redacted_rendered_utf8",
            "prompt_chars": 4000,
            "prompt_bytes": 4000,
            "prompt_words": 500,
            "estimated_prompt_tokens": 1000,
            "estimator_id": "char4-v1",
            "blocks_json": "[]",
            "capture_status": "CAPTURED",
            "redaction_status": "REDACTED",
            "created_at": "2026-09-10T10:00:00+00:00",
        },
    )
    db.insert(
        "run_usage",
        {
            "run_id": "run-modern",
            "input_tokens_total": 100,
            "output_tokens_total": 50,
            "source": "CLI_REPORTED",
            "completeness": "COMPLETE",
            "input_basis": "PER_STEP",
            "output_basis": "PER_STEP",
            "parser_version": "usage-v1",
            "evidence_kind": "test",
            "observations_count": 1,
            "native_counts_json": "{}",
            "captured_at": "2026-09-10T10:01:00+00:00",
        },
    )
    db.insert(
        "provider_runs",
        {
            "id": "run-legacy",
            "mission_id": "m1",
            "provider": "fake-a",
            "role": "implementation",
            "command": ["fake-a"],
            "cwd": str(tmp_path),
            "started_at": "2026-09-09T10:00:00+00:00",
            "failure_class": "NONE",
            "provider_state": "COMPLETED",
            "summary": "old",
        },
    )
    app = create_app(tmp_path / "api.db", cfg, orch)
    # Bypass auth by setting token empty? Use the real token header.
    from orchestrator.api.auth import load_or_create_token

    token = load_or_create_token((tmp_path / "api.db").parent)
    client = TestClient(app, headers={"Authorization": f"Bearer {token}"})
    return client, orch


def test_list_runs_pagination_and_filters(tmp_path: Path):
    client, _ = _app(tmp_path)
    resp = client.get("/api/runs", params={"limit": 1})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["runs"]) == 1
    assert body["has_more"] is True
    assert body["next_cursor"]
    # Filter by product narrows to modern only.
    resp2 = client.get("/api/runs", params={"product_project_id": "p1"})
    assert resp2.status_code == 200
    assert len(resp2.json()["runs"]) == 1
    assert resp2.json()["runs"][0]["id"] == "run-modern"


def test_get_run_includes_usage_and_redaction(tmp_path: Path):
    client, _ = _app(tmp_path)
    resp = client.get("/api/runs/run-modern")
    assert resp.status_code == 200
    body = resp.json()
    assert body["usage"]["input_tokens_total"] == 100
    assert body["context"]["estimated_prompt_tokens"] == 1000
    # Secret-shaped summary must be redacted on read.
    assert "supersecretvalue" not in body["run"]["summary"]


def test_legacy_run_without_manifest_is_not_captured(tmp_path: Path):
    client, _ = _app(tmp_path)
    resp = client.get("/api/runs/run-legacy/context")
    assert resp.status_code == 200
    assert resp.json()["capture_status"] == "NOT_CAPTURED"


def test_usage_analytics_excludes_unknown(tmp_path: Path):
    client, _ = _app(tmp_path)
    resp = client.get("/api/analytics/usage")
    assert resp.status_code == 200
    body = resp.json()
    assert "by_provider" in body
    assert "never zero-filled" in body["note"]
