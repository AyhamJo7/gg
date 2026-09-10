-- Invocation observability (Increment 1): durable invocation boundary.
-- Additive only. Historical rows keep NULL/UNKNOWN for new telemetry.

-- Extend provider_runs with attribution, stage, lifecycle, model provenance.
ALTER TABLE provider_runs ADD COLUMN product_project_id TEXT REFERENCES product_projects(id) ON DELETE SET NULL;
ALTER TABLE provider_runs ADD COLUMN phase_id TEXT REFERENCES project_phases(id) ON DELETE SET NULL;
ALTER TABLE provider_runs ADD COLUMN operation_id TEXT;
ALTER TABLE provider_runs ADD COLUMN attempt_number INTEGER NOT NULL DEFAULT 1;
ALTER TABLE provider_runs ADD COLUMN retry_of_run_id TEXT REFERENCES provider_runs(id) ON DELETE SET NULL;
ALTER TABLE provider_runs ADD COLUMN stage TEXT NOT NULL DEFAULT '';
ALTER TABLE provider_runs ADD COLUMN run_status TEXT NOT NULL DEFAULT '';
ALTER TABLE provider_runs ADD COLUMN model_requested TEXT;
ALTER TABLE provider_runs ADD COLUMN model_observed TEXT;
ALTER TABLE provider_runs ADD COLUMN effort_requested TEXT;
ALTER TABLE provider_runs ADD COLUMN cli_version TEXT;
ALTER TABLE provider_runs ADD COLUMN session_ref TEXT;
ALTER TABLE provider_runs ADD COLUMN duration_ms INTEGER;
ALTER TABLE provider_runs ADD COLUMN prompt_template_version TEXT NOT NULL DEFAULT '';
ALTER TABLE provider_runs ADD COLUMN context_policy_version TEXT NOT NULL DEFAULT '';
ALTER TABLE provider_runs ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_runs_product ON provider_runs(product_project_id);
CREATE INDEX IF NOT EXISTS idx_runs_phase ON provider_runs(phase_id);
CREATE INDEX IF NOT EXISTS idx_runs_operation ON provider_runs(operation_id);
CREATE INDEX IF NOT EXISTS idx_runs_stage ON provider_runs(stage);
CREATE INDEX IF NOT EXISTS idx_runs_status ON provider_runs(run_status);
CREATE INDEX IF NOT EXISTS idx_runs_started ON provider_runs(started_at);

-- One safe context manifest per invocation (metadata only, no raw secrets).
CREATE TABLE IF NOT EXISTS run_context_manifests (
    run_id TEXT PRIMARY KEY REFERENCES provider_runs(id) ON DELETE CASCADE,
    schema_version TEXT NOT NULL DEFAULT 'v1',
    prompt_hash TEXT NOT NULL DEFAULT '',
    hash_basis TEXT NOT NULL DEFAULT 'redacted_rendered_utf8',
    prompt_chars INTEGER NOT NULL DEFAULT 0,
    prompt_bytes INTEGER NOT NULL DEFAULT 0,
    prompt_words INTEGER NOT NULL DEFAULT 0,
    estimated_prompt_tokens INTEGER,
    estimator_id TEXT NOT NULL DEFAULT 'char4-v1',
    blocks_json TEXT NOT NULL DEFAULT '[]',
    capture_status TEXT NOT NULL DEFAULT 'CAPTURED',
    redaction_status TEXT NOT NULL DEFAULT 'REDACTED',
    created_at TEXT NOT NULL
);

-- One normalized usage aggregate per invocation. NULL means unknown.
CREATE TABLE IF NOT EXISTS run_usage (
    run_id TEXT PRIMARY KEY REFERENCES provider_runs(id) ON DELETE CASCADE,
    input_tokens_total INTEGER CHECK (input_tokens_total IS NULL OR input_tokens_total >= 0),
    output_tokens_total INTEGER CHECK (output_tokens_total IS NULL OR output_tokens_total >= 0),
    cache_read_input_tokens INTEGER CHECK (cache_read_input_tokens IS NULL OR cache_read_input_tokens >= 0),
    cache_write_input_tokens INTEGER CHECK (cache_write_input_tokens IS NULL OR cache_write_input_tokens >= 0),
    reasoning_output_tokens INTEGER CHECK (reasoning_output_tokens IS NULL OR reasoning_output_tokens >= 0),
    native_total_tokens INTEGER CHECK (native_total_tokens IS NULL OR native_total_tokens >= 0),
    output_text_tokens_estimated INTEGER CHECK (output_text_tokens_estimated IS NULL OR output_text_tokens_estimated >= 0),
    estimator_id TEXT,
    source TEXT NOT NULL DEFAULT 'UNKNOWN',
    completeness TEXT NOT NULL DEFAULT 'UNKNOWN',
    input_basis TEXT NOT NULL DEFAULT 'UNKNOWN',
    output_basis TEXT NOT NULL DEFAULT 'UNKNOWN',
    parser_version TEXT NOT NULL DEFAULT '',
    evidence_kind TEXT NOT NULL DEFAULT '',
    observations_count INTEGER NOT NULL DEFAULT 0,
    native_counts_json TEXT NOT NULL DEFAULT '{}',
    requested_model TEXT,
    observed_model TEXT,
    captured_at TEXT NOT NULL
);

-- Capacity ownership: one active lease per invocation at most.
CREATE TABLE IF NOT EXISTS invocation_leases (
    run_id TEXT PRIMARY KEY REFERENCES provider_runs(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    released_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_leases_provider_active ON invocation_leases(provider) WHERE released_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_leases_active ON invocation_leases(released_at) WHERE released_at IS NULL;

-- Durable long operations (initially product planning). One active op per product+kind.
CREATE TABLE IF NOT EXISTS orchestration_operations (
    id TEXT PRIMARY KEY,
    product_project_id TEXT NOT NULL REFERENCES product_projects(id) ON DELETE CASCADE,
    kind TEXT NOT NULL DEFAULT 'PLAN',
    state TEXT NOT NULL DEFAULT 'RUNNING',
    expected_plan_revision INTEGER NOT NULL DEFAULT 0,
    current_run_id TEXT REFERENCES provider_runs(id) ON DELETE SET NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    error_code TEXT,
    error_detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_operation
    ON orchestration_operations(product_project_id, kind) WHERE finished_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_operations_product ON orchestration_operations(product_project_id, finished_at);
