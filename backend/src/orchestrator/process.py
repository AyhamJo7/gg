"""Robust subprocess management.

- argv arrays only (never shell=True)
- merged line-streamed stdout/stderr with timestamps
- timeout with SIGTERM → SIGKILL escalation on the whole process group
- cancellation support
- full raw output captured to files, redacted tail kept in memory
"""

from __future__ import annotations

import asyncio
import os
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .security import redact

GRACEFUL_TERMINATE_SECONDS = 5.0
MAX_TAIL_LINES = 4000

OutputCallback = Callable[[str, str], None]  # (stream, line)


@dataclass
class ProcessResult:
    exit_code: int | None
    timed_out: bool
    cancelled: bool
    duration_s: float
    stdout_tail: list[str] = field(default_factory=list)
    stderr_tail: list[str] = field(default_factory=list)

    @property
    def combined_tail(self) -> str:
        return "\n".join((self.stdout_tail + self.stderr_tail)[-MAX_TAIL_LINES:])


async def _kill_process_tree(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, PermissionError):
        pgid = None
    for sig, wait in ((signal.SIGTERM, GRACEFUL_TERMINATE_SECONDS), (signal.SIGKILL, 0)):
        try:
            if pgid is not None:
                os.killpg(pgid, sig)
            else:
                proc.send_signal(sig)
        except (ProcessLookupError, PermissionError):
            return
        try:
            await asyncio.wait_for(asyncio.shield(proc.wait()), timeout=wait)
            return
        except TimeoutError:
            continue


async def run_process(
    argv: list[str],
    cwd: Path,
    timeout_s: float,
    on_output: OutputCallback | None = None,
    stdout_path: Path | None = None,
    stderr_path: Path | None = None,
    env: dict[str, str] | None = None,
    cancel_event: asyncio.Event | None = None,
) -> ProcessResult:
    """Run a subprocess, streaming redacted lines to on_output.

    Never raises for process failure; outcome is encoded in ProcessResult.
    """
    start = time.monotonic()
    stdout_tail: list[str] = []
    stderr_tail: list[str] = []
    stdout_fh = stdout_path.open("w", encoding="utf-8", errors="replace") if stdout_path else None
    stderr_fh = stderr_path.open("w", encoding="utf-8", errors="replace") if stderr_path else None

    async def _pump(stream: asyncio.StreamReader, name: str, tail: list[str], fh: object) -> None:
        while True:
            raw = await stream.readline()
            if not raw:
                return
            line = raw.decode("utf-8", errors="replace").rstrip("\n")
            if fh is not None:
                fh.write(line + "\n")  # type: ignore[attr-defined]
                fh.flush()  # type: ignore[attr-defined]
            safe = redact(line)
            tail.append(safe)
            if len(tail) > MAX_TAIL_LINES:
                del tail[: len(tail) - MAX_TAIL_LINES]
            if on_output:
                on_output(name, safe)

    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        start_new_session=True,  # own process group → reliable tree kill
    )
    if proc.stdout is None or proc.stderr is None:
        raise RuntimeError("subprocess pipes not captured")
    pumps = [
        asyncio.create_task(_pump(proc.stdout, "stdout", stdout_tail, stdout_fh)),
        asyncio.create_task(_pump(proc.stderr, "stderr", stderr_tail, stderr_fh)),
    ]

    timed_out = False
    cancelled = False
    try:
        deadline = start + timeout_s
        while proc.returncode is None:
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            try:
                await asyncio.wait_for(asyncio.shield(proc.wait()), timeout=min(1.0, remaining))
            except TimeoutError:
                continue
        if timed_out or cancelled:
            await _kill_process_tree(proc)
        # Bounded final reap: after kill escalation the process is expected to
        # be dead; never wait forever (zombie/pipe edge cases must not deadlock).
        try:
            await asyncio.wait_for(asyncio.shield(proc.wait()), timeout=GRACEFUL_TERMINATE_SECONDS)
        except TimeoutError:  # pragma: no cover - defensive
            pass
    finally:
        await asyncio.gather(*pumps, return_exceptions=True)
        for fh in (stdout_fh, stderr_fh):
            if fh is not None:
                fh.close()

    return ProcessResult(
        exit_code=proc.returncode,
        timed_out=timed_out,
        cancelled=cancelled,
        duration_s=time.monotonic() - start,
        stdout_tail=stdout_tail,
        stderr_tail=stderr_tail,
    )
