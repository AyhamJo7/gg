"""Simulation providers for deterministic testing without burning AI quotas.

Each fake performs *real* filesystem actions (when asked to) so the git
ledger, checkpoints and verification engine exercise genuine behavior.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from ..models import FailureClass, ProviderState
from .base import ExecutionRequest, ExecutionResult, OutputHandler, ProviderAdapter


class FakeAdapter(ProviderAdapter):
    """Scripted adapter. Behavior is configured per instance."""

    executable = "true"  # always "installed"

    def __init__(self, name: str, script: list[str] | None = None):
        super().__init__(executable="true")
        self.name = name
        # script behaviors: ok, ratelimit, crash, slow, auth, work
        self.script = script or ["ok"]
        self.calls = 0
        self.flood_lines = 0  # when >0, execute() emits this many output lines
        self._cancelled = asyncio.Event()

    async def interrupt(self, run_id: str) -> bool:
        self._cancelled.set()
        return True

    def build_command(self, request: ExecutionRequest) -> list[str]:
        return ["true"]  # never actually spawned — execute() is overridden

    async def execute(self, request: ExecutionRequest, on_output: OutputHandler) -> ExecutionResult:
        self._cancelled.clear()
        idx = min(self.calls, len(self.script) - 1)
        behavior = self.script[idx]
        self.calls += 1
        on_output(f"[{self.name}] starting role={request.role} behavior={behavior}")
        await asyncio.sleep(0.01)

        if self.flood_lines:
            for i in range(self.flood_lines):
                on_output(f"[{self.name}] flood line {i}")

        if behavior == "slow":
            for _ in range(36_000):  # ~1h, interruptible
                if self._cancelled.is_set():
                    on_output(f"[{self.name}] interrupted")
                    return ExecutionResult(
                        state=ProviderState.AVAILABLE,
                        failure_class=FailureClass.CANCELLED,
                        exit_code=None,
                        duration_s=0.1,
                        summary="",
                        raw_tail="interrupted",
                    )
                await asyncio.sleep(0.1)
        if behavior == "work" or (behavior == "ok" and request.role in ("implementation", "repair", "testing")):
            # Perform real, idempotent work so git checkpoints capture changes.
            marker = request.workdir / "agent_work.txt"
            marker.write_text(f"{self.name} {request.role} call={self.calls}\n")
            on_output(f"[{self.name}] wrote {marker.name}")
        if request.role == "review" and behavior == "ok":
            on_output("REVIEW_FINDINGS_JSON: []")

        result_map = {
            "ok": (ProviderState.COMPLETED, FailureClass.NONE, 0),
            "ratelimit": (ProviderState.RATE_LIMITED, FailureClass.RATE_LIMIT, 1),
            "crash": (ProviderState.CRASHED, FailureClass.CRASH, 2),
            "auth": (ProviderState.AUTH_REQUIRED, FailureClass.AUTH, 1),
        }
        state, failure, code = result_map.get(behavior, (ProviderState.COMPLETED, FailureClass.NONE, 0))
        if behavior == "ratelimit":
            on_output("Error: rate limit exceeded — try again later")
        return ExecutionResult(
            state=state,
            failure_class=failure,
            exit_code=code,
            duration_s=0.01,
            summary=f"fake {self.name} {request.role} ({behavior})",
            stdout_path=Path(request.log_dir / f"{request.run_id}.stdout.log"),
            stderr_path=Path(request.log_dir / f"{request.run_id}.stderr.log"),
            raw_tail=f"fake output {behavior}",
        )


def success_provider(name: str = "fake-success") -> FakeAdapter:
    return FakeAdapter(name, ["ok"])


def rate_limit_provider(name: str = "fake-ratelimit") -> FakeAdapter:
    return FakeAdapter(name, ["ratelimit"])


def crash_provider(name: str = "fake-crash") -> FakeAdapter:
    return FakeAdapter(name, ["crash"])


def working_provider(name: str = "fake-worker") -> FakeAdapter:
    return FakeAdapter(name, ["work"])


def flaky_provider(name: str = "fake-flaky", failures: int = 1) -> FakeAdapter:
    return FakeAdapter(name, ["ratelimit"] * failures + ["work"])
