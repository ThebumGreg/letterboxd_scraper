-- "The newest finished check of this kind for this file" is asked for every file whenever the Library's low-quality list is worked out. With only an index on the
-- file (and one on the status) SQLite could choose the status index, which walks every finished check once per file (36 s for 1,500 files and 1,200 checks).
-- This index answers it without reading the check rows at all, for one file or for all files in one ordered pass (all three leading columns are equalities).
CREATE INDEX IF NOT EXISTS ix_quality_mode_status_file ON quality_check (mode, status, library_file_id, id);
