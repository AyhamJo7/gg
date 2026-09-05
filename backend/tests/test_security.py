import pytest

from orchestrator.security import (
    ensure_gitignore_protections,
    ensure_within,
    is_sensitive_file,
    redact,
    validate_workspace_path,
)


def test_redact_api_keys():
    assert "sk-ant-" not in redact("key is sk-ant-abc123def456ghi")
    assert "[REDACTED_ANTHROPIC_KEY]" in redact("key is sk-ant-abc123def456ghi")
    assert "ghp_" not in redact("token ghp_0123456789abcdefghijklm")
    assert "[REDACTED]" in redact("api_key = 'supersecretvalue123'")


def test_redact_jwt():
    text = redact("header eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJVadQssw5c")
    assert "eyJ" not in text


def test_sensitive_files():
    assert is_sensitive_file(".env")
    assert is_sensitive_file("config/.env.local")
    assert is_sensitive_file("certs/server.pem")
    assert is_sensitive_file("auth.json")
    assert not is_sensitive_file("src/main.py")
    assert not is_sensitive_file(".env.example")


def test_validate_workspace(tmp_path):
    assert validate_workspace_path(str(tmp_path)) == tmp_path.resolve()
    with pytest.raises(ValueError):
        validate_workspace_path(tmp_path / "missing")
    with pytest.raises(ValueError):
        validate_workspace_path(tmp_path / "file.txt") if (tmp_path / "file.txt").write_text("x") else None
    with pytest.raises(ValueError):
        validate_workspace_path("/")


def test_ensure_within(tmp_path):
    assert ensure_within(tmp_path, "sub/dir") == (tmp_path / "sub/dir").resolve()
    with pytest.raises(ValueError):
        ensure_within(tmp_path, "../escape")
    with pytest.raises(ValueError):
        ensure_within(tmp_path, "/etc/passwd")


def test_gitignore_protections(tmp_path):
    added = ensure_gitignore_protections(tmp_path)
    assert ".env" in added
    content = (tmp_path / ".gitignore").read_text()
    assert ".env" in content
    # idempotent
    assert ensure_gitignore_protections(tmp_path) == []
