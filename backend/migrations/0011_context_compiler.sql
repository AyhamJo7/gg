-- Context Compiler v2 (Increment 2): budgeted block manifests.
-- Additive only. Legacy manifests keep schema_version v1 with legacy_prompt blocks.

ALTER TABLE run_context_manifests ADD COLUMN budget_estimated_tokens INTEGER;
ALTER TABLE run_context_manifests ADD COLUMN used_estimated_tokens INTEGER;
ALTER TABLE run_context_manifests ADD COLUMN remaining_estimated_tokens INTEGER;
ALTER TABLE run_context_manifests ADD COLUMN repeated_context_ratio REAL;
ALTER TABLE run_context_manifests ADD COLUMN warnings_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE run_context_manifests ADD COLUMN plan_revision INTEGER;
