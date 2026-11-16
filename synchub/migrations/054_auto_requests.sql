-- Titles SyncHub itself asked Radarr/Sonarr for (the automatic requests), so it can count how many are still on their way,
-- pace new requests, and never ask twice for a title (one that was removed from Radarr/Sonarr on purpose stays removed).
CREATE TABLE auto_request (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    service      TEXT NOT NULL CHECK (service IN ('radarr', 'sonarr')),
    media_id     INTEGER NOT NULL REFERENCES media (id) ON DELETE CASCADE,
    title        TEXT,
    requested_at TEXT NOT NULL,
    UNIQUE (service, media_id)
);
CREATE INDEX ix_auto_request_when ON auto_request (service, requested_at);

-- Requests start as a dry run (a list of what would be asked for) until it is switched off by hand.
INSERT INTO app_setting (key, value) VALUES ('radarr_dry_run', '1'), ('sonarr_dry_run', '1')
    ON CONFLICT(key) DO UPDATE SET value = '1';
