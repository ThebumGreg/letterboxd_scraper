-- AI quality test: a person picks a file and runs the usual check, an AI model check, or both, to compare them. Results are not stored here:
-- the page reads the standard check from quality_check and the AI numbers from the job, so there is one place for each. Nothing here changes a flag.
CREATE TABLE quality_ai_test (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    library_file_id  INTEGER NOT NULL REFERENCES library_file (id) ON DELETE CASCADE,
    mode             TEXT NOT NULL CHECK (mode IN ('standard', 'ai', 'both')),
    standard_check_id INTEGER,
    ai_job_id        INTEGER,
    requested_by     TEXT,
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now'))
);
CREATE INDEX ix_quality_ai_test_file ON quality_ai_test (library_file_id, id);
