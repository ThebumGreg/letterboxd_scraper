-- Things done to a film that is already in the library, one row per film and kind:
--   variant    a second, smaller file next to the original (for example a 1080p copy), which Jellyfin shows as another version of the same film
--   dv_remux   the film's Dolby Vision profile 7 converted to profile 8.1 in place (the picture is not re-encoded; the original is kept on the worker)
-- A row with status 'skipped' and no job is a person saying "not this one", so the recommendation does not come back.
CREATE TABLE library_action (
    id            INTEGER PRIMARY KEY,
    kind          TEXT NOT NULL CHECK (kind IN ('variant', 'dv_remux')),
    path_key      TEXT NOT NULL,                 -- library_file.path_key of the film
    root_name     TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    label         TEXT NOT NULL DEFAULT '',      -- the version's name, such as 1080p; empty for a Dolby Vision conversion
    status        TEXT NOT NULL DEFAULT 'queued'
                  CHECK (status IN ('queued', 'converting', 'done', 'failed', 'blocked', 'skipped')),
    job_id        INTEGER,
    dest_rel      TEXT,                          -- where the new file goes (a version) or the file's own path (a conversion)
    options_json  TEXT,                          -- what the person chose for this film (height, ...)
    result_json   TEXT,
    message       TEXT,
    decided_by    TEXT,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now')),
    updated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now')),
    finished_at   TEXT
);
CREATE UNIQUE INDEX library_action_once ON library_action (kind, path_key, label);
CREATE INDEX library_action_job ON library_action (job_id);
