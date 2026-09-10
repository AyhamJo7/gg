"""Round-11 regression: the WS bearer token must never reach uvicorn's access log.

Every other test that exercises WS auth (test_api.py) runs in-process over
ASGI transport, or (test_sandbox.py's isolated_server.py helper) spawns a real
uvicorn with log_level="warning" — neither reproduces the actual production
entrypoint (server.py's main(), log_level="info", default access logging on),
which is exactly the config that let the token-in-query-param bug through
every prior review round undetected. This test spawns the real entrypoint.
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import websockets


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_health(port: int, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"http://127.0.0.1:{port}/api/health", timeout=2.0).status_code == 200:
                return
        except Exception:
            time.sleep(0.3)
    raise TimeoutError("backend never became healthy")


async def _connect_twice(port: int, token: str) -> None:
    async with websockets.connect(f"ws://127.0.0.1:{port}/ws/events", subprotocols=[token]) as ws:
        assert ws.subprotocol == token  # server echoed the selected subprotocol per RFC 6455
    # Reconnect once too — ws.ts backs off and reconnects on every disconnect,
    # so a real session logs this connect event repeatedly.
    async with websockets.connect(f"ws://127.0.0.1:{port}/ws/events", subprotocols=[token]):
        pass
    # Give uvicorn's access logger a moment to flush the connect lines.
    await asyncio.sleep(0.5)


def test_ws_token_never_appears_in_production_access_log(tmp_path: Path) -> None:
    backend_root = Path(__file__).resolve().parents[1]
    port = _free_port()
    log_path = tmp_path / "server.log"
    venv_python = backend_root / ".venv" / "bin" / "python"
    binary = str(venv_python) if venv_python.exists() else sys.executable
    env = dict(os.environ, PYTHONPATH=str(backend_root / "src"), GG_SERVER_PORT=str(port))

    with open(log_path, "ab") as log_file:
        proc = subprocess.Popen(
            [binary, "-m", "orchestrator.server"],
            cwd=tmp_path,
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    try:
        _wait_health(port)
        token = (tmp_path / ".orchestrator" / "auth_token").read_text().strip()
        assert token, "server did not generate an auth token"

        asyncio.run(_connect_twice(port, token))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)

    output = log_path.read_text(errors="replace")

    # Positive control: prove the connections were actually logged (so this
    # test can't pass vacuously because the server crashed silently or
    # logging was off).
    assert "/ws/events" in output, f"WS connect was never logged at all:\n{output}"

    # The actual assertion: the bearer token itself never appears anywhere in
    # the real production access-log output, at the real log_level="info".
    assert token not in output, f"WS bearer token leaked into the access log:\n{output}"
