-- Jellyfin as the DVR. src_jellyfin_item remembers where each item lives and whether that is a recording folder (setting dvr_path_prefixes), so a
-- later change can treat a recording differently from a library file. Nothing reads is_dvr yet; every row starts as 0 and the next sync fills both columns.
ALTER TABLE src_jellyfin_item ADD COLUMN path TEXT;
ALTER TABLE src_jellyfin_item ADD COLUMN is_dvr INTEGER NOT NULL DEFAULT 0;

-- What Jellyfin has scheduled to record (Live TV timers and series timers), read from Jellyfin and never changed by SyncHub.
CREATE TABLE dvr_timer (
    timer_id        TEXT PRIMARY KEY,
    kind            TEXT NOT NULL CHECK (kind IN ('timer', 'series')),
    name            TEXT,
    channel_name    TEXT,
    program_id      TEXT,
    series_timer_id TEXT,
    status          TEXT,
    start_at        TEXT,
    end_at          TEXT,
    first_seen      TEXT NOT NULL,
    last_seen       TEXT NOT NULL,
    removed_at      TEXT
);
CREATE INDEX ix_dvr_timer_status ON dvr_timer (status);
