"""Deterministic artifact-evidence evaluation (Increment 3, P-07..P-17, P-27).

Hand-built git history + evidence rows, no providers: review/verification/
criterion/fresh validity across SHAs, writer-set independence, plan-revision
invalidation, unknown-writer fail-closed, and the full READY state.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from orchestrator.db import Database
from orchestrator.models import utcnow
from orchestrator.provenance import EvidenceInputs, PhaseCandidate, evaluate_artifact_evidence

PLAN = {
    "product_name": "P",
    "goal": "g",
    "users": "u",
    "requirements": [
        {
            "id": "R1",
            "title": "T1",
            "description": "d",
            "kind": "functional",
            "acceptance": [{"id": "R1-A1", "description": "works", "verify": "npm run test"}],
        }
    ],
    "architecture": {"backend": "b", "frontend": "f", "database": "d", "decisions": []},
    "phases": [],
}


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, str, str, str]:
    ws = tmp_path / "repo"
    ws.mkdir()
    _git(ws, "init")
    _git(ws, "config", "user.email", "t@t")
    _git(ws, "config", "user.name", "t")
    (ws / "app.txt").write_text("v0\n")
    _git(ws, "add", ".")
    _git(ws, "commit", "-m", "init")
    init = _git(ws, "rev-parse", "HEAD")
    (ws / "app.txt").write_text("v1 opencode\n")
    _git(ws, "add", ".")
    _git(ws, "commit", "-m", "impl")
    sha_a = _git(ws, "rev-parse", "HEAD")
    (ws / "app.txt").write_text("v2 claude repair\n")
    _git(ws, "add", ".")
    _git(ws, "commit", "-m", "repair")
    sha_b = _git(ws, "rev-parse", "HEAD")
    return ws, init, sha_a, sha_b


def _product(db: Database, pid: str = "prod-1", revision: int = 1) -> None:
    db.insert(
        "projects",
        {"id": "proj-1", "name": "p", "path": "/tmp/e", "detected_type": "node", "created_at": "t"},  # noqa: S108
    )
    db.insert(
        "missions",
        {
            "id": "m-1",
            "project_id": "proj-1",
            "title": "t",
            "task": "t",
            "status": "COMPLETED",
            "created_at": "t",
            "updated_at": "t",
        },
    )
    db.insert(
        "product_projects",
        {
            "id": pid,
            "name": "P",
            "idea": "i",
            "constraints_text": "",
            "state": "EXECUTING",
            "acceptance_state": "",
            "auto_execute": 0,
            "require_plan_approval": 0,
            "target_project_id": None,
            "target_repo_path": "",
            "plan_revision": revision,
            "created_at": "t",
            "updated_at": "t",
        },
    )
    db.insert(
        "plan_revisions",
        {
            "id": f"rev-{revision}",
            "project_id": pid,
            "revision": revision,
            "plan_json": PLAN,
            "created_by": "t",
            "reason": "t",
            "created_at": "t",
        },
    )


def _write(
    db: Database,
    run_id: str | None,
    mission: str,
    provider: str | None,
    role: str,
    base: str | None,
    result: str,
    actor: str = "PROVIDER",
    dirty: bool = False,
) -> None:
    db.insert(
        "write_provenance",
        {
            "id": f"w-{run_id or result[:8]}",
            "run_id": run_id,
            "mission_id": mission,
            "task_id": None,
            "product_project_id": "prod-1",
            "phase_id": "phase-1",
            "actor_type": actor,
            "actor_detail": "",
            "provider": provider,
            "role": role,
            "base_sha": base,
            "result_sha": result,
            "tree_sha": None,
            "dirty_before": int(dirty),
            "repo_key": "",
            "created_at": utcnow().isoformat(),
        },
    )


def _review(
    db: Database, mission: str, reviewer: str, base: str, head: str, independent: bool, writers: list[str]
) -> None:
    import json as _json

    db.insert(
        "reviews",
        {
            "id": f"rev-{mission}-{head[:8]}",
            "mission_id": mission,
            "implementation_provider": "opencode",
            "review_provider": reviewer,
            "independent": int(independent),
            "degradation_reason": None,
            "review_parsed": 1,
            "reviewed_base_sha": base,
            "reviewed_head_sha": head,
            "writer_set_json": _json.dumps(writers),
            "created_at": utcnow().isoformat(),
        },
    )


def _verify(db: Database, sha: str, status: str = "passed") -> None:
    db.insert(
        "verification_attempts",
        {
            "id": f"ver-{sha[:8]}-{status}",
            "mission_id": "m-1",
            "product_project_id": "prod-1",
            "task_id": None,
            "sha": sha,
            "repo_key": "",
            "kind": "toolchain",
            "commands_json": "[]",
            "status": status,
            "exit_code": 0,
            "started_at": "t",
            "finished_at": "t",
            "summary": "",
        },
    )


def _criterion(db: Database, sha: str, revision: int = 1, result: str = "SATISFIED", context: str = "fresh") -> None:
    db.insert(
        "criterion_attempts",
        {
            "id": f"crit-{sha[:8]}-{revision}-{context}",
            "project_id": "prod-1",
            "criterion_id": "R1-A1",
            "requirement_id": "R1",
            "plan_revision": revision,
            "command": "npm run test",
            "checked_sha": sha,
            "context": context,
            "capability": "REPLAYABLE",
            "result": result,
            "exit_code": 0,
            "output_tail": "",
            "recheck_of": None,
            "created_at": "t",
        },
    )


def _fresh(db: Database, sha: str, status: str = "passed") -> None:
    db.insert(
        "fresh_checkout_attempts",
        {
            "id": f"fresh-{sha[:8]}-{status}",
            "project_id": "prod-1",
            "sha": sha,
            "repo_key": "",
            "status": status,
            "detail": "",
            "commands_json": "[]",
            "created_at": "t",
        },
    )


def _inputs(repo: Path, candidate: str, phases: list[PhaseCandidate], oldest_base: str | None) -> EvidenceInputs:
    return EvidenceInputs(
        project_id="prod-1",
        candidate_sha=candidate,
        plan_revision=1,
        repo=repo,
        phases=phases,
        oldest_base_sha=oldest_base,
    )


def _phase(candidate: str, mission: str = "m-1") -> PhaseCandidate:
    return PhaseCandidate(phase_id="phase-1", phase_key="foundation", candidate_sha=candidate, mission_id=mission)


async def _evaluate(db: Database, inputs: EvidenceInputs) -> dict:
    return await evaluate_artifact_evidence(db, inputs)


def test_review_valid_at_same_sha_stale_after_repair(tmp_path: Path):
    """P-09/P-10: review of A is VALID for A, STALE for B."""
    import asyncio

    ws, init, sha_a, sha_b = _repo(tmp_path)
    db = Database(tmp_path / "e.db")
    _product(db)
    _write(db, "run-impl", "m-1", "opencode", "implementation", init, sha_a)
    _write(db, "run-rep", "m-1", "claude", "repair", sha_a, sha_b)
    _review(db, "m-1", "agy", init, sha_a, True, ["opencode"])

    out_a = asyncio.run(_evaluate(db, _inputs(ws, sha_a, [_phase(sha_a)], init)))
    assert out_a["review"]["state"] == "VALID", out_a["review"]
    assert out_a["review"]["phases"][0]["reviewer"] == "agy"

    out_b = asyncio.run(_evaluate(db, _inputs(ws, sha_b, [_phase(sha_b)], init)))
    assert out_b["review"]["state"] == "STALE", out_b["review"]
    assert any("changed after review" in r for r in out_b["blocking_reasons"])
    assert not out_b["delivery_ready"]
    db.close()


def test_reviewer_who_wrote_is_not_independent(tmp_path: Path):
    """P-08: reviewer inside the candidate writer set fails independence."""
    import asyncio

    ws, init, sha_a, sha_b = _repo(tmp_path)
    db = Database(tmp_path / "e.db")
    _product(db)
    _write(db, "run-impl", "m-1", "opencode", "implementation", init, sha_a)
    _write(db, "run-rep", "m-1", "claude", "repair", sha_a, sha_b)
    _review(db, "m-1", "claude", init, sha_b, False, ["opencode", "claude"])

    out = asyncio.run(_evaluate(db, _inputs(ws, sha_b, [_phase(sha_b)], init)))
    assert out["review"]["state"] == "FAILED", out["review"]
    assert any("candidate writer" in r or "not independent" in r for r in out["blocking_reasons"])
    db.close()


def test_unknown_commit_blocks_delivery(tmp_path: Path):
    """P-05: an unattributed commit in range fails writer completeness."""
    import asyncio

    ws, init, sha_a, sha_b = _repo(tmp_path)
    (ws / "app.txt").write_text("v3 mystery\n")
    _git(ws, "add", ".")
    _git(ws, "commit", "-m", "mystery")
    sha_c = _git(ws, "rev-parse", "HEAD")
    db = Database(tmp_path / "e.db")
    _product(db)
    _write(db, "run-impl", "m-1", "opencode", "implementation", init, sha_a)
    _write(db, "run-rep", "m-1", "claude", "repair", sha_a, sha_b)
    # NOTE: no write row for sha_c.
    _review(db, "m-1", "agy", init, sha_c, True, ["opencode", "claude"])

    out = asyncio.run(_evaluate(db, _inputs(ws, sha_c, [_phase(sha_c)], init)))
    assert not out["writers_complete"]
    assert out["review"]["state"] in ("FAILED", "STALE", "MISSING")
    assert any("INCOMPLETE" in r or "unreviewed" in r for r in out["blocking_reasons"])
    assert not out["delivery_ready"]
    db.close()


def test_evidence_classes_bound_to_exact_sha(tmp_path: Path):
    """P-12/P-13/P-15/P-17: A-evidence cannot satisfy B; full B evidence is READY."""
    import asyncio

    ws, init, sha_a, sha_b = _repo(tmp_path)
    db = Database(tmp_path / "e.db")
    _product(db)
    _write(db, "run-impl", "m-1", "opencode", "implementation", init, sha_a)
    _write(db, "run-rep", "m-1", "claude", "repair", sha_a, sha_b)
    _review(db, "m-1", "agy", init, sha_a, True, ["opencode"])
    _verify(db, sha_a)
    _criterion(db, sha_a)
    _fresh(db, sha_a)

    partial = asyncio.run(_evaluate(db, _inputs(ws, sha_b, [_phase(sha_b)], init)))
    assert partial["verification"]["state"] == "MISSING"
    assert partial["fresh_checkout"]["state"] == "MISSING"
    assert partial["criteria"]["missing"] == 1
    assert not partial["delivery_ready"]

    # Complete the B evidence: independent re-review + all checks at B.
    _review(db, "m-1", "agy", init, sha_b, True, ["opencode", "claude"])
    _verify(db, sha_b)
    _criterion(db, sha_b)
    _fresh(db, sha_b)
    full = asyncio.run(_evaluate(db, _inputs(ws, sha_b, [_phase(sha_b)], init)))
    assert full["review"]["state"] == "VALID", full["review"]
    assert full["verification"]["state"] == "VALID"
    assert full["criteria"]["passed"] == 1
    assert full["fresh_checkout"]["state"] == "VALID"
    assert full["writers_complete"]
    assert full["delivery_ready"] is True, full["blocking_reasons"]
    db.close()


def test_plan_revision_invalidates_criteria(tmp_path: Path):
    """P-27: rev-1 evidence cannot satisfy rev-2 requirements."""
    import asyncio

    ws, init, sha_a, _ = _repo(tmp_path)
    db = Database(tmp_path / "e.db")
    _product(db)
    db.insert(
        "plan_revisions",
        {
            "id": "rev-2",
            "project_id": "prod-1",
            "revision": 2,
            "plan_json": PLAN,
            "created_by": "t",
            "reason": "t",
            "created_at": "t",
        },
    )
    db.update("product_projects", "prod-1", {"plan_revision": 2})
    _write(db, "run-impl", "m-1", "opencode", "implementation", init, sha_a)
    _criterion(db, sha_a, revision=1)

    inputs = EvidenceInputs(
        project_id="prod-1",
        candidate_sha=sha_a,
        plan_revision=2,
        repo=ws,
        phases=[_phase(sha_a)],
        oldest_base_sha=init,
    )
    out = asyncio.run(_evaluate(db, inputs))
    assert out["criteria"]["missing"] == 1, out["criteria"]
    assert not out["delivery_ready"]
    db.close()


def test_human_writer_keeps_provenance_complete(tmp_path: Path):
    """P-06: HUMAN_OPERATOR commits are attributed, not unknown."""
    import asyncio

    ws, init, sha_a, sha_b = _repo(tmp_path)
    (ws / "app.txt").write_text("v1.1 human tweak\n")
    _git(ws, "add", ".")
    _git(ws, "commit", "-m", "human tweak")
    sha_h = _git(ws, "rev-parse", "HEAD")
    db = Database(tmp_path / "e.db")
    _product(db)
    _write(db, "run-impl", "m-1", "opencode", "implementation", init, sha_a)
    _write(db, "run-rep", "m-1", "claude", "repair", sha_a, sha_b)
    _write(db, None, "m-1", None, "human", sha_b, sha_h, actor="HUMAN_OPERATOR")

    out = asyncio.run(_evaluate(db, _inputs(ws, sha_h, [_phase(sha_h)], init)))
    assert out["writers_complete"], out["blocking_reasons"]
    actors = {w["actor_type"] for w in out["writers"]}
    assert "HUMAN_OPERATOR" in actors
    db.close()


def test_legacy_criterion_results_compat(tmp_path: Path):
    """Migration-era criterion_results rows (no attempts yet) still count."""
    import asyncio

    ws, init, sha_a, _ = _repo(tmp_path)
    db = Database(tmp_path / "e.db")
    _product(db)
    _write(db, "run-impl", "m-1", "opencode", "implementation", init, sha_a)
    db.execute(
        "INSERT INTO criterion_results(project_id, criterion_id, requirement_id, status, command,"
        " exit_code, output_tail, sha, checked_at, plan_revision, context)"
        " VALUES ('prod-1','R1-A1','R1','SATISFIED','npm run test',0,'',?,'t',1,'workdir')",
        (sha_a,),
    )
    out = asyncio.run(_evaluate(db, _inputs(ws, sha_a, [_phase(sha_a)], init)))
    assert out["criteria"]["passed"] == 1
    db.close()
