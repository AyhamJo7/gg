"""Durable long operations (Increment 1: product planning).

A planning request creates (or attaches to) one active operation per
product. Double-clicking Generate Plan never launches two competing
planners. Stale results cannot overwrite newer revisions.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from .models import utcnow

KIND_PLAN = "PLAN"
STATE_RUNNING = "RUNNING"
STATE_SUCCEEDED = "SUCCEEDED"
STATE_FAILED = "FAILED"
STATE_CANCELLED = "CANCELLED"


@dataclass(frozen=True)
class Operation:
    id: str
    product_project_id: str
    kind: str
    state: str
    expected_plan_revision: int
    current_run_id: str | None
    attempts: int
    cancel_requested: bool


def _row_to_op(row: dict[str, Any]) -> Operation:
    return Operation(
        id=row["id"],
        product_project_id=row["product_project_id"],
        kind=row["kind"],
        state=row["state"],
        expected_plan_revision=int(row.get("expected_plan_revision") or 0),
        current_run_id=row.get("current_run_id"),
        attempts=int(row.get("attempts") or 0),
        cancel_requested=bool(row.get("cancel_requested")),
    )


def get_active_operation(db: Any, product_id: str, kind: str = KIND_PLAN) -> Operation | None:
    rows = db.query(
        "SELECT * FROM orchestration_operations WHERE product_project_id=? AND kind=? AND finished_at IS NULL"
        " ORDER BY created_at DESC LIMIT 1",
        (product_id, kind),
    )
    return _row_to_op(rows[0]) if rows else None


def create_or_attach_operation(db: Any, product_id: str, kind: str = KIND_PLAN) -> tuple[Operation, bool]:
    """Return (operation, created). Attach to the active operation if present."""
    existing = get_active_operation(db, product_id, kind)
    if existing is not None:
        return existing, False
    product = db.get("product_projects", product_id)
    expected = int((product or {}).get("plan_revision") or 0)
    now = utcnow().isoformat()
    op_id = f"op-{uuid.uuid4().hex[:12]}"
    try:
        db.insert(
            "orchestration_operations",
            {
                "id": op_id,
                "product_project_id": product_id,
                "kind": kind,
                "state": STATE_RUNNING,
                "expected_plan_revision": expected,
                "current_run_id": None,
                "attempts": 0,
                "cancel_requested": 0,
                "error_code": None,
                "error_detail": "",
                "created_at": now,
                "updated_at": now,
                "finished_at": None,
            },
        )
    except Exception:
        # Lost a race with a concurrent creator: attach to the winner.
        existing = get_active_operation(db, product_id, kind)
        if existing is not None:
            return existing, False
        raise
    row = db.get("orchestration_operations", op_id)
    if row is None:
        raise RuntimeError(f"operation {op_id} vanished after insert")
    return _row_to_op(row), True


def note_attempt(db: Any, op_id: str, run_id: str | None) -> None:
    row = db.get("orchestration_operations", op_id)
    if not row or row.get("finished_at"):
        return
    db.execute(
        "UPDATE orchestration_operations SET attempts=attempts+1, current_run_id=?, updated_at=? WHERE id=?",
        (run_id, utcnow().isoformat(), op_id),
    )


def request_cancel(db: Any, op_id: str) -> bool:
    row = db.get("orchestration_operations", op_id)
    if not row or row.get("finished_at"):
        return False
    db.execute(
        "UPDATE orchestration_operations SET cancel_requested=1, updated_at=? WHERE id=?",
        (utcnow().isoformat(), op_id),
    )
    return True


def finish_operation(db: Any, op_id: str, state: str, error_code: str | None = None, error_detail: str = "") -> bool:
    """Persist a terminal state once. Returns False if already terminal."""
    row = db.get("orchestration_operations", op_id)
    if not row or row.get("finished_at"):
        return False
    now = utcnow().isoformat()
    db.execute(
        "UPDATE orchestration_operations SET state=?, error_code=?, error_detail=?, updated_at=?, finished_at=?"
        " WHERE id=? AND finished_at IS NULL",
        (state, error_code, error_detail[:2000], now, now, op_id),
    )
    return True


def is_stale_result(db: Any, op_id: str, product_id: str) -> bool:
    """True when the operation's expected revision no longer matches HEAD."""
    op = db.get("orchestration_operations", op_id)
    product = db.get("product_projects", product_id)
    if not op or not product:
        return True
    if op.get("finished_at") or op.get("cancel_requested"):
        return True
    return int(product.get("plan_revision") or 0) != int(op.get("expected_plan_revision") or 0)


def recover_operations(db: Any) -> list[str]:
    """Mark RUNNING operations with no live owner as CANCELLED-interrupted.

    Returns operation ids that were reconciled. A backend restart never
    leaves an operation RUNNING forever and never fabricates success.
    """
    rows = db.query("SELECT * FROM orchestration_operations WHERE finished_at IS NULL")
    reconciled: list[str] = []
    for row in rows:
        run_id = row.get("current_run_id")
        run_active = False
        if run_id:
            run = db.get("provider_runs", run_id)
            if run and not run.get("finished_at"):
                # Check the run is not itself terminal via run_status.
                status = str(run.get("run_status") or "")
                if status not in (
                    "SUCCEEDED",
                    "FAILED",
                    "TIMED_OUT",
                    "CANCELLED",
                    "CRASHED",
                ):
                    run_active = True
        if not run_active:
            finish_operation(
                db,
                row["id"],
                STATE_CANCELLED,
                error_code="ORCHESTRATOR_RESTART",
                error_detail="backend restart interrupted planning operation; no result committed",
            )
            reconciled.append(row["id"])
    return reconciled
