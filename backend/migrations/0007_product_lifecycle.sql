-- Idea-to-Product Lifecycle: project coordinator above the mission engine.
-- Durable project state, versioned plans, phase tracking with mission
-- traceability, project-level human gates, and requirement evidence.

-- ---------------------------------------------------------------------------
-- 1. Product projects (the lifecycle entity; distinct from repo `projects`)
-- ---------------------------------------------------------------------------
CREATE TABLE product_projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    idea TEXT NOT NULL,
    constraints_text TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'DRAFT',
    acceptance_state TEXT NOT NULL DEFAULT '',
    auto_execute INTEGER NOT NULL DEFAULT 0,
    require_plan_approval INTEGER NOT NULL DEFAULT 1,
    target_project_id TEXT REFERENCES projects(id) ON DELETE SET NULL,
    target_repo_path TEXT NOT NULL DEFAULT '',
    plan_revision INTEGER NOT NULL DEFAULT 0,
    blocking_reason TEXT,
    delivery_sha TEXT,
    delivery_report TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX idx_product_projects_state ON product_projects(state);

-- ---------------------------------------------------------------------------
-- 2. Immutable plan revisions
-- ---------------------------------------------------------------------------
CREATE TABLE plan_revisions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES product_projects(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL,
    plan_json TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT 'planner',
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE (project_id, revision)
);
CREATE INDEX idx_plan_revisions_project ON plan_revisions(project_id, revision);

-- ---------------------------------------------------------------------------
-- 3. Project phases (roadmap steps; each maps to at most one mission)
-- ---------------------------------------------------------------------------
CREATE TABLE project_phases (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES product_projects(id) ON DELETE CASCADE,
    phase_key TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    goal TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'PENDING',
    mission_id TEXT REFERENCES missions(id) ON DELETE SET NULL,
    depends_on TEXT NOT NULL DEFAULT '[]',
    acceptance_json TEXT NOT NULL DEFAULT '[]',
    evidence_json TEXT NOT NULL DEFAULT '{}',
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 2,
    blocking_issue TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (project_id, phase_key)
);
CREATE INDEX idx_project_phases_project ON project_phases(project_id, status);

-- ---------------------------------------------------------------------------
-- 4. Project-level human gates (mirror mission gates or originate here)
-- ---------------------------------------------------------------------------
CREATE TABLE project_gates (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES product_projects(id) ON DELETE CASCADE,
    phase_id TEXT REFERENCES project_phases(id) ON DELETE SET NULL,
    mission_gate_id TEXT REFERENCES human_gates(id) ON DELETE SET NULL,
    gate_type TEXT NOT NULL DEFAULT 'action',
    title TEXT NOT NULL,
    what_required TEXT NOT NULL DEFAULT '',
    why_required TEXT NOT NULL DEFAULT '',
    blocked_ref TEXT NOT NULL DEFAULT '',
    completed_so_far TEXT NOT NULL DEFAULT '',
    human_action TEXT NOT NULL DEFAULT '',
    where_to_provide TEXT NOT NULL DEFAULT '',
    validation TEXT NOT NULL DEFAULT '',
    after_resolve TEXT NOT NULL DEFAULT '',
    required_vars TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'open',
    resolution TEXT,
    created_at TEXT NOT NULL,
    resolved_at TEXT
);
CREATE INDEX idx_project_gates_project ON project_gates(project_id, status);

-- ---------------------------------------------------------------------------
-- 5. Requirement acceptance evidence (requirement -> evidence traceability)
-- ---------------------------------------------------------------------------
CREATE TABLE requirement_evidence (
    project_id TEXT NOT NULL REFERENCES product_projects(id) ON DELETE CASCADE,
    requirement_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    evidence_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (project_id, requirement_id)
);
