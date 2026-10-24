-- The Library: the original analysis and migration tool's records, kept with its column names so its decision logic runs unchanged.
-- A file is identified by share name + relative path (path_key), never by a machine's path.

CREATE TABLE library_file (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    -- identity and source signature -------------------------------------------------------------------------------
    path_key TEXT NOT NULL UNIQUE,                 -- lower(root_name) | casefolded relative path
    root_name TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    source_path TEXT NOT NULL,                     -- for display: the share-style path
    source_dir TEXT NOT NULL,                      -- what was scanned when this was last seen
    file_name TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    mtime REAL NOT NULL,
    sha256 TEXT,
    partial_signature TEXT,                        -- first and last megabyte, to rule out duplicates cheaply
    source_revision INTEGER NOT NULL DEFAULT 1,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    last_seen_run_id TEXT,
    missing_since TEXT,
    signature_changed_at TEXT,

    -- analysis results ---------------------------------------------------------------------------------------------
    analysis_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (analysis_status IN ('pending', 'completed', 'incomplete', 'failed')),
    analysis_run_id TEXT, analyzed_at TEXT, analysis_logic_version INTEGER,
    integrity_mode TEXT, integrity_status TEXT, integrity_scope TEXT,
    audit_error TEXT, cache_status TEXT, unreadable_reason TEXT,
    container TEXT, duration_seconds REAL, bitrate_kbps REAL,
    video_codec TEXT, resolution TEXT, frame_rate REAL,
    audio_codecs TEXT, audio_languages TEXT, subtitle_languages TEXT,
    video_streams INTEGER, audio_streams INTEGER, subtitle_streams INTEGER,
    streams_json TEXT,
    media_type TEXT, recommended_name TEXT, show_title TEXT, season_number INTEGER,
    naming_confidence TEXT, naming_score REAL, naming_flags TEXT,
    duplicate_group TEXT, duplicate_status TEXT,
    default_audio_language TEXT, audio_detail TEXT, audio_flags TEXT, audio_recommendation TEXT,
    subtitle_detail TEXT, subtitle_flags TEXT, subtitle_recommendation TEXT,
    english_audio_status TEXT
        CHECK (english_audio_status IS NULL OR english_audio_status IN ('present', 'none', 'unknown', 'no_audio')),
    recommended_action TEXT
        CHECK (recommended_action IS NULL OR recommended_action IN ('copy_rename', 'remux_copy', 'transcode_copy', 'manual_review')),
    base_action TEXT
        CHECK (base_action IS NULL OR base_action IN ('copy_rename', 'remux_copy', 'transcode_copy')),
    action_reason TEXT,
    proposed_track_changes_json TEXT,
    decision_fingerprint TEXT,
    warnings_json TEXT, errors_json TEXT,
    review_categories TEXT, review_reason TEXT, review_reasons_json TEXT,
    findings_json TEXT,                            -- what the worker found about this one file, before findings that need other files

    -- review decision (analysis sets the initial value; a person sets 'reviewed') ---------------------------------------
    review_status TEXT NOT NULL DEFAULT 'not_required'
        CHECK (review_status IN ('not_required', 'manual_review', 'reviewed')),
    review_decided_at TEXT, review_decided_by TEXT, review_note TEXT,

    -- approval decision (people only) ---------------------------------------------------------------------------------
    approval_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (approval_status IN ('pending', 'approved', 'rejected')),
    approved_action TEXT
        CHECK (approved_action IS NULL OR approved_action IN ('copy_rename', 'remux_copy', 'transcode_copy')),
    approved_name TEXT,
    approved_track_changes_json TEXT,
    approval_decided_at TEXT, approval_decided_by TEXT, approval_note TEXT,

    -- migration state (written by the processing jobs, a later phase) ------------------------------------------------
    migration_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (migration_status IN ('pending', 'in_progress', 'completed', 'failed', 'manual_review')),
    migration_attempt_count INTEGER NOT NULL DEFAULT 0,
    migration_started_at TEXT, migration_finished_at TEXT,
    migration_destination_path TEXT, migration_last_error TEXT,
    migration_verification_status TEXT
        CHECK (migration_verification_status IS NULL
               OR migration_verification_status IN ('not_verified', 'verified', 'mismatch', 'error')),
    migration_verification_detail TEXT, migration_verified_at TEXT,
    migrated_source_revision INTEGER
);
CREATE INDEX ix_library_file_root ON library_file (root_name, relative_path);
CREATE INDEX ix_library_file_size ON library_file (size_bytes);
CREATE INDEX ix_library_file_sha256 ON library_file (sha256);
CREATE INDEX ix_library_file_dup ON library_file (duplicate_group);
CREATE INDEX ix_library_file_states ON library_file (approval_status, migration_status, review_status, analysis_status);

CREATE TABLE library_attempt (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    media_file_id INTEGER NOT NULL REFERENCES library_file (id) ON DELETE CASCADE,
    attempt_number INTEGER NOT NULL,
    source_revision INTEGER NOT NULL,
    action TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('in_progress', 'completed', 'failed', 'interrupted', 'manual_review')),
    job_id INTEGER REFERENCES job (id) ON DELETE SET NULL,
    worker TEXT,
    started_at TEXT NOT NULL, finished_at TEXT,
    destination_path TEXT, error TEXT, command TEXT,
    verification_status TEXT
        CHECK (verification_status IS NULL OR verification_status IN ('not_verified', 'verified', 'mismatch', 'error')),
    verification_detail TEXT,
    UNIQUE (media_file_id, attempt_number)
);

-- Every decision and every change the scans notice, for the audit trail.
CREATE TABLE library_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    media_file_id INTEGER NOT NULL REFERENCES library_file (id) ON DELETE CASCADE,
    created_at TEXT NOT NULL, actor TEXT NOT NULL, run_id TEXT,
    event_type TEXT NOT NULL, detail_json TEXT
);
CREATE INDEX ix_library_event_file ON library_event (media_file_id);

-- A scan (or a duplicate search) over a share or a folder in it.
CREATE TABLE library_run (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('scan', 'duplicates')),
    root_name TEXT,
    prefix TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'completed', 'completed_with_errors', 'failed', 'cancelled')),
    options_json TEXT NOT NULL DEFAULT '{}',
    counts_json TEXT NOT NULL DEFAULT '{}',
    error TEXT,
    started_by TEXT,
    created_at TEXT NOT NULL,
    finished_at TEXT
);
