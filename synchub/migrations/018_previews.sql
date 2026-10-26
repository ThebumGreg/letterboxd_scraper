-- Preview clips (docs/DESIGN_ENCODING.md section 12): a worker cuts a short window from a film, encodes it with the planned settings and makes the
-- matching untouched clip; a person watches both before a compression goes ahead. The files live in SyncHub's data folder (previews/<id>/).
CREATE TABLE preview (
    id              INTEGER PRIMARY KEY,
    subject_type    TEXT NOT NULL CHECK (subject_type IN ('download_import', 'library_file')),
    subject_id      INTEGER NOT NULL,
    record_id       INTEGER,
    title           TEXT,
    mode            TEXT NOT NULL DEFAULT 'random' CHECK (mode IN ('random', 'worst')),
    seconds         INTEGER NOT NULL,
    target_gb       REAL,                         -- the target the clip was made for
    signature       TEXT,                         -- kind of file, for "ask until I have approved K similar ones"
    job_id          INTEGER,
    status          TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'making', 'ready', 'failed', 'cancelled', 'expired')),
    message         TEXT,
    start_seconds   REAL,
    encoded_bytes   INTEGER,
    source_bytes    INTEGER,
    stats_json      TEXT,                         -- what the worker measured, and the size predicted for the whole film
    decision        TEXT,                         -- approve, retarget, skip, reject, another
    decided_by      TEXT,
    decided_at      TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now')),
    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now')),
    expires_at      TEXT
);
CREATE INDEX preview_subject ON preview (subject_type, subject_id);
CREATE INDEX preview_status ON preview (status);

-- A different size target chosen for one film when approving it from the review screen.
ALTER TABLE library_file ADD COLUMN size_target_gb REAL;

-- How many files of one kind (resolution class, codec family, HDR type, bit depth, size band) were approved from a preview without changes.
CREATE TABLE preview_trust (
    signature   TEXT PRIMARY KEY,
    approved    INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now'))
);
