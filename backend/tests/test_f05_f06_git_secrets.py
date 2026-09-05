"""F-05 & F-06 regression suite: secrets and orchestrator internals are never committed to git history.

- F-05: .orchestrator/ unredacted logs and handoffs are unconditionally excluded from git checkpoints.
- F-06: Comprehensive sensitive file patterns (keys, credentials, tokens, keystores) and staged content
  diff checks prevent any secrets from entering git history.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator import git_ops
from orchestrator.security import ensure_gitignore_protections, is_sensitive_file


@pytest.mark.asyncio
async def test_f05_f06_secrets_and_orchestrator_logs_never_committed(tmp_path: Path) -> None:
    await git_ops.init_repo(tmp_path)

    # 1. Plant .orchestrator files (F-05)
    log_dir = tmp_path / ".orchestrator" / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "run-1.stdout.log").write_text("raw log with sk-ant-api03-TOPSECRET1234567890")
    handoff_dir = tmp_path / ".orchestrator" / "handoffs"
    handoff_dir.mkdir(parents=True)
    (handoff_dir / "impl.md").write_text("# Handoff doc")

    # 2. Plant sensitive files from the audit matrix (F-06)
    (tmp_path / "id_rsa").write_text("-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA0...")
    (tmp_path / ".npmrc").write_text("//registry.npmjs.org/:_authToken=npm_SECRETTOKEN123")
    (tmp_path / ".git-credentials").write_text("https://user:password123@github.com")
    (tmp_path / "aws_credentials").write_text("[default]\naws_access_key_id=AKIAIOSFODNN7EXAMPLE")
    (tmp_path / "secrets.yaml").write_text("api_key: sk-live-1234567890abcdef")
    (tmp_path / "secret.yml").write_text("secret_val: foo")
    (tmp_path / "keystore.jks").write_text("binary-keystore-content")
    (tmp_path / "server.pem").write_text("cert")
    (tmp_path / "tls.key").write_text("key")
    (tmp_path / "auth.json").write_text('{"token": "xyz"}')

    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir(parents=True)
    (ssh_dir / "id_ed25519").write_text("ssh-ed25519-key")

    aws_dir = tmp_path / ".aws"
    aws_dir.mkdir(parents=True)
    (aws_dir / "config").write_text("[default]\nregion=us-east-1")

    # 3. Plant code file with inline secret (content-side check)
    src_dir = tmp_path / "src"
    src_dir.mkdir(parents=True)
    (src_dir / "leaky.py").write_text('API_KEY = "sk-ant-api03-SECRETKEYINCODE12345678"\n')

    # 4. Plant clean benign file
    (src_dir / "clean.py").write_text('print("Clean operational code")\n')

    # Run checkpoint
    sha = await git_ops.checkpoint(tmp_path, "test: hardened checkpoint")
    assert sha is not None, "Checkpoint should succeed committing clean files"

    # Verify committed files
    committed_files_raw = await git_ops._git(tmp_path, "show", "--name-only", "--format=", "HEAD")
    committed_files = [line.strip() for line in committed_files_raw.splitlines() if line.strip()]

    # Assert clean.py was committed
    assert "src/clean.py" in committed_files

    # Assert NO sensitive or orchestrator files were committed
    forbidden_committed = [
        f for f in committed_files
        if f.startswith(".orchestrator")
        or is_sensitive_file(f)
        or f == "src/leaky.py"
    ]
    assert forbidden_committed == [], f"Forbidden files committed into git: {forbidden_committed}"

    # Verify commit diff contains zero secrets
    full_commit_diff = await git_ops._git(tmp_path, "show", "HEAD")
    assert "sk-ant-" not in full_commit_diff
    assert "npm_SECRETTOKEN" not in full_commit_diff
    assert "password123" not in full_commit_diff
    assert "RSA PRIVATE KEY" not in full_commit_diff
    assert "TOPSECRET" not in full_commit_diff


def test_gitignore_protections_includes_audit_entries(tmp_path: Path) -> None:
    added = ensure_gitignore_protections(tmp_path)
    content = (tmp_path / ".gitignore").read_text()
    for entry in [".orchestrator/", "id_rsa*", ".npmrc", ".git-credentials", "*credentials*", "*secret*.yaml"]:
        assert entry in content
        assert entry in added
