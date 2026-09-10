-- Exact-SHA evidence + immutable attempt lineage + writer provenance (Increment 3).
-- Additive only. Historical rows keep NULL/UNKNOWN provenance; nothing is backfilled.

-- Every code-affecting checkpoint linked to its producer (provider run, human,
-- or system). One row per producing commit; run_id unique where a provider ran.
CREATE TABLE IF NOT EXISTS write_provenance (
    id TEXT PRIMARY KEY,
    run_id TEXT UNIQUE,
    mission_id TEXT,
    task_id TEXT,
    product_project_id TEXT,
    phase_id TEXT,
    actor_type TEXT NOT NULL DEFAULT 'PROVIDER',
    actor_detail TEXT NOT NULL DEFAULT '',
    provider TEXT,
    role TEXT,
    base_sha TEXT,
    result_sha TEXT NOT NULL,
    tree_sha TEXT,
    dirty_before INTEGER NOT NULL DEFAULT 0,
    repo_key TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_write_prov_mission ON write_provenance(mission_id);
CREATE INDEX IF NOT EXISTS idx_write_prov_result ON write_provenance(result_sha);
CREATE INDEX IF NOT EXISTS idx_write_prov_product ON write_provenance(product_project_id);

-- Reviews are append-only per cycle; bind each to the exact reviewed range.
ALTER TABLE reviews ADD COLUMN reviewed_base_sha TEXT;
ALTER TABLE reviews ADD COLUMN reviewed_head_sha TEXT;
ALTER TABLE reviews ADD COLUMN writer_set_json TEXT NOT NULL DEFAULT '[]';

-- Findings keep origin + verified-resolution lineage as statuses evolve.
ALTER TABLE review_findings ADD COLUMN origin_review_id TEXT;
ALTER TABLE review_findings ADD COLUMN origin_sha TEXT;
ALTER TABLE review_findings ADD COLUMN resolved_review_id TEXT;
ALTER TABLE review_findings ADD COLUMN resolved_sha TEXT;

-- Immutable generic verification attempts (verify.py currently persists none).
CREATE TABLE IF NOT EXISTS verification_attempts (
    id TEXT PRIMARY KEY,
    mission_id TEXT,
    product_project_id TEXT,
    task_id TEXT,
    sha TEXT NOT NULL,
    repo_key TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'toolchain',
    commands_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL,
    exit_code INTEGER,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    summary TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_verify_attempts_scope ON verification_attempts(mission_id, sha);
CREATE INDEX IF NOT EXISTS idx_verify_attempts_product ON verification_attempts(product_project_id, sha);

-- Immutable criterion attempts; criterion_results remains the latest-status cache.
CREATE TABLE IF NOT EXISTS criterion_attempts (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    criterion_id TEXT NOT NULL,
    requirement_id TEXT NOT NULL DEFAULT '',
    plan_revision INTEGER,
    command TEXT NOT NULL DEFAULT '',
    checked_sha TEXT NOT NULL,
    context TEXT NOT NULL DEFAULT 'workdir',
    capability TEXT NOT NULL DEFAULT 'REPLAYABLE',
    result TEXT NOT NULL,
    exit_code INTEGER,
    output_tail TEXT NOT NULL DEFAULT '',
    recheck_of TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_criterion_attempts_lookup
    ON criterion_attempts(project_id, criterion_id, checked_sha);
ALTER TABLE criterion_results ADD COLUMN plan_revision INTEGER;
ALTER TABLE criterion_results ADD COLUMN context TEXT NOT NULL DEFAULT 'workdir';

-- Immutable fresh-checkout attempts (currently ephemeral).
CREATE TABLE IF NOT EXISTS fresh_checkout_attempts (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    sha TEXT NOT NULL,
    repo_key TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    commands_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fresh_attempts_lookup ON fresh_checkout_attempts(project_id, sha);

-- Immutable phase attempts; project_phases keeps the current/latest pointer.
CREATE TABLE IF NOT EXISTS project_phase_attempts (
    id TEXT PRIMARY KEY,
    phase_id TEXT NOT NULL,
    attempt_number INTEGER NOT NULL,
    mission_id TEXT,
    trigger TEXT NOT NULL DEFAULT 'INITIAL',
    status TEXT NOT NULL DEFAULT 'RUNNING',
    base_sha TEXT,
    result_sha TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    previous_attempt_id TEXT,
    UNIQUE (phase_id, attempt_number)
);
CREATE INDEX IF NOT EXISTS idx_phase_attempts_phase ON project_phase_attempts(phase_id);
