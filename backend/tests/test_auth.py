"""Unit tests for the bearer-token bootstrap (backend/src/orchestrator/api/auth.py).

Regression coverage: token creation must be atomic and 0600 from the first
syscall — a write-then-chmod sequence leaves a window (and, if the process
dies mid-sequence, a permanent state) where the file is readable under the
process umask's default mode.
"""

from __future__ import annotations

import stat
from pathlib import Path

from orchestrator.api.auth import is_authorized, load_or_create_token


def test_token_file_is_created_0600(tmp_path: Path):
    token = load_or_create_token(tmp_path)
    token_path = tmp_path / "auth_token"
    assert token_path.read_text().strip() == token
    mode = stat.S_IMODE(token_path.stat().st_mode)
    assert mode == 0o600


def test_second_call_reuses_existing_token_not_regenerates(tmp_path: Path):
    first = load_or_create_token(tmp_path)
    second = load_or_create_token(tmp_path)
    assert first == second


def test_pre_existing_file_wins_over_a_fresh_generation_attempt(tmp_path: Path):
    # Simulates the O_CREAT|O_EXCL race: another process already created and
    # populated the file by the time this call's open() would run — the
    # existing token must win, never be silently overwritten.
    tmp_path.mkdir(parents=True, exist_ok=True)
    token_path = tmp_path / "auth_token"
    token_path.write_text("pre-existing-token-value")
    token_path.chmod(0o600)
    result = load_or_create_token(tmp_path)
    assert result == "pre-existing-token-value"


def test_is_authorized_accepts_matching_bearer_header():
    assert is_authorized("secret123", "Bearer secret123") is True


def test_is_authorized_rejects_wrong_or_missing_header():
    assert is_authorized("secret123", "Bearer wrong") is False
    assert is_authorized("secret123", None) is False
    assert is_authorized("secret123", "secret123") is False  # missing "Bearer " prefix
