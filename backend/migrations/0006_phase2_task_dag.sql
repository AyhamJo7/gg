-- Phase 2A: Persistent Task DAG + Parallel Scheduling
-- Adds task graph, resource locks, provider reservations, worktree tracking,
-- integration records, and DAG revision history.

-- ---------------------------------------------------------------------------
-- 1. Extend existing tasks table with Phase 2 columns
-- ---------------------------------------------------------------------------
ALTER TABLE tasks ADD COLUMN title TEXT DEFAULT '';
ALTER TABLE tasks ADD COLUMN task_type TEXT DEFAULT 'phase';
ALTER TABLE tasks ADD COLUMN description TEXT DEFAULT '';
ALTER TABLE tasks ADD COLUMN preferred_providers TEXT DEFAULT '[]';
ALTER TABLE tasks ADD COLUMN assigned_provider TEXT;
ALTER TABLE tasks ADD COLUMN workspace_scope TEXT DEFAULT '[]';
ALTER TABLE tasks ADD COLUMN resource_locks TEXT DEFAULT '[]';
ALTER TABLE tasks ADD COLUMN max_attempts INTEGER NOT NULL DEFAULT 3;
ALTER TABLE tasks ADD COLUMN priority INTEGER NOT NULL DEFAULT 0;
ALTER TABLE tasks ADD COLUMN ready_at TEXT;
ALTER TABLE tasks ADD COLUMN started_at TEXT;
ALTER TABLE tasks ADD COLUMN provider_run_id TEXT;
ALTER TABLE tasks ADD COLUMN checkpoint_before TEXT;
ALTER TABLE tasks ADD COLUMN checkpoint_after TEXT;
ALTER TABLE tasks ADD COLUMN result TEXT DEFAULT '{}';
ALTER TABLE tasks ADD COLUMN blocking_issue TEXT;
ALTER TABLE tasks ADD COLUMN dag_revision INTEGER NOT NULL DEFAULT 1;

-- ---------------------------------------------------------------------------
-- 2. Task dependencies (edge table for the DAG)
-- ---------------------------------------------------------------------------
CREATE TABLE task_dependencies (
    from_task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    to_task_id   TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (from_task_id, to_task_id)
);
CREATE INDEX idx_task_deps_from ON task_dependencies(from_task_id);
CREATE INDEX idx_task_deps_to   ON task_dependencies(to_task_id);

-- ---------------------------------------------------------------------------
-- 3. Persistent resource locks
-- ---------------------------------------------------------------------------
CREATE TABLE task_locks (
    id          TEXT PRIMARY KEY,
    task_id     TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    lock_type   TEXT NOT NULL,
    resource_key TEXT NOT NULL,
    acquired_at TEXT NOT NULL,
    released_at TEXT
);
CREATE INDEX idx_locks_task     ON task_locks(task_id);
CREATE INDEX idx_locks_resource ON task_locks(resource_key, released_at);
CREATE INDEX idx_locks_active   ON task_locks(resource_key) WHERE released_at IS NULL;

-- ---------------------------------------------------------------------------
-- 4. Provider reservations (atomic concurrency control)
-- ---------------------------------------------------------------------------
CREATE TABLE provider_reservations (
    id          TEXT PRIMARY KEY,
    task_id     TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    provider    TEXT NOT NULL,
    reserved_at TEXT NOT NULL,
    released_at TEXT,
    run_id      TEXT
);
CREATE INDEX idx_reservations_provider ON provider_reservations(provider, released_at);
CREATE INDEX idx_reservations_active   ON provider_reservations(provider) WHERE released_at IS NULL;

-- ---------------------------------------------------------------------------
-- 5. DAG revisions (audit trail of graph mutations)
-- ---------------------------------------------------------------------------
CREATE TABLE dag_revisions (
    id          TEXT PRIMARY KEY,
    mission_id  TEXT NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
    revision    INTEGER NOT NULL,
    changed_by  TEXT NOT NULL DEFAULT 'planner',
    reason      TEXT NOT NULL DEFAULT '',
    tasks_added TEXT NOT NULL DEFAULT '[]',
    tasks_removed TEXT NOT NULL DEFAULT '[]',
    deps_changed TEXT NOT NULL DEFAULT '[]',
    created_at  TEXT NOT NULL,
    UNIQUE(mission_id, revision)
);
CREATE INDEX idx_dag_revisions_mission ON dag_revisions(mission_id);

-- ---------------------------------------------------------------------------
-- 6. Task branches / worktrees
-- ---------------------------------------------------------------------------
CREATE TABLE task_branches (
    id          TEXT PRIMARY KEY,
    task_id     TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    branch_name TEXT NOT NULL,
    base_commit TEXT NOT NULL,
    worktree_path TEXT,
    created_at  TEXT NOT NULL,
    removed_at  TEXT
);
CREATE INDEX idx_branches_task ON task_branches(task_id);

-- ---------------------------------------------------------------------------
-- 7. Integration records
-- ---------------------------------------------------------------------------
CREATE TABLE task_integrations (
    id          TEXT PRIMARY KEY,
    mission_id  TEXT NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
    status      TEXT NOT NULL DEFAULT 'pending',
    branch_names TEXT NOT NULL DEFAULT '[]',
    conflict_files TEXT DEFAULT '[]',
    merged_commit TEXT,
    started_at  TEXT,
    finished_at TEXT,
    provider    TEXT,
    summary     TEXT DEFAULT '',
    created_at  TEXT NOT NULL
);
CREATE INDEX idx_integrations_mission ON task_integrations(mission_id);

-- ---------------------------------------------------------------------------
-- 8. Provider specialization / capability scores (JSON blob per provider)
-- ---------------------------------------------------------------------------
CREATE TABLE provider_profiles (
    provider    TEXT PRIMARY KEY REFERENCES providers(name) ON DELETE CASCADE,
    capability_scores TEXT NOT NULL DEFAULT '{}',
    average_duration REAL NOT NULL DEFAULT 0.0,
    updated_at  TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- 9. Mission scheduling mode (sequential vs parallel-safe DAG)
-- ---------------------------------------------------------------------------
ALTER TABLE missions ADD COLUMN scheduling_mode TEXT DEFAULT 'SEQUENTIAL';
