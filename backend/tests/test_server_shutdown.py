"""A connected UI websocket must not hold the backend's graceful shutdown open."""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path

import uvicorn
from websockets.asyncio.client import connect
from websockets.typing import Subprotocol

from conftest import make_config
from orchestrator.api.app import create_app
from orchestrator.api.auth import load_or_create_token
from orchestrator.db import Database
from orchestrator.orchestrator import Orchestrator
from orchestrator.providers.fake import FakeAdapter
from orchestrator.server import GRACEFUL_SHUTDOWN_SECONDS, build_uvicorn_config

STARTUP_POLL_SECONDS = 0.05
SHUTDOWN_MARGIN_SECONDS = 10


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def test_open_websocket_does_not_block_shutdown(tmp_path: Path) -> None:
    db_path = tmp_path / "api.db"
    orch = Orchestrator(Database(db_path), make_config(), {"fake-a": FakeAdapter("fake-a", ["ok"])})
    app = create_app(db_path, make_config(), orch)
    port = _free_port()

    server = uvicorn.Server(build_uvicorn_config(app, "127.0.0.1", port, log_level="warning"))
    serving = asyncio.create_task(server.serve())
    while not server.started:  # noqa: ASYNC110 - uvicorn exposes startup only as a bool
        await asyncio.sleep(STARTUP_POLL_SECONDS)

    token = load_or_create_token(tmp_path)
    async with connect(f"ws://127.0.0.1:{port}/ws/events", subprotocols=[Subprotocol(token)]):
        server.should_exit = True
        await asyncio.wait_for(serving, GRACEFUL_SHUTDOWN_SECONDS + SHUTDOWN_MARGIN_SECONDS)

    assert orch._shutdown.is_set(), "lifespan shutdown must still run orchestrator.shutdown()"
