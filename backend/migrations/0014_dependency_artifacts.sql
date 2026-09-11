-- Deterministic DAG dependency execution correctness (Increment 3B).
-- Additive only. Historical tasks keep NULL input/result SHAs (UNKNOWN, never
-- backfilled); historical provider runs keep their existing linkage.

-- Pinned input + immutable result per task attempt (current attempt; full
-- per-run history lives in write_provenance + provider_runs.retry_of chain).
ALTER TABLE tasks ADD COLUMN input_sha TEXT;
ALTER TABLE tasks ADD COLUMN result_sha TEXT;

-- Mission DAG base: the exact artifact root tasks execute against.
ALTER TABLE missions ADD COLUMN dag_base_sha TEXT;

-- Dependency input preparation record: one row per preparation; retries and
-- restarts match by content (task + dependency SHAs) and reuse verified rows
-- instead of stacking redundant merges.
CREATE TABLE IF NOT EXISTS task_dependency_inputs (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    mission_id TEXT NOT NULL,
    attempt_number INTEGER NOT NULL DEFAULT 0,
    dependency_set_json TEXT NOT NULL DEFAULT '[]',
    input_sha TEXT NOT NULL,
    input_tree_sha TEXT,
    integration_sha TEXT,
    status TEXT NOT NULL DEFAULT 'READY',
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dep_inputs_task ON task_dependency_inputs(task_id, created_at);
