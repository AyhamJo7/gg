"""Real provider smoke test: `make smoke` / `uv run python -m orchestrator.smoke`.

Sends one tiny read-only prompt to each detected provider and reports the
classified outcome. Consumes a small amount of subscription quota by design.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

from .config import Config
from .providers import build_real_adapters
from .providers.base import ExecutionRequest

SMOKE_PROMPT = "Reply with exactly the token ORCHESTRATOR_OK and nothing else. Do not use any tools."


async def smoke_one(name: str, adapter: object, workdir: Path) -> dict[str, object]:
    from .providers.base import ProviderAdapter

    assert isinstance(adapter, ProviderAdapter)
    installed, path = adapter.detect()
    if not installed:
        return {"provider": name, "installed": False}
    lines: list[str] = []
    request = ExecutionRequest(
        prompt=SMOKE_PROMPT,
        workdir=workdir,
        role="smoke",
        timeout_s=150,
        run_id=f"smoke-{name}",
        log_dir=workdir / ".orchestrator" / "logs",
    )
    result = await adapter.execute(request, lines.append)
    return {
        "provider": name,
        "installed": True,
        "path": path,
        "state": result.state.value,
        "failure_class": result.failure_class.value,
        "exit_code": result.exit_code,
        "duration_s": round(result.duration_s, 1),
        "responded_ok": "ORCHESTRATOR_OK" in (result.summary + "\n".join(lines) + result.raw_tail),
        "summary": result.summary[:200],
    }


async def main() -> int:
    only = sys.argv[1:] or None
    config = Config.load()
    adapters = build_real_adapters(config)
    results = []
    with tempfile.TemporaryDirectory(prefix="gg-smoke-") as tmp:
        workdir = Path(tmp)
        for name, adapter in adapters.items():
            if only and name not in only:
                continue
            print(f"--- smoke: {name} ---", flush=True)
            try:
                outcome = await smoke_one(name, adapter, workdir)
            except Exception as exc:
                outcome = {"provider": name, "error": str(exc)}
            print(json.dumps(outcome, indent=2), flush=True)
            results.append(outcome)
    failures = [r for r in results if r.get("state") not in ("COMPLETED",) and r.get("installed", False)]
    return 1 if failures and not only else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
