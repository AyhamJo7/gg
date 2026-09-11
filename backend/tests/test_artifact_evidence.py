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


def _key(repo: Path) -> str:
    raw = _git(repo, "rev-parse", "--git-common-dir")
    p = Path(raw)
    return str(repo / p) if not p.is_absolute() else raw


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
    key: str = "",
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
            "repo_key": key,
            "created_at": utcnow().isoformat(),
        },
    )


def _review(
    db: Database,
    mission: str,
    reviewer: str,
    base: str,
    head: str,
    independent: bool,
    writers: list[str],
    key: str = "",
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
            "repo_key": key,
            "created_at": utcnow().isoformat(),
        },
    )


def _uid(prefix: str) -> str:
    _uid.n += 1
    return f"{prefix}-{_uid.n}"


_uid.n = 0


def _verify(db: Database, sha: str, status: str = "passed", key: str = "", product: str = "prod-1") -> None:
    db.insert(
        "verification_attempts",
        {
            "id": _uid(f"ver-{sha[:8]}-{status}"),
            "mission_id": "m-1",
            "product_project_id": product,
            "task_id": None,
            "sha": sha,
            "repo_key": key,
            "kind": "toolchain",
            "commands_json": "[]",
            "status": status,
            "exit_code": 0,
            "started_at": "t",
            "finished_at": "t",
            "summary": "",
        },
    )


def _criterion(
    db: Database, sha: str, revision: int = 1, result: str = "SATISFIED", context: str = "fresh", key: str = ""
) -> None:
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
            "repo_key": key,
            "created_at": "t",
        },
    )


def _fresh(db: Database, sha: str, status: str = "passed", key: str = "") -> None:
    db.insert(
        "fresh_checkout_attempts",
        {
            "id": f"fresh-{sha[:8]}-{status}",
            "project_id": "prod-1",
            "sha": sha,
            "repo_key": key,
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
    _write(db, "run-impl", "m-1", "opencode", "implementation", init, sha_a, key=_key(ws))
    _write(db, "run-rep", "m-1", "claude", "repair", sha_a, sha_b, key=_key(ws))
    _review(db, "m-1", "agy", init, sha_a, True, ["opencode"], key=_key(ws))

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
    _write(db, "run-impl", "m-1", "opencode", "implementation", init, sha_a, key=_key(ws))
    _write(db, "run-rep", "m-1", "claude", "repair", sha_a, sha_b, key=_key(ws))
    _review(db, "m-1", "claude", init, sha_b, False, ["opencode", "claude"], key=_key(ws))

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
    _write(db, "run-impl", "m-1", "opencode", "implementation", init, sha_a, key=_key(ws))
    _write(db, "run-rep", "m-1", "claude", "repair", sha_a, sha_b, key=_key(ws))
    # NOTE: no write row for sha_c.
    _review(db, "m-1", "agy", init, sha_c, True, ["opencode", "claude"], key=_key(ws))

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
    _write(db, "run-impl", "m-1", "opencode", "implementation", init, sha_a, key=_key(ws))
    _write(db, "run-rep", "m-1", "claude", "repair", sha_a, sha_b, key=_key(ws))
    _review(db, "m-1", "agy", init, sha_a, True, ["opencode"], key=_key(ws))
    _verify(db, sha_a, key=_key(ws))
    _criterion(db, sha_a, key=_key(ws))
    _fresh(db, sha_a, key=_key(ws))

    partial = asyncio.run(_evaluate(db, _inputs(ws, sha_b, [_phase(sha_b)], init)))
    assert partial["verification"]["state"] == "MISSING"
    assert partial["fresh_checkout"]["state"] == "MISSING"
    assert partial["criteria"]["missing"] == 1
    assert not partial["delivery_ready"]

    # Complete the B evidence: independent re-review + all checks at B.
    _review(db, "m-1", "agy", init, sha_b, True, ["opencode", "claude"], key=_key(ws))
    _verify(db, sha_b, key=_key(ws))
    _criterion(db, sha_b, key=_key(ws))
    _fresh(db, sha_b, key=_key(ws))
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
    _write(db, "run-impl", "m-1", "opencode", "implementation", init, sha_a, key=_key(ws))
    _criterion(db, sha_a, revision=1, key=_key(ws))

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
    _write(db, "run-impl", "m-1", "opencode", "implementation", init, sha_a, key=_key(ws))
    _write(db, "run-rep", "m-1", "claude", "repair", sha_a, sha_b, key=_key(ws))
    _write(db, None, "m-1", None, "human", sha_b, sha_h, actor="HUMAN_OPERATOR", key=_key(ws))

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
    _write(db, "run-impl", "m-1", "opencode", "implementation", init, sha_a, key=_key(ws))
    db.execute(
        "INSERT INTO criterion_results(project_id, criterion_id, requirement_id, status, command,"
        " exit_code, output_tail, sha, checked_at, plan_revision, context, repo_key)"
        " VALUES ('prod-1','R1-A1','R1','SATISFIED','npm run test',0,'',?,'t',1,'workdir',?)",
        (sha_a, _key(ws)),
    )
    out = asyncio.run(_evaluate(db, _inputs(ws, sha_a, [_phase(sha_a)], init)))
    assert out["criteria"]["passed"] == 1
    db.close()


def test_actual_writes_determine_writer_status(tmp_path: Path):
    """P-31/S-01/S-02: any role's committed change makes its provider a
    writer (even with NULL role metadata); no-change runs do not."""
    import asyncio

    from orchestrator.provenance import provider_writers, range_writers

    ws, init, sha_a, sha_b = _repo(tmp_path)
    db = Database(tmp_path / "e.db")
    _product(db)
    key = _key(ws)
    # Planning + testing runs with REAL committed changes (roles vary).
    _write(db, "r-plan", "m-1", "planner-p", "planning", init, sha_a, key=key)
    _write(db, "r-test", "m-1", "tester-p", "testing", sha_a, sha_b, key=key)
    # Implementation run with NO committed change (base == result).
    _write(db, "r-impl", "m-1", "idle-p", "implementation", sha_b, sha_b, key=key)
    # Row with missing role metadata but a real change.
    _write(db, "r-myst", "m-1", "norole-p", None, sha_b, sha_b, key=key)  # type: ignore[arg-type]

    async def main() -> None:
        out = await range_writers(db, ws, init, sha_b)
        assert out["complete"], out["unattributed"]
        providers = provider_writers(out["writers"])
        assert providers == {"planner-p", "tester-p"}, providers
        by_provider = {w["provider"]: w for w in out["writers"] if w.get("provider")}
        assert by_provider["planner-p"]["role"] == "planning"
        assert by_provider["planner-p"]["changed_paths"] == ["app.txt"]
        # No-change + null-role-no-change rows never taint the set.
        assert "idle-p" not in providers
        assert "norole-p" not in providers

    asyncio.run(main())
    db.close()


def test_null_product_row_cannot_certify_foreign_project(tmp_path: Path):
    """S-05/S-06: a repo-local row with product_id NULL and an unlinked
    mission cannot certify another project, even in the same repo."""
    import asyncio

    ws, init, sha_a, _ = _repo(tmp_path)
    db = Database(tmp_path / "e.db")
    _product(db)
    key = _key(ws)
    _write(db, "run-impl", "m-1", "opencode", "implementation", init, sha_a, key=key)
    db.insert(
        "verification_attempts",
        {
            "id": "v-foreign",
            "mission_id": "m-other",
            "product_project_id": None,
            "task_id": None,
            "sha": sha_a,
            "repo_key": key,
            "kind": "toolchain",
            "commands_json": "[]",
            "status": "passed",
            "exit_code": 0,
            "started_at": "t",
            "finished_at": "t",
            "summary": "",
        },
    )
    out = asyncio.run(_evaluate(db, _inputs(ws, sha_a, [_phase(sha_a)], init)))
    assert out["verification"]["state"] == "MISSING", out["verification"]
    db.close()


def test_cross_repo_same_sha_cannot_share_evidence(tmp_path: Path):
    """F-PROV-05/P-26/§29: identical commit SHA X in repo A and repo B.
    Verification + writers recorded for A must not certify B; real B
    evidence then does."""
    import asyncio
    import subprocess as _sp

    ws_a = tmp_path / "repoA"
    ws_a.mkdir()
    _git(ws_a, "init")
    _git(ws_a, "config", "user.email", "t@t")
    _git(ws_a, "config", "user.name", "t")
    (ws_a / "f").write_text("same\n")
    _git(ws_a, "add", ".")
    _git(ws_a, "commit", "-m", "same")
    sha_x = _git(ws_a, "rev-parse", "HEAD")
    ws_b = tmp_path / "repoB"
    _sp.run(["git", "clone", "-q", str(ws_a), str(ws_b)], check=True)
    assert _git(ws_b, "rev-parse", "HEAD") == sha_x
    assert _key(ws_a) != _key(ws_b), "clone must have a distinct repo identity"

    db = Database(tmp_path / "e.db")
    _product(db)
    db.insert(
        "product_projects",
        {
            "id": "prod-B",
            "name": "B",
            "idea": "i",
            "constraints_text": "",
            "state": "EXECUTING",
            "acceptance_state": "",
            "auto_execute": 0,
            "require_plan_approval": 0,
            "target_project_id": None,
            "target_repo_path": "",
            "plan_revision": 1,
            "created_at": "t",
            "updated_at": "t",
        },
    )
    db.insert(
        "plan_revisions",
        {
            "id": "rev-B1",
            "project_id": "prod-B",
            "revision": 1,
            "plan_json": PLAN,
            "created_by": "t",
            "reason": "t",
            "created_at": "t",
        },
    )
    # Evidence recorded for repo A only (verification + a writer row).
    _verify(db, sha_x, key=_key(ws_a))
    db.insert(
        "write_provenance",
        {
            "id": "w-a",
            "run_id": "run-a",
            "mission_id": "m-1",
            "task_id": None,
            "product_project_id": "prod-1",
            "phase_id": None,
            "actor_type": "PROVIDER",
            "actor_detail": "",
            "provider": "opencode",
            "role": "implementation",
            "base_sha": None,
            "result_sha": sha_x,
            "tree_sha": None,
            "dirty_before": 0,
            "repo_key": _key(ws_a),
            "created_at": "t",
        },
    )

    inputs_b = EvidenceInputs(
        project_id="prod-B", candidate_sha=sha_x, plan_revision=1, repo=ws_b, phases=[], oldest_base_sha=None
    )
    out_b = asyncio.run(_evaluate(db, inputs_b))
    assert out_b["verification"]["state"] != "VALID", out_b["verification"]
    assert out_b["writers"] == [], "repo-A writers must not leak into repo B"

    # Real evidence for repo B then validates: a B-only commit with B rows.
    (ws_b / "g").write_text("b-only\n")
    _git(ws_b, "add", ".")
    _git(ws_b, "commit", "-m", "b change")
    sha_y = _git(ws_b, "rev-parse", "HEAD")
    assert sha_y != sha_x
    _verify(db, sha_y, key=_key(ws_b), product="prod-B")
    db.insert(
        "write_provenance",
        {
            "id": "w-b",
            "run_id": "run-b",
            "mission_id": "m-1",
            "task_id": None,
            "product_project_id": "prod-B",
            "phase_id": None,
            "actor_type": "PROVIDER",
            "actor_detail": "",
            "provider": "codex",
            "role": "implementation",
            "base_sha": sha_x,
            "result_sha": sha_y,
            "tree_sha": None,
            "dirty_before": 0,
            "repo_key": _key(ws_b),
            "created_at": "t",
        },
    )
    inputs_by = EvidenceInputs(
        project_id="prod-B", candidate_sha=sha_y, plan_revision=1, repo=ws_b, phases=[], oldest_base_sha=sha_x
    )
    out_b2 = asyncio.run(_evaluate(db, inputs_by))
    assert out_b2["verification"]["state"] == "VALID", out_b2["verification"]
    providers_b = {w["provider"] for w in out_b2["writers"] if w.get("provider")}
    assert providers_b == {"codex"}, f"only repo-B writers, got {providers_b}"
    assert out_b2["writers_complete"]
    db.close()


def test_legacy_keyless_rows_cannot_certify(tmp_path: Path):
    """§30: historical rows without repository identity stay visible but
    cannot certify a current artifact."""
    import asyncio

    ws, init, sha_a, _ = _repo(tmp_path)
    db = Database(tmp_path / "e.db")
    _product(db)
    # Keyless (legacy-style) rows everywhere.
    _write(db, "run-impl", "m-1", "opencode", "implementation", init, sha_a)
    _review(db, "m-1", "agy", init, sha_a, True, ["opencode"])
    _verify(db, sha_a)
    _criterion(db, sha_a)
    _fresh(db, sha_a)
    out = asyncio.run(_evaluate(db, _inputs(ws, sha_a, [_phase(sha_a)], init)))
    assert not out["writers_complete"]
    assert out["review"]["state"] == "FAILED"
    assert out["verification"]["state"] != "VALID"
    assert out["criteria"]["missing"] == 1
    assert out["fresh_checkout"]["state"] != "VALID"
    assert not out["delivery_ready"]
    db.close()


def test_repairer_self_review_rejected_at_both_layers(tmp_path: Path):
    """F-PROV-01/§37: opencode impl A, claude repair B, claude reviews B ->
    NOT independent in record_review_attempt() AND evaluate(). agy reviewing
    B stays independent."""
    import asyncio
    import json as _json

    from orchestrator.provenance import record_review_attempt

    ws, init, sha_a, sha_b = _repo(tmp_path)
    db = Database(tmp_path / "e.db")
    _product(db)
    key = _key(ws)
    _write(db, "run-impl", "m-1", "opencode", "implementation", init, sha_a, key=key)
    _write(db, "run-rep", "m-1", "claude", "repair", sha_a, sha_b, key=key)

    class _Events:
        def publish(self, *a, **k):
            pass

    async def main() -> None:
        # Claude reviews B (its own repair).
        db.insert(
            "provider_runs",
            {
                "id": "run-rev1",
                "mission_id": "m-1",
                "provider": "claude",
                "role": "review",
                "command": "[]",
                "cwd": "/tmp",
                "started_at": "t",
                "failure_class": "NONE",
                "provider_state": "COMPLETED",
                "summary": "",
                "stage": "review",
                "run_status": "SUCCEEDED",
                "git_commit_before": sha_b,
                "git_commit_after": sha_b,
            },
        )
        row = await record_review_attempt(db, _Events(), mission_id="m-1", repo=ws)
        assert row is not None
        assert row["independent"] == 0, "claude self-review must not be independent"
        assert "self-review" in (row["degradation_reason"] or "")
        assert set(_json.loads(row["writer_set_json"])) == {"opencode", "claude"}
        detail = {d["provider"]: d for d in _json.loads(row["writer_detail_json"])}
        assert detail["claude"]["role"] == "repair"
        assert detail["claude"]["run_id"] == "run-rep"
        # AGY reviews the same B -> independent.
        db.insert(
            "provider_runs",
            {
                "id": "run-rev2",
                "mission_id": "m-1",
                "provider": "agy",
                "role": "review",
                "command": "[]",
                "cwd": "/tmp",
                "started_at": "t2",
                "failure_class": "NONE",
                "provider_state": "COMPLETED",
                "summary": "",
                "stage": "review",
                "run_status": "SUCCEEDED",
                "git_commit_before": sha_b,
                "git_commit_after": sha_b,
            },
        )
        row2 = await record_review_attempt(db, _Events(), mission_id="m-1", repo=ws)
        assert row2 is not None and row2["independent"] == 1
        # evaluate() agrees at both layers (latest review wins per mission).
        out = await _evaluate(db, _inputs(ws, sha_b, [_phase(sha_b)], init))
        assert out["review"]["state"] == "VALID", out["review"]

    asyncio.run(main())
    db.close()
