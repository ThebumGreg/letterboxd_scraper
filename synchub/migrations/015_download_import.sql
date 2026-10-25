-- The download pipeline: a file Radarr or Sonarr just imported is looked at by a worker and, if the rules call for it, replaced in place.
-- One row per imported file. The row follows the file through: scanned -> (approved) -> converted and swapped in -> Radarr/Sonarr told to rescan.
CREATE TABLE download_import (
    id              INTEGER PRIMARY KEY,
    service         TEXT NOT NULL CHECK (service IN ('radarr', 'sonarr')),
    service_item_id INTEGER,                     -- Radarr movie id or Sonarr series id, for the rescan command
    title           TEXT,
    root_name       TEXT NOT NULL,               -- the storage root the file is in and its path in it
    relative_path   TEXT NOT NULL,
    path_key        TEXT NOT NULL,               -- library_file.path_key of the file
    event           TEXT,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'scanning', 'awaiting_approval', 'queued', 'converting', 'replaced', 'nothing_to_do', 'blocked', 'failed', 'skipped')),
    message         TEXT,                        -- why it is in this state, in words for the page
    scan_run_id     TEXT,
    record_id       INTEGER,                     -- library_file.id once the file is analysed
    job_id          INTEGER,
    size_bytes      INTEGER,                     -- size of the imported file, to recognise our own replacement coming back as an import
    result_json     TEXT,                        -- what the worker reported when it finished
    rescan_status   TEXT,                        -- NULL = not due, 'due', 'done', 'failed'
    rescan_message  TEXT,
    decided_by      TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now')),
    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now')),
    finished_at     TEXT
);
CREATE INDEX download_import_status ON download_import (status);
CREATE INDEX download_import_path ON download_import (path_key);
