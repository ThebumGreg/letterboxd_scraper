-- Compression test: a few short clips of a film are encoded with the planned conversion profile and scored with VMAF on a worker, before any real conversion.
-- Only numbers are kept (per clip: VMAF and bitrates). A result belongs to the file as it was (size and modified time) and to the profile that was tested.
CREATE TABLE compression_test (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    library_file_id  INTEGER NOT NULL REFERENCES library_file (id) ON DELETE CASCADE,
    size_bytes       INTEGER,
    mtime            REAL,
    profile_key      TEXT,                                 -- profile name, quality and speed: a different one makes the result stale
    profile_name     TEXT,
    status           TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'done', 'failed', 'cancelled')),
    job_id           INTEGER,
    clips            INTEGER,
    clip_seconds     INTEGER,
    margin           REAL,                                 -- how far under the floor the worst clip may be and still pass
    from_gb          REAL,
    est_gb           REAL,                                 -- estimated size of the whole converted file
    mean_vmaf        REAL,
    min_vmaf         REAL,
    floor            REAL,
    verdict          TEXT,                                 -- pass, borderline, fail; empty when it could not be scored (HDR, no libvmaf)
    result_json      TEXT,                                 -- what the worker reported, per clip
    message          TEXT,
    requested_by     TEXT,
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now')),
    finished_at      TEXT,
    updated_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now'))
);
CREATE INDEX ix_compression_test_file ON compression_test (library_file_id, id);
CREATE INDEX ix_compression_test_status ON compression_test (status);
