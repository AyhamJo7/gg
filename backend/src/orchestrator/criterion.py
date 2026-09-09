"""Requirement-criterion acceptance checks (F-LIFE-01).

A phase mission reaching COMPLETED records only that implementation work
finished. A required criterion becomes SATISFIED solely through an executed
check: the criterion's declared ``verify`` command run against the target
repository, with command, exit code, output, and Git SHA recorded. Reviewer
prose is never equivalent to an objective result.

Safety: commands are matched against a closed set of fully-specified argv
SHAPES per tool (exact token counts, exact subcommand adjacency, path
arguments syntax-checked for traversal/absolute escapes and re-checked
against the real repo via ``Path.resolve()`` at execution time). A command
that does not match one of these shapes exactly is rejected — there is no
"allowlisted first word + free-form trailing args" fallback for any tool.
This replaced an earlier denylist-of-dangerous-flags design that could not
converge against tools (``uv run``, ``go run``, ``node``, ``make``) with
effectively unbounded flag grammars for shelling out to something else.
"""

from __future__ import annotations

import logging
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .process import run_process
from .security import redact

logger = logging.getLogger(__name__)

#: Never executed, even inside an otherwise-matched shape — matched as whole
#: shlex tokens (case-insensitive), never as substrings of an unrelated word.
#: Belt-and-suspenders on top of shape matching, not the primary defense.
BLOCKED_TOKENS = frozenset(
    {"rm", "sudo", "su", "shutdown", "reboot", "halt", "poweroff", "mkfs", "dd", "chmod", "chown", "git"}
)

#: Characters that are never legitimate inside a pytest -k/-m pattern
#: argument. execvp with an argv array never invokes a shell, so these can't
#: actually trigger shell interpretation — this is defense in depth only.
_SHELL_META = frozenset(";&|$()<>`\n")

#: npm run/make target names: must start with a word character so a token
#: like "-fMakefile.evil" or "-C/tmp" (a flag disguised as a target/script
#: name, valid via shlex but never a legitimate identifier) is rejected.
_TARGET_NAME_RE = re.compile(r"^[A-Za-z0-9_][\w.:-]*$")
_SCRIPT_NAME_RE = re.compile(r"^[A-Za-z0-9_][\w:-]*$")

_PY_MODULE_ALLOWLIST = frozenset({"pytest", "unittest", "mypy", "ruff"})

_PYTEST_FLAGS_NO_ARG = frozenset({"-q", "-v", "-vv", "-x", "-s", "--tb=short", "--tb=long", "--tb=no"})
_PYTEST_FLAGS_WITH_ARG = frozenset({"-k", "-m"})

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


@dataclass
class _ShapeMatch:
    """A command matched one of the closed argv shapes below.

    ``path_tokens`` lists the argv tokens that name a path inside the repo —
    re-checked against the real filesystem (symlink-aware) by
    ``confined_to_repo`` at execution time, since syntax alone can't catch a
    symlink that resolves outside the repo.
    """

    path_tokens: tuple[str, ...] = field(default_factory=tuple)


def _no_shell_meta(token: str) -> bool:
    return not any(c in _SHELL_META for c in token)


def _safe_rel_path(token: str) -> bool:
    """Syntactic-only path safety: relative, no `..` segment, no shell metachars.

    This is necessarily conservative and repo-agnostic (no filesystem access
    here) — ``confined_to_repo`` performs the real ``Path.resolve()``
    containment check once a repo is known, which also catches a symlink
    inside the repo that resolves outside it.
    """
    if not token or not _no_shell_meta(token):
        return False
    if token.startswith("/") or token.startswith("~"):
        return False
    if token.startswith("-"):
        # A leading "-" is never a legitimate path — it's always a disguised
        # flag. Without this, an attached-flag token that happens to end in
        # a recognized extension (`-mpy.py`, `--require=./py.js`) slides past
        # the extension check into the "run this script" shape while the
        # real interpreter parses it as a flag with code-execution effects
        # the shape was never meant to permit (python -m, node --require).
        return False
    parts = token.replace("\\", "/").split("/")
    return ".." not in parts and "" not in parts[1:]


def _match_pytest(argv: list[str]) -> _ShapeMatch | None:
    if not argv or argv[0] != "pytest":
        return None
    paths: list[str] = []
    i = 1
    while i < len(argv):
        tok = argv[i]
        if tok in _PYTEST_FLAGS_WITH_ARG:
            if i + 1 >= len(argv) or not _no_shell_meta(argv[i + 1]):
                return None
            i += 2
            continue
        if tok in _PYTEST_FLAGS_NO_ARG:
            i += 1
            continue
        if tok.startswith("-") or not _safe_rel_path(tok):
            return None
        paths.append(tok)
        i += 1
    return _ShapeMatch(path_tokens=tuple(paths))


def _match_npm(argv: list[str]) -> _ShapeMatch | None:
    # Deliberately npm-only — npx/pnpm/yarn all carry a "download and execute
    # an arbitrary/remote package" primitive (npx <pkg>, pnpm|yarn dlx) at
    # their core, which can't be made safe with a fixed-shape allowlist the
    # way "run a package.json-declared script" can. None of them are tested
    # as a legitimate pattern here, so they are not supported at all.
    if not argv or argv[0] != "npm":
        return None
    if len(argv) == 2 and argv[1] in ("test", "ci", "install"):
        return _ShapeMatch()
    if len(argv) == 3 and argv[1] == "run" and _SCRIPT_NAME_RE.match(argv[2]):
        return _ShapeMatch()
    return None


def _match_go(argv: list[str]) -> _ShapeMatch | None:
    if len(argv) != 3 or argv[0] != "go" or argv[1] not in ("test", "build", "run"):
        return None
    target = argv[2]
    if target == "./...":
        return _ShapeMatch()
    if target == ".":
        return _ShapeMatch(path_tokens=(".",))
    if target.startswith("./") and _safe_rel_path(target):
        return _ShapeMatch(path_tokens=(target,))
    return None


def _match_make(argv: list[str]) -> _ShapeMatch | None:
    if len(argv) != 2 or argv[0] != "make" or not _TARGET_NAME_RE.match(argv[1]):
        return None
    return _ShapeMatch()


def _match_cargo(argv: list[str]) -> _ShapeMatch | None:
    # "cargo run" is deliberately not a shape: unlike go's package-relative
    # target, cargo run accepts a --manifest-path escape and is not exercised
    # by any legitimate pattern here.
    if not argv or argv[0] != "cargo" or len(argv) < 2 or argv[1] not in ("test", "build"):
        return None
    if len(argv) == 2:
        return _ShapeMatch()
    if len(argv) == 3 and argv[2] == "--release":
        return _ShapeMatch()
    return None


def _match_node(argv: list[str]) -> _ShapeMatch | None:
    if len(argv) != 2 or argv[0] != "node":
        return None
    path = argv[1]
    if not path.endswith((".js", ".mjs", ".cjs")) or not _safe_rel_path(path):
        return None
    return _ShapeMatch(path_tokens=(path,))


def _match_python(argv: list[str]) -> _ShapeMatch | None:
    if not argv or argv[0] not in ("python", "python3"):
        return None
    if len(argv) == 2 and argv[1].endswith(".py") and _safe_rel_path(argv[1]):
        return _ShapeMatch(path_tokens=(argv[1],))
    if len(argv) == 3 and argv[1] == "-m" and argv[2] in _PY_MODULE_ALLOWLIST:
        return _ShapeMatch()
    return None


def _match_uv(argv: list[str]) -> _ShapeMatch | None:
    """`uv run <inner>` where `<inner>` is itself one of the other shapes.

    Recursion is capped at one level — an inner command starting with `uv`
    is rejected outright, so this can never nest ("uv run uv run ...").
    """
    if len(argv) < 3 or argv[0] != "uv" or argv[1] != "run":
        return None
    inner = argv[2:]
    if inner[0] == "uv":
        return None
    return _match_shape(inner)


_MATCHERS = (_match_pytest, _match_npm, _match_go, _match_make, _match_cargo, _match_node, _match_python, _match_uv)


def _match_shape(argv: list[str]) -> _ShapeMatch | None:
    for matcher in _MATCHERS:
        match = matcher(argv)
        if match is not None:
            return match
    return None


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
    if any(tok.lower() in BLOCKED_TOKENS for tok in argv):
        return False, ""
    if _match_shape(argv) is None:
        return False, ""
    return True, text


def confined_to_repo(command: str, repo: Path) -> bool:
    """Re-check an already-`is_executable_command`-approved command's path
    arguments against the real filesystem.

    Syntactic checks alone (no `..`, not absolute) can't catch a symlink
    that lives inside the repo but resolves outside it — `Path.resolve()`
    follows symlinks, so this closes that gap. Must be called with the real
    target repo immediately before execution; never trust the syntactic
    check alone for that reason.
    """
    try:
        argv = shlex.split(command)
    except ValueError:
        return False
    match = _match_shape(argv)
    if match is None:
        return False
    repo_resolved = repo.resolve()
    for tok in match.path_tokens:
        if tok == "./...":
            continue
        candidate = (repo / tok).resolve()
        if not candidate.is_relative_to(repo_resolved):
            return False
    return True


async def run_check_command(repo: Path, command: str) -> tuple[int | None, str]:
    """Execute one allowlisted verification command in the target repo."""
    if not confined_to_repo(command, repo):
        return None, "path argument in verification command resolves outside the target repository"
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
            check.skipped_reason = "verify is not an executable allowlisted command"
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
