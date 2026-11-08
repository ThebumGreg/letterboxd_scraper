-- "Check first, then convert": a conversion that was approved waits here until the file's quality check (and damage check when that pass is on) has finished.
-- A passing file then gets its conversion job; a low quality or damaged one is held and gets none.
CREATE TABLE convert_wait (
    library_file_id INTEGER PRIMARY KEY REFERENCES library_file(id) ON DELETE CASCADE,
    created_at      TEXT NOT NULL,
    requested_by    TEXT,
    quality_check_id INTEGER,
    damage_check_id  INTEGER
);
