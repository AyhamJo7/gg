#!/usr/bin/env python3
"""Test backend with deterministic fake providers for lifecycle browser E2E.

Usage: lifecycle-fake-backend.py PORT DB_PATH PRODUCTS_ROOT [gated]
Serves the full GG API with FakeAdapters + PlanProvider (no quota).
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend" / "src"))

import uvicorn

from orchestrator.api.app import create_app
from orchestrator.config import Config
from orchestrator.db import Database
from orchestrator.orchestrator import Orchestrator
from orchestrator.providers.fake import FakeAdapter, PlanProvider, default_test_plan, gated_test_plan


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8789
    db_path = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(tempfile.mkdtemp()) / "e2e.db"
    products_root = sys.argv[3] if len(sys.argv) > 3 else tempfile.mkdtemp(prefix="gg-e2e-products-")
    gated = len(sys.argv) > 4 and sys.argv[4] == "gated"
    plan = gated_test_plan() if gated else default_test_plan()
    script = ["malformed", "ok"] if os.environ.get("GG_E2E_MALFORMED_FIRST") else ["ok"]
    adapters = {
        "fake-planner": PlanProvider("fake-planner", script, plan),
        "fake-a": FakeAdapter("fake-a", ["work"]),
        "fake-b": FakeAdapter("fake-b", ["ok"]),
    }
    names = list(adapters.keys())
    data = {
        "providers": {n: {"enabled": True, "timeout_minutes": 1} for n in names},
        "orchestration": {
            "review_required": True,
            "max_repair_cycles": 1,
            "max_phase_attempts": 2,
            "cooldown_base_seconds": 0.05,
            "cooldown_multiplier": 1.0,
            "cooldown_max_seconds": 0.2,
            "scheduler_tick_seconds": 0.5,
            "max_provider_wait_seconds": 30,
        },
        "priority": {
            "planning": ["fake-planner"],
            "implementation": ["fake-a", "fake-b"],
            "testing": ["fake-a", "fake-b"],
            "review": ["fake-a", "fake-b"],
            "repair": ["fake-a", "fake-b"],
        },
        "lifecycle": {"workspace_root": products_root},
        "git": {"auto_checkpoint": True},
    }
    config = Config(data)
    db = Database(db_path)
    orch = Orchestrator(db, config, adapters)
    # Test scaffolding only: fake implementers write agent_work.txt but cannot
    # author a package manifest, so seed a minimal passing toolchain into each
    # provisioned target repo (what a real foundation phase would create).
    import json as _json

    _real_ensure = orch.coordinator.ensure_target_repo

    def _ensure_and_seed(project_id: str) -> dict:
        target = _real_ensure(project_id)
        pkg = Path(target["path"]) / "package.json"
        if not pkg.exists():
            pkg.write_text(
                _json.dumps(
                    {
                        "name": "e2e-target",
                        "scripts": {
                            "test": "node -e \"process.exit(0)\"",
                            "build": "node -e \"process.exit(0)\"",
                        },
                    }
                )
            )
        return target

    orch.coordinator.ensure_target_repo = _ensure_and_seed  # type: ignore[method-assign]
    app = create_app(db_path, config, orch)
    print(f"fake lifecycle backend on {port} db={db_path} products={products_root}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
