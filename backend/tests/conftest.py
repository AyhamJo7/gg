"""Shared fixtures: temp workspace, test config, fake-provider orchestrator."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestrator.config import Config
from orchestrator.db import Database
from orchestrator.orchestrator import Orchestrator
from orchestrator.providers.fake import FakeAdapter


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    """A real on-disk node-ish project whose toolchain always passes."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "package.json").write_text(
        json.dumps(
            {
                "name": "fixture-project",
                "scripts": {
                    "test": "node -e \"process.exit(0)\"",
                    "build": "node -e \"process.exit(0)\"",
                },
            }
        )
    )
    (ws / "README.md").write_text("# fixture\n")
    return ws


def make_config(priority: dict[str, list[str]] | None = None, providers: list[str] | None = None) -> Config:
    names = providers or ["fake-a", "fake-b", "fake-c"]
    data: dict = {
        "providers": {name: {"enabled": True, "timeout_minutes": 1} for name in names},
        "orchestration": {
            "checkpoint_before_provider_switch": True,
            "review_required": True,
            "max_repair_cycles": 2,
            "max_phase_attempts": 4,
            "cooldown_base_seconds": 0.05,
            "cooldown_multiplier": 1.0,
            "cooldown_max_seconds": 0.2,
            "scheduler_tick_seconds": 0.05,
        },
        "priority": priority
        or {
            "planning": names,
            "implementation": names,
            "testing": names,
            "review": names,
            "repair": names,
        },
        "git": {"auto_checkpoint": True},
    }
    return Config(data)


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    return Database(tmp_path / "test.db")


def make_orchestrator(
    tmp_path: Path,
    adapters: dict[str, FakeAdapter],
    config: Config | None = None,
) -> Orchestrator:
    db = Database(tmp_path / "orch.db")
    cfg = config or make_config(providers=list(adapters.keys()))
    return Orchestrator(db, cfg, adapters)
