CREATE TABLE reviews (
    id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL REFERENCES missions(id),
    implementation_provider TEXT,
    review_provider TEXT NOT NULL,
    independent INTEGER NOT NULL DEFAULT 1,
    degradation_reason TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_reviews_mission ON reviews(mission_id);
