"""FastAPI application: REST + WebSocket surface for Mission Control."""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .. import git_ops
from ..config import Config
from ..models import utcnow
from ..orchestrator import IllegalMissionTransitionError, Orchestrator
from ..security import validate_workspace_path
from ..workspace import inspect_workspace

logger = logging.getLogger(__name__)


class CreateProjectRequest(BaseModel):
    path: str
    name: str | None = None


class CreateMissionRequest(BaseModel):
    project_id: str
    title: str
    task: str
    autonomy: str = Field(default="BALANCED", pattern="^(SAFE|BALANCED|AUTONOMOUS)$")
    profile: str = "balanced"
    scheduling_mode: str = Field(default="SEQUENTIAL", pattern="^(SEQUENTIAL|PARALLEL_SAFE)$")
    start: bool = True


class GateResolutionRequest(BaseModel):
    resolution: str


class PriorityUpdateRequest(BaseModel):
    role: str
    providers: list[str]


class ProfileSaveRequest(BaseModel):
    name: str
    matrix: dict[str, list[str]]


class ProviderToggleRequest(BaseModel):
    enabled: bool


def _jsonable(row: dict[str, Any]) -> dict[str, Any]:
    for key in ("providers_used", "providers_failed", "choices", "payload", "command"):
        if isinstance(row.get(key), str):
            try:
                row[key] = json.loads(row[key])
            except json.JSONDecodeError:
                pass
    return row


def create_app(db_path: Path, config: Config, orchestrator: Orchestrator) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
        await orchestrator.start()
        yield
        await orchestrator.shutdown()

    app = FastAPI(title="GG Orchestrator", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "tauri://localhost", "http://tauri.localhost"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.db = db_path

    # ---------------- projects ----------------
    @app.get("/api/projects")
    def list_projects() -> list[dict[str, Any]]:
        return orchestrator.db.query("SELECT * FROM projects ORDER BY created_at DESC")

    @app.post("/api/projects", status_code=201)
    async def add_project(req: CreateProjectRequest) -> dict[str, Any]:
        try:
            path = validate_workspace_path(req.path, orchestrator.config.allowed_roots())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        existing = orchestrator.db.get("projects", str(path), key="path")
        if existing:
            return existing
        info = await inspect_workspace(path, orchestrator.config.allowed_roots())
        project_id = __import__("uuid").uuid4().hex[:16]
        name = req.name or path.name
        orchestrator.db.insert(
            "projects",
            {
                "id": project_id,
                "name": name,
                "path": str(path),
                "detected_type": info.project_type,
                "created_at": utcnow(),
            },
        )
        return orchestrator.db.get("projects", project_id) or {}

    @app.get("/api/projects/{project_id}")
    def get_project(project_id: str) -> dict[str, Any]:
        project = orchestrator.db.get("projects", project_id)
        if not project:
            raise HTTPException(404, "project not found")
        project["missions"] = orchestrator.db.query(
            "SELECT id, title, status, created_at FROM missions WHERE project_id=? ORDER BY created_at DESC",
            (project_id,),
        )
        return project

    @app.delete("/api/projects/{project_id}", status_code=204)
    def remove_project(project_id: str, force: bool = False) -> None:
        project = orchestrator.db.get("projects", project_id)
        if not project:
            raise HTTPException(404, "project not found")
        missions = orchestrator.db.query("SELECT id FROM missions WHERE project_id=?", (project_id,))
        if missions and not force:
            raise HTTPException(
                409,
                f"cannot delete project {project_id}: contains {len(missions)} mission(s); "
                "pass force=true to cascade delete",
            )
        import sqlite3

        try:
            if force and missions:
                for m in missions:
                    mid = m["id"]
                    orchestrator.db.execute("DELETE FROM review_findings WHERE mission_id=?", (mid,))
                    orchestrator.db.execute("DELETE FROM reviews WHERE mission_id=?", (mid,))
                    orchestrator.db.execute("DELETE FROM human_gates WHERE mission_id=?", (mid,))
                    orchestrator.db.execute("DELETE FROM handoffs WHERE mission_id=?", (mid,))
                    orchestrator.db.execute("DELETE FROM tasks WHERE mission_id=?", (mid,))
                    orchestrator.db.execute("DELETE FROM provider_runs WHERE mission_id=?", (mid,))
                    orchestrator.db.execute("DELETE FROM events WHERE mission_id=?", (mid,))
                    orchestrator.db.execute("DELETE FROM missions WHERE id=?", (mid,))
            orchestrator.db.execute("DELETE FROM projects WHERE id=?", (project_id,))
        except sqlite3.IntegrityError as e:
            raise HTTPException(409, f"cannot delete project: {e}") from e

    @app.post("/api/projects/{project_id}/validate")
    async def validate_project(project_id: str) -> dict[str, Any]:
        project = orchestrator.db.get("projects", project_id)
        if not project:
            raise HTTPException(404, "project not found")
        info = await inspect_workspace(project["path"], orchestrator.config.allowed_roots())
        st = await git_ops.status(Path(project["path"]))
        return {
            "workspace": info.summary(),
            "project_type": info.project_type,
            "git": {
                "is_repo": st.is_repo,
                "branch": st.branch,
                "head": st.head,
                "clean": st.is_clean,
                "modified": st.modified,
                "untracked": st.untracked,
            },
        }

    # ---------------- missions ----------------
    @app.get("/api/missions")
    def list_missions(project_id: str | None = None) -> list[dict[str, Any]]:
        if project_id:
            rows = orchestrator.db.query("SELECT * FROM missions WHERE project_id=? ORDER BY created_at DESC",
                (project_id,))
        else:
            rows = orchestrator.db.query("SELECT * FROM missions ORDER BY created_at DESC LIMIT 200")
        return [_jsonable(r) for r in rows]

    @app.post("/api/missions", status_code=201)
    async def create_mission(req: CreateMissionRequest) -> dict[str, Any]:
        if not orchestrator.db.get("projects", req.project_id):
            raise HTTPException(404, "project not found")
        mission = orchestrator.create_mission(req.project_id, req.title, req.task, req.autonomy, req.profile)
        if req.scheduling_mode:
            orchestrator.db.update("missions", mission["id"], {"scheduling_mode": req.scheduling_mode})
            mission["scheduling_mode"] = req.scheduling_mode
        if req.start:
            orchestrator.start_mission(mission["id"])
        return _jsonable(mission)

    @app.get("/api/missions/{mission_id}")
    def get_mission(mission_id: str) -> dict[str, Any]:
        mission = orchestrator.db.get("missions", mission_id)
        if not mission:
            raise HTTPException(404, "mission not found")
        mission = _jsonable(mission)
        mission["tasks"] = orchestrator.db.query(
            "SELECT id, role, status, summary, attempts, created_at, finished_at "
            "FROM tasks WHERE mission_id=? ORDER BY created_at",
            (mission_id,),
        )
        mission["gates"] = [
            _jsonable(g)
            for g in orchestrator.db.query("SELECT * FROM human_gates WHERE mission_id=? ORDER BY created_at DESC",
                (mission_id,))
        ]
        mission["findings"] = orchestrator.db.query(
            "SELECT * FROM review_findings WHERE mission_id=? ORDER BY created_at", (mission_id,)
        )
        reviews = orchestrator.db.query(
            "SELECT * FROM reviews WHERE mission_id=? ORDER BY created_at", (mission_id,)
        )
        mission["reviews"] = reviews
        latest_review = reviews[-1] if reviews else None
        mission["latest_review"] = latest_review
        mission["degraded_review"] = bool(latest_review and not latest_review["independent"])
        mission["runs"] = orchestrator.db.query(
            "SELECT id, provider, role, failure_class, provider_state, exit_code, started_at, finished_at, "
            "summary FROM provider_runs WHERE mission_id=? ORDER BY started_at",
            (mission_id,),
        )
        latest_handoff = orchestrator.db.query(
            "SELECT * FROM handoffs WHERE mission_id=? ORDER BY created_at DESC LIMIT 1", (mission_id,)
        )
        mission["latest_handoff"] = latest_handoff[0]["content"] if latest_handoff else None
        return mission

    @app.post("/api/missions/{mission_id}/start")
    async def start_mission(mission_id: str) -> dict[str, str]:
        try:
            orchestrator.start_mission(mission_id)
        except KeyError:
            raise HTTPException(404, f"mission {mission_id} not found") from None
        except IllegalMissionTransitionError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"status": "started"}

    @app.post("/api/missions/{mission_id}/pause")
    async def pause_mission(mission_id: str) -> dict[str, str]:
        try:
            orchestrator.pause_mission(mission_id)
        except KeyError:
            raise HTTPException(404, f"mission {mission_id} not found") from None
        except IllegalMissionTransitionError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"status": "pausing"}

    @app.post("/api/missions/{mission_id}/resume")
    async def resume_mission(mission_id: str) -> dict[str, str]:
        try:
            orchestrator.resume_mission(mission_id)
        except KeyError:
            raise HTTPException(404, f"mission {mission_id} not found") from None
        except IllegalMissionTransitionError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"status": "resumed"}

    @app.post("/api/missions/{mission_id}/cancel")
    async def cancel_mission(mission_id: str) -> dict[str, str]:
        try:
            orchestrator.cancel_mission(mission_id)
        except KeyError:
            raise HTTPException(404, f"mission {mission_id} not found") from None
        except IllegalMissionTransitionError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"status": "cancelling"}

    @app.post("/api/missions/{mission_id}/retry")
    async def retry_mission(mission_id: str) -> dict[str, Any]:
        try:
            new_mission = orchestrator.retry_mission(mission_id)
            return new_mission
        except KeyError:
            raise HTTPException(404, f"mission {mission_id} not found") from None
        except IllegalMissionTransitionError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/missions/{mission_id}/gates/{gate_id}/resolve")
    async def resolve_gate(mission_id: str, gate_id: str, req: GateResolutionRequest) -> dict[str, str]:
        try:
            orchestrator.resolve_gate(gate_id, req.resolution)
        except IllegalMissionTransitionError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"status": "resolved"}

    # ---------------- DAG / parallel task endpoints ----------------
    @app.get("/api/missions/{mission_id}/dag")
    def get_mission_dag(mission_id: str) -> dict[str, Any]:
        mission = orchestrator.db.get("missions", mission_id)
        if not mission:
            raise HTTPException(404, "mission not found")
        tasks = orchestrator.db.query("SELECT * FROM tasks WHERE mission_id=?", (mission_id,))
        deps = orchestrator.db.query(
            "SELECT * FROM task_dependencies WHERE from_task_id IN (SELECT id FROM tasks WHERE mission_id=?)",
            (mission_id,),
        )
        branches = orchestrator.db.query(
            "SELECT * FROM task_branches WHERE task_id IN (SELECT id FROM tasks WHERE mission_id=?)",
            (mission_id,),
        )
        reservations = orchestrator.db.query(
            """SELECT pr.*, t.title FROM provider_reservations pr
               JOIN tasks t ON pr.task_id = t.id
               WHERE t.mission_id=? AND pr.released_at IS NULL""",
            (mission_id,),
        )
        locks = orchestrator.db.query(
            """SELECT tl.*, t.title FROM task_locks tl
               JOIN tasks t ON tl.task_id = t.id
               WHERE t.mission_id=? AND tl.released_at IS NULL""",
            (mission_id,),
        )
        return {
            "mission_id": mission_id,
            "scheduling_mode": mission.get("scheduling_mode", "SEQUENTIAL"),
            "tasks": [_jsonable(t) for t in tasks],
            "dependencies": [_jsonable(d) for d in deps],
            "branches": [_jsonable(b) for b in branches],
            "reservations": [_jsonable(r) for r in reservations],
            "locks": [_jsonable(lock) for lock in locks],
        }

    @app.get("/api/missions/{mission_id}/active-tasks")
    def get_active_tasks(mission_id: str) -> list[dict[str, Any]]:
        mission = orchestrator.db.get("missions", mission_id)
        if not mission:
            raise HTTPException(404, "mission not found")
        rows = orchestrator.db.query(
            """SELECT t.*, pr.provider, pr.started_at as provider_started
               FROM tasks t
               LEFT JOIN provider_runs pr ON t.provider_run_id = pr.id
               WHERE t.mission_id=? AND t.status IN ('CLAIMED','RUNNING','WAITING_FOR_PROVIDER')""",
            (mission_id,),
        )
        return [_jsonable(r) for r in rows]

    @app.post("/api/missions/{mission_id}/tasks/{task_id}/retry")
    async def retry_task(mission_id: str, task_id: str) -> dict[str, str]:
        task = orchestrator.db.get("tasks", task_id)
        if not task or task["mission_id"] != mission_id:
            raise HTTPException(404, "task not found")
        orchestrator.db.update(
            "tasks",
            task_id,
            {"status": "PENDING", "blocking_issue": "manual retry", "attempt": 0, "finished_at": None},
        )
        engine = orchestrator._engines.get(mission_id)
        if engine:
            engine.wake()
        return {"status": "retry queued"}

    @app.post("/api/missions/{mission_id}/tasks/{task_id}/cancel")
    async def cancel_task(mission_id: str, task_id: str) -> dict[str, str]:
        task = orchestrator.db.get("tasks", task_id)
        if not task or task["mission_id"] != mission_id:
            raise HTTPException(404, "task not found")
        orchestrator.db.update(
            "tasks",
            task_id,
            {"status": "CANCELLED", "finished_at": utcnow().isoformat()},
        )
        # Release any reservation/locks
        from .. import reservations as res_module
        from .. import task_locks as tl_module

        res_module.release_provider_reservation(orchestrator.db, orchestrator.events, task_id)
        tl_module.release_locks_for_task(orchestrator.db, orchestrator.events, task_id)
        return {"status": "task cancelled"}

    # ---------------- git ----------------
    @app.get("/api/projects/{project_id}/git")
    async def git_state(project_id: str) -> dict[str, Any]:
        project = orchestrator.db.get("projects", project_id)
        if not project:
            raise HTTPException(404, "project not found")
        root = Path(project["path"])
        st = await git_ops.status(root)
        return {
            "is_repo": st.is_repo,
            "branch": st.branch,
            "head": st.head,
            "modified": st.modified,
            "added": st.added,
            "deleted": st.deleted,
            "untracked": st.untracked,
            "diff_stat": await git_ops.diff(root, stat_only=True),
            "diff": (await git_ops.diff(root))[:20000],
            "recent_commits": await git_ops.recent_commits(root, 15),
        }

    # ---------------- providers ----------------
    @app.get("/api/providers")
    def list_providers() -> list[dict[str, Any]]:
        return orchestrator.registry.health()

    @app.post("/api/providers/{name}/test")
    async def test_provider(name: str) -> dict[str, Any]:
        adapter = orchestrator.registry.get_adapter(name)
        if not adapter:
            raise HTTPException(404, "provider not found")
        installed, path = adapter.detect()
        version = await adapter.get_version() if installed else None
        return {"installed": installed, "path": path, "version": version}

    @app.post("/api/providers/{name}/toggle")
    async def toggle_provider(name: str, req: ProviderToggleRequest) -> dict[str, str]:
        orchestrator.registry.set_enabled(name, req.enabled)
        return {"status": "enabled" if req.enabled else "disabled"}

    # ---------------- settings / priority ----------------
    @app.get("/api/settings/priority")
    def get_priority() -> dict[str, list[str]]:
        return {
            role: orchestrator.config.priority_for(role)
            for role in ("planning", "implementation", "testing", "review", "repair")
        }

    ALLOWED_ORIGINS = {
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "tauri://localhost",
        "http://tauri.localhost",
        "http://localhost:8787",
        "http://127.0.0.1:8787",
    }

    @app.post("/api/settings/priority")
    async def set_priority(req: PriorityUpdateRequest) -> dict[str, list[str]]:
        # Validate role
        valid_roles = {"planning", "implementation", "testing", "review", "repair"}
        if req.role not in valid_roles:
            raise HTTPException(422, f"Unknown role '{req.role}'. Must be one of: {sorted(valid_roles)}")
        # Validate providers
        known_providers = set(orchestrator.registry.adapters.keys())
        unknown = set(req.providers) - known_providers
        if unknown:
            raise HTTPException(422, f"Unknown provider(s): {sorted(unknown)}. Known: {sorted(known_providers)}")
        # Check for duplicates
        if len(req.providers) != len(set(req.providers)):
            raise HTTPException(422, "Duplicate providers in priority list")
        # Check non-empty
        if not req.providers:
            raise HTTPException(422, "Priority list must not be empty")
        orchestrator.config.set_priority(req.role, req.providers)
        orchestrator.db.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
            (f"priority.{req.role}", json.dumps(req.providers)),
        )
        return get_priority()

    @app.post("/api/settings/profiles")
    async def save_profile(req: ProfileSaveRequest) -> dict[str, str]:
        orchestrator.db.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)",
            (f"profile.{req.name}", json.dumps(req.matrix)),
        )
        return {"status": "saved"}

    @app.get("/api/settings/profiles")
    def list_profiles() -> dict[str, Any]:
        rows = orchestrator.db.query("SELECT key, value FROM settings WHERE key LIKE 'profile.%'")
        return {r["key"][len("profile."):]: json.loads(r["value"]) for r in rows}

    # ---------------- events / analytics ----------------
    @app.get("/api/missions/{mission_id}/events")
    def mission_events(mission_id: str, limit: int = 500) -> list[dict[str, Any]]:
        return orchestrator.events.history(mission_id, limit)

    @app.get("/api/analytics")
    def analytics() -> dict[str, Any]:
        return orchestrator.analytics()

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "time": utcnow().isoformat()}

    # ---------------- websocket ----------------
    @app.websocket("/ws/missions/{mission_id}")
    async def mission_ws(websocket: WebSocket, mission_id: str) -> None:
        origin = websocket.headers.get("origin")
        if origin and origin not in ALLOWED_ORIGINS:
            await websocket.close(code=1008)
            return
        await websocket.accept()
        queue = orchestrator.events.subscribe()
        try:
            # Replay: durable structured history (authoritative) + bounded
            # transient terminal tail. Raw provider output is NOT persisted
            # (see events.py); reconnect restores the last 500 lines at most.
            for row in orchestrator.events.history(mission_id, limit=1000):
                await websocket.send_text(json.dumps(row, default=str))
            for event in orchestrator.events.transient_replay(mission_id):
                await websocket.send_text(event.model_dump_json())
            while True:
                event = await queue.get()
                if event.mission_id == mission_id or event.mission_id is None:
                    await websocket.send_text(event.model_dump_json())
        except WebSocketDisconnect:
            pass
        finally:
            orchestrator.events.unsubscribe(queue)

    @app.websocket("/ws/events")
    async def global_ws(websocket: WebSocket) -> None:
        origin = websocket.headers.get("origin")
        if origin and origin not in ALLOWED_ORIGINS:
            await websocket.close(code=1008)
            return
        await websocket.accept()
        queue = orchestrator.events.subscribe()
        try:
            while True:
                event = await queue.get()
                await websocket.send_text(event.model_dump_json())
        except WebSocketDisconnect:
            pass
        finally:
            orchestrator.events.unsubscribe(queue)

    return app
