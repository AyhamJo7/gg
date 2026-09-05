"""Git as the engineering ledger.

- argv-only subprocess (never shell)
- checkpoints commit all non-sensitive changes with attributed messages
- sensitive files (.env, keys, credentials) are force-excluded from commits
- never destructive: no reset --hard, no clean -fd
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from .security import is_sensitive_file


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


async def _git(root: Path, *args: str, check: bool = True) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(root), *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {err.decode(errors='replace')[:400]}")
    return out.decode(errors="replace").strip()


async def is_repo(root: Path) -> bool:
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(root), "rev-parse", "--is-inside-work-tree",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await proc.communicate()
    return proc.returncode == 0 and out.decode().strip() == "true"


async def init_repo(root: Path) -> None:
    await _git(root, "init")
    await _git(root, "config", "user.email", "orchestrator@local")
    await _git(root, "config", "user.name", "GG Orchestrator")


async def head_sha(root: Path) -> str | None:
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(root), "rev-parse", "HEAD",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await proc.communicate()
    return out.decode().strip() if proc.returncode == 0 else None


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

    Sensitive files are never staged — they are explicitly reset out of the index.
    """
    st = await status(root)
    if not st.is_repo:
        raise GitError("not a git repository")
    # Stage everything, then unstage sensitive paths. `git add -A` + targeted reset
    # is safer than enumerating files ourselves (handles renames/deletes).
    await _git(root, "add", "-A")
    sensitive = [p for p in (st.modified + st.added + st.untracked) if is_sensitive_file(p)]
    for path in sensitive:
        await _git(root, "reset", "-q", "--", path, check=False)
    staged = await _git(root, "diff", "--cached", "--name-only")
    if not staged.strip():
        return None
    await _git(root, "commit", "-m", message, "--no-verify")
    return await head_sha(root)
