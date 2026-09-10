-- Bind acceptance waivers to the content of the criterion/finding they excuse
-- (F-LIFE-01 hardening): a plan revision that reuses a criterion id for a
-- different check must not silently inherit an old waiver.
ALTER TABLE acceptance_waivers ADD COLUMN content_hash TEXT NOT NULL DEFAULT '';
