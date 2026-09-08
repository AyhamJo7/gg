from pathlib import Path

import pytest

from orchestrator.security import (
    ensure_gitignore_protections,
    ensure_within,
    is_sensitive_file,
    read_redacted_tail,
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
    assert is_sensitive_file(".env.production")
    assert is_sensitive_file("config/.env.local")
    assert is_sensitive_file("certs/server.pem")
    assert is_sensitive_file("tls.key")
    assert is_sensitive_file("cert.p12")
    assert is_sensitive_file("cert.pfx")
    assert is_sensitive_file("client.ppk")
    assert is_sensitive_file("keystore.jks")
    assert is_sensitive_file("auth.json")
    assert is_sensitive_file("credentials.json")
    assert is_sensitive_file("aws_credentials")
    assert is_sensitive_file("id_rsa")
    assert is_sensitive_file("id_ed25519")
    assert is_sensitive_file("id_ecdsa")
    assert is_sensitive_file(".npmrc")
    assert is_sensitive_file(".pypirc")
    assert is_sensitive_file(".netrc")
    assert is_sensitive_file(".git-credentials")
    assert is_sensitive_file("secrets.yaml")
    assert is_sensitive_file("secret.yml")
    assert is_sensitive_file(".orchestrator/logs/run-123.stdout.log")
    assert is_sensitive_file(".ssh/id_rsa")
    assert is_sensitive_file(".aws/credentials")
    assert not is_sensitive_file("src/main.py")
    assert not is_sensitive_file(".env.example")
    assert not is_sensitive_file(".env.sample")
    assert not is_sensitive_file(".env.template")


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
    assert ".orchestrator/" in added
    content = (tmp_path / ".gitignore").read_text()
    assert ".env" in content
    assert ".orchestrator/" in content
    # idempotent
    assert ensure_gitignore_protections(tmp_path) == []


# ---------------------------------------------------------------------------
# MED-01: bounded tail redaction must not leak across truncation boundaries.
# Fake secrets only.
# ---------------------------------------------------------------------------

FAKE_TOKEN = "sk-" + "Q" * 40
FAKE_YAML_VALUE = "fakepassvalue123"


def _write(tmp_path: Path, name: str, data: bytes) -> Path:
    p = tmp_path / name
    p.write_bytes(data)
    return p


def test_tail_single_line_secret_redacted(tmp_path: Path):
    p = _write(tmp_path, "a.log", b"start\nkey " + FAKE_TOKEN.encode() + b"\nend\n")
    text, size, truncated = read_redacted_tail(p, 1024)
    assert not truncated
    assert FAKE_TOKEN not in text
    assert "[REDACTED_API_KEY]" in text
    assert "start" in text and "end" in text


def test_tail_yaml_anchor_split_by_cut(tmp_path: Path):
    """AGY MED-01 repro: a long run without newlines pushes the cut between
    the YAML anchor line and its value. The old slice-then-redact code
    dropped the anchor and served the value raw."""
    giant = b"f" * 2000 + b"password:\n"
    value = b"  " + FAKE_YAML_VALUE.encode() + b"\n"
    tail = b"t\n" * 50
    p = _write(tmp_path, "b.log", giant + value + tail)
    text, size, truncated = read_redacted_tail(p, 1024)
    assert truncated
    assert FAKE_YAML_VALUE not in text
    assert text.endswith("t\n")
    assert size == len(giant + value + tail)


def test_tail_cut_inside_long_token_line(tmp_path: Path):
    """One huge line carrying a token near its end: the whole line is
    redacted together, so only marker fragments (inert) may be served."""
    filler = b"L" * 3000
    line = b"export TOKEN=" + FAKE_TOKEN.encode() + b" done\n"
    tail = b"final line\n"
    p = _write(tmp_path, "c.log", filler + b"\n" + line + tail)
    text, size, truncated = read_redacted_tail(p, 1024)
    assert truncated
    assert FAKE_TOKEN not in text
    assert "Q" * 16 not in text
    assert "[REDACTED]" in text
    assert "final line" in text


def test_tail_single_line_larger_than_cap(tmp_path: Path):
    line = b"x" * 500 + FAKE_TOKEN.encode() + b"y" * 500 + b"\n"
    p = _write(tmp_path, "d.log", line)
    text, size, truncated = read_redacted_tail(p, 1024)
    assert truncated
    assert len(text.encode("utf-8")) <= 1024
    assert FAKE_TOKEN not in text
    assert "Q" * 16 not in text


def test_tail_utf8_boundaries(tmp_path: Path):
    body = ("héllo wörld €€€\n" * 200).encode("utf-8") + b"key " + FAKE_TOKEN.encode() + b"\nfin \xe2\x82\xac\n"
    p = _write(tmp_path, "e.log", body)
    text, size, truncated = read_redacted_tail(p, 1024)
    assert truncated
    text.encode("utf-8")  # must be valid, no lone surrogates
    assert FAKE_TOKEN not in text
    assert "fin" in text


def test_tail_empty_file(tmp_path: Path):
    p = _write(tmp_path, "f.log", b"")
    assert read_redacted_tail(p, 1024) == ("", 0, False)


def test_tail_exact_cap_not_truncated(tmp_path: Path):
    p = _write(tmp_path, "g.log", b"z" * 1024)
    text, size, truncated = read_redacted_tail(p, 1024)
    assert (size, truncated) == (1024, False)
    assert text == "z" * 1024


def test_tail_large_log_bounded(tmp_path: Path):
    import time

    chunk = b"0123456789abcdef\n" * 64  # 1088 bytes, newline-terminated
    p = _write(tmp_path, "h.log", chunk * 3000 + b"key " + FAKE_TOKEN.encode() + b"\n")
    assert p.stat().st_size > 3_000_000
    started = time.monotonic()
    text, size, truncated = read_redacted_tail(p, 1024)
    elapsed = time.monotonic() - started
    assert truncated and size > 3_000_000
    assert len(text.encode("utf-8")) <= 1024
    assert FAKE_TOKEN not in text
    assert elapsed < 5
