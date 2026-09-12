-- Retry finding lineage (DOG-02).
-- Additive only. A retry mission inherits awareness of unresolved ancestor
-- findings without mutating historical rows. Inherited copies carry the
-- parent mission/finding identity explicitly; fingerprint dedupes
-- rediscovery so history stays auditable and retry chains do not multiply.
ALTER TABLE review_findings ADD COLUMN inherited_from_mission_id TEXT;
ALTER TABLE review_findings ADD COLUMN inherited_from_finding_id TEXT;
CREATE INDEX IF NOT EXISTS idx_review_findings_inherited
    ON review_findings(mission_id, fingerprint, status);
