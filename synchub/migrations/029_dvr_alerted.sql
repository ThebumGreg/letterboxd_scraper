-- Each scheduled recording is checked once, after it ends, to see whether it went wrong (clash, never appeared, much shorter than scheduled).
-- `alerted` holds what was found (ok, full, short, missing, conflict, cancelled, ...) so a recording is never reported twice.
-- Timers that are already over are marked old now, so the first check does not report history.
ALTER TABLE dvr_timer ADD COLUMN alerted TEXT;
UPDATE dvr_timer SET alerted = 'old' WHERE removed_at IS NOT NULL OR status IN ('Error', 'ConflictedNotOk');
