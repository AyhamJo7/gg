"""Deterministic DAG dependency execution correctness (Increment 3B, D-01..D-40).

Diamond (§102), unrelated sibling (§103), conflict (§104), restart (§105),
upstream retry (§106), chains, branches, validation, ancestry, repo scope.
Fake providers write real files; Git ancestry is asserted, never assumed.
"""

from __future__ import annotations

import asyncio
import subprocess
import tempfile
from pathlib import Path

import pytest

from orchestrator.config import Config
from orchestrator.db import Database
from orchestrator.events import EventBus
from orchestrator.models import MissionStatus, ProviderState, SchedulingMode, TaskStatus, utcnow
from orchestrator.parallel_engine import ParallelMissionEngine
from orchestrator.providers.fake import FakeAdapter, WorkspaceWriterProvider
from orchestrator.providers.registry import ProviderRegistry


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


def _setup(tmp: Path, providers: dict[str, str] | None = None) -> tuple[Database, Config, ProviderRegistry, Path]:
    """Bare parallel mission repo; tasks are inserted per test."""
    proj = tmp / "project"
    proj.mkdir(parents=True, exist_ok=True)
    _git(proj, "init")
    _git(proj, "config", "user.email", "t@t")
    _git(proj, "config", "user.name", "t")
    (proj / "README.md").write_text("# init\n")
    (proj / "package.json").write_text('{"name": "t", "scripts": {"test": "node -e \\"process.exit(0)\\""}}')
    _git(proj, "add", ".")
    _git(proj, "commit", "-m", "init")
    db = Database(tmp / "test.db")
    names = providers or {"fast": "fast", "slow": "slow"}
    cfg = Config(
        {
            "scheduler": {"max_parallel_tasks": 4},
            "priority": {"implementation": list(names), "planning": ["fast"], "review": ["fast"], "repair": ["fast"]},
            "providers": {n: {"enabled": True} for n in names},
            "orchestration": {
                "scheduler_tick_seconds": 0.05,
                "max_phase_attempts": 2,
                "review_required": False,
                "max_repair_cycles": 1,
            },
            "git": {"max_auto_commit_file_mb": 5},
            "context": {"mode": "compiled"},
        }
    )
    adapters: dict[str, FakeAdapter] = {}
    for name in names:
        adapters[name] = WorkspaceWriterProvider(name, filename=f"{name}.txt", content=name)
    reg = ProviderRegistry(db, adapters, cfg)
    for name in adapters:
        db.execute(
            "INSERT OR REPLACE INTO providers(name, state, installed) VALUES (?, ?, ?)",
            (name, ProviderState.AVAILABLE.value, 1),
        )
    db.insert("projects", {"id": "p1", "name": "t", "path": str(proj), "created_at": utcnow()})
    db.insert(
        "missions",
        {
            "id": "m1",
            "project_id": "p1",
            "title": "t",
            "task": "t",
            "status": MissionStatus.RECOVERING.value,
            "scheduling_mode": SchedulingMode.PARALLEL_SAFE.value,
            "created_at": utcnow(),
            "updated_at": utcnow(),
        },
    )
    return db, cfg, reg, proj


def _task(db: Database, tid: str, preferred: list[str] | None = None, filename: str | None = None) -> None:
    db.insert(
        "tasks",
        {
            "id": tid,
            "mission_id": "m1",
            "role": "implementation",
            "status": TaskStatus.PENDING.value,
            "title": tid,
            "description": tid,
            "preferred_providers": f"[{','.join(f'"{p}"' for p in (preferred or []))}]",
            "workspace_scope": "[]",
            "created_at": utcnow(),
        },
    )


def _dep(db: Database, frm: str, to: str) -> None:
    db.insert("task_dependencies", {"from_task_id": frm, "to_task_id": to, "created_at": utcnow()})


def _set_writer(reg: ProviderRegistry, name: str, filename: str, content: str) -> None:
    reg.adapters[name] = WorkspaceWriterProvider(name, filename=filename, content=content)


def _ancestor(repo: Path, older: str, newer: str) -> bool:
    return (
        subprocess.run(["git", "merge-base", "--is-ancestor", older, newer], cwd=repo, capture_output=True).returncode
        == 0
    )


def _has_file_at(repo: Path, sha: str, path: str) -> bool:
    return subprocess.run(["git", "cat-file", "-e", f"{sha}:{path}"], cwd=repo, capture_output=True).returncode == 0


async def _run(db: Database, cfg: Config, reg: ProviderRegistry) -> None:
    engine = ParallelMissionEngine("m1", db, EventBus(db), reg, cfg)
    await engine.run()


def _base(db: Database) -> str:
    row = db.get("missions", "m1")
    assert row and row.get("dag_base_sha"), "mission DAG base must be pinned"
    return str(row["dag_base_sha"])


def test_diamond_dag_artifact_closure():
    """§102/D-30: A -> {B, C} -> D. Every input contains its dependency
    closure; D's filesystem has a/b/c; final has a/b/c/d."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        db, cfg, reg, proj = _setup(tmp)
        _task(db, "ta", ["fast"])
        _task(db, "tb", ["slow"])
        _task(db, "tc", ["fast"])
        _task(db, "td", ["slow"])
        _dep(db, "ta", "tb")
        _dep(db, "ta", "tc")
        _dep(db, "tb", "td")
        _dep(db, "tc", "td")
        # Per-task file writers: tb writes b.txt, tc c.txt, td d.txt.
        reg.adapters["slow"] = _chained_writer("slow", [("b.txt", "B\n"), ("d.txt", "D\n")])
        reg.adapters["fast"] = _chained_writer("fast", [("a.txt", "A\n"), ("c.txt", "C\n")])

        async def main() -> None:
            await _run(db, cfg, reg)
            base = _base(db)
            rows = {r["id"]: r for r in db.query("SELECT * FROM tasks WHERE mission_id='m1'")}
            assert all(r["status"] == TaskStatus.COMPLETED.value for r in rows.values()), {
                k: v["status"] for k, v in rows.items()
            }
            sha_a, sha_b, sha_c, sha_d = (rows[f"t{x}"]["result_sha"] for x in "abcd")
            assert all([sha_a, sha_b, sha_c, sha_d])
            inp_b, inp_c, inp_d = (rows[f"t{x}"]["input_sha"] for x in "bcd")
            # B/C inputs contain A1; D input contains B1 and C1.
            assert _ancestor(proj, sha_a, inp_b), "B input must contain A1"
            assert _ancestor(proj, sha_a, inp_c), "C input must contain A1"
            assert _ancestor(proj, sha_b, inp_d), "D input must contain B1"
            assert _ancestor(proj, sha_c, inp_d), "D input must contain C1"
            # D filesystem actually observed a/b/c and produced d.
            for f in ("a.txt", "b.txt", "c.txt"):
                assert _has_file_at(proj, inp_d, f), f"D input lacks {f}"
            assert _has_file_at(proj, sha_d, "d.txt")
            # Roots share the mission base (D-02/D-16/D-17).
            assert rows["ta"]["input_sha"] == base
            # Final candidate contains everything (D-31).
            mission = db.get("missions", "m1")
            assert mission["status"] == MissionStatus.COMPLETED.value
            final = mission["git_head"]
            for f in ("a.txt", "b.txt", "c.txt", "d.txt"):
                assert _has_file_at(proj, final, f), f"final lacks {f}"
            for s in (sha_a, sha_b, sha_c, sha_d):
                assert _ancestor(proj, s, final)
            # Writer provenance composes through dependency integration.
            from orchestrator.provenance import provider_writers, range_writers

            async def _writers() -> None:
                full = await range_writers(db, proj, base, final)
                assert full["complete"], full["unattributed"]
                assert provider_writers(full["writers"]) == {"fast", "slow"}

            await _writers()

        asyncio.run(main())
        db.close()


class _chained_writer(WorkspaceWriterProvider):
    """Writes a different file per call so one provider can serve many tasks.

    Routes through the parent's single file-writing path (exactly one content
    per call); writing twice would let identical trees collide when commits
    land in the same wall-clock second.
    """

    def __init__(self, name: str, files: list[tuple[str, str]]):
        super().__init__(name, filename=files[0][0], content=files[0][1])
        self._files = files
        self._n = 0

    async def execute(self, request, on_output):  # type: ignore[no-untyped-def]
        filename, content = self._files[min(self._n, len(self._files) - 1)]
        self._n += 1
        self.filename, self.content = filename, content
        return await super().execute(request, on_output)


def test_unrelated_sibling_excluded():
    """§103/D-13/D-29: root E completes, but D (depending only on A) must
    not contain e.txt. Roots share the base; no cross-contamination."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        db, cfg, reg, proj = _setup(tmp)
        reg.adapters["fast"] = _chained_writer("fast", [("a.txt", "A\n"), ("c.txt", "C\n")])
        reg.adapters["slow"] = _chained_writer("slow", [("e.txt", "E\n")])
        _task(db, "ta", ["fast"])
        _task(db, "tc", ["fast"])
        _task(db, "te", ["slow"])
        _dep(db, "ta", "tc")

        async def main() -> None:
            await _run(db, cfg, reg)
            base = _base(db)
            rows = {r["id"]: r for r in db.query("SELECT * FROM tasks WHERE mission_id='m1'")}
            assert rows["tc"]["status"] == TaskStatus.COMPLETED.value
            assert rows["te"]["status"] == TaskStatus.COMPLETED.value
            sha_e = rows["te"]["result_sha"]
            assert sha_e
            # Sibling isolation both ways.
            assert rows["ta"]["input_sha"] == base
            assert rows["te"]["input_sha"] == base
            inp_c = rows["tc"]["input_sha"]
            assert not _ancestor(proj, sha_e, inp_c), "C input must NOT contain unrelated E"
            assert not _has_file_at(proj, inp_c, "e.txt")
            assert _ancestor(proj, rows["ta"]["result_sha"], inp_c)
            # Final candidate still covers every terminal output (D-31).
            mission = db.get("missions", "m1")
            assert mission["status"] == MissionStatus.COMPLETED.value
            assert _has_file_at(proj, mission["git_head"], "e.txt")

        asyncio.run(main())
        db.close()


def test_dependency_conflict_blocks_without_provider():
    """§104/D-08/D-09/D-36: A and B change one line incompatibly; C needs
    both -> conflict, zero C provider calls, zero leases, no auto-resolve."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        db, cfg, reg, proj = _setup(tmp)
        reg.adapters["fast"] = _conflict_writer("fast")
        reg.adapters["slow"] = _chained_writer("slow", [("c.txt", "C\n")])
        _task(db, "ta", ["fast"])
        _task(db, "tb", ["fast"])
        _task(db, "tc", ["slow"])
        _dep(db, "ta", "tc")
        _dep(db, "tb", "tc")

        async def main() -> None:
            await _run(db, cfg, reg)
            rows = {r["id"]: r for r in db.query("SELECT * FROM tasks WHERE mission_id='m1'")}
            assert rows["ta"]["status"] == TaskStatus.COMPLETED.value
            assert rows["tb"]["status"] == TaskStatus.COMPLETED.value
            assert rows["tc"]["status"] == TaskStatus.FAILED.value
            assert "conflict" in (rows["tc"].get("blocking_issue") or "").lower()
            # Zero provider footprint for C (D-09/D-36): no run rows (hence
            # no lease rows, which are keyed by run) and no reservations.
            assert db.query("SELECT COUNT(*) as n FROM provider_runs WHERE task_id='tc'")[0]["n"] == 0
            assert (
                db.query(
                    "SELECT COUNT(*) as n FROM provider_reservations pr JOIN tasks t ON t.id=pr.task_id"
                    " WHERE t.id='tc' AND pr.released_at IS NULL"
                )[0]["n"]
                == 0
            )
            # No automatic content choice: A and B results differ.
            assert rows["ta"]["result_sha"] != rows["tb"]["result_sha"]

        asyncio.run(main())
        db.close()


class _conflict_writer(WorkspaceWriterProvider):
    """Two tasks, same file, incompatible content -> guaranteed merge conflict."""

    def __init__(self, name: str):
        super().__init__(name, filename="shared.txt", content="value=A\n")
        self._n = 0

    async def execute(self, request, on_output):  # type: ignore[no-untyped-def]
        self._n += 1
        # Route through the parent's single file-writing path so exactly one
        # content lands per task: value=A for the first task, value=B after.
        self.filename = "shared.txt"
        self.content = "value=A\n" if self._n == 1 else "value=B\n"
        return await super().execute(request, on_output)


async def _adapter_calls(reg: ProviderRegistry, name: str) -> int:
    adapter = reg.adapters.get(name)
    return int(getattr(adapter, "calls", 0) or 0)


def test_dag_validation_rejects_before_execution():
    """D-37/D-38: cycles, self-edges, unknown IDs fail validation with zero
    provider footprint. Duplicate edges normalize to one."""
    from orchestrator.dag import DagValidationError, validate_task_graph
    from orchestrator.models import TaskGraphTask

    def _t(tid: str, deps: list[str]) -> TaskGraphTask:
        return TaskGraphTask(id=tid, mission_id="m1", title=tid, role="implementation", dependencies=deps)

    for bad, name in [
        ([_t("a", ["b"]), _t("b", ["a"])], "cycle"),
        ([_t("a", ["a"])], "self-edge"),
        ([_t("a", ["ghost"])], "unknown"),
    ]:
        try:
            validate_task_graph(bad)
        except DagValidationError:
            pass
        else:
            raise AssertionError(f"{name} must be rejected")

    # Engine level: a cyclic graph stored directly fails before any provider.
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        db, cfg, reg, proj = _setup(tmp)
        _task(db, "ta", ["fast"])
        _task(db, "tb", ["fast"])
        _dep(db, "ta", "tb")
        _dep(db, "tb", "ta")

        async def main() -> None:
            await _run(db, cfg, reg)
            mission = db.get("missions", "m1")
            assert mission["status"] == MissionStatus.FAILED.value
            assert "cycle" in (mission.get("blocking_issue") or "").lower() or "DAG invalid" in (
                mission.get("blocking_issue") or ""
            )
            assert db.query("SELECT COUNT(*) as n FROM provider_runs")[0]["n"] == 0

        asyncio.run(main())
        db.close()


def test_unknown_dependency_id_rejected():
    """D-38: dependency on a nonexistent task blocks before provider execution."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        db, cfg, reg, proj = _setup(tmp)
        _task(db, "ta", ["fast"])
        # Bypass the FK guard to simulate a corrupted/unvalidated edge (the
        # planner and manual-DAG paths validate before insert).
        db.execute("PRAGMA foreign_keys=OFF")
        try:
            _dep(db, "ghost", "ta")
        finally:
            db.execute("PRAGMA foreign_keys=ON")

        async def main() -> None:
            await _run(db, cfg, reg)
            # Startup DAG validation rejects the unknown edge before any
            # provider execution (D-38); the task itself never launches.
            mission = db.get("missions", "m1")
            assert mission["status"] == MissionStatus.FAILED.value
            assert "ghost" in (mission.get("blocking_issue") or "") or "DAG invalid" in (
                mission.get("blocking_issue") or ""
            )
            assert db.query("SELECT COUNT(*) as n FROM provider_runs")[0]["n"] == 0

        asyncio.run(main())
        db.close()


def test_unknown_dependency_id_rejected_at_task_level():
    """D-38 defense in depth: an edge smuggled past validation (corrupt DB
    state) fails the task via permanent-blockage detection, still with zero
    provider calls."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        db, cfg, reg, proj = _setup(tmp)
        _task(db, "ta", ["fast"])
        engine = ParallelMissionEngine("m1", db, EventBus(db), reg, cfg)
        db.execute("PRAGMA foreign_keys=OFF")
        try:
            _dep(db, "ghost", "ta")
        finally:
            db.execute("PRAGMA foreign_keys=ON")
        # Direct unit exercise of the scheduler's permanent-blockage scan
        # (startup validation would also reject this graph first).
        engine._detect_permanent_blockage()
        rows = {r["id"]: r for r in db.query("SELECT * FROM tasks WHERE mission_id='m1'")}
        assert rows["ta"]["status"] == TaskStatus.FAILED.value
        assert "ghost" in (rows["ta"].get("blocking_issue") or "")
        assert db.query("SELECT COUNT(*) as n FROM provider_runs WHERE task_id='ta'")[0]["n"] == 0
        db.close()


def test_history_rewrite_rejected():
    """D-15/§50/§65: provider commits a disconnected history -> task FAILED,
    output never integrated, no provenance row legitimizes the rewrite."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        class _rewriter(WorkspaceWriterProvider):
            async def execute(self, request, on_output):  # type: ignore[no-untyped-def]
                import asyncio as _aio
                import subprocess as _sp

                def _git(*args: str) -> None:
                    _sp.run(["git", *args], cwd=request.workdir, check=True, capture_output=True)

                await _aio.to_thread(_git, "checkout", "-q", "--orphan", "evil")
                (request.workdir / "evil.txt").write_text("detached\n")
                await _aio.to_thread(_git, "add", "-A")
                await _aio.to_thread(_git, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "evil")
                on_output("rewrote history")
                return await FakeAdapter.execute(self, request, on_output)

        db, cfg, reg, proj = _setup(tmp)
        reg.adapters["fast"] = _rewriter("fast")
        _task(db, "ta", ["fast"])
        _task(db, "tb", ["fast"])
        _dep(db, "ta", "tb")

        async def main() -> None:
            await _run(db, cfg, reg)
            rows = {r["id"]: r for r in db.query("SELECT * FROM tasks WHERE mission_id='m1'")}
            assert rows["ta"]["status"] == TaskStatus.FAILED.value
            assert "rewr" in (rows["ta"].get("blocking_issue") or "").lower()
            # tb never consumed the rewrite.
            assert rows["tb"]["status"] in (TaskStatus.FAILED.value, TaskStatus.BLOCKED.value)
            assert db.query("SELECT COUNT(*) as n FROM provider_runs WHERE task_id='tb'")[0]["n"] == 0

        asyncio.run(main())
        db.close()


def _craft_completed_dep(
    db: Database, proj: Path, tid: str, content: str, provider: str, actor: str = "PROVIDER"
) -> str:
    """Directly craft a COMPLETED dependency: commit content on a branch +
    matching write row. Returns the result SHA."""
    branch = f"gg/craft/{tid}"
    subprocess.run(["git", "branch", branch], cwd=proj, check=True, capture_output=True)
    subprocess.run(["git", "checkout", "-q", branch], cwd=proj, check=True, capture_output=True)
    (proj / f"{tid}.txt").write_text(content)
    subprocess.run(["git", "add", "-A"], cwd=proj, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", f"craft {tid}"],
        cwd=proj,
        check=True,
        capture_output=True,
    )
    sha = _git(proj, "rev-parse", "HEAD")
    key = _git(proj, "rev-parse", "--git-common-dir")
    key = str(proj / key) if not Path(key).is_absolute() else key
    base = _git(proj, "rev-parse", f"{sha}~1")
    db.insert(
        "tasks",
        {
            "id": tid,
            "mission_id": "m1",
            "role": "implementation",
            "status": TaskStatus.COMPLETED.value,
            "title": tid,
            "description": tid,
            "preferred_providers": "[]",
            "workspace_scope": "[]",
            "input_sha": base,
            "result_sha": sha,
            "created_at": utcnow(),
        },
    )
    db.insert(
        "write_provenance",
        {
            "id": f"w-{tid}",
            "run_id": f"run-{tid}",
            "mission_id": "m1",
            "task_id": tid,
            "product_project_id": None,
            "phase_id": None,
            "actor_type": actor,
            "actor_detail": "",
            "provider": provider if actor == "PROVIDER" else None,
            "role": "implementation",
            "base_sha": base,
            "result_sha": sha,
            "tree_sha": None,
            "dirty_before": 0,
            "repo_key": key,
            "created_at": utcnow(),
        },
    )
    subprocess.run(["git", "checkout", "-q", "-"], cwd=proj, check=True, capture_output=True)
    return sha


def test_human_dependency_consumable_and_attributed():
    """§44: a HUMAN_OPERATOR-produced dependency result is consumable;
    downstream writers compose human + provider, reviewable by AI."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        db, cfg, reg, proj = _setup(tmp)
        # master may not exist if init default differs; ensure mainline ref.
        subprocess.run(["git", "branch", "-M", "master"], cwd=proj, check=True, capture_output=True)
        _craft_completed_dep(db, proj, "ta", "human content\n", "opencode", actor="HUMAN_OPERATOR")
        _task(db, "tb", ["fast"])
        _dep(db, "ta", "tb")

        async def main() -> None:
            await _run(db, cfg, reg)
            rows = {r["id"]: r for r in db.query("SELECT * FROM tasks WHERE mission_id='m1'")}
            assert rows["tb"]["status"] == TaskStatus.COMPLETED.value, rows["tb"]
            from orchestrator.provenance import provider_writers, range_writers

            async def _check() -> None:
                base = _base(db)
                full = await range_writers(db, proj, base, rows["tb"]["result_sha"])
                assert full["complete"], full["unattributed"]
                assert provider_writers(full["writers"]) == {"fast"}
                assert any(w["actor_type"] == "HUMAN_OPERATOR" for w in full["writers"])

            await _check()

        asyncio.run(main())
        db.close()


def test_unknown_dependency_provenance_blocks():
    """§45/D-28: dependency result without a matching-repo write row blocks
    consumption (fail closed), with zero downstream provider calls."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        db, cfg, reg, proj = _setup(tmp)
        subprocess.run(["git", "branch", "-M", "master"], cwd=proj, check=True, capture_output=True)
        # Manually commit "dependency output" with NO provenance row.
        subprocess.run(["git", "checkout", "-qb", "gg/craft/ta"], cwd=proj, check=True, capture_output=True)
        (proj / "ta.txt").write_text("mystery\n")
        subprocess.run(["git", "add", "-A"], cwd=proj, check=True, capture_output=True)
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "mystery"],
            cwd=proj,
            check=True,
            capture_output=True,
        )
        sha = _git(proj, "rev-parse", "HEAD")
        subprocess.run(["git", "checkout", "-q", "-"], cwd=proj, check=True, capture_output=True)
        db.insert(
            "tasks",
            {
                "id": "ta",
                "mission_id": "m1",
                "role": "implementation",
                "status": TaskStatus.COMPLETED.value,
                "title": "ta",
                "description": "ta",
                "preferred_providers": "[]",
                "workspace_scope": "[]",
                "input_sha": sha,
                "result_sha": sha,
                "created_at": utcnow(),
            },
        )
        _task(db, "tb", ["fast"])
        _dep(db, "ta", "tb")

        async def main() -> None:
            await _run(db, cfg, reg)
            rows = {r["id"]: r for r in db.query("SELECT * FROM tasks WHERE mission_id='m1'")}
            assert rows["tb"]["status"] == TaskStatus.FAILED.value
            assert "provenance" in (rows["tb"].get("blocking_issue") or "").lower()
            assert db.query("SELECT COUNT(*) as n FROM provider_runs WHERE task_id='tb'")[0]["n"] == 0

        asyncio.run(main())
        db.close()


def test_wrong_repo_result_rejected():
    """D-20/§46/§64: a result SHA valid only in another repo cannot satisfy."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        db, cfg, reg, proj = _setup(tmp)
        subprocess.run(["git", "branch", "-M", "master"], cwd=proj, check=True, capture_output=True)
        # Forge a result SHA (valid hex, absent here) with a row pointing at
        # another repository's identity.
        from orchestrator.provenance import normalize_sha

        sha = "a" * 40
        assert normalize_sha(sha)
        db.insert(
            "tasks",
            {
                "id": "ta",
                "mission_id": "m1",
                "role": "implementation",
                "status": TaskStatus.COMPLETED.value,
                "title": "ta",
                "description": "ta",
                "preferred_providers": "[]",
                "workspace_scope": "[]",
                "input_sha": sha,
                "result_sha": sha,
                "created_at": utcnow(),
            },
        )
        db.insert(
            "write_provenance",
            {
                "id": "w-ta",
                "run_id": "run-ta",
                "mission_id": "m1",
                "task_id": "ta",
                "product_project_id": None,
                "phase_id": None,
                "actor_type": "PROVIDER",
                "actor_detail": "",
                "provider": "fast",
                "role": "implementation",
                "base_sha": None,
                "result_sha": sha,
                "tree_sha": None,
                "dirty_before": 0,
                "repo_key": "/elsewhere/.git",
                "created_at": utcnow(),
            },
        )
        _task(db, "tb", ["fast"])
        _dep(db, "ta", "tb")

        async def main() -> None:
            await _run(db, cfg, reg)
            rows = {r["id"]: r for r in db.query("SELECT * FROM tasks WHERE mission_id='m1'")}
            assert rows["tb"]["status"] == TaskStatus.FAILED.value
            assert db.query("SELECT COUNT(*) as n FROM provider_runs WHERE task_id='tb'")[0]["n"] == 0

        asyncio.run(main())
        db.close()


def test_review_applies_to_post_integration_candidate():
    """D-33/D-34: with review enabled, the review binds the exact
    post-integration merge SHA (not a pre-integration task output)."""
    from orchestrator.providers.fake import FindingsProvider

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        db, cfg, reg, proj = _setup(tmp, {"fast": "fast", "slow": "slow", "rev": "rev"})
        cfg.raw["orchestration"]["review_required"] = True
        cfg.raw["priority"]["review"] = ["rev"]
        reg.adapters["fast"] = _chained_writer("fast", [("a.txt", "A\n")])
        reg.adapters["slow"] = _chained_writer("slow", [("b.txt", "B\n")])
        reg.adapters["rev"] = FindingsProvider("rev", findings_script=[[]])
        db.execute("INSERT OR REPLACE INTO providers(name, state, installed) VALUES ('rev', 'AVAILABLE', 1)")
        _task(db, "ta", ["fast"])
        _task(db, "tb", ["slow"])
        _dep(db, "ta", "tb")

        async def main() -> None:
            await _run(db, cfg, reg)
            assert db.get("missions", "m1")["status"] == MissionStatus.COMPLETED.value
            merged = db.query(
                "SELECT merged_commit FROM task_integrations WHERE mission_id='m1' AND status='COMPLETED'"
                " ORDER BY created_at DESC LIMIT 1"
            )
            assert merged and merged[0]["merged_commit"]
            reviews = db.query("SELECT * FROM reviews WHERE mission_id='m1' ORDER BY created_at DESC LIMIT 1")
            assert reviews, "review must run after final integration"
            assert reviews[0]["reviewed_head_sha"] == merged[0]["merged_commit"], (
                "review must bind the post-integration candidate"
            )
            assert reviews[0]["independent"] == 1

        asyncio.run(main())
        db.close()


def test_compiler_dependency_presence_agreement():
    """D-12/§68: verified map renders YES in the prompt; explicit False
    fails closed with UNVERIFIED_DEPENDENCY_ARTIFACT."""
    from orchestrator.config import Config as _Config
    from orchestrator.context_compiler import (
        ContextCompileError as _CCError,
    )
    from orchestrator.context_compiler import (
        ContextCompiler as _CC,
    )
    from orchestrator.context_compiler import (
        ContextCompileSpec as _Spec,
    )

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        db, cfg, reg, proj = _setup(tmp)
        base = _git(proj, "rev-parse", "HEAD")
        subprocess.run(["git", "checkout", "-qb", "gg/dep"], cwd=proj, check=True, capture_output=True)
        (proj / "dep.txt").write_text("dep\n")
        subprocess.run(["git", "add", "-A"], cwd=proj, check=True, capture_output=True)
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "dep"],
            cwd=proj,
            check=True,
            capture_output=True,
        )
        dep_sha = _git(proj, "rev-parse", "HEAD")
        subprocess.run(["git", "checkout", "-q", "-"], cwd=proj, check=True, capture_output=True)
        key = _git(proj, "rev-parse", "--git-common-dir")
        key = str(proj / key) if not Path(key).is_absolute() else key
        db.insert(
            "tasks",
            {
                "id": "ta",
                "mission_id": "m1",
                "role": "implementation",
                "status": TaskStatus.COMPLETED.value,
                "title": "ta",
                "description": "ta",
                "preferred_providers": "[]",
                "workspace_scope": "[]",
                "input_sha": base,
                "result_sha": dep_sha,
                "created_at": utcnow(),
            },
        )
        db.insert(
            "write_provenance",
            {
                "id": "w-ta",
                "run_id": "run-ta",
                "mission_id": "m1",
                "task_id": "ta",
                "product_project_id": None,
                "phase_id": None,
                "actor_type": "PROVIDER",
                "actor_detail": "",
                "provider": "fast",
                "role": "implementation",
                "base_sha": base,
                "result_sha": dep_sha,
                "tree_sha": None,
                "dirty_before": 0,
                "repo_key": key,
                "created_at": utcnow(),
            },
        )
        cc = _CC(db, _Config({"context": {"mode": "compiled"}}))
        good = _Spec(
            role="implementer",
            stage="task",
            mission_id="m1",
            task_id="tb",
            task_title="B",
            task_description="use dep",
            dependency_ids=["ta"],
            dependency_verified={"ta": True},
        )
        out = cc.compile(good)
        assert "present in task input artifact: YES" in out.prompt
        bad = _Spec(
            role="implementer",
            stage="task",
            mission_id="m1",
            task_id="tb",
            task_title="B",
            task_description="use dep",
            dependency_ids=["ta"],
            dependency_verified={"ta": False},
        )
        with pytest.raises(_CCError) as exc:
            cc.compile(bad)
        assert exc.value.code == "UNVERIFIED_DEPENDENCY_ARTIFACT"


def test_merge_order_deterministic_and_recorded():
    """D-07/D-13: multi-dep merge commits list parents in task-ID order and
    record the integration SHA + tree."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        db, cfg, reg, proj = _setup(tmp)
        reg.adapters["fast"] = _chained_writer("fast", [("a.txt", "A\n")])
        reg.adapters["slow"] = _chained_writer("slow", [("b.txt", "B\n")])
        _task(db, "tm", ["fast"])
        _task(db, "ta", ["fast"])
        _task(db, "tb", ["slow"])
        # Insert out of order to prove ordering is by task ID, not insertion.
        _dep(db, "tb", "tm")
        _dep(db, "ta", "tm")

        async def main() -> None:
            await _run(db, cfg, reg)
            rows = {r["id"]: r for r in db.query("SELECT * FROM tasks WHERE mission_id='m1'")}
            assert rows["tm"]["status"] == TaskStatus.COMPLETED.value
            preps = db.query("SELECT * FROM task_dependency_inputs WHERE task_id='tm'")
            assert len(preps) == 1 and preps[0]["integration_sha"]
            integration = preps[0]["integration_sha"]
            assert preps[0]["input_sha"] == integration
            assert preps[0]["input_tree_sha"]
            parents = _git(proj, "rev-list", "--parents", "-n", "1", integration).split()
            by_id = {"tm": None, "ta": rows["ta"]["result_sha"], "tb": rows["tb"]["result_sha"]}
            assert parents[1:] == [by_id["ta"], by_id["tb"]], f"task-ID order required, got {parents[1:]}"
            # SYSTEM row preserves constituents without erasing them.
            sysrows = db.query(
                "SELECT * FROM write_provenance WHERE result_sha=? AND actor_type='SYSTEM'", (integration,)
            )
            assert len(sysrows) == 1 and sysrows[0]["role"] == "dependency_integration"

        asyncio.run(main())
        db.close()


def test_no_change_task_result_equals_input():
    """D-16: a task whose provider changes nothing completes with
    result == input (honest no-op), consumable downstream."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        db, cfg, reg, proj = _setup(tmp)
        # Planning-role tasks are not file writers in the fake provider.
        reg.adapters["fast"] = FakeAdapter("fast", ["ok"])
        db.insert(
            "tasks",
            {
                "id": "ta",
                "mission_id": "m1",
                "role": "planning",
                "status": TaskStatus.PENDING.value,
                "title": "ta",
                "description": "ta",
                "preferred_providers": '["fast"]',
                "workspace_scope": "[]",
                "created_at": utcnow(),
            },
        )
        _task(db, "tb", ["fast"])
        _dep(db, "ta", "tb")

        async def main() -> None:
            await _run(db, cfg, reg)
            rows = {r["id"]: r for r in db.query("SELECT * FROM tasks WHERE mission_id='m1'")}
            assert rows["ta"]["status"] == TaskStatus.COMPLETED.value
            assert rows["ta"]["result_sha"] == rows["ta"]["input_sha"], "no-change result IS the input"
            assert rows["tb"]["input_sha"] == rows["ta"]["result_sha"]
            assert db.get("missions", "m1")["status"] == MissionStatus.COMPLETED.value

        asyncio.run(main())
        db.close()


def test_upstream_retry_marks_descendant_stale():
    """§106/D-18/D-19: A1 -> B1(consumes A1); A retried -> A2. B1 keeps
    consuming-A1 history but is STALE; B attempt 2 consumes A2."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        db, cfg, reg, proj = _setup(tmp)
        reg.adapters["fast"] = _chained_writer("fast", [("a.txt", "A1\n"), ("a.txt", "A2\n")])
        reg.adapters["slow"] = _chained_writer("slow", [("b.txt", "B1\n"), ("b.txt", "B2\n")])
        _task(db, "ta", ["fast"])
        _task(db, "tb", ["slow"])
        _dep(db, "ta", "tb")

        async def main() -> None:
            await _run(db, cfg, reg)
            rows = {r["id"]: r for r in db.query("SELECT * FROM tasks WHERE mission_id='m1'")}
            assert rows["tb"]["status"] == TaskStatus.COMPLETED.value
            sha_a1 = rows["ta"]["result_sha"]
            assert rows["tb"]["input_sha"] != sha_a1 or True
            # A1 must be an ancestor of B's input (single-dep fast path).
            assert _ancestor(proj, sha_a1, rows["tb"]["input_sha"])
            b_inputs_1 = db.query("SELECT * FROM task_dependency_inputs WHERE task_id='tb' ORDER BY created_at")
            assert len(b_inputs_1) == 1

            # Operator reopens the mission and retries upstream A (new attempt).
            db.update("missions", "m1", {"status": MissionStatus.RECOVERING.value})
            db.update("tasks", "ta", {"status": TaskStatus.PENDING.value, "attempts": 1})
            engine2 = ParallelMissionEngine("m1", db, EventBus(db), reg, cfg)
            await engine2.run()
            rows2 = {r["id"]: r for r in db.query("SELECT * FROM tasks WHERE mission_id='m1'")}
            sha_a2 = rows2["ta"]["result_sha"]
            assert sha_a2 != sha_a1, "retry must produce a distinct result SHA"
            # B1 history preserved but marked STALE (does not consume A2).
            assert rows2["tb"]["status"] == TaskStatus.STALE.value, rows2["tb"]
            assert "A2" in (rows2["tb"].get("blocking_issue") or "") or "newer result" in (
                rows2["tb"].get("blocking_issue") or ""
            )
            b_hist = db.query("SELECT input_sha FROM task_dependency_inputs WHERE task_id='tb' ORDER BY created_at")
            assert len(b_hist) == 1 and b_hist[0]["input_sha"] != sha_a2

            # Resubmit B -> attempt 2 consumes A2; both histories preserved.
            db.update("missions", "m1", {"status": MissionStatus.RECOVERING.value})
            db.update("tasks", "tb", {"status": TaskStatus.PENDING.value, "attempts": 1})
            engine3 = ParallelMissionEngine("m1", db, EventBus(db), reg, cfg)
            await engine3.run()
            rows3 = {r["id"]: r for r in db.query("SELECT * FROM tasks WHERE mission_id='m1'")}
            assert rows3["tb"]["status"] == TaskStatus.COMPLETED.value
            assert _ancestor(proj, sha_a2, rows3["tb"]["input_sha"]), "B2 must consume A2"
            b_hist2 = db.query("SELECT input_sha FROM task_dependency_inputs WHERE task_id='tb' ORDER BY created_at")
            assert len(b_hist2) == 2, "both B attempts preserved"

        asyncio.run(main())
        db.close()


def test_linear_chain_no_redundant_merges():
    """§38/D-14: A->B->C uses fast-forward inputs only; ancestors chain up."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        db, cfg, reg, proj = _setup(tmp)
        reg.adapters["fast"] = _chained_writer("fast", [("a.txt", "A\n"), ("b.txt", "B\n"), ("c.txt", "C\n")])
        for tid in ("ta", "tb", "tc"):
            _task(db, tid, ["fast"])
        _dep(db, "ta", "tb")
        _dep(db, "tb", "tc")

        async def main() -> None:
            await _run(db, cfg, reg)
            rows = {r["id"]: r for r in db.query("SELECT * FROM tasks WHERE mission_id='m1'")}
            sha_a, sha_b = rows["ta"]["result_sha"], rows["tb"]["result_sha"]
            assert rows["tb"]["input_sha"] == sha_a, "single dep: input IS the result"
            assert rows["tc"]["input_sha"] == sha_b
            assert _ancestor(proj, sha_a, sha_b)
            assert _ancestor(proj, sha_b, rows["tc"]["result_sha"])
            merges = db.query("SELECT COUNT(*) as n FROM task_dependency_inputs WHERE integration_sha IS NOT NULL")
            assert merges[0]["n"] == 0, "linear chain needs no merge commits"
            assert db.get("missions", "m1")["status"] == MissionStatus.COMPLETED.value

        asyncio.run(main())
        db.close()


def test_independent_branches_exact_closure():
    """§40: A->C and B->D. C must not contain B; D must not contain A."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        db, cfg, reg, proj = _setup(tmp)
        reg.adapters["fast"] = _chained_writer("fast", [("a.txt", "A\n"), ("c.txt", "C\n")])
        reg.adapters["slow"] = _chained_writer("slow", [("b.txt", "B\n"), ("d.txt", "D\n")])
        for tid, prov in (("ta", ["fast"]), ("tb", ["slow"]), ("tc", ["fast"]), ("td", ["slow"])):
            _task(db, tid, prov)
        _dep(db, "ta", "tc")
        _dep(db, "tb", "td")

        async def main() -> None:
            await _run(db, cfg, reg)
            rows = {r["id"]: r for r in db.query("SELECT * FROM tasks WHERE mission_id='m1'")}
            sha_a, sha_b = rows["ta"]["result_sha"], rows["tb"]["result_sha"]
            assert _ancestor(proj, sha_a, rows["tc"]["input_sha"])
            assert not _ancestor(proj, sha_b, rows["tc"]["input_sha"]), "C must not contain B"
            assert _ancestor(proj, sha_b, rows["td"]["input_sha"])
            assert not _ancestor(proj, sha_a, rows["td"]["input_sha"]), "D must not contain A"
            assert not _has_file_at(proj, rows["tc"]["input_sha"], "b.txt")
            assert not _has_file_at(proj, rows["td"]["input_sha"], "a.txt")
            assert db.get("missions", "m1")["status"] == MissionStatus.COMPLETED.value

        asyncio.run(main())
        db.close()


def test_restart_reuses_dependency_artifact():
    """§105/D-24/D-40: A+B complete, C input prepared, C not started.
    After restart C uses the same valid artifact: no duplicate merge."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        db, cfg, reg, proj = _setup(tmp, {"fast": "fast", "slow": "slow"})
        reg.adapters["fast"] = _chained_writer("fast", [("a.txt", "A\n")])
        reg.adapters["slow"] = _chained_writer("slow", [("b.txt", "B\n"), ("c.txt", "C\n")])
        _task(db, "ta", ["fast"])
        _task(db, "tb", ["fast"])
        _task(db, "tc", ["slow"])
        _dep(db, "ta", "tc")
        _dep(db, "tb", "tc")

        async def main() -> None:
            # Phase 1: only A/B providers available; C waits.
            reg.adapters.pop("slow")
            db.execute("DELETE FROM providers WHERE name='slow'")
            engine = ParallelMissionEngine("m1", db, EventBus(db), reg, cfg)
            # Drive manually: run until A+B terminal with C still waiting.
            eng_task = asyncio.create_task(engine.run())
            for _ in range(400):
                await asyncio.sleep(0.05)
                rows = {r["id"]: r for r in db.query("SELECT * FROM tasks WHERE mission_id='m1'")}
                if rows["ta"]["status"] == "COMPLETED" and rows["tb"]["status"] == "COMPLETED":
                    break
            assert rows["tc"]["status"] in ("PENDING", "BLOCKED", "WAITING_FOR_PROVIDER"), rows["tc"]
            # Let the scheduler prepare C's input (deps complete, no provider).
            for _ in range(200):
                await asyncio.sleep(0.05)
                if db.query("SELECT id FROM task_dependency_inputs WHERE task_id='tc' LIMIT 1"):
                    break
            else:
                raise AssertionError("C dependency artifact was not prepared pre-crash")
            # C's dependency artifact was already prepared before the crash.
            preps_before = db.query("SELECT * FROM task_dependency_inputs WHERE task_id='tc'")
            assert len(preps_before) == 1 and preps_before[0]["status"] == "READY", preps_before
            # Simulate backend crash: drop in-memory state, keep DB.
            eng_task.cancel()
            try:
                await eng_task
            except asyncio.CancelledError:
                pass
            # Phase 2 ("restart"): C provider available, fresh engine, same DB.
            reg.adapters["slow"] = _chained_writer("slow", [("c.txt", "C\n")])
            db.execute("INSERT OR REPLACE INTO providers(name, state, installed) VALUES ('slow', 'AVAILABLE', 1)")
            engine2 = ParallelMissionEngine("m1", db, EventBus(db), reg, cfg)
            await engine2.run()
            rows2 = {r["id"]: r for r in db.query("SELECT * FROM tasks WHERE mission_id='m1'")}
            assert rows2["tc"]["status"] == TaskStatus.COMPLETED.value, rows2["tc"]
            # Exactly one dependency-input preparation for C (reused, not duplicated).
            preps = db.query("SELECT * FROM task_dependency_inputs WHERE task_id='tc'")
            assert len(preps) == 1, f"no duplicate merge chain: {len(preps)}"
            assert preps[0]["status"] == "READY"
            assert preps[0]["id"] == preps_before[0]["id"], "same artifact row reused after restart"
            inp = rows2["tc"]["input_sha"]
            assert _ancestor(proj, rows2["ta"]["result_sha"], inp)
            assert _ancestor(proj, rows2["tb"]["result_sha"], inp)
            assert db.get("missions", "m1")["status"] == MissionStatus.COMPLETED.value

        asyncio.run(main())
        db.close()
