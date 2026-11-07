-- Quality checks: a worker samples a film and measures what a person would see (blockiness, blur, interlacing, frozen frames, decode errors).
-- SyncHub keeps the raw numbers and works out the score, so the thresholds can change without a new worker. A file is only judged against itself,
-- never against an original, which is what VMAF needs. Results belong to one version of the file: size and modified time are stored with the check.
CREATE TABLE quality_check (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    library_file_id  INTEGER NOT NULL REFERENCES library_file (id) ON DELETE CASCADE,
    size_bytes       INTEGER,
    mtime            REAL,
    source           TEXT NOT NULL DEFAULT 'manual',      -- manual (a button), download (a new Radarr/Sonarr import), cleanup (the background pass)
    status           TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'done', 'failed', 'cancelled')),
    job_id           INTEGER,
    check_version    INTEGER,                              -- the scoring rules that produced score and grade
    score            REAL,                                 -- 0 to 100, higher is better
    grade            TEXT,                                 -- good, ok, poor, bad
    findings_json    TEXT,                                 -- [{"key", "severity", "text"}]
    metrics_json     TEXT,                                 -- what the worker measured, per slice
    samples          INTEGER,
    message          TEXT,
    requested_by     TEXT,
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now')),
    finished_at      TEXT,
    updated_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now'))
);
CREATE INDEX ix_quality_file ON quality_check (library_file_id, id);
CREATE INDEX ix_quality_status ON quality_check (status);

-- "Accept as is": a person looked at a flagged film and it stays. It only holds for the file as it was (size and modified time).
CREATE TABLE quality_accept (
    library_file_id  INTEGER PRIMARY KEY REFERENCES library_file (id) ON DELETE CASCADE,
    size_bytes       INTEGER,
    mtime            REAL,
    accepted_by      TEXT,
    accepted_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now'))
);
