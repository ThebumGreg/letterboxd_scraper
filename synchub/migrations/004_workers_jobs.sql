-- Worker PCs and the jobs they run. SyncHub decides and records; a worker asks for work and reports back over HTTP.
CREATE TABLE worker (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE,
    token_hash   TEXT UNIQUE,                       -- sha256 of the token; NULL once revoked (the token itself is never stored)
    token_hint   TEXT,                              -- last characters of the token, to tell tokens apart in the UI
    enabled      INTEGER NOT NULL DEFAULT 1,
    paused       INTEGER NOT NULL DEFAULT 0,        -- keeps its current jobs, takes no new ones
    draining     INTEGER NOT NULL DEFAULT 0,        -- finishes its jobs, then takes no more
    tags         TEXT NOT NULL DEFAULT '[]',        -- JSON list; a job can require tags
    slots        INTEGER NOT NULL DEFAULT 1,        -- jobs at once
    protocol     INTEGER,
    version      TEXT,
    os           TEXT,
    hostname     TEXT,
    capabilities TEXT NOT NULL DEFAULT '{}',        -- JSON: ffmpeg version, working encoders, cores...
    status       TEXT NOT NULL DEFAULT '{}',        -- JSON: the summary sent with the latest poll
    last_seen    TEXT,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now')),
    revoked_at   TEXT
);

CREATE TABLE job (
    id                  INTEGER PRIMARY KEY,
    kind                TEXT NOT NULL CHECK (kind IN ('scan', 'library', 'download', 'preview')),
    title               TEXT,
    ref_type            TEXT,                        -- what the job is about, e.g. 'library_file' or 'media'
    ref_id              INTEGER,
    source_root         TEXT,                        -- files are named by logical root + relative path, never by a machine's path
    source_rel          TEXT,
    dest_root           TEXT,
    dest_rel            TEXT,
    plan                TEXT NOT NULL DEFAULT '{}',  -- JSON, decided by SyncHub
    policy              TEXT NOT NULL DEFAULT '{}',  -- JSON: settings the worker needs (retention, preview length...)
    status              TEXT NOT NULL DEFAULT 'queued'
                        CHECK (status IN ('queued', 'running', 'needs_review', 'done', 'failed', 'cancelled', 'skipped')),
    stage               TEXT,
    progress            REAL,
    fps                 REAL,
    speed               REAL,
    eta_seconds         INTEGER,
    message             TEXT,
    priority            INTEGER NOT NULL DEFAULT 0,
    attempts            INTEGER NOT NULL DEFAULT 0,
    max_attempts        INTEGER NOT NULL DEFAULT 3,
    worker_id           INTEGER REFERENCES worker (id) ON DELETE SET NULL,
    preferred_worker_id INTEGER REFERENCES worker (id) ON DELETE SET NULL,   -- the worker that holds this job's files
    preferred_until     TEXT,                        -- other workers leave the job alone until then
    pinned_worker_id    INTEGER REFERENCES worker (id) ON DELETE SET NULL,   -- only this worker may run it
    required_tags       TEXT NOT NULL DEFAULT '[]',
    need_bytes          INTEGER,                     -- free space a worker needs to take it
    not_before          TEXT,                        -- retry back-off
    lease_until         TEXT,
    claimed_at          TEXT,
    cancel_requested    INTEGER NOT NULL DEFAULT 0,
    cleanup             INTEGER NOT NULL DEFAULT 0,  -- the holder may delete this job's local files
    review_reason       TEXT,
    review_detail       TEXT,                        -- JSON
    decision            TEXT,                        -- JSON: the latest human decision
    error_class         TEXT,
    error               TEXT,
    result              TEXT,                        -- JSON: sizes, verification, quality
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now')),
    started_at          TEXT,
    finished_at         TEXT,
    updated_at          TEXT
);
CREATE INDEX ix_job_claim ON job (status, priority DESC, id);
CREATE INDEX ix_job_worker ON job (worker_id) WHERE status = 'running';

CREATE TABLE job_attempt (
    id          INTEGER PRIMARY KEY,
    job_id      INTEGER NOT NULL REFERENCES job (id) ON DELETE CASCADE,
    attempt_no  INTEGER NOT NULL,
    worker_id   INTEGER REFERENCES worker (id) ON DELETE SET NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    outcome     TEXT,                                -- done, failed, worker_lost, cancelled, needs_review
    error_class TEXT,
    error       TEXT,
    command     TEXT,                                -- the exact ffmpeg command, when there was one
    log_tail    TEXT,
    UNIQUE (job_id, attempt_no)
);
