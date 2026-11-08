-- Following a better-copy request through Radarr's download queue: searching, queued, downloading (with progress), importing, imported or nothing found.
ALTER TABLE quality_request ADD COLUMN track_state TEXT;
ALTER TABLE quality_request ADD COLUMN track_detail TEXT;
ALTER TABLE quality_request ADD COLUMN track_percent INTEGER;
ALTER TABLE quality_request ADD COLUMN track_eta TEXT;
ALTER TABLE quality_request ADD COLUMN track_seen INTEGER NOT NULL DEFAULT 0;     -- 1 once Radarr's queue has shown the film
ALTER TABLE quality_request ADD COLUMN tracked_at TEXT;

-- A "damage check" reads the whole file and counts decode errors; it has no score, so it is kept apart from the scored checks by this column.
ALTER TABLE quality_check ADD COLUMN mode TEXT NOT NULL DEFAULT 'quality';
