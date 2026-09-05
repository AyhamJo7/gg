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
    TEST_STARTED = "TEST_STARTED"
    TEST_FAILED = "TEST_FAILED"
    TEST_PASSED = "TEST_PASSED"
    REVIEW_FINDING_CREATED = "REVIEW_FINDING_CREATED"
    HUMAN_GATE_CREATED = "HUMAN_GATE_CREATED"
    HUMAN_GATE_RESOLVED = "HUMAN_GATE_RESOLVED"
    MISSION_COMPLETED = "MISSION_COMPLETED"
    MISSION_FAILED = "MISSION_FAILED"
    MISSION_PAUSED = "MISSION_PAUSED"
    MISSION_RESUMED = "MISSION_RESUMED"


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
