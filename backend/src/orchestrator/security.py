"""Security helpers: path validation and secret redaction.

Provider CLIs are privileged processes. All workspace paths are canonicalized
and validated before use; all log output passes through redaction.
"""

from __future__ import annotations

import re
from pathlib import Path

# Common secret shapes. Keep conservative: redact on suspicion.
SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"), "[REDACTED_ANTHROPIC_KEY]"),
    (re.compile(r"sk-[A-Za-z0-9_\-]{16,}"), "[REDACTED_API_KEY]"),
    (re.compile(r"ghp_[A-Za-z0-9]{20,}"), "[REDACTED_GH_TOKEN]"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "[REDACTED_GH_PAT]"),
    (re.compile(r"AIza[0-9A-Za-z_\-]{20,}"), "[REDACTED_GOOGLE_KEY]"),
    (re.compile(r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"), "[REDACTED_JWT]"),
    (re.compile(r"(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*['\"]?[^\s'\"]{8,}"), r"\1=[REDACTED]"),
]

# Filenames that must never be committed by the git ledger.
SENSITIVE_FILE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(^|/)\.env(\..*)?$"),
    re.compile(r"(^|/)\.env\.local$"),
    re.compile(r".*\.(pem|key|p12|pfx)$"),
    re.compile(r"(^|/)(credentials|auth)\.json$"),
]


def redact(text: str) -> str:
    for pattern, replacement in SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


NEVER_SENSITIVE = {".env.example", ".env.sample", ".env.template"}


def is_sensitive_file(relative_path: str) -> bool:
    normalized = relative_path.replace("\\", "/")
    if normalized.rsplit("/", 1)[-1] in NEVER_SENSITIVE:
        return False
    return any(p.search(normalized) for p in SENSITIVE_FILE_PATTERNS)


def validate_workspace_path(path: str | Path) -> Path:
    """Resolve and validate a user-supplied workspace path.

    Raises ValueError for nonexistent paths, non-directories, and anything
    suspicious (e.g. filesystem root or home dir itself, to avoid an agent
    being unleashed on the user's entire $HOME).
    """
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise ValueError(f"Workspace path does not exist: {p}")
    if not p.is_dir():
        raise ValueError(f"Workspace path is not a directory: {p}")
    if p == Path.home() or p == p.anchor or len(p.parts) < 2:  # type: ignore[comparison-overlap]
        raise ValueError(f"Refusing unsafe workspace root: {p}")
    return p


def ensure_within(root: Path, candidate: str | Path) -> Path:
    """Resolve candidate and guarantee it stays inside root (anti path-traversal)."""
    resolved = (root / candidate).resolve() if not Path(candidate).is_absolute() else Path(candidate).resolve()
    if root not in resolved.parents and resolved != root:
        raise ValueError(f"Path escapes workspace: {candidate}")
    return resolved


GITIGNORE_SECRET_ENTRIES = [".env", ".env.*", "!.env.example", "*.pem", "*.key"]


def ensure_gitignore_protections(git_root: Path) -> list[str]:
    """Append secret patterns to .gitignore if missing. Returns added entries."""
    gitignore = git_root / ".gitignore"
    existing = gitignore.read_text().splitlines() if gitignore.exists() else []
    existing_set = {line.strip() for line in existing}
    added: list[str] = []
    for entry in GITIGNORE_SECRET_ENTRIES:
        if entry not in existing_set:
            added.append(entry)
    if added:
        with gitignore.open("a") as fh:
            if existing and not existing[-1] == "":
                fh.write("\n")
            fh.write("# Added by GG Orchestrator — secret protection\n")
            fh.write("\n".join(added) + "\n")
    return added
