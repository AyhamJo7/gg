-- Durable product-level pause (autonomous repair worker, W-10).
-- Additive only. pause_project sets paused=1; resume_project clears it.
-- The repair worker never launches new attempts for paused projects; after
-- resume, durable cycles may continue. Existing rows read 0 (unpaused).
ALTER TABLE product_projects ADD COLUMN paused INTEGER NOT NULL DEFAULT 0;
