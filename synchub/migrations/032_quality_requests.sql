-- "Request a better copy": what SyncHub asked Radarr to do about a low quality file, and what came of it. One row per request, so a repeat is visible
-- and a person can see which release was blocked.
CREATE TABLE quality_request (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    library_file_id  INTEGER NOT NULL REFERENCES library_file (id) ON DELETE CASCADE,
    service          TEXT NOT NULL DEFAULT 'radarr',
    service_item_id  TEXT,
    title            TEXT,
    mode             TEXT NOT NULL CHECK (mode IN ('auto', 'pick')),   -- auto: Radarr searches; pick: a person chose a release from the list
    bad_release      TEXT,                                              -- the release that was grabbed for the file, when Radarr's history still has it
    history_id       TEXT,
    blocklisted      INTEGER NOT NULL DEFAULT 0,
    chosen_release   TEXT,
    status           TEXT NOT NULL CHECK (status IN ('requested', 'grabbed', 'failed')),
    message          TEXT,
    requested_by     TEXT,
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now')),
    updated_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now'))
);
CREATE INDEX ix_quality_request_file ON quality_request (library_file_id, id);
