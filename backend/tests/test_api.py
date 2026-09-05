"""API tests over ASGI transport (orchestrator started manually, no uvicorn)."""

from __future__ import annotations

import json
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
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        c.orchestrator = orch  # type: ignore[attr-defined]
        yield c
    await orch.shutdown()


async def test_health(client: httpx.AsyncClient):
    resp = await client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


async def test_project_crud(client: httpx.AsyncClient, workspace: Path):
    resp = await client.post("/api/projects", json={"path": str(workspace)})
    assert resp.status_code in (200, 201), resp.text
    project = resp.json()
    assert project["detected_type"] == "node"

    resp = await client.get("/api/projects")
    assert any(p["id"] == project["id"] for p in resp.json())

    resp = await client.post(f"/api/projects/{project['id']}/validate")
    assert resp.status_code == 200
    body = resp.json()
    assert body["project_type"] == "node"
    assert "git" in body

    resp = await client.post("/api/projects", json={"path": "/nonexistent/xyz"})
    assert resp.status_code == 400


async def test_mission_lifecycle_endpoints(client: httpx.AsyncClient, workspace: Path):
    project = (await client.post("/api/projects", json={"path": str(workspace)})).json()
    resp = await client.post(
        "/api/missions",
        json={"project_id": project["id"], "title": "t", "task": "do it", "autonomy": "AUTONOMOUS", "start": False},
    )
    assert resp.status_code == 201
    mission = resp.json()
    assert mission["status"] == "CREATED"

    resp = await client.get(f"/api/missions/{mission['id']}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["tasks"] == []
    assert body["gates"] == []

    for action in ("pause", "resume", "cancel"):
        resp = await client.post(f"/api/missions/{mission['id']}/{action}")
        assert resp.status_code == 200

    resp = await client.get(f"/api/missions/{mission['id']}")
    assert resp.json()["status"] == "CANCELLED"


async def test_providers_endpoint(client: httpx.AsyncClient):
    resp = await client.get("/api/providers")
    assert resp.status_code == 200
    providers = resp.json()
    assert any(p["name"] == "fake-a" for p in providers)

    resp = await client.post("/api/providers/fake-a/test")
    assert resp.status_code == 200
    assert resp.json()["installed"] is True

    resp = await client.post("/api/providers/fake-a/toggle", json={"enabled": False})
    assert resp.status_code == 200
    providers = (await client.get("/api/providers")).json()
    assert next(p for p in providers if p["name"] == "fake-a")["state"] == "DISABLED"


async def test_priority_settings(client: httpx.AsyncClient):
    resp = await client.get("/api/settings/priority")
    assert resp.status_code == 200
    matrix = resp.json()
    assert "planning" in matrix

    resp = await client.post(
        "/api/settings/priority", json={"role": "review", "providers": ["fake-a"]}
    )
    assert resp.status_code == 200
    assert resp.json()["review"] == ["fake-a"]

    resp = await client.post(
        "/api/settings/profiles",
        json={"name": "custom", "matrix": {"review": ["fake-a"]}},
    )
    assert resp.status_code == 200
    profiles = (await client.get("/api/settings/profiles")).json()
    assert profiles["custom"]["review"] == ["fake-a"]


async def test_events_replay_and_git_endpoint(client: httpx.AsyncClient, workspace: Path):
    orch = client.orchestrator  # type: ignore[attr-defined]
    project = (await client.post("/api/projects", json={"path": str(workspace)})).json()
    mission = orch.create_mission(project["id"], "m", "t", "AUTONOMOUS", "balanced")

    resp = await client.get(f"/api/missions/{mission['id']}/events")
    assert resp.status_code == 200
    assert any(e["type"] == "MISSION_CREATED" for e in resp.json())

    resp = await client.get(f"/api/projects/{project['id']}/git")
    assert resp.status_code == 200
    body = resp.json()
    assert "diff" in body and "recent_commits" in body

    resp = await client.get("/api/analytics")
    assert resp.status_code == 200
    assert "provider_stats" in resp.json()


async def test_websocket_replays_history(client: httpx.AsyncClient, workspace: Path):
    from starlette.testclient import TestClient

    orch = client.orchestrator  # type: ignore[attr-defined]
    project = (await client.post("/api/projects", json={"path": str(workspace)})).json()
    mission = orch.create_mission(project["id"], "m", "t", "AUTONOMOUS", "balanced")

    with TestClient(client._transport.app) as tc:  # type: ignore[union-attr]
        with tc.websocket_connect(f"/ws/missions/{mission['id']}") as ws:
            first = json.loads(ws.receive_text())
            assert first["type"] == "MISSION_CREATED"


async def test_f02_f17_terminal_states_and_404s(client: httpx.AsyncClient, workspace: Path):
    orch = client.orchestrator  # type: ignore[attr-defined]
    project = (await client.post("/api/projects", json={"path": str(workspace)})).json()

    # 404 on nonexistent mission
    for endpoint in ("start", "pause", "resume", "cancel"):
        resp = await client.post(f"/api/missions/nonexistent/{endpoint}")
        assert resp.status_code == 404

    # 409 on terminal statuses
    for status in ("COMPLETED", "CANCELLED", "FAILED", "UNVERIFIED"):
        m = orch.create_mission(project["id"], f"m-{status}", "t", "AUTONOMOUS", "balanced")
        orch.db.update("missions", m["id"], {"status": status})

        for endpoint in ("start", "pause", "resume"):
            resp = await client.post(f"/api/missions/{m['id']}/{endpoint}")
            assert resp.status_code == 409, f"{status} -> {endpoint} did not return 409: {resp.status_code}"

        # Cancel on already terminal is idempotent 200
        resp = await client.post(f"/api/missions/{m['id']}/cancel")
        assert resp.status_code == 200


async def test_f13_websocket_origin_check(client: httpx.AsyncClient, workspace: Path):
    from starlette.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    orch = client.orchestrator  # type: ignore[attr-defined]
    project = (await client.post("/api/projects", json={"path": str(workspace)})).json()
    mission = orch.create_mission(project["id"], "m", "t", "AUTONOMOUS", "balanced")

    with TestClient(client._transport.app) as tc:  # type: ignore[union-attr]
        # Untrusted origin rejected with 1008
        try:
            with tc.websocket_connect(
                f"/ws/missions/{mission['id']}", headers={"origin": "http://evil.example"}
            ) as ws:
                ws.receive_text()
                pytest.fail("untrusted origin was accepted")
        except WebSocketDisconnect as exc:
            assert exc.code == 1008

        # Trusted origin accepted
        with tc.websocket_connect(
            f"/ws/missions/{mission['id']}", headers={"origin": "http://localhost:5173"}
        ) as ws:
            first = json.loads(ws.receive_text())
            assert first["type"] == "MISSION_CREATED"


async def test_f14_project_deletion_fk_and_cascade(client: httpx.AsyncClient, workspace: Path):
    orch = client.orchestrator  # type: ignore[attr-defined]
    project = (await client.post("/api/projects", json={"path": str(workspace)})).json()
    orch.create_mission(project["id"], "m", "t", "AUTONOMOUS", "balanced")

    # Without force -> 409 Conflict
    resp = await client.delete(f"/api/projects/{project['id']}")
    assert resp.status_code == 409
    assert "force=true" in resp.json()["detail"]

    # With force=true -> 204 No Content
    resp = await client.delete(f"/api/projects/{project['id']}?force=true")
    assert resp.status_code == 204
    assert orch.db.get("projects", project["id"]) is None


async def test_f15_priority_persisted_to_settings(client: httpx.AsyncClient):
    orch = client.orchestrator  # type: ignore[attr-defined]
    resp = await client.post(
        "/api/settings/priority",
        json={"role": "planning", "providers": ["fake-b", "fake-a"]},
    )
    assert resp.status_code == 200
    row = orch.db.get("settings", "priority.planning", key="key")
    assert row is not None
    assert json.loads(row["value"]) == ["fake-b", "fake-a"]

