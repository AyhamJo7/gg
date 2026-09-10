"""Isolated GG backend for kill/restart tests.

Real uvicorn server + real Orchestrator, deterministic fake providers.
Usage: isolated_server.py <db_path> <port> <workspace>

Only stdlib + the orchestrator package on sys.path (set PYTHONPATH).
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path


class SleepFake:
    """Provider double that parks in a verifiable `sleep` child process.

    The child argv[0] carries the provider token so backend-start reaping
    can positively identify it. First execution sleeps long (the kill
    window); the process is expected to be reaped after a backend restart.
    """

    executable = "true"

    def __init__(self, name: str, sleep_secs: float = 120.0):
        self.name = name
        self.sleep_secs = sleep_secs
        self.calls = 0
        self._cancel_events: dict[str, asyncio.Event] = {}

    def detect(self) -> tuple[bool, str | None]:
        return True, "true"

    async def get_version(self) -> str | None:
        return "sleepfake-1"

    def cancel_event_for(self, run_id: str) -> asyncio.Event:
        return self._cancel_events.setdefault(run_id, asyncio.Event())

    def build_command(self, request):  # type: ignore[no-untyped-def]
        return ["true"]

    async def execute(self, request, on_output):  # type: ignore[no-untyped-def]
        from orchestrator.models import FailureClass, ProviderState
        from orchestrator.providers.base import ExecutionResult

        self.calls += 1
        on_output(f"[{self.name}] parking in sleep")
        proc = await asyncio.create_subprocess_exec(
            "bash", "-c", f"exec -a {self.name}-sleeper sleep {self.sleep_secs}",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        if request.on_spawn:
            request.on_spawn(proc.pid, os.getpgid(proc.pid), time.time())
        try:
            await proc.wait()
        except asyncio.CancelledError:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            raise
        marker = Path(request.workdir) / f"{self.name}.parked"
        marker.write_text("parked\n")
        return ExecutionResult(
            state=ProviderState.COMPLETED,
            failure_class=FailureClass.NONE,
            exit_code=0,
            duration_s=0.01,
            summary=f"fake {self.name} done",
            raw_tail="done",
            assistant_text="done",
        )


def main() -> None:
    db_path = Path(sys.argv[1])
    port = int(sys.argv[2])
    Path(sys.argv[3]).mkdir(parents=True, exist_ok=True)

    import uvicorn

    from orchestrator.api.app import create_app
    from orchestrator.config import Config
    from orchestrator.server import build_orchestrator

    config = Config(
        {
            "scheduler": {"max_parallel_tasks": 3},
            "priority": {
                "planning": ["slow-a"],
                "implementation": ["slow-a", "slow-b"],
                "testing": ["slow-a"],
                "review": ["slow-a"],
                "repair": ["slow-a"],
            },
            "providers": {"slow-a": {"enabled": True}, "slow-b": {"enabled": True}},
            "orchestration": {
                "scheduler_tick_seconds": 0.2,
                "max_phase_attempts": 3,
                "review_required": False,
            },
            "git": {"auto_checkpoint": True},
            "server": {"host": "127.0.0.1", "port": port},
        }
    )
    adapters = {"slow-a": SleepFake("slowa"), "slow-b": SleepFake("slowb")}
    orchestrator = build_orchestrator(db_path, config, adapters)
    app = create_app(db_path, config, orchestrator)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
