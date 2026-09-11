-- Repository-scoped evidence (Increment 3 seal, F-PROV-05).
-- Additive only. Historical rows keep ''/NULL repo identity and therefore
-- cannot certify a current exact artifact (LEGACY/UNKNOWN); nothing is backfilled.

-- Reviews bind to the repository they inspected.
ALTER TABLE reviews ADD COLUMN repo_key TEXT NOT NULL DEFAULT '';
-- Stable safe writer representation alongside the compat provider-name list.
ALTER TABLE reviews ADD COLUMN writer_detail_json TEXT NOT NULL DEFAULT '[]';

-- Criterion evidence binds to the repository it executed in.
ALTER TABLE criterion_attempts ADD COLUMN repo_key TEXT NOT NULL DEFAULT '';
ALTER TABLE criterion_results ADD COLUMN repo_key TEXT NOT NULL DEFAULT '';
