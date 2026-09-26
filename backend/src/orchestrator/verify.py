"""Verification engine: evidence-based completion.

Runs the project's real toolchain (detected by workspace.inspect_workspace),
records every command + exit code, and never trusts agent self-reporting.
Commands run via argv arrays; each string from the detector is split with
shlex (safe — no shell interpretation, word-splitting only).

Detected commands are fixed strings (e.g. "npm test", "cargo build"), but
their *behavior* is driven by the target repo's own manifest content, which
is exactly as attacker/AI-influenceable as the criterion/gate-validation
text executed elsewhere in this codebase — a repo's package.json/Cargo.toml
can redirect what "npm test"/"cargo build" actually does regardless of the
literal command string. Execution therefore goes through the same
bwrap-sandboxed boundary as criterion.py/project_engine's gate validation
(see sandbox.py's module docstring) rather than running directly on the
host — there is no unsandboxed fallback.
"""

from __future__ import annotations

import logging
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

from .db import Database
from .events import EventBus
from .models import EventType, utcnow
from .sandbox import run_sandboxed, sandbox_available
from .workspace import WorkspaceInfo

logger = logging.getLogger(__name__)

VERIFY_TIMEOUT_S = 600.0

# High-confidence signatures of an environment/tooling-provisioning failure
# (DNS resolution unreachable — verify-time sandboxing has no network by
# design, see sandbox.py) rather than a genuine code/test defect. Deliberately
# narrow: only signatures that essentially never occur for any other reason,
# so a real code failure is never misclassified and skipped past a repair
# attempt that could have actually helped. A toolchain trying to
# self-provision over network it doesn't have (e.g. corepack fetching a
# pinned pnpm version) is the concrete case this was written for — no repair
# attempt, however good, can fix a DNS lookup failing inside a sandbox that
# has no network by design.
_ENVIRONMENT_FAILURE_MARKERS = ("EAI_AGAIN", "getaddrinfo", "ENETUNREACH")


def _is_environment_failure(tail: str) -> bool:
    return any(marker in tail for marker in _ENVIRONMENT_FAILURE_MARKERS)


# Test-runner summary lines that report skipped tests. Exit code stays the
# oracle for pass/fail; this is accounting so "passed with N skipped" is never
# recorded as indistinguishable from a clean pass. Patterns are anchored to
# each runner's per-test summary line (not file/suite lines, not log prose).
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_SKIP_SUMMARIES = (
    # pytest: "324 passed, 30 skipped in 5.31s" (optionally framed by ====).
    re.compile(r"^=*\s*(?:\d+ [a-z]+(?:, )?)*?(\d+) skipped(?:, \d+ [a-z]+)* in [\d.]+s\b"),
    # vitest: "Tests  137 passed | 2 skipped (139)" — not "Test Files".
    re.compile(r"^Tests\s{2,}.*?\b(\d+) skipped\b"),
    # jest: "Tests:       2 skipped, 10 passed, 12 total" — not "Test Suites:".
    re.compile(r"^Tests:\s.*?\b(\d+) skipped\b"),
    # cargo: "test result: ok. 12 passed; 0 failed; 3 ignored; ..."
    re.compile(r"^test result:.*?\b(\d+) ignored\b"),
)


# Repo-controlled output: an implausible count is unknown, never stored.
SKIP_COUNT_MAX = 1_000_000


def count_skipped(tail: str) -> int | None:
    """Skipped tests reported in a runner summary; None when not reported."""
    total: int | None = None
    for raw in tail.splitlines():
        line = _ANSI.sub("", raw).strip()
        for pattern in _SKIP_SUMMARIES:
            if m := pattern.search(line):
                total = (total or 0) + int(m.group(1))
                break
    if total is not None and total > SKIP_COUNT_MAX:
        return None
    return total


@dataclass
class CommandResult:
    command: str
    exit_code: int | None
    passed: bool
    duration_s: float
    tail: str = ""
    likely_environment_issue: bool = False
    skipped: int | None = None


@dataclass
class VerificationReport:
    results: list[CommandResult] = field(default_factory=list)

    @property
    def all_passed(self) -> bool:
        return bool(self.results) and all(r.passed for r in self.results)

    @property
    def attempted(self) -> bool:
        return bool(self.results)

    @property
    def skipped_total(self) -> int | None:
        counts = [r.skipped for r in self.results if r.skipped is not None]
        return sum(counts) if counts else None

    def summary(self) -> str:
        lines = []
        for r in self.results:
            mark = "PASS" if r.passed else "FAIL"
            skipped = f", {r.skipped} skipped" if r.skipped else ""
            lines.append(f"[{mark}] {r.command} (exit={r.exit_code}, {r.duration_s:.1f}s{skipped})")
        return "\n".join(lines)


def _persist_verification_attempt(
    db: Database,
    *,
    mission_id: str,
    product_project_id: str | None,
    task_id: str | None,
    sha: str | None,
    repo_key: str,
    kind: str,
    report: VerificationReport,
    started_at: str,
) -> str | None:
    """Immutable SHA-bound verification attempt (P-12). No SHA → no row (never fabricate)."""
    import json as _json
    import uuid as _uuid

    if not sha or len(sha) != 40 or any(c not in "0123456789abcdef" for c in sha.lower()):
        logger.debug("verification attempt not persisted: no exact SHA")
        return None
    attempt_id = f"ver-{_uuid.uuid4().hex[:12]}"
    failed = [r for r in report.results if not r.passed]
    row = {
        "id": attempt_id,
        "mission_id": mission_id or None,
        "product_project_id": product_project_id,
        "task_id": task_id,
        "sha": sha.lower(),
        "repo_key": repo_key,
        "kind": kind,
        "commands_json": _json.dumps([r.command for r in report.results]),
        "status": "passed" if report.all_passed else "failed",
        "exit_code": failed[0].exit_code if failed else 0,
        "started_at": started_at,
        "finished_at": utcnow().isoformat(),
        "summary": report.summary()[:2000],
        "skipped_tests": report.skipped_total,
    }
    try:
        db.insert("verification_attempts", row)
    except Exception:
        # Accounting must never cost the SHA-bound record itself.
        logger.warning("verification attempt insert failed; retrying without skip count", exc_info=True)
        try:
            db.insert("verification_attempts", {k: v for k, v in row.items() if k != "skipped_tests"})
        except Exception:
            logger.warning("verification attempt insert failed", exc_info=True)
            return None
    return attempt_id


async def run_verification(
    workspace: WorkspaceInfo,
    db: Database,
    events: EventBus,
    mission_id: str,
    workdir: Path,
    include_build: bool = True,
    *,
    sha: str | None = None,
    repo_key: str = "",
    kind: str = "toolchain",
    product_project_id: str | None = None,
    task_id: str | None = None,
) -> VerificationReport:
    from . import git_ops

    started = utcnow().isoformat()
    if sha is None:
        try:
            sha = await git_ops.head_sha(workdir)
        except Exception:
            sha = None
    report = VerificationReport()
    _repo_key = repo_key
    if not _repo_key:
        try:
            _repo_key = await git_ops.common_dir(workdir)
        except Exception:
            _repo_key = ""
    commands: list[str] = []
    commands.extend(workspace.test_commands)
    commands.extend(workspace.lint_commands)
    commands.extend(workspace.typecheck_commands)
    if include_build:
        commands.extend(workspace.build_commands)

    sandbox_ok = sandbox_available()
    for command in commands:
        argv = shlex.split(command)
        if not argv:
            continue
        events.publish(EventType.TEST_STARTED, mission_id, command=command)
        if not sandbox_ok:
            report.results.append(
                CommandResult(
                    command=command,
                    exit_code=None,
                    passed=False,
                    duration_s=0.0,
                    tail="sandboxed execution unavailable on this platform — bubblewrap (bwrap) not found",
                    likely_environment_issue=True,
                )
            )
            events.publish(EventType.TEST_FAILED, mission_id, command=command, exit_code=None)
            continue
        result = await run_sandboxed(argv, workdir, timeout_s=VERIFY_TIMEOUT_S)
        passed = result.exit_code == 0 and not result.timed_out
        tail = result.combined_tail[-1500:]
        report.results.append(
            CommandResult(
                command=command,
                exit_code=result.exit_code,
                passed=passed,
                duration_s=result.duration_s,
                tail=tail,
                likely_environment_issue=(not passed) and _is_environment_failure(tail),
                skipped=count_skipped(tail),
            )
        )
        _ = db  # events are persisted by EventBus.publish; db kept for API symmetry
        events.publish(
            EventType.TEST_PASSED if passed else EventType.TEST_FAILED,
            mission_id,
            command=command,
            exit_code=result.exit_code,
            skipped=report.results[-1].skipped,
        )
    _persist_verification_attempt(
        db,
        mission_id=mission_id,
        product_project_id=product_project_id,
        task_id=task_id,
        sha=sha,
        repo_key=_repo_key,
        kind=kind,
        report=report,
        started_at=started,
    )
    return report
