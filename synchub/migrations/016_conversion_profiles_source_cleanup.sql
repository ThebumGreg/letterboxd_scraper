-- Conversion profiles, rename only, a cancel that stays cancelled, source clean-up after a migration, and the Dashboard stages of a job.

-- A profile says how a file is converted: the video codec and quality, the audio bitrate and what happens to subtitles.
-- `move_only` leaves the file's contents alone whatever the analysis suggested. Built-in profiles cannot be changed or deleted.
CREATE TABLE conversion_profile (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE COLLATE NOCASE,
    description TEXT,
    video_codec TEXT NOT NULL DEFAULT 'keep' CHECK (video_codec IN ('keep', 'hevc', 'h264')),
    crf         INTEGER,                                   -- quality: lower is better and larger (x265 and x264 constant rate factor)
    preset      TEXT,                                      -- encoder speed: slower is smaller at the same quality
    ten_bit     INTEGER NOT NULL DEFAULT 0,                -- 10-bit video (HEVC only)
    audio_kbps  INTEGER NOT NULL DEFAULT 192,              -- bitrate of audio that has to be re-encoded
    subtitles   TEXT NOT NULL DEFAULT 'remove_non_english' CHECK (subtitles IN ('remove_non_english', 'keep_all')),
    move_only   INTEGER NOT NULL DEFAULT 0,
    builtin     INTEGER NOT NULL DEFAULT 0,
    created_by  TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now')),
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now'))
);
INSERT INTO conversion_profile (id, name, description, video_codec, crf, preset, ten_bit, audio_kbps, subtitles, move_only, builtin) VALUES
    (1, 'Standard',         'Keeps the video as it is (older formats become H.264) and removes audio and subtitles in other languages when English is there.', 'keep', NULL, NULL, 0, 192, 'remove_non_english', 0, 1),
    (2, 'Keep all subtitles', 'Like Standard, but every subtitle track stays.',                                                                              'keep', NULL, NULL, 0, 192, 'keep_all',           0, 1),
    (3, 'Move only',        'Copies the file exactly as it is. Nothing inside it is changed.',                                                              'keep', NULL, NULL, 0, 192, 'keep_all',           1, 1),
    (4, 'HEVC smaller',     'Re-encodes the video to HEVC 10-bit at the smallest size. Slowest to look at closely, saves the most space.',                 'hevc', 27, 'medium', 1, 160, 'remove_non_english', 0, 1),
    (5, 'HEVC balanced',    'Re-encodes the video to HEVC 10-bit. A good size for the quality.',                                                            'hevc', 23, 'medium', 1, 192, 'remove_non_english', 0, 1),
    (6, 'HEVC high quality', 'Re-encodes the video to HEVC 10-bit with very little loss. The largest of the three.',                                         'hevc', 20, 'slow',   1, 256, 'remove_non_english', 0, 1);

-- What a person chose for one file: a profile, or a rename that touches nothing else. NULL profile = the default profile at the time of queuing.
ALTER TABLE library_file ADD COLUMN approved_profile_id INTEGER;
ALTER TABLE library_file ADD COLUMN approved_mode TEXT CHECK (approved_mode IS NULL OR approved_mode IN ('rename_only'));

-- A cancelled job stays cancelled until someone approves or queues the file again.
ALTER TABLE library_file ADD COLUMN migration_cancelled_at TEXT;

-- Clean-up of the source file after a verified migration (and what to tell Radarr/Sonarr).
ALTER TABLE library_file ADD COLUMN migrated_dest_size INTEGER;
ALTER TABLE library_file ADD COLUMN migrated_dest_sha256 TEXT;
ALTER TABLE library_file ADD COLUMN migrated_how TEXT;            -- 'copy', 'convert' or 'rename': what the finished job did
ALTER TABLE library_file ADD COLUMN source_delete_status TEXT
    CHECK (source_delete_status IS NULL OR source_delete_status IN ('waiting', 'queued', 'deleted', 'held', 'failed'));
ALTER TABLE library_file ADD COLUMN source_delete_due_at TEXT;
ALTER TABLE library_file ADD COLUMN source_delete_message TEXT;
ALTER TABLE library_file ADD COLUMN source_deleted_at TEXT;
ALTER TABLE library_file ADD COLUMN arr_status TEXT
    CHECK (arr_status IS NULL OR arr_status IN ('waiting', 'due', 'done', 'skipped', 'failed'));
ALTER TABLE library_file ADD COLUMN arr_message TEXT;
CREATE INDEX ix_library_file_source_delete ON library_file (source_delete_status, source_delete_due_at);
CREATE INDEX ix_library_file_arr ON library_file (arr_status);

-- Dashboard stages while a worker handles a file. `transferring` and `encoding` already exist; they are renamed to say what happens.
UPDATE state_def SET label = 'Transferring to Worker' WHERE code = 'transferring';
UPDATE state_def SET label = 'Converting' WHERE code = 'encoding';
INSERT INTO state_def (code, label, rank, color, icon, is_terminal) VALUES
    ('working',               'Working',                 45, 'warning', 'bi-gear',               0),
    ('transferring_library',  'Transferring to Library', 60, 'warning', 'bi-box-arrow-in-right', 0);
