"""Orphaned provider-process supervision shared by backend startup and engines.

A recorded PID is only ever signaled when it can be positively identified as
our provider process (PGID match, start-time match, provider token in
cmdline). On any doubt the process is left alone and the caller must handle
the unverifiable survivor without killing or requeueing blindly.
"""

from __future__ import annotations

import logging
import os
import signal
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def verify_process_ownership(
    pid: int,
    pgid: int,
    started_at_ts: float | None,
    provider: str,
) -> bool:
    """Return True only when PID is positively identified as our provider process."""
    proc_path = Path(f"/proc/{pid}")
    if not proc_path.exists():
        return False

    # 1. Verify PGID matches
    try:
        actual_pgid = os.getpgid(pid)
        if actual_pgid != pgid:
            logger.warning(
                "PID %d PGID mismatch: recorded=%d actual=%d — not killing",
                pid, pgid, actual_pgid,
            )
            return False
    except (ProcessLookupError, PermissionError):
        return False

    # 2. Verify start time if we have a recorded timestamp
    if started_at_ts is not None:
        try:
            stat_data = (proc_path / "stat").read_text()
            # /proc/[pid]/stat format: pid (comm) state ... field22=starttime
            # comm can contain spaces/parens, so find the closing ')' first
            close_paren = stat_data.rfind(")")
            if close_paren == -1:
                return False
            fields = stat_data[close_paren + 2 :].split()
            # starttime is field 22 (1-indexed), but after stripping pid+comm+state,
            # it's at index 19 in the remaining fields (state=0, ppid=1, ...)
            proc_starttime = int(fields[19])  # starttime in clock ticks

            # Convert our recorded time.time() to clock ticks for comparison
            # Read system boot time from /proc/stat
            boot_time = None
            with open("/proc/stat") as f:
                for line in f:
                    if line.startswith("btime "):
                        boot_time = int(line.split()[1])
                        break
                if boot_time is not None:
                    clk_tck = os.sysconf("SC_CLK_TCK")
                    expected_starttime_ticks = int((started_at_ts - boot_time) * clk_tck)
                    # 10s tolerance: /proc btime has 1s resolution and wall-vs-boot
                    # clocks visibly drift under load (3.4s observed live); PGID +
                    # cmdline conjunction still makes misidentification negligible.
                    if abs(proc_starttime - expected_starttime_ticks) > 10 * clk_tck:
                        logger.warning(
                            "PID %d start time mismatch: recorded=%.1f proc_start=%d expected_ticks=%d — not killing",
                            pid, started_at_ts, proc_starttime, expected_starttime_ticks,
                        )
                        return False
        except (OSError, ValueError, IndexError):
            # Cannot verify start time — err on the side of NOT killing
            logger.warning("PID %d: could not verify start time — not killing", pid)
            return False

    # 3. Verify command line contains provider-related token
    try:
        cmdline_bytes = (proc_path / "cmdline").read_bytes()
        if not cmdline_bytes:
            logger.warning("PID %d: empty cmdline — not killing", pid)
            return False
        cmdline = cmdline_bytes.replace(b"\x00", b" ").decode(errors="replace").lower()
        provider_token = provider.replace("-", "").replace("_", "").lower()
        if provider_token not in cmdline:
            logger.warning(
                "PID %d cmdline does not contain provider token '%s': %s — not killing",
                pid,
                provider_token,
                cmdline[:200],
            )
            return False
    except OSError as exc:
        logger.warning("PID %d: cannot read cmdline (%s) — not killing", pid, exc)
        return False

    return True


def kill_process_tree(pgid: int) -> None:
    """SIGTERM a process group, escalating to SIGKILL. Best effort."""
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    time.sleep(0.05)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def process_alive(pid: Any) -> bool:
    """True when /proc/<pid> exists and is not a zombie (no signal sent).

    Zombies occupy a /proc entry but can do no work; treating them as alive
    races backend-start reaping (which just SIGKILLed them) against engine
    reconcile and would fail tasks that should simply resume.
    """
    try:
        stat = Path(f"/proc/{int(pid)}/stat").read_text()
    except (ValueError, TypeError, OSError):
        return False
    close_paren = stat.rfind(")")
    if close_paren == -1:
        return True  # unparseable: assume alive (conservative)
    fields = stat[close_paren + 2 :].split()
    if not fields:
        return True
    return fields[0] != "Z"
