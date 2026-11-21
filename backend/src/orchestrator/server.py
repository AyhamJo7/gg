"""Application wiring + server entrypoint."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import uvicorn
from starlette.types import ASGIApp

from .api.app import create_app
from .config import Config
from .db import Database
from .orchestrator import Orchestrator
from .providers import ProviderAdapter, build_real_adapters

logger = logging.getLogger(__name__)

# Open UI websockets otherwise keep uvicorn's graceful shutdown waiting forever,
# so SIGTERM/Ctrl-C never reached orchestrator.shutdown() while a tab was open.
GRACEFUL_SHUTDOWN_SECONDS = 3


def build_uvicorn_config(app: ASGIApp, host: str, port: int, log_level: str = "info") -> uvicorn.Config:
    return uvicorn.Config(
        app, host=host, port=port, log_level=log_level, timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS
    )


def build_orchestrator(
    db_path: Path, config: Config, adapters: dict[str, ProviderAdapter] | None = None
) -> Orchestrator:
    db = Database(db_path)
    # Merge persisted priorities from settings table
    import json

    for row in db.query("SELECT key, value FROM settings WHERE key LIKE 'priority.%'"):
        role = row["key"][len("priority.") :]
        try:
            config.set_priority(role, json.loads(row["value"]))
        except Exception:
            logger.debug("failed to parse persisted priority for %s", role, exc_info=True)
    if adapters is None:
        adapters = build_real_adapters(config)
    return Orchestrator(db, config, adapters)


def main() -> None:
    import os

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    config = Config.load()
    db_path = Path(config.get("database.path", ".orchestrator/orchestrator.db"))
    if "--init-token-only" in sys.argv[1:]:
        # Generate (or reuse) the auth token and exit, without starting the
        # server. Used by `make dev`/`make frontend` to guarantee the token
        # file exists before the frontend needs to read it — see
        # frontend/vite.config.ts and Makefile.
        from .api.auth import load_or_create_token

        load_or_create_token(db_path.parent)
        return
    orchestrator = build_orchestrator(db_path, config)
    app = create_app(db_path, config, orchestrator)
    port = int(os.environ.get("GG_SERVER_PORT") or config.get("server.port", 8787))
    uvicorn.Server(build_uvicorn_config(app, config.get("server.host", "127.0.0.1"), port)).run()


if __name__ == "__main__":
    main()
