"""Application wiring + server entrypoint."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import uvicorn

from .api.app import create_app
from .config import Config
from .db import Database
from .orchestrator import Orchestrator
from .providers import ProviderAdapter, build_real_adapters

logger = logging.getLogger(__name__)


def build_orchestrator(
    db_path: Path, config: Config, adapters: dict[str, ProviderAdapter] | None = None
) -> Orchestrator:
    db = Database(db_path)
    # Merge persisted priorities from settings table
    import json
    for row in db.query("SELECT key, value FROM settings WHERE key LIKE 'priority.%'"):
        role = row["key"][len("priority."):]
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
    orchestrator = build_orchestrator(db_path, config)
    app = create_app(db_path, config, orchestrator)
    port = int(os.environ.get("GG_SERVER_PORT") or config.get("server.port", 8787))
    uvicorn.run(
        app,
        host=config.get("server.host", "127.0.0.1"),
        port=port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
