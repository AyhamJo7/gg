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

import shlex
from dataclasses import dataclass, field
from pathlib import Path

from .db import Database
from .events import EventBus
from .models import EventType
from .sandbox import run_sandboxed, sandbox_available
from .workspace import WorkspaceInfo

VERIFY_TIMEOUT_S = 600.0


@dataclass
class CommandResult:
    command: str
    exit_code: int | None
    passed: bool
    duration_s: float
    tail: str = ""


@dataclass
class VerificationReport:
    results: list[CommandResult] = field(default_factory=list)

    @property
    def all_passed(self) -> bool:
        return bool(self.results) and all(r.passed for r in self.results)

    @property
    def attempted(self) -> bool:
        return bool(self.results)

    def summary(self) -> str:
        lines = []
        for r in self.results:
            mark = "PASS" if r.passed else "FAIL"
            lines.append(f"[{mark}] {r.command} (exit={r.exit_code}, {r.duration_s:.1f}s)")
        return "\n".join(lines)


async def run_verification(
    workspace: WorkspaceInfo,
    db: Database,
    events: EventBus,
    mission_id: str,
    workdir: Path,
    include_build: bool = True,
) -> VerificationReport:
    report = VerificationReport()
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
                )
            )
            events.publish(EventType.TEST_FAILED, mission_id, command=command, exit_code=None)
            continue
        result = await run_sandboxed(argv, workdir, timeout_s=VERIFY_TIMEOUT_S)
        passed = result.exit_code == 0 and not result.timed_out
        report.results.append(
            CommandResult(
                command=command,
                exit_code=result.exit_code,
                passed=passed,
                duration_s=result.duration_s,
                tail=result.combined_tail[-1500:],
            )
        )
        _ = db  # events are persisted by EventBus.publish; db kept for API symmetry
        events.publish(
            EventType.TEST_PASSED if passed else EventType.TEST_FAILED,
            mission_id,
            command=command,
            exit_code=result.exit_code,
        )
    return report
