"""Provider adapter interface.

Adapters know HOW to execute a CLI. They never decide orchestration policy
(priority, failover, phases) — that belongs to the scheduler/state machine.
"""

from __future__ import annotations

import abc
import asyncio
import json
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..models import FailureClass, ProviderState, utcnow
from ..process import run_process
from .classify import FAILURE_TO_STATE, RAW_TAIL_CHARS, classify_output

OutputHandler = Callable[[str], None]


@dataclass
class ProviderCapabilities:
    streaming_json: bool = False
    supports_cwd_flag: bool = False
    non_interactive: bool = True


@dataclass
class ExecutionRequest:
    prompt: str
    workdir: Path
    role: str
    timeout_s: float
    run_id: str
    log_dir: Path
    on_spawn: Callable[[int, int, float], None] | None = None


@dataclass
class ExecutionResult:
    state: ProviderState
    failure_class: FailureClass
    exit_code: int | None
    duration_s: float
    summary: str
    argv: list[str] = field(default_factory=list)
    stdout_path: Path | None = None
    stderr_path: Path | None = None
    raw_tail: str = ""
    assistant_text: str = ""
    started_at: str = field(default_factory=lambda: utcnow().isoformat())
    finished_at: str = field(default_factory=lambda: utcnow().isoformat())
    pid: int | None = None
    pgid: int | None = None
    gate_refused: bool = False

    @property
    def ok(self) -> bool:
        return self.state == ProviderState.COMPLETED


class ProviderAdapter(abc.ABC):
    """Base class for CLI provider adapters."""

    name: str = "base"
    executable: str = ""

    def __init__(self, executable: str | None = None):
        if executable:
            self.executable = executable
        self._cancel_events: dict[str, asyncio.Event] = {}

    def cancel_event_for(self, run_id: str) -> asyncio.Event:
        """Return (creating if needed) the cancellation event for a run.

        The orchestrator registers the event BEFORE starting execution, so an
        interrupt arriving in the gap between run registration and subprocess
        start is never lost.
        """
        return self._cancel_events.setdefault(run_id, asyncio.Event())

    # -- detection ---------------------------------------------------------
    def detect(self) -> tuple[bool, str | None]:
        """Return (installed, executable_path)."""
        path = shutil.which(self.executable)
        return (path is not None, path)

    async def get_version(self) -> str | None:
        path = shutil.which(self.executable)
        if not path:
            return None
        try:
            proc = await asyncio.create_subprocess_exec(
                path,
                "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
            return out.decode(errors="replace").strip().splitlines()[0][:120]
        except Exception:
            return None

    def get_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities()

    # -- execution ---------------------------------------------------------
    @abc.abstractmethod
    def build_command(self, request: ExecutionRequest) -> list[str]:
        """Return the exact argv array. Never a shell string."""

    def normalize_output_line(self, line: str) -> str | None:
        """Translate a raw stdout line into display text.

        JSON-streaming providers override this to extract readable text.
        Return None to suppress a line from the live terminal.
        """
        return line

    def extract_summary(self, stdout_tail: list[str]) -> str:
        """Best-effort final-message extraction for handoffs."""
        for line in reversed(stdout_tail):
            text = line.strip()
            if text:
                return text[:2000]
        return ""

    def extract_assistant_text(self, stdout_tail: list[str]) -> str:
        """Reconstruct the canonical assistant response from raw stdout lines.

        JSON-streaming providers override this to translate NDJSON events into
        plain assistant text. Default implementation simply joins stdout lines.
        """
        return "\n".join(stdout_tail)

    async def execute(self, request: ExecutionRequest, on_output: OutputHandler) -> ExecutionResult:
        argv = self.build_command(request)
        started = utcnow()
        request.log_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = request.log_dir / f"{request.run_id}.stdout.log"
        stderr_path = request.log_dir / f"{request.run_id}.stderr.log"
        cancel_event = self.cancel_event_for(request.run_id)

        def handle_line(stream: str, line: str) -> None:
            display = self.normalize_output_line(line) if stream == "stdout" else line
            if display:
                on_output(display)

        result = await run_process(
            argv,
            cwd=request.workdir,
            timeout_s=request.timeout_s,
            on_output=handle_line,
            on_spawn=request.on_spawn,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            cancel_event=cancel_event,
        )
        self._cancel_events.pop(request.run_id, None)
        finished = utcnow()
        failure = self.classify_failure(
            result.exit_code, result.combined_tail, result.timed_out, result.cancelled, result.gate_refused
        )
        state = ProviderState.COMPLETED if failure == FailureClass.NONE else FAILURE_TO_STATE[failure]
        return ExecutionResult(
            state=state,
            failure_class=failure,
            exit_code=result.exit_code,
            duration_s=result.duration_s,
            summary=self.extract_summary(result.stdout_tail),
            argv=argv,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            raw_tail=result.combined_tail[-RAW_TAIL_CHARS:],
            assistant_text=self.extract_assistant_text(result.stdout_tail),
            started_at=started.isoformat(),
            finished_at=finished.isoformat(),
            pid=result.pid,
            pgid=result.pgid,
            gate_refused=result.gate_refused,
        )

    async def interrupt(self, run_id: str) -> bool:
        event = self._cancel_events.get(run_id)
        if event:
            event.set()
            return True
        return False

    def is_success_marker(self, text: str) -> bool:
        """Hook for provider-specific success events in output."""
        return False

    # -- failure translation -------------------------------------------------
    def classify_failure(
        self, exit_code: int | None, combined: str, timed_out: bool, cancelled: bool, gate_refused: bool = False
    ) -> FailureClass:
        return classify_output(
            exit_code, combined, timed_out=timed_out, cancelled=cancelled, adapter=self, gate_refused=gate_refused
        )

    async def health_check(self) -> ProviderState:
        installed, _ = self.detect()
        return ProviderState.AVAILABLE if installed else ProviderState.UNAVAILABLE


def extract_json_line(line: str) -> dict[str, Any] | None:
    line = line.strip()
    if not line.startswith("{"):
        return None
    try:
        value = json.loads(line)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None
