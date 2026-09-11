"""Core domain models and enumerations.

All persisted entities use these types. Enums are stored as strings in SQLite
so the database remains human-inspectable.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field


def new_id() -> str:
    return uuid.uuid4().hex[:16]


def utcnow() -> datetime:
    return datetime.now(UTC)


StrValueEnum = enum.StrEnum


class ProviderState(StrValueEnum):
    AVAILABLE = "AVAILABLE"
    BUSY = "BUSY"
    RATE_LIMITED = "RATE_LIMITED"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    UNAVAILABLE = "UNAVAILABLE"
    CRASHED = "CRASHED"
    TIMED_OUT = "TIMED_OUT"
    HUMAN_INPUT_REQUIRED = "HUMAN_INPUT_REQUIRED"
    COMPLETED = "COMPLETED"
    DISABLED = "DISABLED"


class FailureClass(StrValueEnum):
    NONE = "NONE"
    RATE_LIMIT = "RATE_LIMIT"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    AUTH = "AUTH"
    OVERLOADED = "OVERLOADED"
    TIMEOUT = "TIMEOUT"
    CRASH = "CRASH"
    MALFORMED_OUTPUT = "MALFORMED_OUTPUT"
    HUMAN_INPUT = "HUMAN_INPUT"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


class Role(StrValueEnum):
    PLANNING = "planning"
    IMPLEMENTATION = "implementation"
    TESTING = "testing"
    REVIEW = "review"
    REPAIR = "repair"


class MissionStatus(StrValueEnum):
    CREATED = "CREATED"
    ANALYZING = "ANALYZING"
    PLANNING = "PLANNING"
    IMPLEMENTING = "IMPLEMENTING"
    TESTING = "TESTING"
    REVIEWING = "REVIEWING"
    REPAIRING = "REPAIRING"
    FINAL_VALIDATION = "FINAL_VALIDATION"
    COMPLETED = "COMPLETED"
    PAUSED = "PAUSED"
    WAITING_FOR_PROVIDER = "WAITING_FOR_PROVIDER"
    WAITING_FOR_WORKSPACE = "WAITING_FOR_WORKSPACE"
    WAITING_FOR_HUMAN = "WAITING_FOR_HUMAN"
    RATE_LIMITED = "RATE_LIMITED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    RECOVERING = "RECOVERING"
    UNVERIFIED = "UNVERIFIED"  # terminal: work finished but evidence incomplete


ACTIVE_STATUSES = frozenset(
    {
        MissionStatus.ANALYZING,
        MissionStatus.PLANNING,
        MissionStatus.IMPLEMENTING,
        MissionStatus.TESTING,
        MissionStatus.REVIEWING,
        MissionStatus.REPAIRING,
        MissionStatus.FINAL_VALIDATION,
        MissionStatus.WAITING_FOR_PROVIDER,
        MissionStatus.RATE_LIMITED,
        MissionStatus.RECOVERING,
    }
)

TERMINAL_STATUSES = frozenset(
    {MissionStatus.COMPLETED, MissionStatus.FAILED, MissionStatus.CANCELLED, MissionStatus.UNVERIFIED}
)


class Autonomy(StrValueEnum):
    SAFE = "SAFE"
    BALANCED = "BALANCED"
    AUTONOMOUS = "AUTONOMOUS"


class Severity(StrValueEnum):
    BLOCKER = "BLOCKER"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class SchedulingMode(StrValueEnum):
    SEQUENTIAL = "SEQUENTIAL"
    PARALLEL_SAFE = "PARALLEL_SAFE"


class ProductStatus(StrValueEnum):
    DRAFT = "DRAFT"
    PLANNING = "PLANNING"
    PLAN_READY = "PLAN_READY"
    EXECUTING = "EXECUTING"
    WAITING_FOR_HUMAN = "WAITING_FOR_HUMAN"
    REVIEWING = "REVIEWING"
    FINAL_ACCEPTANCE = "FINAL_ACCEPTANCE"
    DELIVERED = "DELIVERED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


ACTIVE_PRODUCT_STATUSES = frozenset(
    {
        ProductStatus.PLANNING,
        ProductStatus.EXECUTING,
        ProductStatus.WAITING_FOR_HUMAN,
        ProductStatus.REVIEWING,
        ProductStatus.FINAL_ACCEPTANCE,
    }
)

TERMINAL_PRODUCT_STATUSES = frozenset(
    {ProductStatus.DELIVERED, ProductStatus.FAILED, ProductStatus.CANCELLED}
)


class AcceptanceState(StrValueEnum):
    PENDING = "PENDING"
    ENGINEERING_COMPLETE = "ENGINEERING_COMPLETE"
    LOCAL_ACCEPTED = "LOCAL_ACCEPTED"
    WAITING_FOR_HUMAN = "WAITING_FOR_HUMAN"
    EXTERNALLY_BLOCKED = "EXTERNALLY_BLOCKED"
    UNVERIFIED = "UNVERIFIED"
    DELIVERED = "DELIVERED"


class ProjectPhaseStatus(StrValueEnum):
    PENDING = "PENDING"
    READY = "READY"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    WAITING_FOR_HUMAN = "WAITING_FOR_HUMAN"
    SKIPPED = "SKIPPED"


TERMINAL_PHASE_STATUSES = frozenset(
    {
        ProjectPhaseStatus.COMPLETED,
        ProjectPhaseStatus.FAILED,
        ProjectPhaseStatus.BLOCKED,
        ProjectPhaseStatus.SKIPPED,
    }
)


class TaskStatus(StrValueEnum):
    PENDING = "PENDING"
    BLOCKED = "BLOCKED"
    READY = "READY"
    CLAIMED = "CLAIMED"
    RUNNING = "RUNNING"
    WAITING_FOR_PROVIDER = "WAITING_FOR_PROVIDER"
    WAITING_FOR_HUMAN = "WAITING_FOR_HUMAN"
    WAITING_FOR_INTEGRATION = "WAITING_FOR_INTEGRATION"
    REVIEWING = "REVIEWING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    UNVERIFIED = "UNVERIFIED"
    # Increment 3B: a completed descendant whose upstream dependency produced
    # a newer result it does not contain. Non-runnable and non-terminal: the
    # mission cannot complete with STALE tasks; operator resubmits/retries
    # them into new attempts against the current upstream results.
    STALE = "STALE"


TERMINAL_TASK_STATUSES = frozenset(
    {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.UNVERIFIED}
)


class LockType(StrValueEnum):
    WORKSPACE_EXCLUSIVE = "WORKSPACE_EXCLUSIVE"
    PATH_PREFIX = "PATH_PREFIX"
    GIT = "GIT"
    DATABASE = "DATABASE"
    INTEGRATION = "INTEGRATION"


class IntegrationStatus(StrValueEnum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    MERGE_CONFLICT = "MERGE_CONFLICT"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class EventType(StrValueEnum):
    MISSION_CREATED = "MISSION_CREATED"
    MISSION_STATUS_CHANGED = "MISSION_STATUS_CHANGED"
    PHASE_STARTED = "PHASE_STARTED"
    PHASE_COMPLETED = "PHASE_COMPLETED"
    TASK_STARTED = "TASK_STARTED"
    TASK_COMPLETED = "TASK_COMPLETED"
    PROVIDER_SELECTED = "PROVIDER_SELECTED"
    PROVIDER_STARTED = "PROVIDER_STARTED"
    PROVIDER_OUTPUT = "PROVIDER_OUTPUT"
    PROVIDER_RATE_LIMITED = "PROVIDER_RATE_LIMITED"
    PROVIDER_FAILED = "PROVIDER_FAILED"
    PROVIDER_HEALTH_CHANGED = "PROVIDER_HEALTH_CHANGED"
    HANDOFF_CREATED = "HANDOFF_CREATED"
    GIT_CHECKPOINT_CREATED = "GIT_CHECKPOINT_CREATED"
    GIT_CHECKPOINT_FAILED = "GIT_CHECKPOINT_FAILED"
    TEST_STARTED = "TEST_STARTED"
    TEST_FAILED = "TEST_FAILED"
    TEST_PASSED = "TEST_PASSED"
    REVIEW_FINDING_CREATED = "REVIEW_FINDING_CREATED"
    REVIEW_RECORDED = "REVIEW_RECORDED"
    HUMAN_GATE_CREATED = "HUMAN_GATE_CREATED"
    HUMAN_GATE_RESOLVED = "HUMAN_GATE_RESOLVED"
    MISSION_COMPLETED = "MISSION_COMPLETED"
    MISSION_FAILED = "MISSION_FAILED"
    MISSION_PAUSED = "MISSION_PAUSED"
    MISSION_RESUMED = "MISSION_RESUMED"
    # Phase 2A events
    DAG_CREATED = "DAG_CREATED"
    DAG_VALIDATED = "DAG_VALIDATED"
    DAG_REVISED = "DAG_REVISED"
    TASK_READY = "TASK_READY"
    TASK_CLAIMED = "TASK_CLAIMED"
    TASK_BLOCKED = "TASK_BLOCKED"
    TASK_FAILED = "TASK_FAILED"
    TASK_CANCELLED = "TASK_CANCELLED"
    PROVIDER_RESERVED = "PROVIDER_RESERVED"
    PROVIDER_RELEASED = "PROVIDER_RELEASED"
    LOCK_ACQUIRED = "LOCK_ACQUIRED"
    LOCK_RELEASED = "LOCK_RELEASED"
    LOCK_CONFLICT = "LOCK_CONFLICT"
    WORKTREE_CREATED = "WORKTREE_CREATED"
    WORKTREE_REMOVED = "WORKTREE_REMOVED"
    INTEGRATION_STARTED = "INTEGRATION_STARTED"
    MERGE_CONFLICT = "MERGE_CONFLICT"
    INTEGRATION_COMPLETED = "INTEGRATION_COMPLETED"
    # Idea-to-Product lifecycle events (mission_id carries the phase mission
    # when present; payload always includes product_project_id)
    PRODUCT_PROJECT_CREATED = "PRODUCT_PROJECT_CREATED"
    PRODUCT_STATUS_CHANGED = "PRODUCT_STATUS_CHANGED"
    PRODUCT_PLAN_READY = "PRODUCT_PLAN_READY"
    PRODUCT_PHASE_STARTED = "PRODUCT_PHASE_STARTED"
    PRODUCT_PHASE_COMPLETED = "PRODUCT_PHASE_COMPLETED"
    PRODUCT_GATE_CREATED = "PRODUCT_GATE_CREATED"
    PRODUCT_GATE_RESOLVED = "PRODUCT_GATE_RESOLVED"
    PRODUCT_ACCEPTANCE_RECORDED = "PRODUCT_ACCEPTANCE_RECORDED"
    PRODUCT_DELIVERED = "PRODUCT_DELIVERED"


# ---------------------------------------------------------------------------
# Pydantic API/domain models
# ---------------------------------------------------------------------------


class Project(BaseModel):
    id: str = Field(default_factory=new_id)
    name: str
    path: str
    detected_type: str = "unknown"
    created_at: datetime = Field(default_factory=utcnow)


class Mission(BaseModel):
    id: str = Field(default_factory=new_id)
    project_id: str
    title: str
    task: str
    status: MissionStatus = MissionStatus.CREATED
    current_phase: MissionStatus | None = None
    current_provider: str | None = None
    autonomy: Autonomy = Autonomy.BALANCED
    profile: str = "balanced"
    providers_used: list[str] = Field(default_factory=list)
    providers_failed: list[str] = Field(default_factory=list)
    repair_cycles: int = 0
    blocking_issue: str | None = None
    git_head: str | None = None
    scheduling_mode: SchedulingMode = SchedulingMode.SEQUENTIAL
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None


class TaskRecord(BaseModel):
    id: str = Field(default_factory=new_id)
    mission_id: str
    role: Role
    status: str = "pending"  # pending | running | completed | failed | skipped
    prompt: str = ""
    summary: str = ""
    attempts: int = 0
    created_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None


class ProviderRun(BaseModel):
    id: str = Field(default_factory=new_id)
    mission_id: str | None = None
    task_id: str | None = None
    provider: str
    role: str = ""
    command: list[str] = Field(default_factory=list)
    cwd: str = ""
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None
    exit_code: int | None = None
    failure_class: FailureClass = FailureClass.NONE
    provider_state: ProviderState = ProviderState.AVAILABLE
    stdout_path: str | None = None
    stderr_path: str | None = None
    git_commit_before: str | None = None
    git_commit_after: str | None = None
    summary: str = ""


class Event(BaseModel):
    id: str = Field(default_factory=new_id)
    mission_id: str | None = None
    type: EventType
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)


class Handoff(BaseModel):
    id: str = Field(default_factory=new_id)
    mission_id: str
    from_provider: str | None = None
    to_provider: str | None = None
    role: str = ""
    content: str = ""
    path: str | None = None
    git_head: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


class Checkpoint(BaseModel):
    id: str = Field(default_factory=new_id)
    mission_id: str | None = None
    project_id: str
    commit_sha: str
    message: str
    created_at: datetime = Field(default_factory=utcnow)


class HumanGate(BaseModel):
    id: str = Field(default_factory=new_id)
    mission_id: str
    reason: str
    detail: str = ""
    choices: list[str] = Field(default_factory=list)
    recommended: str | None = None
    status: str = "open"  # open | resolved
    resolution: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    resolved_at: datetime | None = None


class ReviewFinding(BaseModel):
    id: str = Field(default_factory=new_id)
    mission_id: str
    severity: Severity
    category: str = "general"
    file: str | None = None
    description: str
    recommended_fix: str = ""
    status: str = "open"  # open | resolved | wontfix
    created_at: datetime = Field(default_factory=utcnow)


class ProviderHealth(BaseModel):
    name: str
    state: ProviderState = ProviderState.UNAVAILABLE
    installed: bool = False
    executable_path: str | None = None
    version: str | None = None
    last_run_at: datetime | None = None
    last_error: str | None = None
    cooldown_until: datetime | None = None
    consecutive_failures: int = 0
    total_runs: int = 0
    successful_runs: int = 0
    rate_limit_events: int = 0
    total_runtime_seconds: float = 0.0

    @property
    def success_rate(self) -> float:
        return self.successful_runs / self.total_runs if self.total_runs else 0.0


# ---------------------------------------------------------------------------
# Phase 2A models
# ---------------------------------------------------------------------------


class TaskGraphTask(BaseModel):
    """A node in the mission task DAG."""

    id: str = Field(default_factory=new_id)
    mission_id: str
    title: str = ""
    description: str = ""
    task_type: str = "implementation"
    role: Role = Role.IMPLEMENTATION
    status: TaskStatus = TaskStatus.PENDING
    dependencies: list[str] = Field(default_factory=list)
    dependents: list[str] = Field(default_factory=list)
    preferred_providers: list[str] = Field(default_factory=list)
    assigned_provider: str | None = None
    workspace_scope: list[str] = Field(default_factory=list)
    resource_locks: list[str] = Field(default_factory=list)
    attempts: int = 0
    max_attempts: int = 3
    priority: int = 0
    created_at: datetime = Field(default_factory=utcnow)
    ready_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    provider_run_id: str | None = None
    checkpoint_before: str | None = None
    checkpoint_after: str | None = None
    result: dict[str, Any] = Field(default_factory=dict)
    blocking_issue: str | None = None
    dag_revision: int = 1


class DagRevision(BaseModel):
    id: str = Field(default_factory=new_id)
    mission_id: str
    revision: int
    changed_by: str = "planner"
    reason: str = ""
    tasks_added: list[str] = Field(default_factory=list)
    tasks_removed: list[str] = Field(default_factory=list)
    deps_changed: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)


class ProviderReservation(BaseModel):
    id: str = Field(default_factory=new_id)
    task_id: str
    provider: str
    reserved_at: datetime = Field(default_factory=utcnow)
    released_at: datetime | None = None
    run_id: str | None = None


class TaskLockRecord(BaseModel):
    id: str = Field(default_factory=new_id)
    task_id: str
    lock_type: LockType
    resource_key: str
    acquired_at: datetime = Field(default_factory=utcnow)
    released_at: datetime | None = None


class TaskBranch(BaseModel):
    id: str = Field(default_factory=new_id)
    task_id: str
    branch_name: str
    base_commit: str
    worktree_path: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    removed_at: datetime | None = None


class IntegrationRecord(BaseModel):
    id: str = Field(default_factory=new_id)
    mission_id: str
    status: IntegrationStatus = IntegrationStatus.PENDING
    branch_names: list[str] = Field(default_factory=list)
    conflict_files: list[str] = Field(default_factory=list)
    merged_commit: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    provider: str | None = None
    summary: str = ""
    created_at: datetime = Field(default_factory=utcnow)


class ProviderProfile(BaseModel):
    provider: str
    capability_scores: dict[str, float] = Field(default_factory=dict)
    average_duration: float = 0.0
    updated_at: datetime = Field(default_factory=utcnow)
