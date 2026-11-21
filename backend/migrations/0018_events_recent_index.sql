-- Workspace activity feed reads the newest events across all missions.
CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at);
