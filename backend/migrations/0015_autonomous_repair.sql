-- Bounded autonomous repair (Increment 4).
-- Additive only. Repair cycles bind immutable failure evidence to exact-SHA
-- repair attempts; historical criterion/verification/review rows are untouched
-- (no backfilled autonomous history is fabricated for old rows).

-- One autonomous repair cycle per trigger: identifiable independently of any
-- individual provider run (R-01..R-04).
CREATE TABLE IF NOT EXISTS repair_cycles (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES product_projects(id) ON DELETE CASCADE,
    phase_id TEXT REFERENCES project_phases(id) ON DELETE SET NULL,
    trigger_type TEXT NOT NULL,
    trigger_evidence_id TEXT NOT NULL,
    trigger_sha TEXT NOT NULL,
    repo_key TEXT NOT NULL DEFAULT '',
    target_requirement_id TEXT,
    target_criterion_id TEXT,
    target_finding_id TEXT,
    classification TEXT NOT NULL DEFAULT 'UNKNOWN',
    status TEXT NOT NULL DEFAULT 'CREATED',
    max_attempts INTEGER NOT NULL DEFAULT 2,
    attempts_used INTEGER NOT NULL DEFAULT 0,
    failure_signatures_json TEXT NOT NULL DEFAULT '[]',
    providers_used_json TEXT NOT NULL DEFAULT '[]',
    stop_reason TEXT,
    gate_hint TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_repair_cycles_project ON repair_cycles(project_id, status);
CREATE INDEX IF NOT EXISTS idx_repair_cycles_trigger ON repair_cycles(project_id, trigger_type, trigger_evidence_id, status);

-- Immutable repair attempts: one row per coding attempt, progressively
-- updated through repair -> review -> recheck stages so restart recovery can
-- resume without duplicating completed stages (R-32..R-35).
CREATE TABLE IF NOT EXISTS repair_attempts (
    id TEXT PRIMARY KEY,
    cycle_id TEXT NOT NULL REFERENCES repair_cycles(id) ON DELETE CASCADE,
    attempt_number INTEGER NOT NULL,
    provider TEXT NOT NULL DEFAULT '',
    provider_run_id TEXT REFERENCES provider_runs(id) ON DELETE SET NULL,
    base_sha TEXT NOT NULL,
    result_sha TEXT,
    outcome TEXT NOT NULL DEFAULT 'PREPARED',
    operational_failure INTEGER NOT NULL DEFAULT 0,
    touched_files_json TEXT NOT NULL DEFAULT '[]',
    review_reviewer TEXT,
    review_outcome TEXT,
    review_detail TEXT NOT NULL DEFAULT '',
    recheck_attempt_id TEXT,
    recheck_outcome TEXT,
    failure_signature TEXT,
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    finished_at TEXT,
    UNIQUE (cycle_id, attempt_number)
);
CREATE INDEX IF NOT EXISTS idx_repair_attempts_cycle ON repair_attempts(cycle_id, attempt_number);
