"""Legacy-v1 vs compiled-v2 structural benchmark (Increment 2 seal: F-03).

Deterministic, no provider calls. Legacy fixtures are realistic shapes taken
from the actual builders (no string multiplication): engine role prompts,
REVIEW_INSTRUCTIONS, planner schema prompt, DAG task prompt. Each case reports
legacy/compiled char4-v1 estimates, the delta, and mandatory semantic
coverage.

Honest conclusion this demonstrates: compiled-v2 often gets LARGER for small
under-specified legacy prompts (it restores missing requirement/acceptance/
architecture context) and becomes substantially smaller where accumulated
history would otherwise grow without bound.

Usage:
    PYTHONPATH=backend/src backend/.venv/bin/python backend/scripts/compare_context.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from orchestrator.config import Config  # noqa: E402
from orchestrator.context_compiler import ContextCompiler, ContextCompileSpec  # noqa: E402
from orchestrator.context_manifest import estimate_tokens  # noqa: E402
from orchestrator.db import Database  # noqa: E402
from orchestrator.engine import ROLE_PROMPTS  # noqa: E402
from orchestrator.models import Role  # noqa: E402
from orchestrator.product_plan import build_planner_prompt  # noqa: E402
from orchestrator.review import REVIEW_INSTRUCTIONS  # noqa: E402


def _seed(db: Database) -> str:
    db.insert(
        "projects",
        {
            "id": "proj-1",
            "name": "p",
            "path": "/tmp/gg-bench",  # noqa: S108
            "detected_type": "node",
            "created_at": "t",
        },
    )
    db.insert(
        "missions",
        {
            "id": "m-1",
            "project_id": "proj-1",
            "title": "Foundation mission",
            "task": "implement login with input validation",
            "status": "IMPLEMENTING",
            "created_at": "t",
            "updated_at": "t",
        },
    )
    pid = "prod-1"
    plan = {
        "product_name": "Bench",
        "goal": "prove compiler",
        "users": "testers",
        "requirements": [
            {
                "id": "R4",
                "title": "Input Validation",
                "description": "reject blank titles",
                "kind": "functional",
                "acceptance": [
                    {
                        "id": "R4-A1",
                        "description": "whitespace-only title returns HTTP 400",
                        "verify": "node checks/whitespace-probe.js",
                    }
                ],
            },
            {
                "id": "R9",
                "title": "Unrelated",
                "description": "unrelated req",
                "kind": "functional",
                "acceptance": [{"id": "R9-A1", "description": "unrelated", "verify": "npm run test"}],
            },
        ],
        "non_functional": [],
        "architecture": {
            "backend": "python",
            "frontend": "react",
            "database": "sqlite",
            "decisions": [
                {"area": "auth", "choice": "session cookies, bcrypt", "rationale": "security"},
                {"area": "api", "choice": "REST /api/issues", "rationale": "simple"},
                {"area": "styling", "choice": "css modules", "rationale": "isolated styles"},
            ],
        },
        "phases": [
            {
                "key": "foundation",
                "title": "Foundation",
                "goal": "auth backend",
                "deliverables": ["login"],
                "tasks": ["implement login"],
                "depends_on": [],
                "workspace_scopes": ["backend"],
                "suggested_providers": [],
                "acceptance": [{"id": "f-A1", "description": "login works", "verify": "npm run test"}],
                "requirement_ids": ["R4"],
                "verify_commands": ["npm run test"],
                "human_prerequisites": [],
                "effort": "S",
            }
        ],
    }
    db.insert(
        "product_projects",
        {
            "id": pid,
            "name": "Bench",
            "idea": "prove compiler",
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
            "id": "r1",
            "project_id": pid,
            "revision": 1,
            "plan_json": plan,
            "created_by": "t",
            "reason": "t",
            "created_at": "t",
        },
    )
    db.insert(
        "project_phases",
        {
            "id": "phase-1",
            "project_id": pid,
            "phase_key": "foundation",
            "title": "Foundation",
            "goal": "auth backend",
            "status": "RUNNING",
            "mission_id": "m-1",
            "depends_on": "[]",
            "acceptance_json": "[]",
            "evidence_json": "{}",
            "attempts": 1,
            "created_at": "t",
            "updated_at": "t",
        },
    )
    return pid


def _seed_dependency_tasks(db: Database) -> None:
    for tid, title, desc, sha, summary in (
        (
            "task-a",
            "Add issues API",
            "implement GET /api/issues with validation",
            "aaa111",
            "added GET /api/issues, Issue type, validation middleware",
        ),
        (
            "task-b",
            "Add issue schema",
            "create issues table migration",
            "bbb222",
            "added migration 0002, IssueRecord type",
        ),
    ):
        db.insert(
            "tasks",
            {
                "id": tid,
                "mission_id": "m-1",
                "role": "implementation",
                "status": "COMPLETED",
                "prompt": "",
                "summary": summary,
                "attempts": 1,
                "created_at": "t",
                "title": title,
                "description": desc,
                "checkpoint_after": sha,
            },
        )
    db.insert(
        "tasks",
        {
            "id": "task-c",
            "mission_id": "m-1",
            "role": "implementation",
            "status": "PENDING",
            "prompt": "",
            "summary": "",
            "attempts": 0,
            "created_at": "t",
            "title": "Build issues UI",
            "description": "consume GET /api/issues in the frontend",
        },
    )
    for dep in (("task-a", "task-c"), ("task-b", "task-c")):
        db.insert("task_dependencies", {"from_task_id": dep[0], "to_task_id": dep[1], "created_at": "t"})


def est(text: str) -> int:
    return estimate_tokens(text).tokens


def _handoff(completed: list[str]) -> str:
    lines = ["## Handoff (previous provider summary)"]
    lines.extend(f"- {c}" for c in completed)
    return "\n".join(lines)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="gg-ctx-bench-"))
    db = Database(tmp / "bench.db")
    pid = _seed(db)
    _seed_dependency_tasks(db)
    cfg = Config({"context": {"mode": "compiled"}})
    cc = ContextCompiler(db, cfg)

    idea = "a tiny issue tracker with login and input validation"
    legacy_planner = build_planner_prompt(idea, "")

    legacy_small = "\n".join(
        [
            "You are working as the **implementation** engineer in a multi-provider orchestrated mission.",
            "",
            _handoff(["workspace is a fresh node repo; no prior work"]),
            "",
            f"## Your instructions for this phase\n{ROLE_PROMPTS[Role.IMPLEMENTATION]}",
            "",
            "## Output contract\nWork autonomously in the current directory. "
            "When finished, end with a one-paragraph summary of what you did.",
        ]
    )

    legacy_medium = "\n".join(
        [
            "You are working as the **implementation** engineer in a multi-provider orchestrated mission.",
            "",
            _handoff(
                [
                    "opencode: implemented login endpoint POST /api/login with bcrypt (2 files)",
                    "codex: added session middleware and cookie parsing",
                    "opencode: ran npm run test — 14 passed, 1 flaky (session expiry timing)",
                ]
            ),
            "",
            f"## Your instructions for this phase\n{ROLE_PROMPTS[Role.IMPLEMENTATION]}",
            "",
            "## Additional context\nFoundation mission\nimplement login with input validation",
            "",
            "## Output contract\nWork autonomously in the current directory. "
            "When finished, end with a one-paragraph summary of what you did.",
        ]
    )

    legacy_review = "\n".join(
        [
            "You are working as the **review** engineer in a multi-provider orchestrated mission.",
            "",
            _handoff(["opencode: implemented login endpoint; claims all tests pass and code is ready"]),
            "",
            f"## Your instructions for this phase\n{REVIEW_INSTRUCTIONS}",
            "",
            "## Additional context",
            "## Prior findings (re-flag if still present)",
            "- id=F1 fp=aa [MEDIUM/open] src/login.ts: missing rate limiting",
            "- id=F2 fp=bb [LOW/open] src/login.ts: error message leaks user existence",
        ]
    )

    legacy_repair = "\n".join(
        [
            "You are working as the **repair** engineer in a multi-provider orchestrated mission.",
            "",
            "## Open findings to fix",
            "- [HIGH] id=F3 src/api.ts: whitespace-only title accepted with 200 → return 400",
            "",
            "## Verification failures to fix",
            "node checks/whitespace-probe.js: exit=1",
            "AssertionError: expected 400 got 200 (tail: response body showed created issue)",
        ]
    )

    legacy_task = "\n".join(
        [
            "You are working as the **implementation** engineer in a multi-provider orchestrated mission.",
            "",
            "Task: Build issues UI",
            "Description: consume GET /api/issues in the frontend",
            "",
            "Work autonomously in the current directory. Follow existing project conventions. "
            "When finished, end with a one-paragraph summary of what you did.",
        ]
    )

    # Long historical repair: what legacy prompts grow into when every cycle
    # appends prior findings plus accumulated mission history. Sizes here are
    # representative of the audit's measured evidence (repeated full mission
    # text ~71.8% of reconstructed prompt characters): one full mission brief
    # plus two prior review transcripts carried verbatim into the repair.
    _mission_brief = (
        "Mission: Foundation — implement login with input validation for the Bench issue tracker. "
        "Scope: backend/ only. Requirements: session cookies with bcrypt passwords, "
        "POST /api/login 200/401, whitespace-only titles rejected with HTTP 400 per R4-A1 "
        "(verify: node checks/whitespace-probe.js), REST JSON under /api, sqlite via db module. "
        "Conventions: python3, pytest, ruff, no new dependencies. Verification: repo toolchain "
        "plus whitespace probe. Constraints: do not touch frontend/ or infra/, no secrets, "
        "do not weaken tests. Prior work: planning produced foundation/auth phases; attempt 1 "
        "added login; review 1 raised F1/F2/F3; repair 1 fixed F1/F2. Goal: fix R4-A1."
    )
    _review_t1 = (
        "Review 1 transcript (verbatim): checked login against R4 and auth architecture. "
        "bcrypt + HttpOnly cookie match the plan. Rate limiting missing (F1, medium). "
        "401 message differs for unknown users vs wrong passwords (F2, low). "
        "Whitespace-only titles accepted with 200 instead of 400 (F3, HIGH blocker). "
        "Fairly confident the rest is fine. Files: src/login.ts, src/api.ts, src/session.ts."
    )
    _review_t2 = (
        "Review 2 transcript (verbatim): re-checked after repair 1. F1 present (10/min/IP), "
        "F2 message unified — verified. F3 still open: probe still 200. New: pagination "
        "missing (F4, defer), sorting unstable (F5, defer), empty-body POST 500s (F6, verify). "
        "Believe F1/F2 fixed; focus on F3."
    )
    legacy_long_repair = "\n".join(
        [
            legacy_repair,
            "",
            "## Full mission brief (repeated verbatim each cycle)",
            _mission_brief,
            "",
            "## Prior review transcripts (carried verbatim)",
            _review_t1,
            "",
            _review_t2,
            "",
            "## Prior findings history",
            "- id=F1 [resolved] src/login.ts: missing rate limiting → fixed in c0ffee",
            "- id=F2 [resolved] src/login.ts: error message leaks user existence → fixed in c0ffee",
            "- id=F4 [open] src/api.ts: pagination missing → defer",
            "- id=F5 [open] src/api.ts: sorting unstable → defer",
            "- id=F6 [repair_attempted] src/api.ts: 500 on empty body → verify",
        ]
    )

    db.insert(
        "review_findings",
        {
            "id": "F3",
            "mission_id": "m-1",
            "severity": "HIGH",
            "category": "correctness",
            "file": "src/api.ts",
            "description": "whitespace-only title accepted with 200",
            "recommended_fix": "return 400",
            "status": "open",
            "created_at": "t",
        },
    )

    cases = [
        (
            "small-trivial",
            legacy_small,
            ContextCompileSpec(
                role="implementer",
                stage="implementation",
                product_project_id=pid,
                project_phase_id="phase-1",
                mission_id="m-1",
                task_title="Add title check",
                task_description="reject blank titles",
                requirement_ids=["R4"],
            ),
            ["R4", "R4-A1"],
        ),
        (
            "medium-implementation",
            legacy_medium,
            ContextCompileSpec(
                role="implementer",
                stage="implementation",
                product_project_id=pid,
                project_phase_id="phase-1",
                mission_id="m-1",
                task_title="Implement login",
                task_description="implement login with input validation",
                requirement_ids=["R4"],
            ),
            ["R4", "R4-A1"],
        ),
        (
            "planner",
            legacy_planner,
            ContextCompileSpec(
                role="planner",
                stage="product_plan",
                product_project_id=pid,
                task_title="Bench",
                task_objective=idea,
                extra_context=legacy_planner,
            ),
            [idea],
        ),
        (
            "review",
            legacy_review,
            ContextCompileSpec(
                role="reviewer",
                stage="review",
                product_project_id=pid,
                mission_id="m-1",
                base_sha="base000",
                candidate_sha="head111",
                task_title="Review login",
                requirement_ids=["R4"],
                git_diff_summary="M src/api.ts\nM src/login.ts",
                git_files_changed=["src/api.ts", "src/login.ts"],
            ),
            ["R4", "head111"],
        ),
        (
            "repair",
            legacy_repair,
            ContextCompileSpec(
                role="repairer",
                stage="repair",
                product_project_id=pid,
                mission_id="m-1",
                candidate_sha="head111",
                task_title="Fix R4-A1",
                requirement_ids=["R4"],
                finding_ids=["F3"],
                failure_text="AssertionError: expected 400 got 200",
                failure_command="node checks/whitespace-probe.js",
                failure_exit_code=1,
            ),
            ["R4", "F3", "AssertionError"],
        ),
        (
            "dependency-heavy-task",
            legacy_task,
            ContextCompileSpec(
                role="implementer",
                stage="task",
                product_project_id=pid,
                mission_id="m-1",
                task_id="task-c",
                task_title="Build issues UI",
                task_description="consume GET /api/issues in the frontend",
                requirement_ids=["R4"],
                dependency_ids=["task-a", "task-b"],
            ),
            ["task-a", "task-b"],
        ),
        (
            "long-historical-repair",
            legacy_long_repair,
            ContextCompileSpec(
                role="repairer",
                stage="repair",
                product_project_id=pid,
                mission_id="m-1",
                candidate_sha="head111",
                task_title="Fix R4-A1",
                requirement_ids=["R4"],
                finding_ids=["F3"],
                failure_text="AssertionError: expected 400 got 200",
                failure_command="node checks/whitespace-probe.js",
                failure_exit_code=1,
            ),
            ["R4", "F3"],
        ),
    ]
    print("case | legacy_est | compiled_est | delta | mandatory_ok")
    for name, legacy, spec, required in cases:
        compiled = cc.compile(spec)
        mandatory_ok = all(k in compiled.prompt for k in required)
        print(
            f"{name} | {est(legacy)} | {compiled.estimated_tokens} | "
            f"{compiled.estimated_tokens - est(legacy)} | {mandatory_ok}"
        )
    print(f"db={tmp / 'bench.db'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
