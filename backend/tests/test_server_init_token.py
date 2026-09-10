"""`gg-backend --init-token-only`: generate the auth token and exit without
starting the server — used by `make dev`/`make frontend` to guarantee the
token file exists before the frontend needs to read it."""

from __future__ import annotations

import sys
from pathlib import Path

from orchestrator import server


def test_init_token_only_writes_token_and_returns(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["gg-backend", "--init-token-only"])
    called = {"uvicorn": False}
    monkeypatch.setattr(server.uvicorn, "run", lambda *a, **k: called.__setitem__("uvicorn", True))

    server.main()

    assert called["uvicorn"] is False
    token_path = tmp_path / ".orchestrator" / "auth_token"
    assert token_path.exists()
    assert len(token_path.read_text().strip()) > 20


def test_init_token_only_is_idempotent(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["gg-backend", "--init-token-only"])
    monkeypatch.setattr(server.uvicorn, "run", lambda *a, **k: None)

    server.main()
    first = (tmp_path / ".orchestrator" / "auth_token").read_text()
    server.main()
    second = (tmp_path / ".orchestrator" / "auth_token").read_text()
    assert first == second
