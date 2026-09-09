"""Spawn handshake: no provider write before durably persisted identity.

Uses real subprocesses through run_process, not mocks. mark file presence
is the single source of truth for 'the provider executed'.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

import pytest

from orchestrator._spawn_gate import GATE_REFUSED_EXIT
from orchestrator.process import run_process


@pytest.fixture()
def scratch():
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        (base / "logs").mkdir()
        yield base


def _writer_argv(mark: Path) -> list[str]:
    return [sys.executable, "-c", f"open({str(mark)!r}, 'w').write('wrote\\n')"]


@pytest.mark.asyncio()
async def test_normal_path_releases_and_executes(scratch: Path):
    mark = scratch / "mark.txt"
    logs = scratch / "logs"
    result = await run_process(
        _writer_argv(mark),
        cwd=scratch,
        timeout_s=15.0,
        stdout_path=logs / "o.log",
        stderr_path=logs / "e.log",
    )
    assert result.exit_code == 0
    assert mark.read_text() == "wrote\n"


@pytest.mark.asyncio()
async def test_failed_persistence_never_executes(scratch: Path):
    """Backend dies (simulated by a failing on_spawn persist) after spawn:
    the child must never write."""

    def doomed_spawn(pid: int, pgid: int, ts: float) -> None:
        raise RuntimeError("simulated backend death before persist")

    mark = scratch / "mark.txt"
    logs = scratch / "logs"
    result = await run_process(
        _writer_argv(mark),
        cwd=scratch,
        timeout_s=15.0,
        on_spawn=doomed_spawn,
        stdout_path=logs / "o.log",
        stderr_path=logs / "e.log",
    )
    assert not mark.exists()
    assert result.exit_code != 0
    assert result.gate_refused is True


@pytest.mark.asyncio()
async def test_normal_completion_is_not_flagged_gate_refused(scratch: Path):
    mark = scratch / "mark.txt"
    logs = scratch / "logs"
    result = await run_process(
        _writer_argv(mark),
        cwd=scratch,
        timeout_s=15.0,
        stdout_path=logs / "o.log",
        stderr_path=logs / "e.log",
    )
    assert result.gate_refused is False


@pytest.mark.asyncio()
async def test_gate_is_transparent_to_program(scratch: Path):
    """PATH lookup, argv, env, and cwd survive the shim (real-CLI shape)."""
    import os as _os

    logs = scratch / "logs"
    env = dict(_os.environ, SPAWN_GATE_PROBE="probe-value-123")
    result = await run_process(
        ["true"],
        cwd=scratch,
        timeout_s=15.0,
        stdout_path=logs / "o.log",
        stderr_path=logs / "e.log",
        env=env,
    )
    assert result.exit_code == 0
    result = await run_process(
        ["python3", "-c", "import os,sys;print(os.environ['SPAWN_GATE_PROBE']);print(sys.argv[1:]);print(os.getcwd())"],
        cwd=scratch,
        timeout_s=15.0,
        stdout_path=logs / "o2.log",
        stderr_path=logs / "e2.log",
        env=env,
    )
    assert result.exit_code == 0
    out = (logs / "o2.log").read_text()
    assert "probe-value-123" in out
    assert str(scratch) in out


@pytest.mark.asyncio()
async def test_gate_eof_child_exits_refused(scratch: Path):
    """Parent gone before release: EOF on the gate, exit 42, no execution."""
    gate_r, gate_w = os.pipe()
    shim = str(Path(__file__).resolve().parents[1] / "src" / "orchestrator" / "_spawn_gate.py")
    mark = scratch / "mark.txt"
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        shim,
        str(gate_r),
        *_writer_argv(mark),
        cwd=str(scratch),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        stdin=asyncio.subprocess.DEVNULL,
        start_new_session=True,
        pass_fds=(gate_r,),
    )
    os.close(gate_r)
    os.close(gate_w)  # simulate backend death: EOF, never released
    rc = await asyncio.wait_for(proc.wait(), timeout=15)
    assert rc == GATE_REFUSED_EXIT
    assert not mark.exists()


@pytest.mark.asyncio()
async def test_gate_release_single_byte_executes(scratch: Path):
    gate_r, gate_w = os.pipe()
    shim = str(Path(__file__).resolve().parents[1] / "src" / "orchestrator" / "_spawn_gate.py")
    mark = scratch / "mark.txt"
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        shim,
        str(gate_r),
        *_writer_argv(mark),
        cwd=str(scratch),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        stdin=asyncio.subprocess.DEVNULL,
        start_new_session=True,
        pass_fds=(gate_r,),
    )
    os.close(gate_r)
    os.write(gate_w, b"G")
    os.close(gate_w)
    rc = await asyncio.wait_for(proc.wait(), timeout=15)
    assert rc == 0
    assert mark.read_text() == "wrote\n"
