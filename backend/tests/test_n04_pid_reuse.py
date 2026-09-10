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
        run_row = {
            "id": "run_1",
            "mission_id": "mission_1",
            "provider": "sleep",
            "pid": pid,
            "pgid": pgid,
            "started_at_ts": started_at_ts,
        }
        # First query serves unfinished runs; second serves operations recovery.
        orchestrator.db.query.side_effect = [[run_row], []]

        orchestrator._reap_orphaned_processes()

        mock_killpg.assert_not_called()

        # Atomic terminal-safe reconciliation, not the legacy generic update.
        execute_calls = orchestrator.db.execute.call_args_list
        run_updates = [c for c in execute_calls if "UPDATE provider_runs" in str(c.args[0])]
        assert run_updates, "expected atomic provider_runs reconciliation via db.execute"
        sql, params = run_updates[0].args[0], run_updates[0].args[1]
        assert "WHERE id=? AND finished_at IS NULL" in sql
        assert "run_1" in params
        assert FailureClass.CRASH.value in params
        assert ProviderState.CRASHED.value in params
        # Lease/capacity is reconciled according to the current contract.
        lease_updates = [c for c in execute_calls if "invocation_leases" in str(c.args[0])]
        assert lease_updates, "expected lease reconciliation via db.execute"
        orchestrator.db.update.assert_not_called()
    finally:
        proc.kill()
        proc.wait()
