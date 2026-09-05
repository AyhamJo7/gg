"""Verification engine: evidence-based completion.

Runs the project's real toolchain (detected by workspace.inspect_workspace),
records every command + exit code, and never trusts agent self-reporting.
Commands run via argv arrays; each string from the detector is split with
shlex (safe — no shell interpretation, word-splitting only).
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from pathlib import Path

from .db import Database
from .events import EventBus
from .models import EventType
from .process import run_process
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

    for command in commands:
        argv = shlex.split(command)
        if not argv:
            continue
        events.publish(EventType.TEST_STARTED, mission_id, command=command)
        result = await run_process(argv, cwd=workdir, timeout_s=VERIFY_TIMEOUT_S)
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
