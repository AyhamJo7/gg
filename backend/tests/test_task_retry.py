"""Task retry endpoint: schema, ownership, terminal-mission rules, idempotency."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from conftest import make_config, make_orchestrator
from orchestrator.api.app import create_app
from orchestrator.providers.fake import FakeAdapter


@pytest.fixture()
async def client(tmp_path: Path, workspace: Path):
    orch = make_orchestrator(tmp_path, {"fake-a": FakeAdapter("fake-a", ["ok"])})
    await orch.registry.detect_all()
    app = create_app(tmp_path / "api.db", make_config(providers=["fake-a"]), orch)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers={"Authorization": f"Bearer {app.state.auth_token}"}
    ) as c:
        c.orchestrator = orch  # type: ignore[attr-defined]
        yield c
    await orch.shutdown()


async def _mission_with_task(client: httpx.AsyncClient, workspace: Path, title: str) -> tuple[dict, str]:
    project = (await client.post("/api/projects", json={"path": str(workspace)})).json()
    mission = (
        await client.post(
            "/api/missions",
            json={
                "project_id": project["id"],
                "title": title,
                "task": "do it",
                "autonomy": "AUTONOMOUS",
                "scheduling_mode": "PARALLEL_SAFE",
                "start": False,
            },
        )
    ).json()
    dag = {
        "tasks": [
            {
                "id": "task-a",
                "role": "implementation",
                "title": "task-a",
                "description": "do a",
                "preferred_providers": '["fake-a"]',
                "workspace_scope": '["src/a.py"]',
                "priority": 0,
            }
        ],
        "dependencies": [],
    }
    resp = await client.post(f"/api/missions/{mission['id']}/dag", json=dag)
    assert resp.status_code == 200, resp.text
    namespaced = f"{mission['id'][:8]}-task-a"
    return mission, namespaced


async def test_retry_failed_task_resets_attempts_and_status(client: httpx.AsyncClient, workspace: Path):
    mission, tid = await _mission_with_task(client, workspace, "retry-ok")
    db = client.orchestrator.db
    db.update("tasks", tid, {"status": "FAILED", "attempts": 2, "blocking_issue": "boom"})
    resp = await client.post(f"/api/missions/{mission['id']}/tasks/{tid}/retry")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "retry queued"
    row = db.get("tasks", tid)
    assert row is not None
    assert row["status"] == "PENDING"
    assert int(row["attempts"]) == 0
    assert row["blocking_issue"] == "manual retry"
    assert row["finished_at"] is None


async def test_retry_is_idempotent(client: httpx.AsyncClient, workspace: Path):
    mission, tid = await _mission_with_task(client, workspace, "retry-idem")
    db = client.orchestrator.db
    db.update("tasks", tid, {"status": "FAILED", "attempts": 3})
    for _ in range(2):
        resp = await client.post(f"/api/missions/{mission['id']}/tasks/{tid}/retry")
        assert resp.status_code == 200, resp.text
    row = db.get("tasks", tid)
    assert row is not None and row["status"] == "PENDING" and int(row["attempts"]) == 0


async def test_retry_rejects_foreign_task(client: httpx.AsyncClient, workspace: Path):
    m1, tid = await _mission_with_task(client, workspace, "retry-own")
    m2, _ = await _mission_with_task(client, workspace, "retry-other")
    resp = await client.post(f"/api/missions/{m2['id']}/tasks/{tid}/retry")
    assert resp.status_code == 404, resp.text
    resp = await client.post(f"/api/missions/{m1['id']}/tasks/no-such-task/retry")
    assert resp.status_code == 404, resp.text


async def test_retry_rejected_on_terminal_mission(client: httpx.AsyncClient, workspace: Path):
    mission, tid = await _mission_with_task(client, workspace, "retry-terminal")
    db = client.orchestrator.db
    db.update("tasks", tid, {"status": "FAILED", "attempts": 1})
    db.update("missions", mission["id"], {"status": "COMPLETED"})
    resp = await client.post(f"/api/missions/{mission['id']}/tasks/{tid}/retry")
    assert resp.status_code == 409, resp.text
    row = db.get("tasks", tid)
    assert row is not None and row["status"] == "FAILED"
