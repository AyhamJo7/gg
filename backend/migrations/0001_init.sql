CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    path TEXT NOT NULL UNIQUE,
    detected_type TEXT NOT NULL DEFAULT 'unknown',
    created_at TEXT NOT NULL
);

CREATE TABLE missions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id),
    title TEXT NOT NULL,
    task TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'CREATED',
    current_phase TEXT,
    current_provider TEXT,
    autonomy TEXT NOT NULL DEFAULT 'BALANCED',
    profile TEXT NOT NULL DEFAULT 'balanced',
    providers_used TEXT NOT NULL DEFAULT '[]',
    providers_failed TEXT NOT NULL DEFAULT '[]',
    repair_cycles INTEGER NOT NULL DEFAULT 0,
    blocking_issue TEXT,
    git_head TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX idx_missions_project ON missions(project_id);
CREATE INDEX idx_missions_status ON missions(status);

CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL REFERENCES missions(id),
    role TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    prompt TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX idx_tasks_mission ON tasks(mission_id);

CREATE TABLE providers (
    name TEXT PRIMARY KEY,
    state TEXT NOT NULL DEFAULT 'UNAVAILABLE',
    installed INTEGER NOT NULL DEFAULT 0,
    executable_path TEXT,
    version TEXT,
    last_run_at TEXT,
    last_error TEXT,
    cooldown_until TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    total_runs INTEGER NOT NULL DEFAULT 0,
    successful_runs INTEGER NOT NULL DEFAULT 0,
    rate_limit_events INTEGER NOT NULL DEFAULT 0,
    total_runtime_seconds REAL NOT NULL DEFAULT 0
);

CREATE TABLE provider_runs (
    id TEXT PRIMARY KEY,
    mission_id TEXT,
    task_id TEXT,
    provider TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT '',
    command TEXT NOT NULL DEFAULT '[]',
    cwd TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    finished_at TEXT,
    exit_code INTEGER,
    failure_class TEXT NOT NULL DEFAULT 'NONE',
    provider_state TEXT NOT NULL DEFAULT 'AVAILABLE',
    stdout_path TEXT,
    stderr_path TEXT,
    git_commit_before TEXT,
    git_commit_after TEXT,
    summary TEXT NOT NULL DEFAULT ''
);
CREATE INDEX idx_runs_mission ON provider_runs(mission_id);
CREATE INDEX idx_runs_provider ON provider_runs(provider);

CREATE TABLE events (
    id TEXT PRIMARY KEY,
    mission_id TEXT,
    type TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX idx_events_mission ON events(mission_id);

CREATE TABLE handoffs (
    id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL REFERENCES missions(id),
    from_provider TEXT,
    to_provider TEXT,
    role TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    path TEXT,
    git_head TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_handoffs_mission ON handoffs(mission_id);

CREATE TABLE checkpoints (
    id TEXT PRIMARY KEY,
    mission_id TEXT,
    project_id TEXT NOT NULL,
    commit_sha TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE human_gates (
    id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL REFERENCES missions(id),
    reason TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    choices TEXT NOT NULL DEFAULT '[]',
    recommended TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    resolution TEXT,
    created_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE TABLE review_findings (
    id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL REFERENCES missions(id),
    severity TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'general',
    file TEXT,
    description TEXT NOT NULL,
    recommended_fix TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL
);
CREATE INDEX idx_findings_mission ON review_findings(mission_id);

CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
