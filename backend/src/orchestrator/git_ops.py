"""Git as the engineering ledger.

- argv-only subprocess (never shell)
- checkpoints commit all non-sensitive changes with attributed messages
- sensitive files (.env, keys, credentials) are force-excluded from commits
- never destructive: no reset --hard, no clean -fd
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path

from .security import SECRET_PATTERNS, is_sensitive_file

logger = logging.getLogger(__name__)


@dataclass
class GitStatus:
    is_repo: bool
    branch: str = ""
    head: str | None = None
    modified: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    untracked: list[str] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        return not (self.modified or self.added or self.deleted or self.untracked)


class GitError(RuntimeError):
    pass


@dataclass(frozen=True)
class GitCommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes

    @property
    def text(self) -> str:
        return self.stdout.decode(errors="replace").strip()

    @property
    def err_text(self) -> str:
        return self.stderr.decode(errors="replace").strip()


GIT_TIMEOUT_S = 30.0


def _run_git_sync(root: Path, args: tuple[str, ...]) -> GitCommandResult:
    import subprocess

    proc = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        timeout=GIT_TIMEOUT_S,
        check=False,
    )
    return GitCommandResult(proc.returncode, proc.stdout, proc.stderr)


async def _spawn_git(root: Path, *args: str) -> GitCommandResult:
    """Run a short-lived git command in a worker thread with a hard timeout.

    Uses subprocess.run in a thread instead of asyncio subprocess machinery:
    an engine task must never freeze on event-loop subprocess edge cases;
    any hang becomes a GitError (classified, recoverable) instead.
    """
    try:
        return await asyncio.to_thread(_run_git_sync, root, args)
    except __import__("subprocess").TimeoutExpired as exc:
        raise GitError(f"git {' '.join(args)} timed out after {GIT_TIMEOUT_S}s") from exc


async def _git(root: Path, *args: str, check: bool = True) -> str:
    res = await _spawn_git(root, *args)
    if check and res.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {res.stderr.decode(errors='replace')[:400]}")
    return res.text


async def is_repo(root: Path) -> bool:
    res = await _spawn_git(root, "rev-parse", "--is-inside-work-tree")
    return res.returncode == 0 and res.text == "true"


async def init_repo(root: Path) -> None:
    await _git(root, "init")
    await _git(root, "config", "user.email", "orchestrator@local")
    await _git(root, "config", "user.name", "GG Orchestrator")


async def head_sha(root: Path) -> str | None:
    res = await _spawn_git(root, "rev-parse", "HEAD")
    return res.text if res.returncode == 0 else None


async def status(root: Path) -> GitStatus:
    if not await is_repo(root):
        return GitStatus(is_repo=False)
    branch = await _git(root, "branch", "--show-current", check=False)
    head = await head_sha(root)
    result = GitStatus(is_repo=True, branch=branch, head=head)
    raw = await _git(root, "status", "--porcelain=v1", "-z")
    entries = [e for e in raw.split("\x00") if e]
    for entry in entries:
        code, path = entry[:2], entry[3:]
        x, y = code[0], code[1]
        if code == "??":
            result.untracked.append(path)
        else:
            if x in "M" or y == "M":
                result.modified.append(path)
            if x in "A" or y == "A":
                result.added.append(path)
            if x in "D" or y == "D":
                result.deleted.append(path)
    return result


async def diff(root: Path, stat_only: bool = False) -> str:
    args = ["diff", "--stat"] if stat_only else ["diff"]
    tracked = await _git(root, *args, check=False)
    staged = await _git(root, "diff", "--cached", *(["--stat"] if stat_only else []), check=False)
    return (staged + "\n" + tracked).strip()


async def recent_commits(root: Path, count: int = 10) -> list[str]:
    raw = await _git(root, "log", f"-{count}", "--oneline", check=False)
    return [line for line in raw.splitlines() if line]


async def checkpoint(root: Path, message: str) -> str | None:
    """Commit all non-sensitive changes. Returns commit SHA or None if nothing to commit.

    Sensitive files and internal logs (.orchestrator/) are never staged — they are
    explicitly reset out of the index, and staged additions are scanned for secret patterns.
    """
    st = await status(root)
    if not st.is_repo:
        raise GitError("not a git repository")
    # Stage everything, then unstage internal and sensitive paths.
    await _git(root, "add", "-A")

    # Unconditionally exclude orchestrator directory from commits
    await _git(root, "reset", "-q", "--", ".orchestrator", check=False)

    # 1. Path-based sensitive file check
    staged_raw = await _git(root, "diff", "--cached", "--name-only", check=False)
    staged_files = [p.strip() for p in staged_raw.splitlines() if p.strip()]
    for path in staged_files:
        if is_sensitive_file(path):
            await _git(root, "reset", "-q", "--", path, check=False)

    # 2. Content-based secret check on remaining staged diffs
    staged_raw = await _git(root, "diff", "--cached", "--name-only", check=False)
    remaining_staged = [p.strip() for p in staged_raw.splitlines() if p.strip()]
    for path in remaining_staged:
        patch = await _git(root, "diff", "--cached", "-U0", "--", path, check=False)
        has_secret = False
        for line in patch.splitlines():
            if line.startswith("+") and not line.startswith("+++"):
                added_text = line[1:]
                for pat, _ in SECRET_PATTERNS:
                    if pat.search(added_text):
                        has_secret = True
                        break
            if has_secret:
                break
        if has_secret:
            await _git(root, "reset", "-q", "--", path, check=False)
        else:
            try:
                fp = root / path
                if fp.is_file() and fp.stat().st_size > 5 * 1024 * 1024:
                    logger.warning("Staged file exceeds 5MB threshold: %s (%d bytes)", path, fp.stat().st_size)
            except OSError:
                pass

    staged_final = await _git(root, "diff", "--cached", "--name-only", check=False)
    if not staged_final.strip():
        return None
    await _git(root, "commit", "-m", message)
    return await head_sha(root)
