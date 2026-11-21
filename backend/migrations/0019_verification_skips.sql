-- Skipped-test accounting: a passing run with skipped tests is recorded as
-- such (dogfood 2026-09-12: 30 skipped security tests were logged as PASS).
-- NULL = not reported by the toolchain output (unknown, never zero).
ALTER TABLE verification_attempts ADD COLUMN skipped_tests INTEGER;
