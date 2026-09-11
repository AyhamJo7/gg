"""Bounded autonomous repair dogfoods (Increment 4, R-01..R-55).

Deterministic fake-provider flows over real temp git repos: exact trigger
SHA -> classification -> one immutable scoped attempt -> checkpointed result
SHA -> independent review -> exact evidence recheck -> bounded next attempt.
No real providers, no quota, no network.
"""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path
from typing import Any

from orchestrator.config import Config
from orchestrator.db import Database
from orchestrator.models import utcnow
from orchestrator.repair import (
    AUTO_REPAIRABLE,
    ProviderOperationalError,
    RecheckVerdict,
    RepairClassification,
    RepairCoordinator,
    RepairCycleStatus,
    RepairResult,
    RepairScope,
    RepairTriggerType,
    ReviewVerdict,
    build_repair_scope,
    classify_criterion_failure,
    classify_review_finding,
    classify_verification_failure,
    ensure_human_gate,
    execute_repair_cycle,
    failure_signature,
    resume_repair_cycle,
)

# ---------------------------------------------------------------------------
# Helpers: real git repos, deterministic scripted actors
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


def _make_repo(tmp: Path, name: str = "target") -> Path:
    repo = tmp / f"{name}-{uuid.uuid4().hex[:6]}"
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "server.py").write_text("STATUS = 201\n")
    (repo / "tests").mkdir(exist_ok=True)
    (repo / "tests" / "test_server.py").write_text("assert STATUS == 400\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial buggy implementation")
    return repo


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD")


def _commit(repo: Path, message: str, changes: dict[str, str]) -> str:
    for rel, content in changes.items():
        dest = repo / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)
    return _head(repo)


def _show(repo: Path, sha: str, rel: str) -> str:
    return subprocess.run(
        ["git", "show", f"{sha}:{rel}"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout


def _is_ancestor(repo: Path, older: str, newer: str) -> bool:
    return subprocess.run(["git", "merge-base", "--is-ancestor", older, newer], cwd=repo, capture_output=True).returncode == 0


def _make_db(tmp: Path) -> Database:
    db = Database(tmp / f"repair-{uuid.uuid4().hex[:8]}.db")
    db.insert(
        "product_projects",
        {"id": "p1", "name": "n", "idea": "i", "state": "FINAL_ACCEPTANCE",
         "acceptance_state": "UNVERIFIED", "created_at": utcnow().isoformat(),
         "updated_at": utcnow().isoformat()},
    )
    return db


def _cfg(**overrides: Any) -> Config:
    data: dict[str, Any] = {"repair": {"autonomous_enabled": True, "max_attempts": 2}}
    data["repair"].update(overrides)
    return Config(data)


def _trigger_sha(repo: Path) -> str:
    return _head(repo)


class ScriptedHooks:
    """Scripted repair/review/recheck/regress actors over a real git repo.

    repair_plan: list of ("fix"|"wrong"|"noop"|"tamper"|"raise_op", payload).
    reviews: list of (passed, reviewer). recheck_predicate(content)->bool reads
    server.py AT the result SHA (exact-SHA recheck, never the workdir).
    """

    def __init__(
        self,
        repo: Path,
        *,
        repair_plan: list[tuple[str, Any]],
        reviews: list[tuple[bool, str]],
        recheck_predicate: Any = None,
        regress_predicate: Any = None,
        forbid: set[str] | None = None,
    ) -> None:
        self.repo = repo
        self.repair_plan = list(repair_plan)
        self.reviews = list(reviews)
        self.recheck_predicate = recheck_predicate or (lambda content: "STATUS = 400" in content)
        self.regress_predicate = regress_predicate or (lambda content: "REGRESSION_OK" in content or True)
        self.forbid = forbid or set()
        self.repair_calls = 0
        self.review_calls = 0
        self.recheck_calls = 0
        self.recheck_commands: list[str] = []
        self.last_scope: RepairScope | None = None

    def _check_forbidden(self, stage: str) -> None:
        if stage in self.forbid:
            raise AssertionError(f"{stage} must not run during this resume")

    async def repair(self, scope: RepairScope) -> RepairResult:
        self._check_forbidden("repair")
        self.repair_calls += 1
        self.last_scope = scope
        if not self.repair_plan:
            raise AssertionError("repair plan exhausted: unbounded loop")
        kind, payload = self.repair_plan.pop(0)
        provider = payload.get("provider", "opencode") if isinstance(payload, dict) else "opencode"
        if kind == "raise_op":
            raise ProviderOperationalError("quota exhausted before code work")
        if kind == "noop":
            return RepairResult(result_sha=scope.base_sha, provider=provider, touched_files=[], summary="no change")
        changes = payload.get("changes", {}) if isinstance(payload, dict) else {}
        sha = _commit(self.repo, f"repair attempt {scope.attempt_number} by {provider}", changes)
        return RepairResult(result_sha=sha, provider=provider, touched_files=sorted(changes), summary=kind)

    async def review(self, scope: RepairScope, result_sha: str) -> ReviewVerdict:
        self._check_forbidden("review")
        self.review_calls += 1
        if not self.reviews:
            raise AssertionError("review script exhausted")
        passed, reviewer = self.reviews.pop(0)
        return ReviewVerdict(passed=passed, reviewer=reviewer, detail=f"review of {result_sha[:8]}")

    async def recheck(self, scope: RepairScope, result_sha: str) -> RecheckVerdict:
        self._check_forbidden("recheck")
        self.recheck_calls += 1
        self.recheck_commands.append(scope.command)
        content = _show(self.repo, result_sha, "server.py")
        passed = bool(self.recheck_predicate(content))
        attempt_id = f"crit-recheck-{uuid.uuid4().hex[:8]}"
        # Persist a genuine immutable recheck attempt row bound to the exact SHA.
        db = _RECcheck_DB.get("db")
        if db is not None:
            db.insert(
                "criterion_attempts",
                {
                    "id": attempt_id,
                    "project_id": _RECcheck_DB.get("project", "p1"),
                    "criterion_id": scope.criterion_id or "R1-A1",
                    "requirement_id": scope.requirement_id or "R1",
                    "plan_revision": 1,
                    "command": scope.command,
                    "checked_sha": result_sha.lower(),
                    "context": "workdir",
                    "capability": "REPLAYABLE",
                    "result": "SATISFIED" if passed else "FAILED",
                    "exit_code": 0 if passed else 1,
                    "output_tail": content[:500],
                    "recheck_of": scope.trigger_evidence_id,
                    "repo_key": scope.repo_key,
                    "created_at": utcnow().isoformat(),
                },
            )
        return RecheckVerdict(passed=passed, attempt_id=attempt_id, checked_sha=result_sha, output=content[:500])

    async def regress(self, scope: RepairScope, result_sha: str) -> RecheckVerdict:
        content = _show(self.repo, result_sha, "server.py")
        passed = bool(self.regress_predicate(content))
        return RecheckVerdict(
            passed=passed, attempt_id=f"reg-{uuid.uuid4().hex[:8]}", checked_sha=result_sha, output=""
        )

    async def current_head(self, scope: RepairScope) -> str:
        return _head(self.repo)

    async def is_descendant(self, scope: RepairScope, base_sha: str, result_sha: str) -> bool:
        try:
            return _is_ancestor(self.repo, base_sha, result_sha)
        except Exception:
            return False


_RECcheck_DB: dict[str, Any] = {}


def _open_cycle(
    db: Database,
    repo: Path,
    *,
    classification: str = RepairClassification.IMPLEMENTATION_DEFECT.value,
    max_attempts: int = 2,
    project: str = "p1",
    evidence: str = "crit-1",
    trigger: str = RepairTriggerType.CRITERION_FAILED.value,
    sha: str | None = None,
) -> dict[str, Any]:
    coord = RepairCoordinator(db, _cfg(max_attempts=max_attempts))
    created = coord.create_cycle(
        project_id=project,
        trigger_type=trigger,
        trigger_evidence_id=evidence,
        trigger_sha=sha or _trigger_sha(repo),
        repo_key="repo-A",
        target_requirement_id="R1",
        target_criterion_id="R1-A1",
        classification=classification,
        max_attempts=max_attempts,
    )
    assert created["cycle"] is not None, created.get("reason")
    return coord.classify_cycle(str(created["cycle"]["id"]), classification)


def _scope_ctx(writers: list[str] | None = None) -> dict[str, Any]:
    return {"writers": writers or []}


async def _builder(cycle: dict[str, Any], n: int, base: str, prev: str, ctx: dict[str, Any]) -> RepairScope:
    return build_repair_scope(
        cycle,
        attempt_number=n,
        expected="whitespace-only title returns HTTP 400",
        observed="whitespace-only title returns HTTP 201",
        command="pytest tests/test_server.py -q",
        exit_code=1,
        output_tail="AssertionError: expected 400 got 201",
        base_sha=base,
        relevant_files=["server.py"],
        protected_files=["tests/test_server.py", "plan.json"],
        writers=ctx["writers"],
        previous_attempt_summary=prev,
    )


async def _run(db: Database, repo: Path, cycle_id: str, hooks: ScriptedHooks, ctx: dict[str, Any]) -> dict[str, Any]:
    async def builder(cycle: dict[str, Any], n: int, base: str, prev: str) -> RepairScope:
        return await _builder(cycle, n, base, prev, ctx)

    return await execute_repair_cycle(db, _cfg(), builder, hooks, cycle_id)


# ---------------------------------------------------------------------------
# Classification matrix
# ---------------------------------------------------------------------------


def test_classification_matrix() -> None:
    assert (
        classify_criterion_failure(
            command="pytest tests/test_server.py -q", exit_code=1,
            output_tail="AssertionError: expected 400 got 201",
        )
        == RepairClassification.IMPLEMENTATION_DEFECT
    )
    assert (
        classify_criterion_failure(
            command="pytest tests/test_server.py -q", exit_code=1,
            output_tail="gcc: command not found",
        )
        == RepairClassification.ENVIRONMENT_FAILURE
    )
    assert (
        classify_criterion_failure(
            command="pytest tests/test_api.py -q", exit_code=1,
            output_tail="401 Unauthorized: API key required",
        )
        == RepairClassification.EXTERNAL_PREREQUISITE
    )
    assert (
        classify_criterion_failure(
            command="pytest tests/test_x.py -q", exit_code=1,
            output_tail="AssertionError: wrong",
            requirement_text="TBD: define correct behavior",
        )
        == RepairClassification.AMBIGUOUS_CONTRACT
    )
    assert (
        classify_criterion_failure(
            command="pytest tests/test_x.py -q", exit_code=1,
            output_tail="AssertionError: wrong", contradictory=True,
        )
        == RepairClassification.CONTRADICTORY_CONTRACT
    )
    assert (
        classify_criterion_failure(
            command="pytest tests/test_x.py -q", exit_code=1,
            output_tail="AUTO_MERGE failed: merge conflict in server.py",
        )
        == RepairClassification.DEPENDENCY_CONFLICT
    )
    assert (
        classify_criterion_failure(
            command="pytest tests/test_x.py -q", exit_code=1,
            output_tail="provider error: quota exhausted",
        )
        == RepairClassification.PROVIDER_FAILURE
    )
    # Empty evidence fails closed.
    assert (
        classify_criterion_failure(command="pytest tests/test_x.py -q", exit_code=1, output_tail="   ")
        == RepairClassification.UNKNOWN
    )
    assert (
        classify_verification_failure(
            command="npm test", exit_code=1, output_tail="EAI_AGAIN registry unreachable",
            likely_environment_issue=True,
        )
        == RepairClassification.ENVIRONMENT_FAILURE
    )
    assert (
        classify_verification_failure(
            command="npm test", exit_code=1, output_tail="TypeError: cannot read property",
        )
        == RepairClassification.IMPLEMENTATION_DEFECT
    )
    assert (
        classify_review_finding(
            severity="HIGH", category="correctness",
            description="createIssue accepts whitespace-only titles",
            recommended_fix="trim and reject blank titles",
        )
        == RepairClassification.IMPLEMENTATION_DEFECT
    )
    assert (
        classify_review_finding(severity="HIGH", category="correctness", description="   ")
        == RepairClassification.UNKNOWN
    )
    assert RepairClassification.IMPLEMENTATION_DEFECT.value in AUTO_REPAIRABLE
    assert RepairClassification.UNKNOWN.value not in AUTO_REPAIRABLE


def test_failure_signature_stable_and_secret_safe() -> None:
    a = failure_signature(trigger_type="CRITERION_FAILED", identity="R1-A1", exit_code=1,
                          output_tail="AssertionError: expected 400 got 201 in 0.42s")
    b = failure_signature(trigger_type="CRITERION_FAILED", identity="R1-A1", exit_code=1,
                          output_tail="AssertionError: expected 400 got 201 in 1.97s")
    c = failure_signature(trigger_type="CRITERION_FAILED", identity="R1-A1", exit_code=1,
                          output_tail="TypeError: cannot read property 'x'")
    assert a == b
    assert a != c
    assert len(a) == 32


# ---------------------------------------------------------------------------
# Core repair flows (94-97)
# ---------------------------------------------------------------------------


async def _simple_setup(tmp: Path) -> tuple[Database, Path, str]:
    db = _make_db(tmp)
    repo = _make_repo(tmp)
    _RECcheck_DB["db"] = db
    _RECcheck_DB["project"] = "p1"
    return db, repo, _trigger_sha(repo)


def test_simple_criterion_repair_success(tmp_path: Path) -> None:
    """94: A fails -> B repaired -> independent review PASS -> recheck PASS."""
    import asyncio

    async def main() -> None:
        db, repo, sha_a = await _simple_setup(tmp_path)
        cycle = _open_cycle(db, repo)
        hooks = ScriptedHooks(
            repo,
            repair_plan=[("fix", {"changes": {"server.py": "STATUS = 400\n"}, "provider": "opencode"})],
            reviews=[(True, "claude")],
        )
        final = await _run(db, repo, str(cycle["id"]), hooks, _scope_ctx())
        assert final["status"] == RepairCycleStatus.SUCCEEDED.value, final
        assert final["attempts_used"] == 1
        attempts = RepairCoordinator(db).attempts(str(cycle["id"]))
        assert len(attempts) == 1
        att = attempts[0]
        assert att["base_sha"] == sha_a.lower()
        assert att["result_sha"] and att["result_sha"] != sha_a.lower()
        assert _is_ancestor(repo, sha_a, str(att["result_sha"]))
        assert att["outcome"] == "SUCCEEDED"
        assert att["review_reviewer"] == "claude"
        assert att["recheck_attempt_id"]
        # Recheck is a NEW immutable attempt against the EXACT repaired SHA.
        rechecks = db.query("SELECT * FROM criterion_attempts WHERE id=?", (str(att["recheck_attempt_id"]),))
        assert len(rechecks) == 1
        assert rechecks[0]["checked_sha"] == att["result_sha"]
        assert rechecks[0]["result"] == "SATISFIED"
        # Trigger failure remains historical; repair writer recorded.
        assert hooks.repair_calls == 1 and hooks.review_calls == 1 and hooks.recheck_calls == 1
        db.close()

    asyncio.run(main())


def test_failed_repair_then_success(tmp_path: Path) -> None:
    """95: A->B recheck FAIL, B->C recheck PASS; both attempts immutable."""
    import asyncio

    async def main() -> None:
        db, repo, sha_a = await _simple_setup(tmp_path)
        cycle = _open_cycle(db, repo)
        hooks = ScriptedHooks(
            repo,
            repair_plan=[
                ("wrong", {"changes": {"server.py": "STATUS = 404\n"}, "provider": "opencode"}),
                ("fix", {"changes": {"server.py": "STATUS = 400\n"}, "provider": "opencode"}),
            ],
            reviews=[(True, "claude"), (True, "claude")],
        )
        final = await _run(db, repo, str(cycle["id"]), hooks, _scope_ctx())
        assert final["status"] == RepairCycleStatus.SUCCEEDED.value, final
        assert final["attempts_used"] == 2
        attempts = RepairCoordinator(db).attempts(str(cycle["id"]))
        assert len(attempts) == 2
        sha_b, sha_c = str(attempts[0]["result_sha"]), str(attempts[1]["result_sha"])
        assert attempts[0]["base_sha"] == sha_a.lower()
        assert attempts[1]["base_sha"] == sha_b  # repair2 starts from B, not A
        assert _is_ancestor(repo, sha_b, sha_c)
        assert attempts[0]["outcome"] == "RECHECK_FAILED"
        assert attempts[1]["outcome"] == "SUCCEEDED"
        assert hooks.repair_calls == 2
        db.close()

    asyncio.run(main())


def test_exhaustion_no_third_attempt(tmp_path: Path) -> None:
    """96: max 2, both fail same criterion -> EXHAUSTED, stop reason kept."""
    import asyncio

    async def main() -> None:
        db, repo, _a = await _simple_setup(tmp_path)
        cycle = _open_cycle(db, repo)
        hooks = ScriptedHooks(
            repo,
            repair_plan=[
                ("wrong", {"changes": {"server.py": "STATUS = 404\n"}}),
                ("wrong", {"changes": {"server.py": "STATUS = 500\n"}}),
            ],
            reviews=[(True, "claude"), (True, "claude")],
        )
        final = await _run(db, repo, str(cycle["id"]), hooks, _scope_ctx())
        assert final["status"] == RepairCycleStatus.EXHAUSTED.value, final
        assert final["stop_reason"]
        assert hooks.repair_calls == 2  # no third provider call
        assert RepairCoordinator(db).attempts(str(cycle["id"]))[0]["outcome"] == "RECHECK_FAILED"
        db.close()

    asyncio.run(main())


def test_no_change_bounded_stop(tmp_path: Path) -> None:
    """97: provider produces no change -> bounded stop, never infinite."""
    import asyncio

    async def main() -> None:
        db, repo, _a = await _simple_setup(tmp_path)
        cycle = _open_cycle(db, repo)
        hooks = ScriptedHooks(
            repo, repair_plan=[("noop", {"provider": "opencode"}), ("noop", {"provider": "codex"})], reviews=[]
        )
        final = await _run(db, repo, str(cycle["id"]), hooks, _scope_ctx())
        assert final["status"] == RepairCycleStatus.BLOCKED.value, final
        assert "no change" in (final["stop_reason"] or "").lower()
        assert hooks.repair_calls == 2  # initial + one failover, then stop
        assert hooks.review_calls == 0 and hooks.recheck_calls == 0
        db.close()

    asyncio.run(main())


# ---------------------------------------------------------------------------
# Non-repairable classes launch zero providers (98-101, 112)
# ---------------------------------------------------------------------------


def _terminal_classify(
    tmp_path: Path, classification: str, *, evidence: str = "crit-9", project: str = "p1"
) -> dict[str, Any]:
    db = _make_db(tmp_path)
    repo = _make_repo(tmp_path)
    cycle = _open_cycle(db, repo, classification=classification, evidence=evidence, project=project)
    return {"db": db, "cycle": cycle}


def test_environment_failure_no_code_repair(tmp_path: Path) -> None:
    """98: host executable missing -> BLOCKED, zero repair calls."""
    ctx = _terminal_classify(tmp_path, RepairClassification.ENVIRONMENT_FAILURE.value)
    assert ctx["cycle"]["status"] == RepairCycleStatus.BLOCKED.value
    assert ctx["cycle"]["attempts_used"] == 0
    assert "environment" in (ctx["cycle"]["stop_reason"] or "").lower()
    ctx["db"].close()


def test_external_credential_human_gate(tmp_path: Path) -> None:
    """99: missing API credential -> WAITING_FOR_HUMAN + gate row."""
    ctx = _terminal_classify(tmp_path, RepairClassification.EXTERNAL_PREREQUISITE.value)
    assert ctx["cycle"]["status"] == RepairCycleStatus.WAITING_FOR_HUMAN.value
    db = ctx["db"]
    gate_id = ensure_human_gate(db, project_id="p1", phase_id=None, cycle=ctx["cycle"], title="Provide API credential")
    gates = db.query("SELECT * FROM project_gates WHERE id=?", (gate_id,))
    assert len(gates) == 1 and gates[0]["status"] == "open"
    db.close()


def test_ambiguous_and_contradictory_stop(tmp_path: Path) -> None:
    """100: underspecified contract guesses nothing; contradictions need plans."""
    for cls in (RepairClassification.AMBIGUOUS_CONTRACT.value, RepairClassification.CONTRADICTORY_CONTRACT.value):
        ctx = _terminal_classify(tmp_path, cls, evidence=f"crit-{cls}")
        assert ctx["cycle"]["status"] == RepairCycleStatus.WAITING_FOR_HUMAN.value, cls
        assert ctx["cycle"]["attempts_used"] == 0
        ctx["db"].close()


def test_unknown_fails_closed(tmp_path: Path) -> None:
    ctx = _terminal_classify(tmp_path, RepairClassification.UNKNOWN.value)
    assert ctx["cycle"]["status"] == RepairCycleStatus.BLOCKED.value
    ctx["db"].close()


def test_dependency_conflict_no_autonomous_repair(tmp_path: Path) -> None:
    """112/R-52: merge conflicts stay operator-driven."""
    ctx = _terminal_classify(tmp_path, RepairClassification.DEPENDENCY_CONFLICT.value)
    assert ctx["cycle"]["status"] == RepairCycleStatus.BLOCKED.value
    assert ctx["cycle"]["attempts_used"] == 0
    ctx["db"].close()


def test_self_review_rejected(tmp_path: Path) -> None:
    """101: repair provider cannot certify its own change."""
    import asyncio

    async def main() -> None:
        db, repo, _a = await _simple_setup(tmp_path)
        cycle = _open_cycle(db, repo)
        hooks = ScriptedHooks(
            repo,
            repair_plan=[("fix", {"changes": {"server.py": "STATUS = 400\n"}, "provider": "opencode"})],
            reviews=[(True, "opencode")],
        )
        final = await _run(db, repo, str(cycle["id"]), hooks, _scope_ctx(writers=["opencode"]))
        assert final["status"] == RepairCycleStatus.BLOCKED.value, final
        assert "self-review" in (final["stop_reason"] or "").lower()
        db.close()

    asyncio.run(main())


# ---------------------------------------------------------------------------
# Restart matrix (102-104)
# ---------------------------------------------------------------------------


def test_restart_after_repair_reviews_without_repair(tmp_path: Path) -> None:
    """102: repair->B, crash, restart reviews B (zero new repair calls)."""
    import asyncio

    async def main() -> None:
        db, repo, sha_a = await _simple_setup(tmp_path)
        coord = RepairCoordinator(db, _cfg())
        created = coord.create_cycle(
            project_id="p1", trigger_type=RepairTriggerType.CRITERION_FAILED.value,
            trigger_evidence_id="crit-1", trigger_sha=sha_a, repo_key="repo-A",
            target_requirement_id="R1", target_criterion_id="R1-A1",
            classification=RepairClassification.IMPLEMENTATION_DEFECT.value,
        )
        cycle_id = str(created["cycle"]["id"])
        coord.classify_cycle(cycle_id, RepairClassification.IMPLEMENTATION_DEFECT.value)
        # Crash lands here: attempt row has B, no review yet.
        attempt = coord.start_attempt(cycle_id, "opencode", sha_a)
        sha_b = _commit(repo, "repair by opencode", {"server.py": "STATUS = 400\n"})
        coord.update_attempt(str(attempt["id"]), {"result_sha": sha_b, "provider": "opencode",
                                                  "outcome": "CODE_CHANGED"})
        hooks = ScriptedHooks(repo, repair_plan=[], reviews=[(True, "claude")], forbid={"repair"})
        final = await resume_repair_cycle(db, _cfg(), _resume_builder(), hooks, cycle_id)
        assert final["status"] == RepairCycleStatus.SUCCEEDED.value, final
        assert hooks.repair_calls == 0
        assert hooks.review_calls == 1 and hooks.recheck_calls == 1
        db.close()

    def _resume_builder() -> Any:
        async def builder(cycle: dict[str, Any], n: int, base: str, prev: str) -> RepairScope:
            return await _builder(cycle, n, base, prev, _scope_ctx())

        return builder

    asyncio.run(main())


def test_restart_after_review_rechecks_without_review(tmp_path: Path) -> None:
    """103: B reviewed PASS, crash, restart rechecks B (no duplicate review)."""
    import asyncio

    async def main() -> None:
        db, repo, sha_a = await _simple_setup(tmp_path)
        coord = RepairCoordinator(db, _cfg())
        created = coord.create_cycle(
            project_id="p1", trigger_type=RepairTriggerType.CRITERION_FAILED.value,
            trigger_evidence_id="crit-1", trigger_sha=sha_a, repo_key="repo-A",
            target_requirement_id="R1", target_criterion_id="R1-A1",
            classification=RepairClassification.IMPLEMENTATION_DEFECT.value,
        )
        cycle_id = str(created["cycle"]["id"])
        coord.classify_cycle(cycle_id, RepairClassification.IMPLEMENTATION_DEFECT.value)
        attempt = coord.start_attempt(cycle_id, "opencode", sha_a)
        sha_b = _commit(repo, "repair by opencode", {"server.py": "STATUS = 400\n"})
        coord.update_attempt(
            str(attempt["id"]),
            {"result_sha": sha_b, "provider": "opencode", "outcome": "CODE_CHANGED",
             "review_reviewer": "claude", "review_outcome": "passed", "review_detail": "looks good"},
        )
        hooks = ScriptedHooks(repo, repair_plan=[], reviews=[], forbid={"repair", "review"})
        final = await resume_repair_cycle(db, _cfg(), _resume_builder(), hooks, cycle_id)
        assert final["status"] == RepairCycleStatus.SUCCEEDED.value, final
        assert hooks.repair_calls == 0 and hooks.review_calls == 0 and hooks.recheck_calls == 1
        db.close()

    def _resume_builder() -> Any:
        async def builder(cycle: dict[str, Any], n: int, base: str, prev: str) -> RepairScope:
            return await _builder(cycle, n, base, prev, _scope_ctx())

        return builder

    asyncio.run(main())


def test_restart_after_recheck_pass_is_idempotent(tmp_path: Path) -> None:
    """104: recheck PASS persisted but success not published -> SUCCEEDED, silent."""
    import asyncio

    async def main() -> None:
        db, repo, sha_a = await _simple_setup(tmp_path)
        coord = RepairCoordinator(db, _cfg())
        created = coord.create_cycle(
            project_id="p1", trigger_type=RepairTriggerType.CRITERION_FAILED.value,
            trigger_evidence_id="crit-1", trigger_sha=sha_a, repo_key="repo-A",
            target_requirement_id="R1", target_criterion_id="R1-A1",
            classification=RepairClassification.IMPLEMENTATION_DEFECT.value,
        )
        cycle_id = str(created["cycle"]["id"])
        coord.classify_cycle(cycle_id, RepairClassification.IMPLEMENTATION_DEFECT.value)
        attempt = coord.start_attempt(cycle_id, "opencode", sha_a)
        sha_b = _commit(repo, "repair by opencode", {"server.py": "STATUS = 400\n"})
        coord.update_attempt(
            str(attempt["id"]),
            {"result_sha": sha_b, "provider": "opencode", "outcome": "CODE_CHANGED",
             "review_reviewer": "claude", "review_outcome": "passed",
             "recheck_attempt_id": "crit-x", "recheck_outcome": "passed"},
        )
        hooks = ScriptedHooks(repo, repair_plan=[], reviews=[], forbid={"repair", "review", "recheck"})
        final = await resume_repair_cycle(db, _cfg(), _resume_builder(), hooks, cycle_id)
        assert final["status"] == RepairCycleStatus.SUCCEEDED.value, final
        assert hooks.repair_calls == 0 and hooks.review_calls == 0 and hooks.recheck_calls == 0
        db.close()

    def _resume_builder() -> Any:
        async def builder(cycle: dict[str, Any], n: int, base: str, prev: str) -> RepairScope:
            return await _builder(cycle, n, base, prev, _scope_ctx())

        return builder

    asyncio.run(main())


# ---------------------------------------------------------------------------
# Stale, repetition, tampering, lineage (105-108)
# ---------------------------------------------------------------------------


def test_manual_change_supersedes_cycle(tmp_path: Path) -> None:
    """105: human adopts B before repair launches -> STALE, no overwrite."""
    import asyncio

    async def main() -> None:
        db, repo, sha_a = await _simple_setup(tmp_path)
        cycle = _open_cycle(db, repo)
        _commit(repo, "human fix", {"server.py": "STATUS = 400\n"})
        hooks = ScriptedHooks(
            repo, repair_plan=[("fix", {"changes": {"server.py": "STATUS = 400\n"}})], reviews=[(True, "claude")]
        )
        final = await _run(db, repo, str(cycle["id"]), hooks, _scope_ctx())
        assert final["status"] == RepairCycleStatus.STALE.value, final
        assert hooks.repair_calls == 0
        # Coordinator-level supersede marks the same way.
        assert RepairCoordinator(db).supersede_on_candidate_change("p1", _head(repo)) == []
        db.close()

    asyncio.run(main())


def test_repeated_failure_signature_stops(tmp_path: Path) -> None:
    """106: same normalized failure twice -> bounded stop before budget end."""
    import asyncio

    async def main() -> None:
        db, repo, _a = await _simple_setup(tmp_path)
        cycle = _open_cycle(db, repo, max_attempts=3)
        hooks = ScriptedHooks(
            repo,
            repair_plan=[
                ("wrong", {"changes": {"server.py": "STATUS = 404\n"}}),
                ("wrong", {"changes": {"server.py": "STATUS = 404\n", "notes.txt": "retry\n"}}),
                ("wrong", {"changes": {"server.py": "STATUS = 404\n", "notes2.txt": "retry\n"}}),
            ],
            reviews=[(True, "claude")] * 3,
        )
        final = await execute_repair_cycle(
            db, _cfg(max_attempts=3), _mkbuilder(), hooks, str(cycle["id"])
        )
        assert final["status"] == RepairCycleStatus.BLOCKED.value, final
        assert "without progress" in (final["stop_reason"] or "").lower()
        assert hooks.repair_calls == 2  # stopped early, third plan entry untouched
        assert len(hooks.repair_plan) == 1
        db.close()

    def _mkbuilder() -> Any:
        async def builder(cycle: dict[str, Any], n: int, base: str, prev: str) -> RepairScope:
            return await _builder(cycle, n, base, prev, _scope_ctx())

        return builder

    asyncio.run(main())


def test_oscillation_stops(tmp_path: Path) -> None:
    """A->B->A failure fingerprints terminate boundedly."""
    import asyncio

    async def main() -> None:
        db, repo, _a = await _simple_setup(tmp_path)
        cycle = _open_cycle(db, repo, max_attempts=3)
        outputs = iter(["STATUS = 404\n", "STATUS = 500\n", "STATUS = 404\n"])
        hooks = ScriptedHooks(
            repo,
            repair_plan=[
                ("wrong", {"changes": {"server.py": next(outputs)}}),
                ("wrong", {"changes": {"server.py": next(outputs)}}),
                ("wrong", {"changes": {"server.py": next(outputs)}}),
            ],
            reviews=[(True, "claude")] * 3,
        )
        final = await execute_repair_cycle(
            db, _cfg(max_attempts=3), _mkbuilder(), hooks, str(cycle["id"])
        )
        assert final["status"] == RepairCycleStatus.BLOCKED.value, final
        assert "oscillation" in (final["stop_reason"] or "").lower()
        assert hooks.repair_calls == 3  # bounded by budget even so
        db.close()

    def _mkbuilder() -> Any:
        async def builder(cycle: dict[str, Any], n: int, base: str, prev: str) -> RepairScope:
            return await _builder(cycle, n, base, prev, _scope_ctx())

        return builder

    asyncio.run(main())


def test_contract_tampering_rejected(tmp_path: Path) -> None:
    """107: repair weakening the test assertion can never succeed."""
    import asyncio

    async def main() -> None:
        db, repo, _a = await _simple_setup(tmp_path)
        cycle = _open_cycle(db, repo)
        hooks = ScriptedHooks(
            repo,
            repair_plan=[("tamper", {"changes": {"tests/test_server.py": "assert True\n"}})],
            reviews=[(True, "claude")],  # even a passing review cannot save it
            recheck_predicate=lambda content: True,
        )
        final = await _run(db, repo, str(cycle["id"]), hooks, _scope_ctx())
        assert final["status"] == RepairCycleStatus.BLOCKED.value, final
        assert "protected" in (final["stop_reason"] or "").lower()
        db.close()

    asyncio.run(main())


def test_review_finding_repair_lineage(tmp_path: Path) -> None:
    """108: HIGH correctness finding -> repair -> rereview; origin linked."""
    import asyncio

    async def main() -> None:
        db, repo, sha_a = await _simple_setup(tmp_path)
        coord = RepairCoordinator(db, _cfg())
        created = coord.create_cycle(
            project_id="p1", trigger_type=RepairTriggerType.REVIEW_FINDING.value,
            trigger_evidence_id="F-1", trigger_sha=sha_a, repo_key="repo-A",
            target_finding_id="F-1",
            classification=RepairClassification.IMPLEMENTATION_DEFECT.value,
        )
        cycle = coord.classify_cycle(
            str(created["cycle"]["id"]), RepairClassification.IMPLEMENTATION_DEFECT.value
        )
        hooks = ScriptedHooks(
            repo,
            repair_plan=[("fix", {"changes": {"server.py": "STATUS = 400\n"}, "provider": "opencode"})],
            reviews=[(True, "codex")],
        )

        async def builder(cycle: dict[str, Any], n: int, base: str, prev: str) -> RepairScope:
            scope = await _builder(cycle, n, base, prev, _scope_ctx())
            scope.finding_id = "F-1"
            return scope

        final = await execute_repair_cycle(db, _cfg(), builder, hooks, str(cycle["id"]))
        assert final["status"] == RepairCycleStatus.SUCCEEDED.value, final
        attempts = coord.attempts(str(cycle["id"]))
        assert attempts[0]["review_reviewer"] == "codex"
        assert final["target_finding_id"] == "F-1"
        db.close()

    asyncio.run(main())


def test_verification_repair_reruns_exact_command(tmp_path: Path) -> None:
    """109: generic verification failure reruns the exact failed command."""
    import asyncio

    async def main() -> None:
        db, repo, sha_a = await _simple_setup(tmp_path)
        coord = RepairCoordinator(db, _cfg())
        created = coord.create_cycle(
            project_id="p1", trigger_type=RepairTriggerType.VERIFICATION_FAILED.value,
            trigger_evidence_id="ver-1", trigger_sha=sha_a, repo_key="repo-A",
            classification=RepairClassification.IMPLEMENTATION_DEFECT.value,
        )
        cycle = coord.classify_cycle(
            str(created["cycle"]["id"]), RepairClassification.IMPLEMENTATION_DEFECT.value
        )
        hooks = ScriptedHooks(
            repo,
            repair_plan=[("fix", {"changes": {"server.py": "STATUS = 400\n"}})],
            reviews=[(True, "claude")],
        )
        final = await _run(db, repo, str(cycle["id"]), hooks, _scope_ctx())
        assert final["status"] == RepairCycleStatus.SUCCEEDED.value, final
        assert hooks.recheck_commands and all(c == "pytest tests/test_server.py -q" for c in hooks.recheck_commands)
        db.close()

    asyncio.run(main())


# ---------------------------------------------------------------------------
# Dedupe, budgets, provider semantics, evidence integrity
# ---------------------------------------------------------------------------


def test_duplicate_trigger_creates_one_cycle(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    repo = _make_repo(tmp_path)
    coord = RepairCoordinator(db, _cfg())
    sha = _head(repo)
    first = coord.create_cycle(
        project_id="p1", trigger_type="CRITERION_FAILED", trigger_evidence_id="crit-1",
        trigger_sha=sha, repo_key="repo-A", classification="IMPLEMENTATION_DEFECT",
    )
    second = coord.create_cycle(
        project_id="p1", trigger_type="CRITERION_FAILED", trigger_evidence_id="crit-1",
        trigger_sha=sha, repo_key="repo-A", classification="IMPLEMENTATION_DEFECT",
    )
    assert second["deduplicated"] is True
    assert second["cycle"]["id"] == first["cycle"]["id"]
    assert coord.project_cycle_count("p1") == 1
    db.close()


def test_one_active_cycle_per_project(tmp_path: Path) -> None:
    coord = RepairCoordinator(_make_db(tmp_path), _cfg())
    repo = _make_repo(tmp_path)
    sha = _head(repo)
    first = coord.create_cycle(
        project_id="p1", trigger_type="CRITERION_FAILED", trigger_evidence_id="crit-1",
        trigger_sha=sha, repo_key="repo-A", classification="IMPLEMENTATION_DEFECT",
    )
    assert first["cycle"] is not None
    second = coord.create_cycle(
        project_id="p1", trigger_type="CRITERION_FAILED", trigger_evidence_id="crit-2",
        trigger_sha=sha, repo_key="repo-A", classification="IMPLEMENTATION_DEFECT",
    )
    assert second["cycle"] is None and "already targets" in (second.get("reason") or "")
    db = coord.db
    db.close()


def test_project_and_phase_budgets(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    repo = _make_repo(tmp_path)
    sha = _head(repo)
    coord = RepairCoordinator(db, Config({"repair": {"max_attempts": 2, "max_per_phase": 1, "max_per_project": 1}}))
    first = coord.create_cycle(
        project_id="p1", trigger_type="CRITERION_FAILED", trigger_evidence_id="crit-1",
        trigger_sha=sha, repo_key="repo-A", classification="IMPLEMENTATION_DEFECT",
    )
    assert first["cycle"] is not None
    coord.finish_cycle(str(first["cycle"]["id"]), RepairCycleStatus.EXHAUSTED.value, "done")
    second = coord.create_cycle(
        project_id="p1", trigger_type="CRITERION_FAILED", trigger_evidence_id="crit-2",
        trigger_sha=sha, repo_key="repo-A", classification="IMPLEMENTATION_DEFECT",
    )
    assert second["cycle"] is None and "budget" in (second.get("reason") or "").lower()
    db.close()


def test_operational_failure_consumes_no_budget(tmp_path: Path) -> None:
    """Provider quota failure before code work: WAITING_FOR_PROVIDER, count kept."""
    import asyncio

    async def main() -> None:
        db, repo, _a = await _simple_setup(tmp_path)
        cycle = _open_cycle(db, repo)
        hooks = ScriptedHooks(repo, repair_plan=[("raise_op", {"provider": "opencode"})], reviews=[])
        final = await _run(db, repo, str(cycle["id"]), hooks, _scope_ctx())
        assert final["status"] == RepairCycleStatus.WAITING_FOR_PROVIDER.value, final
        assert final["attempts_used"] == 0  # no code-repair budget consumed
        operational = db.query("SELECT * FROM repair_attempts WHERE cycle_id=?", (str(cycle["id"]),))
        assert len(operational) == 1 and operational[0]["operational_failure"] == 1
        db.close()

    asyncio.run(main())


def test_recheck_sha_mismatch_fails(tmp_path: Path) -> None:
    """Recheck certifying any SHA but the repaired one is rejected."""
    import asyncio

    async def main() -> None:
        db, repo, sha_a = await _simple_setup(tmp_path)
        cycle = _open_cycle(db, repo)
        hooks = ScriptedHooks(
            repo,
            repair_plan=[("fix", {"changes": {"server.py": "STATUS = 400\n"}})],
            reviews=[(True, "claude")],
        )

        async def bad_recheck(scope: RepairScope, result_sha: str) -> RecheckVerdict:
            return RecheckVerdict(passed=True, attempt_id="crit-bad", checked_sha=sha_a, output="")

        hooks.recheck = bad_recheck  # type: ignore[method-assign]
        final = await _run(db, repo, str(cycle["id"]), hooks, _scope_ctx())
        assert final["status"] == RepairCycleStatus.FAILED.value, final
        assert "exact repaired SHA" in (final["stop_reason"] or "")
        db.close()

    asyncio.run(main())


def test_result_not_descendant_rejected(tmp_path: Path) -> None:
    """History rewrite (result outside base ancestry) can never succeed."""
    import asyncio

    async def main() -> None:
        db, repo, _a = await _simple_setup(tmp_path)
        cycle = _open_cycle(db, repo)
        hooks = ScriptedHooks(repo, repair_plan=[], reviews=[(True, "claude")])

        async def evil_repair(scope: RepairScope) -> RepairResult:
            return RepairResult(result_sha="f" * 40, provider="opencode", touched_files=["server.py"])

        hooks.repair = evil_repair  # type: ignore[method-assign]
        final = await _run(db, repo, str(cycle["id"]), hooks, _scope_ctx())
        assert final["status"] == RepairCycleStatus.FAILED.value, final
        assert "descend" in (final["stop_reason"] or "").lower()
        db.close()

    asyncio.run(main())


def test_cancel_stops_cycle_without_calls(tmp_path: Path) -> None:
    import asyncio

    async def main() -> None:
        db, repo, _a = await _simple_setup(tmp_path)
        cycle = _open_cycle(db, repo)
        coord = RepairCoordinator(db, _cfg())
        coord.cancel_cycle(str(cycle["id"]))
        hooks = ScriptedHooks(
            repo, repair_plan=[("fix", {"changes": {"server.py": "STATUS = 400\n"}})], reviews=[(True, "claude")]
        )
        final = await _run(db, repo, str(cycle["id"]), hooks, _scope_ctx())
        assert final["status"] == RepairCycleStatus.CANCELLED.value
        assert hooks.repair_calls == 0
        db.close()

    asyncio.run(main())


def test_repair_success_never_delivers_project(tmp_path: Path) -> None:
    """R-45: cycle SUCCEEDED leaves canonical acceptance authority untouched."""
    import asyncio

    async def main() -> None:
        db, repo, _a = await _simple_setup(tmp_path)
        cycle = _open_cycle(db, repo)
        hooks = ScriptedHooks(
            repo,
            repair_plan=[("fix", {"changes": {"server.py": "STATUS = 400\n"}})],
            reviews=[(True, "claude")],
        )
        final = await _run(db, repo, str(cycle["id"]), hooks, _scope_ctx())
        assert final["status"] == RepairCycleStatus.SUCCEEDED.value
        project = db.get("product_projects", "p1")
        assert project is not None and project["state"] != "DELIVERED"
        assert project.get("delivery_sha") is None
        db.close()

    asyncio.run(main())


def test_repair_scoped_by_repo_key(tmp_path: Path) -> None:
    """114: evidence is scoped by repository identity + SHA."""
    import asyncio

    async def main() -> None:
        db, repo, _a = await _simple_setup(tmp_path)
        other = _make_repo(tmp_path, name="other")
        _commit(other, "diverge other repo", {"other.txt": "other\n"})
        assert _head(other) != _head(repo)
        cycle = _open_cycle(db, repo)
        assert cycle["repo_key"] == "repo-A"
        hooks = ScriptedHooks(
            repo,
            repair_plan=[("fix", {"changes": {"server.py": "STATUS = 400\n"}})],
            reviews=[(True, "claude")],
        )
        final = await _run(db, repo, str(cycle["id"]), hooks, _scope_ctx())
        assert final["status"] == RepairCycleStatus.SUCCEEDED.value
        rows = db.query("SELECT DISTINCT repo_key AS k FROM criterion_attempts")
        assert {r["k"] for r in rows} == {"repo-A"}
        db.close()

    asyncio.run(main())


def test_no_trigger_no_cycle_and_triage_quiet(tmp_path: Path) -> None:
    """113: dirty workspace alone is not repair evidence; no trigger, no cycle."""
    db = _make_db(tmp_path)
    coord = RepairCoordinator(db, _cfg())
    assert coord.active_cycle_for_project("p1") is None
    assert coord.list_cycles("p1") == []
    assert coord.repair_stats("p1")["cycles"] == 0
    db.close()


def test_repair_ticket_and_stats(tmp_path: Path) -> None:
    import asyncio

    from orchestrator.repair import repair_ticket_text

    async def main() -> None:
        db, repo, _a = await _simple_setup(tmp_path)
        cycle = _open_cycle(db, repo)
        scope = await _builder(cycle, 1, str(cycle["trigger_sha"]), "", _scope_ctx())
        ticket = repair_ticket_text(scope)
        assert "attempt 1/2" in ticket and "Base:" in ticket and "Trigger:" in ticket
        stats = RepairCoordinator(db, _cfg()).repair_stats("p1")
        assert stats["cycles"] == 1 and stats["attempts"] == 0
        db.close()

    asyncio.run(main())


def test_blocked_acceptance_opens_classified_cycle(tmp_path: Path) -> None:
    """Acceptance triage: failing criterion persists a classified repair cycle.

    Full product flow with fake providers: BLOCKED verdict unchanged, plus
    one CRITERION_FAILED cycle classified IMPLEMENTATION_DEFECT.
    """
    import asyncio

    from test_acceptance_integrity import _plan_with_verify, _run_with_plan
    from test_lifecycle import drive_project

    async def main() -> None:
        plan = _plan_with_verify("npm run check-fail")
        orch, pid = await _run_with_plan(
            tmp_path, plan, extra_scripts={"check-fail": 'node -e "process.exit(1)"'}
        )
        project = await drive_project(orch, pid)
        assert project["state"] == "BLOCKED"
        cycles = orch.db.query("SELECT * FROM repair_cycles WHERE project_id=?", (pid,))
        assert len(cycles) == 1, cycles
        cycle = cycles[0]
        assert cycle["trigger_type"] == RepairTriggerType.CRITERION_FAILED.value
        assert cycle["classification"] == RepairClassification.IMPLEMENTATION_DEFECT.value
        assert cycle["status"] == "CLASSIFIED"
        assert cycle["trigger_sha"] and len(cycle["trigger_sha"]) == 40
        attempts = orch.db.query("SELECT * FROM criterion_attempts WHERE id=?", (cycle["trigger_evidence_id"],))
        assert len(attempts) == 1 and attempts[0]["result"] == "FAILED"
        await orch.shutdown()

    asyncio.run(main())


def test_successful_dag_creates_no_repair_cycles(tmp_path: Path) -> None:
    """111: normal successful DAG execution leaves repair tracking empty."""
    import asyncio
    import tempfile

    from test_dag_artifacts import _dep as _ddep
    from test_dag_artifacts import _run as _drun
    from test_dag_artifacts import _setup as _dsetup
    from test_dag_artifacts import _task as _dtask

    async def main() -> None:
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            db, cfg, reg, _proj = _dsetup(tmp)
            _dtask(db, "ta", ["fast"])
            _dtask(db, "tb", ["fast"])
            _ddep(db, "ta", "tb")
            await _drun(db, cfg, reg)
            rows = {r["id"]: r for r in db.query("SELECT * FROM tasks WHERE mission_id='m1'")}
            assert all(r["status"] == "COMPLETED" for r in rows.values()), rows
            assert db.query("SELECT COUNT(*) AS n FROM repair_cycles")[0]["n"] == 0
            db.close()

    asyncio.run(main())
