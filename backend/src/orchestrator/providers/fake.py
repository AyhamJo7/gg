"""Simulation providers for deterministic testing without burning AI quotas.

Each fake performs *real* filesystem actions (when asked to) so the git
ledger, checkpoints and verification engine exercise genuine behavior.
"""

from __future__ import annotations

import asyncio
import time
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

    def build_command(self, request: ExecutionRequest) -> list[str]:
        return ["true"]  # never actually spawned — execute() is overridden

    async def execute(self, request: ExecutionRequest, on_output: OutputHandler) -> ExecutionResult:
        cancel_event = self.cancel_event_for(request.run_id)
        idx = min(self.calls, len(self.script) - 1)
        behavior = self.script[idx]
        self.calls += 1
        on_output(f"[{self.name}] starting role={request.role} behavior={behavior}")
        await asyncio.sleep(0.01)
        if request.on_spawn:
            import os
            try:
                pgid = os.getpgrp() if hasattr(os, "getpgrp") else os.getpid()
            except Exception:
                pgid = os.getpid()
            request.on_spawn(os.getpid(), pgid, time.time())

        if self.flood_lines:
            for i in range(self.flood_lines):
                on_output(f"[{self.name}] flood line {i}")

        if behavior == "slow":
            for _ in range(36_000):  # ~1h, interruptible
                if cancel_event.is_set():
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
        if request.role == "review" and behavior in ("ok", "work"):
            on_output("REVIEW_FINDINGS_JSON: []")

        result_map = {
            "ok": (ProviderState.COMPLETED, FailureClass.NONE, 0),
            "work": (ProviderState.COMPLETED, FailureClass.NONE, 0),
            "ratelimit": (ProviderState.RATE_LIMITED, FailureClass.RATE_LIMIT, 1),
            "crash": (ProviderState.CRASHED, FailureClass.CRASH, 2),
            "auth": (ProviderState.AUTH_REQUIRED, FailureClass.AUTH, 1),
        }
        state, failure, code = result_map.get(behavior, (ProviderState.COMPLETED, FailureClass.NONE, 0))
        if behavior == "ratelimit":
            on_output("Error: rate limit exceeded — try again later")
        raw_tail = f"fake output {behavior}"
        assistant_text = raw_tail
        if request.role == "review" and behavior in ("ok", "work"):
            raw_tail += "\nREVIEW_FINDINGS_JSON: []"
            assistant_text = raw_tail
        return ExecutionResult(
            state=state,
            failure_class=failure,
            exit_code=code,
            duration_s=0.01,
            summary=f"fake {self.name} {request.role} ({behavior})",
            stdout_path=Path(request.log_dir / f"{request.run_id}.stdout.log"),
            stderr_path=Path(request.log_dir / f"{request.run_id}.stderr.log"),
            raw_tail=raw_tail,
            assistant_text=assistant_text,
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


class FastSuccessProvider(FakeAdapter):
    """Completes immediately with success."""

    def __init__(self, name: str = "fake-fast"):
        super().__init__(name, ["ok"])


class SlowSuccessProvider(FakeAdapter):
    """Sleeps a configurable duration then succeeds."""

    def __init__(self, name: str = "fake-slow", delay_s: float = 0.5):
        super().__init__(name, ["ok"])
        self.delay_s = delay_s

    async def execute(self, request: ExecutionRequest, on_output: OutputHandler) -> ExecutionResult:
        await asyncio.sleep(self.delay_s)
        return await super().execute(request, on_output)


class WorkspaceWriterProvider(FakeAdapter):
    """Writes to a specific file in the workspace then succeeds."""

    def __init__(self, name: str = "fake-writer", filename: str = "output.txt", content: str = "test"):
        super().__init__(name, ["ok"])
        self.filename = filename
        self.content = content

    async def execute(self, request: ExecutionRequest, on_output: OutputHandler) -> ExecutionResult:
        target = request.workdir / self.filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.content)
        on_output(f"[{self.name}] wrote {target}")
        return ExecutionResult(
            state=ProviderState.COMPLETED,
            failure_class=FailureClass.NONE,
            exit_code=0,
            duration_s=0.01,
            summary=f"wrote {self.filename}",
            stdout_path=Path(request.log_dir / f"{request.run_id}.stdout.log"),
            stderr_path=Path(request.log_dir / f"{request.run_id}.stderr.log"),
            raw_tail=f"wrote {self.filename}",
            assistant_text=f"wrote {self.filename}",
        )


class ConflictProvider(FakeAdapter):
    """Writes to the same file as another task to simulate merge conflicts."""

    def __init__(self, name: str = "fake-conflict", filename: str = "shared.txt", content: str = "conflict-A"):
        super().__init__(name, ["ok"])
        self.filename = filename
        self.content = content

    async def execute(self, request: ExecutionRequest, on_output: OutputHandler) -> ExecutionResult:
        target = request.workdir / self.filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self.content)
        return ExecutionResult(
            state=ProviderState.COMPLETED,
            failure_class=FailureClass.NONE,
            exit_code=0,
            duration_s=0.01,
            summary=f"wrote conflict {self.filename}",
            stdout_path=Path(request.log_dir / f"{request.run_id}.stdout.log"),
            stderr_path=Path(request.log_dir / f"{request.run_id}.stderr.log"),
            raw_tail=f"wrote conflict {self.filename}",
            assistant_text=f"wrote conflict {self.filename}",
        )


class RateLimitAfterDelayProvider(FakeAdapter):
    """Sleeps then rate-limits."""

    def __init__(self, name: str = "fake-ratelimit-delay", delay_s: float = 0.2):
        super().__init__(name, ["ratelimit"])
        self.delay_s = delay_s

    async def execute(self, request: ExecutionRequest, on_output: OutputHandler) -> ExecutionResult:
        await asyncio.sleep(self.delay_s)
        return await super().execute(request, on_output)


class CrashAfterWriteProvider(FakeAdapter):
    """Writes a file then crashes."""

    def __init__(self, name: str = "fake-crash-write", filename: str = "partial.txt"):
        super().__init__(name, ["crash"])
        self.filename = filename

    async def execute(self, request: ExecutionRequest, on_output: OutputHandler) -> ExecutionResult:
        target = request.workdir / self.filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("partial data")
        on_output(f"[{self.name}] wrote {target} then crashing")
        return ExecutionResult(
            state=ProviderState.CRASHED,
            failure_class=FailureClass.CRASH,
            exit_code=2,
            duration_s=0.01,
            summary=f"crashed after writing {self.filename}",
            stdout_path=Path(request.log_dir / f"{request.run_id}.stdout.log"),
            stderr_path=Path(request.log_dir / f"{request.run_id}.stderr.log"),
            raw_tail="crash after write",
            assistant_text="crash after write",
        )
