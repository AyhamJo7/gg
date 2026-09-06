import os
import subprocess
import time
from unittest.mock import MagicMock, patch

import pytest

from orchestrator.models import FailureClass, ProviderState
from orchestrator.orchestrator import Orchestrator


@pytest.fixture
def orchestrator():
    db = MagicMock()
    config = MagicMock()
    adapters = {}
    return Orchestrator(db=db, config=config, adapters=adapters)

def test_verify_own_process(orchestrator):
    proc = subprocess.Popen(["sleep", "10"])
    pid = proc.pid
    pgid = os.getpgid(pid)
    started_at_ts = time.time()
    
    try:
        time.sleep(0.1)
        is_ours = orchestrator._verify_process_ownership(pid, pgid, started_at_ts, "sleep")
        assert is_ours is True
    finally:
        proc.kill()
        proc.wait()

def test_verify_missing_process(orchestrator):
    is_ours = orchestrator._verify_process_ownership(999999, 999999, time.time(), "sleep")
    assert is_ours is False

@patch("os.getpgid")
def test_verify_pgid_mismatch(mock_getpgid, orchestrator):
    proc = subprocess.Popen(["sleep", "10"])
    pid = proc.pid
    pgid = os.getpgid(pid)
    started_at_ts = time.time()

    mock_getpgid.return_value = pgid + 1

    try:
        is_ours = orchestrator._verify_process_ownership(pid, pgid, started_at_ts, "sleep")
        assert is_ours is False
    finally:
        proc.kill()
        proc.wait()

def test_verify_start_time_mismatch(orchestrator):
    proc = subprocess.Popen(["sleep", "10"])
    pid = proc.pid
    pgid = os.getpgid(pid)
    started_at_ts = time.time() - 86400

    try:
        time.sleep(0.1)
        is_ours = orchestrator._verify_process_ownership(pid, pgid, started_at_ts, "sleep")
        assert is_ours is False
    finally:
        proc.kill()
        proc.wait()

@patch("os.killpg")
def test_reap_does_not_kill_mismatched_process(mock_killpg, orchestrator):
    proc = subprocess.Popen(["sleep", "10"])
    pid = proc.pid
    pgid = os.getpgid(pid)
    started_at_ts = time.time() - 86400

    try:
        time.sleep(0.1)
        orchestrator.db.query.return_value = [
            {
                "id": "run_1",
                "mission_id": "mission_1",
                "provider": "sleep",
                "pid": pid,
                "pgid": pgid,
                "started_at_ts": started_at_ts,
            }
        ]

        orchestrator._reap_orphaned_processes()

        mock_killpg.assert_not_called()

        orchestrator.db.update.assert_called_once()
        args, kwargs = orchestrator.db.update.call_args
        assert args[0] == "provider_runs"
        assert args[1] == "run_1"
        assert args[2]["failure_class"] == FailureClass.CRASH.value
        assert args[2]["provider_state"] == ProviderState.CRASHED.value
    finally:
        proc.kill()
        proc.wait()
