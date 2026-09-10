-- Acceptance integrity (F-LIFE-01 / F-LIFE-02).
-- Stable finding identity + explicit resolution evidence, criterion-level
-- acceptance results, and auditable acceptance waivers.

-- ---------------------------------------------------------------------------
-- 1. Finding identity and resolution evidence
-- ---------------------------------------------------------------------------
ALTER TABLE review_findings ADD COLUMN fingerprint TEXT NOT NULL DEFAULT '';
ALTER TABLE review_findings ADD COLUMN verified_by TEXT;
ALTER TABLE review_findings ADD COLUMN resolved_at TEXT;
CREATE INDEX idx_review_findings_fingerprint ON review_findings(mission_id, fingerprint, status);

-- ---------------------------------------------------------------------------
-- 2. Per-criterion acceptance results (one row per required criterion)
-- ---------------------------------------------------------------------------
CREATE TABLE criterion_results (
    project_id TEXT NOT NULL REFERENCES product_projects(id) ON DELETE CASCADE,
    criterion_id TEXT NOT NULL,
    requirement_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'PENDING',
    command TEXT NOT NULL DEFAULT '',
    exit_code INTEGER,
    output_tail TEXT NOT NULL DEFAULT '',
    sha TEXT NOT NULL DEFAULT '',
    checked_at TEXT NOT NULL,
    PRIMARY KEY (project_id, criterion_id)
);
CREATE INDEX idx_criterion_results_project ON criterion_results(project_id, status);

-- ---------------------------------------------------------------------------
-- 3. Auditable acceptance waivers (criteria or findings)
-- ---------------------------------------------------------------------------
CREATE TABLE acceptance_waivers (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES product_projects(id) ON DELETE CASCADE,
    target_kind TEXT NOT NULL,
    target_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT 'human',
    plan_revision INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE (project_id, target_kind, target_id)
);
CREATE INDEX idx_acceptance_waivers_project ON acceptance_waivers(project_id);
