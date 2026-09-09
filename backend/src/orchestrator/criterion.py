"""Requirement-criterion acceptance checks (F-LIFE-01).

A phase mission reaching COMPLETED records only that implementation work
finished. A required criterion becomes SATISFIED solely through an executed
check: the criterion's declared ``verify`` command run against the target
repository, with command, exit code, output, and Git SHA recorded. Reviewer
prose is never equivalent to an objective result.

Safety: only allowlisted first-word commands run, split with shlex (no
shell), pinned to the target repo, bounded in time and output, with secrets
redacted. Planner-supplied text is never executed beyond this contract.
"""

from __future__ import annotations

import logging
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .process import run_process
from .security import redact

logger = logging.getLogger(__name__)

#: First-word allowlist for criterion verification commands.
ALLOWED_COMMANDS = frozenset(
    {"npm", "npx", "node", "python", "python3", "pytest", "uv", "pnpm", "yarn", "cargo", "go", "make"}
)

#: Interpreters allowlisted above that accept an inline-code flag — the flag defeats the
#: allowlist (arbitrary code, not a repo-declared script) so it is rejected outright.
INLINE_CODE_INTERPRETERS = frozenset({"node", "python", "python3"})
INLINE_CODE_FLAGS = frozenset({"-c", "-e", "--eval", "-p", "--print", "-pe", "-ne"})

#: Never executed, even when allowlisted above. Matched as whole shlex tokens (or
#: contiguous token sequences below), never as substrings of an unrelated word.
BLOCKED_TOKENS = frozenset(
    {"rm", "sudo", "su", "shutdown", "reboot", "halt", "poweroff", "mkfs", "dd", "chmod", "chown", "git"}
)
BLOCKED_SEQUENCES: tuple[tuple[str, ...], ...] = (
    ("npm", "publish"),
    ("cargo", "publish"),
)

MAX_COMMAND_CHARS = 2000
CHECK_TIMEOUT_S = 300.0
TAIL_CHARS = 3000


@dataclass
class CriterionCheck:
    criterion_id: str
    command: str
    executable: bool
    exit_code: int | None = None
    passed: bool = False
    output_tail: str = ""
    skipped_reason: str = ""


def is_executable_command(verify: str) -> tuple[bool, str]:
    """Decide whether a declared verify string may be executed. Returns (ok, command)."""
    text = (verify or "").strip()
    if not text or len(text) > MAX_COMMAND_CHARS:
        return False, ""
    try:
        argv = shlex.split(text)
    except ValueError:
        return False, ""
    if not argv:
        return False, ""
    if argv[0] not in ALLOWED_COMMANDS:
        return False, ""
    argv_lower = [a.lower() for a in argv]
    if any(tok in BLOCKED_TOKENS for tok in argv_lower):
        return False, ""
    for seq in BLOCKED_SEQUENCES:
        n = len(seq)
        if any(tuple(argv_lower[i : i + n]) == seq for i in range(len(argv_lower) - n + 1)):
            return False, ""
    if argv[0] in INLINE_CODE_INTERPRETERS and any(a in INLINE_CODE_FLAGS for a in argv_lower[1:]):
        return False, ""
    return True, text


async def run_check_command(repo: Path, command: str) -> tuple[int | None, str]:
    """Execute one allowlisted verification command in the target repo."""
    argv = shlex.split(command)
    result = await run_process(argv, cwd=repo, timeout_s=CHECK_TIMEOUT_S)
    tail = redact(result.combined_tail[-TAIL_CHARS:])
    return result.exit_code, tail


async def evaluate_criteria(repo: Path, criteria: list[dict[str, Any]]) -> list[CriterionCheck]:
    """Execute executable checks; mark the rest non-executable (blocking)."""
    checks: list[CriterionCheck] = []
    for criterion in criteria:
        cid = str(criterion.get("id", ""))
        verify = str(criterion.get("verify", ""))
        ok, command = is_executable_command(verify)
        check = CriterionCheck(criterion_id=cid, command=command, executable=ok)
        if not ok:
            check.skipped_reason = (
                f"verify is not an executable allowlisted command (allowed: {', '.join(sorted(ALLOWED_COMMANDS))})"
            )
            checks.append(check)
            continue
        try:
            exit_code, tail = await run_check_command(repo, command)
        except Exception as exc:
            logger.exception("criterion check %s failed to execute", cid)
            check.output_tail = f"executor error: {exc}"[:TAIL_CHARS]
            checks.append(check)
            continue
        check.exit_code = exit_code
        check.passed = exit_code == 0
        check.output_tail = tail
        checks.append(check)
    return checks
