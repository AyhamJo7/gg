"""Shared durable invocation boundary (Increment 1).

``InvocationService.execute(spec)`` is the authoritative lifecycle for one
attributable attempt to execute one provider for one orchestration
operation. It owns: attribution, prompt measurement, capacity lease,
process lifecycle, cancellation, terminal reconciliation, usage
observation, and exactly-once health accounting.

It does NOT own mission planning, scheduling, review policy, or Git.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import usage as usage_parsers
from .context_manifest import (
    CONTEXT_POLICY_LEGACY,
    TEMPLATE_VERSION_LEGACY,
    measure_prompt,
)
from .models import FailureClass, ProviderState, utcnow
from .security import redact

logger = logging.getLogger(__name__)

# -- Stages -----------------------------------------------------------------
# Stable stage labels for attribution. Keep short; persisted verbatim.
STAGE_PRODUCT_PLANNING = "product_plan"
STAGE_MISSION_PLANNING = "mission_plan"
STAGE_DAG_PLANNING = "dag_plan"
STAGE_TASK = "task"
STAGE_REVIEW = "review"
STAGE_REPAIR = "repair"
STAGE_IMPLEMENTATION = "implementation"
STAGE_TESTING = "testing"

# -- Run statuses ------------------------------------------------------------
# Persisted lifecycle. Terminal states are immutable.
STATUS_PREPARED = "PREPARED"
STATUS_WAITING_FOR_CAPACITY = "WAITING_FOR_CAPACITY"
STATUS_STARTING = "STARTING"
STATUS_RUNNING = "RUNNING"
STATUS_CANCELLING = "CANCELLING"
STATUS_SUCCEEDED = "SUCCEEDED"
STATUS_FAILED = "FAILED"
STATUS_TIMED_OUT = "TIMED_OUT"
STATUS_CANCELLED = "CANCELLED"
STATUS_CRASHED = "CRASHED"

TERMINAL_RUN_STATUSES = frozenset({STATUS_SUCCEEDED, STATUS_FAILED, STATUS_TIMED_OUT, STATUS_CANCELLED, STATUS_CRASHED})

_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    STATUS_PREPARED: frozenset({STATUS_WAITING_FOR_CAPACITY, STATUS_STARTING, STATUS_FAILED, STATUS_CRASHED}),
    STATUS_WAITING_FOR_CAPACITY: frozenset({STATUS_STARTING, STATUS_CANCELLED, STATUS_CRASHED}),
    STATUS_STARTING: frozenset(
        {
            STATUS_RUNNING,
            STATUS_CANCELLING,
            STATUS_FAILED,
            STATUS_TIMED_OUT,
            STATUS_CANCELLED,
            STATUS_CRASHED,
            STATUS_SUCCEEDED,
        }
    ),
    STATUS_RUNNING: frozenset(
        {STATUS_CANCELLING, STATUS_SUCCEEDED, STATUS_FAILED, STATUS_TIMED_OUT, STATUS_CANCELLED, STATUS_CRASHED}
    ),
    STATUS_CANCELLING: frozenset({STATUS_CANCELLED, STATUS_CRASHED}),
    STATUS_SUCCEEDED: frozenset(),
    STATUS_FAILED: frozenset(),
    STATUS_TIMED_OUT: frozenset(),
    STATUS_CANCELLED: frozenset(),
    STATUS_CRASHED: frozenset(),
}


def is_terminal_run_status(status: str) -> bool:
    return status in TERMINAL_RUN_STATUSES


@dataclass
class InvocationOwner:
    mission_id: str | None = None
    task_id: str | None = None
    product_project_id: str | None = None
    phase_id: str | None = None
    operation_id: str | None = None

    def validate(self) -> None:
        if not any([self.mission_id, self.task_id, self.product_project_id, self.phase_id, self.operation_id]):
            raise ValueError("invocation owner requires at least one owner id")


@dataclass
class InvocationSpec:
    owner: InvocationOwner
    stage: str
    role: str
    prompt: str
    workdir: Path
    log_dir: Path
    provider: str
    timeout_s: float
    model_requested: str | None = None
    effort_requested: str | None = None
    run_id: str | None = None
    attempt_number: int = 1
    retry_of_run_id: str | None = None
    cancel_event: asyncio.Event | None = None
    on_output: Any = None

    def __post_init__(self) -> None:
        self.owner.validate()
        if not self.stage:
            raise ValueError("invocation stage is required")
        if not self.provider:
            raise ValueError("invocation provider is required")


@dataclass
class InvocationOutcome:
    run_id: str
    ok: bool
    run_status: str
    failure_class: str
    provider_state: str
    exit_code: int | None
    duration_s: float
    summary: str
    assistant_text: str
    raw_tail: str
    stdout_path: str | None
    stderr_path: str | None
    pid: int | None
    pgid: int | None
    gate_refused: bool
    model_observed: str | None = None
    cancelled: bool = False
    timed_out: bool = False


def _failure_to_run_status(failure: FailureClass, ok: bool) -> str:
    if ok:
        return STATUS_SUCCEEDED
    if failure == FailureClass.CANCELLED:
        return STATUS_CANCELLED
    if failure == FailureClass.TIMEOUT:
        return STATUS_TIMED_OUT
    if failure in (FailureClass.CRASH, FailureClass.MALFORMED_OUTPUT, FailureClass.UNKNOWN):
        return STATUS_CRASHED
    return STATUS_FAILED


def reconcile_terminal_outcome(
    *,
    exit_code: int | None,
    timed_out: bool,
    cancelled: bool,
    gate_refused: bool,
    failure: FailureClass,
    combined_tail: str,
) -> tuple[bool, FailureClass]:
    """Authoritative terminal precedence.

    Intermediate success-like markers never override a later failure.
    Order: confirmed cancellation > timeout > spawn refusal > explicit
    terminal failure evidence > non-zero exit > zero-exit blocking check.
    """
    from .providers.classify import _BLOCKING_SIGNALS

    if cancelled:
        return False, FailureClass.CANCELLED
    if timed_out:
        return False, FailureClass.TIMEOUT
    if gate_refused:
        return False, FailureClass.CRASH
    # Explicit terminal failure evidence beats any earlier success marker.
    for failure_class, pattern in _BLOCKING_SIGNALS:
        if pattern.search(combined_tail[-8000:]):
            return False, failure_class
    # Non-zero exit is failure even if a success marker appears earlier.
    if exit_code is not None and exit_code != 0:
        if failure == FailureClass.NONE:
            return False, FailureClass.CRASH
        return False, failure
    # Zero exit: a blocking signal already returned above; trust classifier
    # except when it claims success alongside explicit failure evidence
    # (handled above).
    if exit_code == 0:
        return (failure == FailureClass.NONE), failure
    # exit_code None without cancel/timeout/refusal: no process outcome.
    if failure == FailureClass.NONE:
        return False, FailureClass.CRASH
    return False, failure


class InvocationService:
    """Durable execution boundary shared by every provider caller."""

    def __init__(self, db: Any, registry: Any, config: Any) -> None:
        self._db = db
        self._registry = registry
        self._config = config
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._active_adapters: dict[str, Any] = {}

    # -- capacity ---------------------------------------------------------
    def _concurrency_limit(self, provider: str) -> int:
        raw = self._config.raw if hasattr(self._config, "raw") else {}
        overrides = (raw.get("scheduler") or {}).get("max_parallel_per_provider") or {}
        if isinstance(overrides, dict) and isinstance(overrides.get(provider), int):
            return int(overrides[provider])
        defaults = {"claude": 1, "codex": 1, "agy": 1, "opencode": 1}
        return int(defaults.get(provider, 1))

    def _active_lease_count(self, provider: str) -> int:
        rows = self._db.query(
            "SELECT COUNT(*) as cnt FROM invocation_leases WHERE provider=? AND released_at IS NULL",
            (provider,),
        )
        return int(rows[0]["cnt"]) if rows else 0

    def has_capacity(self, provider: str) -> bool:
        return self._active_lease_count(provider) < self._concurrency_limit(provider)

    def _acquire_lease(self, run_id: str, provider: str) -> bool:
        if not self.has_capacity(provider):
            return False
        try:
            self._db.insert(
                "invocation_leases",
                {
                    "run_id": run_id,
                    "provider": provider,
                    "acquired_at": utcnow().isoformat(),
                    "released_at": None,
                },
            )
        except Exception:
            logger.debug("lease acquire failed for run %s", run_id, exc_info=True)
            return False
        return True

    def _release_lease(self, run_id: str) -> None:
        try:
            self._db.execute(
                "UPDATE invocation_leases SET released_at=? WHERE run_id=? AND released_at IS NULL",
                (utcnow().isoformat(), run_id),
            )
        except Exception:
            logger.warning("lease release failed for run %s", run_id, exc_info=True)

    # -- run rows ----------------------------------------------------------
    def _transition(self, run_id: str, to_status: str) -> bool:
        """Conditional transition; refuses writes out of terminal states."""
        row = self._db.get("provider_runs", run_id)
        if not row:
            return False
        current = str(row.get("run_status") or "")
        if current in TERMINAL_RUN_STATUSES:
            return False
        if current and to_status not in _ALLOWED_TRANSITIONS.get(current, frozenset()):
            # Allow direct terminal writes from any non-terminal state so
            # crash paths cannot get stuck on an unlisted edge.
            if to_status not in TERMINAL_RUN_STATUSES:
                logger.warning("illegal run transition %s -> %s for %s", current, to_status, run_id)
                return False
        self._db.execute("UPDATE provider_runs SET run_status=? WHERE id=?", (to_status, run_id))
        return True

    def _is_terminal(self, run_id: str) -> bool:
        row = self._db.get("provider_runs", run_id)
        if not row:
            return True
        if row.get("finished_at"):
            return True
        return str(row.get("run_status") or "") in TERMINAL_RUN_STATUSES

    # -- public API ----------------------------------------------------------
    def cancel_event_for(self, run_id: str) -> asyncio.Event:
        return self._cancel_events.setdefault(run_id, asyncio.Event())

    async def cancel(self, run_id: str) -> bool:
        """Request cancellation: persist CANCELLING, interrupt the owned
        process, and wait for confirmed exit before releasing capacity.

        Returns True when the run reached a terminal CANCELLED/CRASHED
        state through this path.
        """
        row = self._db.get("provider_runs", run_id)
        if not row:
            return False
        if str(row.get("run_status") or "") in TERMINAL_RUN_STATUSES or row.get("finished_at"):
            return False
        self._db.execute("UPDATE provider_runs SET cancel_requested=1 WHERE id=?", (run_id,))
        self._transition(run_id, STATUS_CANCELLING)
        event = self._cancel_events.get(run_id)
        if event is not None:
            event.set()
        adapter = self._active_adapters.get(run_id)
        if adapter is not None:
            try:
                await adapter.interrupt(run_id)
            except Exception:
                logger.warning("adapter interrupt failed for run %s", run_id, exc_info=True)
        # Bounded wait for the executing task to persist terminal state and
        # release the lease. Capacity is NOT released here — only the
        # execution path releases after confirmed process exit.
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            if self._is_terminal(run_id):
                return True
            await asyncio.sleep(0.05)
        logger.warning("cancel wait timed out for run %s; lease held until exit", run_id)
        return False

    def recover(self) -> dict[str, int]:
        """Reconcile unfinished runs after backend restart.

        Verifies process ownership before signalling; never fabricates
        success; never leaves a lease held forever.
        """
        from .orphans import kill_process_tree, process_alive, verify_process_ownership

        summary = {"reconciled": 0, "reaped": 0, "leases_released": 0}
        try:
            rows = self._db.query("SELECT * FROM provider_runs WHERE finished_at IS NULL")
        except Exception:
            logger.warning("invocation recovery query failed", exc_info=True)
            return summary
        for row in rows:
            run_id = row["id"]
            status = str(row.get("run_status") or "")
            if status in TERMINAL_RUN_STATUSES:
                # Defensive: finished_at missing but status terminal.
                try:
                    self._db.execute(
                        "UPDATE provider_runs SET finished_at=? WHERE id=? AND finished_at IS NULL",
                        (utcnow().isoformat(), run_id),
                    )
                    self._release_lease(run_id)
                    summary["leases_released"] += 1
                except Exception:
                    logger.warning("lease release failed during recovery for %s", run_id, exc_info=True)
                continue
            pid = row.get("pid")
            pgid = row.get("pgid")
            provider = str(row.get("provider") or "")
            started_ts = row.get("started_at_ts")
            try:
                start_float = float(started_ts) if started_ts is not None else None
            except (TypeError, ValueError):
                start_float = None
            alive = False
            ours = False
            if isinstance(pid, int) and isinstance(pgid, int):
                try:
                    alive = process_alive(pid)
                except Exception:
                    alive = False
                if alive:
                    try:
                        ours = verify_process_ownership(pid, pgid, start_float, provider)
                    except Exception:
                        ours = False
                    if ours:
                        try:
                            kill_process_tree(pgid)
                            summary["reaped"] += 1
                        except Exception:
                            logger.warning("orphan reap failed for run %s", run_id, exc_info=True)
                    else:
                        logger.warning("run %s has unverifiable live pid %s; not killing", run_id, pid)
            try:
                self._db.execute(
                    "UPDATE provider_runs SET run_status=?, failure_class=?, provider_state=?,"
                    " finished_at=?, summary=? WHERE id=? AND finished_at IS NULL",
                    (
                        STATUS_CRASHED,
                        FailureClass.CRASH.value,
                        ProviderState.CRASHED.value,
                        utcnow().isoformat(),
                        "backend restart interrupted invocation; no result committed",
                        run_id,
                    ),
                )
                summary["reconciled"] += 1
            except Exception:
                logger.warning("run reconcile failed for %s", run_id, exc_info=True)
                continue
            try:
                self._release_lease(run_id)
                summary["leases_released"] += 1
            except Exception:
                logger.warning("lease release failed for %s", run_id, exc_info=True)
        # Also reconcile durable planning operations left RUNNING.
        try:
            from . import operations as ops

            ops.recover_operations(self._db)
        except Exception:
            logger.warning("operation recovery failed", exc_info=True)
        return summary

    async def execute(self, spec: InvocationSpec) -> InvocationOutcome:
        run_id = spec.run_id or f"run-{uuid.uuid4().hex[:12]}"
        external_cancel = spec.cancel_event
        internal_cancel = self.cancel_event_for(run_id)
        if external_cancel is not None and external_cancel.is_set():
            internal_cancel.set()

        manifest = measure_prompt(spec.prompt)
        spec.log_dir.mkdir(parents=True, exist_ok=True)
        adapter = self._registry.get_adapter(spec.provider)
        if adapter is None:
            raise RuntimeError(f"no adapter for provider {spec.provider}")

        requested_model: str | None = spec.model_requested
        if requested_model is None and spec.provider == "opencode":
            requested_model = getattr(adapter, "model", None)

        # Persist PREPARED run + manifest + UNKNOWN usage in one step so
        # recovery always has an attributable record before spawn.
        now = utcnow().isoformat()
        self._db.insert(
            "provider_runs",
            {
                "id": run_id,
                "mission_id": spec.owner.mission_id,
                "task_id": spec.owner.task_id,
                "product_project_id": spec.owner.product_project_id,
                "phase_id": spec.owner.phase_id,
                "operation_id": spec.owner.operation_id,
                "attempt_number": spec.attempt_number,
                "retry_of_run_id": spec.retry_of_run_id,
                "provider": spec.provider,
                "role": spec.role,
                "stage": spec.stage,
                "run_status": STATUS_PREPARED,
                "command": [redact(spec.provider)],
                "cwd": str(spec.workdir),
                "started_at": now,
                "finished_at": None,
                "exit_code": None,
                "failure_class": "RUNNING",
                "provider_state": "RUNNING",
                "stdout_path": str(spec.log_dir / f"{run_id}.stdout.log"),
                "stderr_path": str(spec.log_dir / f"{run_id}.stderr.log"),
                "git_commit_before": None,
                "git_commit_after": None,
                "summary": "",
                "pid": None,
                "pgid": None,
                "started_at_ts": None,
                "model_requested": requested_model,
                "model_observed": None,
                "effort_requested": spec.effort_requested,
                "cli_version": None,
                "session_ref": None,
                "duration_ms": None,
                "prompt_template_version": TEMPLATE_VERSION_LEGACY,
                "context_policy_version": CONTEXT_POLICY_LEGACY,
                "cancel_requested": 0,
            },
        )
        self._db.insert(
            "run_context_manifests",
            {
                "run_id": run_id,
                "schema_version": "v1",
                "prompt_hash": manifest.prompt_hash,
                "hash_basis": manifest.hash_basis,
                "prompt_chars": manifest.prompt_chars,
                "prompt_bytes": manifest.prompt_bytes,
                "prompt_words": manifest.prompt_words,
                "estimated_prompt_tokens": manifest.estimated_prompt_tokens,
                "estimator_id": manifest.estimator_id,
                "blocks_json": json.dumps(
                    [{"block_type": "legacy_prompt", "chars": manifest.prompt_chars, "decision": "included"}]
                ),
                "capture_status": "CAPTURED",
                "redaction_status": "REDACTED",
                "created_at": now,
            },
        )
        self._db.insert(
            "run_usage",
            {
                "run_id": run_id,
                "input_tokens_total": None,
                "output_tokens_total": None,
                "cache_read_input_tokens": None,
                "cache_write_input_tokens": None,
                "reasoning_output_tokens": None,
                "native_total_tokens": None,
                "output_text_tokens_estimated": None,
                "estimator_id": None,
                "source": "UNKNOWN",
                "completeness": "UNKNOWN",
                "input_basis": "UNKNOWN",
                "output_basis": "UNKNOWN",
                "parser_version": usage_parsers.PARSER_VERSION,
                "evidence_kind": "",
                "observations_count": 0,
                "native_counts_json": "{}",
                "requested_model": requested_model,
                "observed_model": None,
                "captured_at": now,
            },
        )

        # Capacity: one lease per active invocation. Callers that also use
        # legacy reservations keep them; the lease is the authoritative
        # per-invocation claim and is only released after process exit.
        if not self._acquire_lease(run_id, spec.provider):
            self._transition(run_id, STATUS_WAITING_FOR_CAPACITY)
            self._db.execute(
                "UPDATE provider_runs SET failure_class=?, provider_state=?, finished_at=?, summary=?"
                " WHERE id=? AND finished_at IS NULL",
                (
                    FailureClass.CRASH.value,
                    ProviderState.CRASHED.value,
                    utcnow().isoformat(),
                    f"provider {spec.provider} at capacity; caller must retry",
                    run_id,
                ),
            )
            self._db.execute("UPDATE provider_runs SET run_status=? WHERE id=?", (STATUS_CRASHED, run_id))
            raise RuntimeError(f"provider {spec.provider} at capacity")

        self._transition(run_id, STATUS_STARTING)
        try:
            self._registry.mark_busy(spec.provider)
        except Exception:
            logger.warning("mark_busy failed for %s", spec.provider, exc_info=True)

        observed_lines: list[str] = []

        def on_spawn(pid: int, pgid: int, start_ts: float) -> None:
            # Durable identity BEFORE the spawn gate releases. A failure
            # here holds the gate: nothing ever executes without a writer.
            self._db.execute(
                "UPDATE provider_runs SET pid=?, pgid=?, started_at_ts=? WHERE id=?",
                (pid, pgid, start_ts, run_id),
            )

        # Late-cancel bridge: an external engine event set before/during
        # execution must interrupt this run.
        async def _bridge_external_cancel() -> None:
            if external_cancel is None:
                return
            while not self._is_terminal(run_id):
                if external_cancel.is_set():
                    internal_cancel.set()
                    try:
                        await adapter.interrupt(run_id)
                    except Exception:
                        logger.debug("bridge interrupt failed for %s", run_id, exc_info=True)
                    return
                await asyncio.sleep(0.05)

        bridge_task: asyncio.Task[None] | None = None
        if external_cancel is not None:
            bridge_task = asyncio.create_task(_bridge_external_cancel())

        from .providers.base import ExecutionRequest

        # If service-level cancellation arrived while PREPARED, reflect it.
        row_now = self._db.get("provider_runs", run_id)
        if row_now and int(row_now.get("cancel_requested") or 0):
            internal_cancel.set()
            self._transition(run_id, STATUS_CANCELLING)

        request = ExecutionRequest(
            prompt=spec.prompt,
            workdir=spec.workdir,
            role=spec.role,
            timeout_s=spec.timeout_s,
            run_id=run_id,
            log_dir=spec.log_dir,
            on_spawn=on_spawn,
        )
        # Register interruptibility before spawn so no interrupt is lost.
        self._active_adapters[run_id] = adapter
        adapter.cancel_event_for(run_id)
        if internal_cancel.is_set():
            try:
                (awaitable := adapter.interrupt(run_id))
                if asyncio.iscoroutine(awaitable):
                    await awaitable
            except Exception:
                logger.debug("pre-spawn interrupt failed for %s", run_id, exc_info=True)
        # Only mark RUNNING after the spawn callback had a chance to run;
        # adapter.execute persists identity synchronously before release.
        self._transition(run_id, STATUS_RUNNING)

        def _on_output(line: str) -> None:
            if len(observed_lines) < 4000:
                observed_lines.append(line)
            if spec.on_output is not None:
                try:
                    spec.on_output(line)
                except Exception:
                    logger.warning("invocation on_output failed for run %s", run_id, exc_info=True)

        from .models import ProviderState as _PS

        gate_refused = False
        try:
            result = await adapter.execute(request, _on_output)
        except Exception as exc:
            logger.exception("provider %s crashed", spec.provider)
            from .providers.base import ExecutionResult as _ER

            result = _ER(
                state=_PS.CRASHED,
                failure_class=FailureClass.CRASH,
                exit_code=None,
                duration_s=0.0,
                summary="",
                raw_tail=redact(str(exc))[:4000],
            )
            gate_refused = False
        else:
            gate_refused = bool(result.gate_refused)
        finally:
            self._active_adapters.pop(run_id, None)
            if bridge_task is not None:
                bridge_task.cancel()
                try:
                    await bridge_task
                except (asyncio.CancelledError, Exception):
                    logger.debug("cancel bridge teardown for %s", run_id, exc_info=True)

        # Terminal reconciliation: intermediate success never wins over a
        # later failure. Uses exit code + explicit failure evidence.
        combined = result.raw_tail or "\n".join(observed_lines[-200:])
        timed_out = result.failure_class == FailureClass.TIMEOUT
        cancelled = result.failure_class == FailureClass.CANCELLED or internal_cancel.is_set()
        # If the service-level cancel fired but the adapter reported
        # success, the late result must not overwrite cancellation.
        ok, failure = reconcile_terminal_outcome(
            exit_code=result.exit_code,
            timed_out=timed_out,
            cancelled=cancelled,
            gate_refused=gate_refused,
            failure=result.failure_class,
            combined_tail=combined,
        )
        # A malformed/missing required output on zero exit is a role-level
        # concern handled by callers; the invocation itself completed the
        # protocol. Do not conflate here.

        run_status = _failure_to_run_status(failure, ok)
        provider_state = _PS.COMPLETED.value if ok else _terminal_provider_state(spec.provider, failure).value

        # Usage observation from accumulated stream lines (numeric only).
        # Prefer the stdout log file when present for completeness.
        usage_lines = observed_lines
        try:
            stdout_path = Path(str(spec.log_dir / f"{run_id}.stdout.log"))  # noqa: ASYNC240 - bounded single-file read
            if stdout_path.is_file() and stdout_path.stat().st_size < 8_000_000:  # noqa: ASYNC240
                usage_lines = stdout_path.read_text(encoding="utf-8", errors="replace").splitlines()[-4000:]  # noqa: ASYNC240
        except OSError:
            logger.debug("usage log read failed for %s", run_id, exc_info=True)
        try:
            parsed = usage_parsers.parse_usage_for_provider(spec.provider, usage_lines)
        except Exception:
            logger.warning("usage parse failed for run %s", run_id, exc_info=True)
            parsed = usage_parsers.unknown_usage("parser-error")
        if spec.provider == "agy":
            parsed = usage_parsers.unknown_usage("agy:telemetry-not-captured")

        finished = utcnow().isoformat()
        duration_ms = int(result.duration_s * 1000)
        # Terminal write is conditional: late completions never resurrect a
        # terminal run (notably CANCELLED).
        if not self._is_terminal(run_id):
            self._db.execute(
                "UPDATE provider_runs SET finished_at=?, exit_code=?, failure_class=?, provider_state=?,"
                " summary=?, stdout_path=?, stderr_path=?, pid=?, pgid=?,"
                " model_observed=?, duration_ms=? WHERE id=? AND finished_at IS NULL",
                (
                    finished,
                    result.exit_code,
                    failure.value,
                    provider_state,
                    redact(result.summary or "")[:500],
                    str(result.stdout_path) if result.stdout_path else str(spec.log_dir / f"{run_id}.stdout.log"),
                    str(result.stderr_path) if result.stderr_path else str(spec.log_dir / f"{run_id}.stderr.log"),
                    result.pid,
                    result.pgid,
                    parsed.observed_model,
                    duration_ms,
                    run_id,
                ),
            )
            # run_status last, guarded by transition rules.
            current = self._db.get("provider_runs", run_id) or {}
            if str(current.get("run_status") or "") not in TERMINAL_RUN_STATUSES:
                self._db.execute("UPDATE provider_runs SET run_status=? WHERE id=?", (run_status, run_id))
            try:
                self._db.execute(
                    "UPDATE run_usage SET input_tokens_total=?, output_tokens_total=?,"
                    " cache_read_input_tokens=?, cache_write_input_tokens=?, reasoning_output_tokens=?,"
                    " native_total_tokens=?, source=?, completeness=?, input_basis=?, output_basis=?,"
                    " parser_version=?, evidence_kind=?, observations_count=?, native_counts_json=?,"
                    " observed_model=?, captured_at=? WHERE run_id=?",
                    (
                        parsed.input_tokens_total,
                        parsed.output_tokens_total,
                        parsed.cache_read_input_tokens,
                        parsed.cache_write_input_tokens,
                        parsed.reasoning_output_tokens,
                        parsed.native_total_tokens,
                        parsed.source,
                        parsed.completeness,
                        parsed.input_basis,
                        parsed.output_basis,
                        parsed.parser_version,
                        parsed.evidence_kind,
                        parsed.observations_count,
                        json.dumps(parsed.native_counts)[:8000],
                        parsed.observed_model,
                        finished,
                        run_id,
                    ),
                )
            except Exception:
                logger.warning("usage persist failed for run %s", run_id, exc_info=True)
            # Exactly-once health accounting for this physical invocation.
            try:
                if gate_refused:
                    self._registry.clear_busy_without_penalty(spec.provider)
                elif ok:
                    self._registry.record_success(spec.provider, result.duration_s)
                else:
                    self._registry.record_failure(
                        spec.provider, failure, result.duration_s, (result.raw_tail or "")[:300]
                    )
            except Exception:
                logger.warning("provider health update failed for run %s", run_id, exc_info=True)
        else:
            logger.warning("late terminal event ignored for terminal run %s", run_id)

        # Capacity is released only now, after confirmed process exit and
        # durable terminal persistence.
        self._release_lease(run_id)
        self._cancel_events.pop(run_id, None)

        final_row = self._db.get("provider_runs", run_id) or {}
        final_failure = str(final_row.get("failure_class") or failure.value)
        final_status = str(final_row.get("run_status") or run_status)
        final_ok = final_status == STATUS_SUCCEEDED
        return InvocationOutcome(
            run_id=run_id,
            ok=final_ok,
            run_status=final_status,
            failure_class=final_failure,
            provider_state=str(final_row.get("provider_state") or provider_state),
            exit_code=result.exit_code,
            duration_s=result.duration_s,
            summary=result.summary,
            assistant_text=result.assistant_text,
            raw_tail=result.raw_tail,
            stdout_path=str(result.stdout_path) if result.stdout_path else None,
            stderr_path=str(result.stderr_path) if result.stderr_path else None,
            pid=result.pid,
            pgid=result.pgid,
            gate_refused=gate_refused,
            model_observed=parsed.observed_model,
            cancelled=(final_failure == FailureClass.CANCELLED.value),
            timed_out=(final_failure == FailureClass.TIMEOUT.value),
        )


def _terminal_provider_state(provider: str, failure: FailureClass) -> ProviderState:
    from .providers.classify import FAILURE_TO_STATE

    _ = provider
    return FAILURE_TO_STATE.get(failure, ProviderState.CRASHED)


@dataclass
class InvocationHandle:
    """Lightweight record for architectural tests proving callers route
    through the shared boundary."""

    run_id: str
    stage: str
    provider: str
    spec: InvocationSpec = field(repr=False)
    outcome: InvocationOutcome | None = None
