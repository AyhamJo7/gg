"""Task-log endpoint security: redaction, ownership, path containment.

Uses fake secrets only. Never real credentials.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from conftest import make_config, make_orchestrator
from orchestrator.api.app import create_app
from orchestrator.providers.fake import FakeAdapter

FAKE_ANTHROPIC = "sk-ant-FAKEFAKEFAKE1234567890abcdef"
FAKE_OPENAI = "sk-FAKEFAKEFAKEFAKE1234567890abcd"
FAKE_GH = "ghp_FAKEFAKEFAKEFAKEFAKE12"


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


async def _seed(client: httpx.AsyncClient, workspace: Path, title: str, log_body: str) -> tuple[str, str, Path]:
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
    assert (await client.post(f"/api/missions/{mission['id']}/dag", json=dag)).status_code == 200
    tid = f"{mission['id'][:8]}-task-a"
    log_dir = workspace / ".orchestrator" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stdout_file = log_dir / "run-1.stdout.log"
    stdout_file.write_text(log_body)
    db = client.orchestrator.db
    db.insert(
        "provider_runs",
        {
            "id": "run-1",
            "mission_id": mission["id"],
            "task_id": tid,
            "provider": "fake-a",
            "role": "implementation",
            "command": ["fake-a"],
            "cwd": str(workspace),
            "started_at": "2026-01-01T00:00:00+00:00",
            "finished_at": "2026-01-01T00:00:01+00:00",
            "exit_code": 0,
            "failure_class": "NONE",
            "provider_state": "COMPLETED",
            "stdout_path": str(stdout_file),
            "stderr_path": None,
            "summary": f"used key {FAKE_OPENAI} ok",
        },
    )
    return mission["id"], tid, stdout_file


async def test_log_secrets_redacted(client: httpx.AsyncClient, workspace: Path):
    mid, tid, _ = await _seed(
        client,
        workspace,
        "logs-redact",
        f"hello\nanthropic={FAKE_ANTHROPIC}\ngithub token {FAKE_GH}\ndone\n",
    )
    resp = await client.get(f"/api/missions/{mid}/tasks/{tid}/logs")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert FAKE_ANTHROPIC not in body["stdout"]
    assert FAKE_GH not in body["stdout"]
    assert "[REDACTED_ANTHROPIC_KEY]" in body["stdout"]
    assert "[REDACTED_GH_TOKEN]" in body["stdout"]
    assert "hello" in body["stdout"] and "done" in body["stdout"]
    assert FAKE_OPENAI not in body["run"]["summary"]
    assert "[REDACTED_API_KEY]" in body["run"]["summary"]


async def test_log_tail_is_bounded_and_line_aligned(client: httpx.AsyncClient, workspace: Path):
    lines = "".join(f"line {i:04d} padding-padding-padding\n" for i in range(200))
    mid, tid, _ = await _seed(client, workspace, "logs-tail", lines)
    resp = await client.get(f"/api/missions/{mid}/tasks/{tid}/logs?tail_bytes=1024")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["stdout_size"] == len(lines.encode())
    assert body["stdout_truncated"] is True
    assert len(body["stdout"].encode()) <= 1024 + 64
    # starts on a line boundary — no split first line
    assert body["stdout"].startswith("line ")
    assert "line 0199" in body["stdout"]
    # small request within size serves everything untruncated
    resp = await client.get(f"/api/missions/{mid}/tasks/{tid}/logs")
    assert resp.json()["stdout_truncated"] is False
    assert resp.json()["stdout_size"] == len(lines.encode())


async def test_log_symlink_escape_not_served(client: httpx.AsyncClient, workspace: Path):
    mid, tid, _ = await _seed(client, workspace, "logs-symlink", "hello\n")
    db = client.orchestrator.db
    secret = workspace / "real-secret.txt"
    secret.write_text("sk-ant-FAKEFAKEFAKE1234567890abcdef\n")
    link = workspace / ".orchestrator" / "logs" / "evil.stdout.log"
    link.symlink_to(secret)
    db.update("provider_runs", "run-1", {"stdout_path": str(link)})
    resp = await client.get(f"/api/missions/{mid}/tasks/{tid}/logs")
    assert resp.status_code == 200, resp.text
    assert resp.json()["stdout"] == ""
    assert "FAKEFAKEFAKE" not in resp.text


async def test_log_yaml_value_at_boundary_redacted(client: httpx.AsyncClient, workspace: Path):
    """End-to-end MED-01 repro: anchor pushed out of the served region."""
    project = (await client.post("/api/projects", json={"path": str(workspace)})).json()
    mission = (
        await client.post(
            "/api/missions",
            json={
                "project_id": project["id"],
                "title": "logs-yaml",
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
    assert (await client.post(f"/api/missions/{mission['id']}/dag", json=dag)).status_code == 200
    tid = f"{mission['id'][:8]}-task-a"
    log_dir = workspace / ".orchestrator" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "run-yaml.stdout.log"
    log_file.write_bytes(b"f" * 2000 + b"password:\n  fakepassvalue123\n" + b"t\n" * 50)
    db = client.orchestrator.db
    db.insert(
        "provider_runs",
        {
            "id": "run-yaml",
            "mission_id": mission["id"],
            "task_id": tid,
            "provider": "fake-a",
            "role": "implementation",
            "command": ["fake-a"],
            "cwd": str(workspace),
            "started_at": "2026-01-01T00:00:00+00:00",
            "finished_at": "2026-01-01T00:00:01+00:00",
            "exit_code": 0,
            "failure_class": "NONE",
            "provider_state": "COMPLETED",
            "stdout_path": str(log_file),
            "stderr_path": None,
            "summary": "",
        },
    )
    resp = await client.get(f"/api/missions/{mission['id']}/tasks/{tid}/logs?tail_bytes=1024")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["stdout_truncated"] is True
    assert "fakepassvalue123" not in body["stdout"]


async def test_log_mismatched_ids_404(client: httpx.AsyncClient, workspace: Path):
    mid, tid, _ = await _seed(client, workspace, "logs-owner", "hello\n")
    other = (
        await client.post(
            "/api/missions",
            json={
                "project_id": (await client.post("/api/projects", json={"path": str(workspace)})).json()["id"],
                "title": "logs-other",
                "task": "other",
                "autonomy": "AUTONOMOUS",
                "start": False,
            },
        )
    ).json()
    assert (await client.get(f"/api/missions/{other['id']}/tasks/{tid}/logs")).status_code == 404
    assert (await client.get(f"/api/missions/{mid}/tasks/no-such-task/logs")).status_code == 404


async def test_log_path_outside_log_dir_not_served(client: httpx.AsyncClient, workspace: Path):
    mid, tid, _ = await _seed(client, workspace, "logs-escape", "hello\n")
    db = client.orchestrator.db
    outside = workspace / "secret-outside.txt"
    outside.write_text(f"top secret {FAKE_ANTHROPIC}\n")
    db.update("provider_runs", "run-1", {"stdout_path": str(outside)})
    resp = await client.get(f"/api/missions/{mid}/tasks/{tid}/logs")
    assert resp.status_code == 200, resp.text
    assert resp.json()["stdout"] == ""
    assert FAKE_ANTHROPIC not in resp.text
    # sibling task log inside the dir is still scoped per task row, not path-guessable
    db.update("provider_runs", "run-1", {"stdout_path": "/etc/hostname"})
    resp = await client.get(f"/api/missions/{mid}/tasks/{tid}/logs")
    assert resp.status_code == 200, resp.text
    assert resp.json()["stdout"] == ""
