-- Hidden files: a file of a film that was renamed on its share so Jellyfin, Radarr and Sonarr ignore it (an exact duplicate, an old version, or another reason).
-- library_file.dup_review = 'aside' marks the file as hidden; these columns say why and since when, and whether the day-before notice about deleting it went out.
ALTER TABLE library_file ADD COLUMN hidden_reason TEXT;       -- duplicate | old_version | other
ALTER TABLE library_file ADD COLUMN hidden_at TEXT;
ALTER TABLE library_file ADD COLUMN hidden_notice_at TEXT;
-- the person's own choice "this is an old version", set as soon as it is chosen (before the worker has renamed the file)
ALTER TABLE library_file ADD COLUMN is_old_version INTEGER NOT NULL DEFAULT 0;
UPDATE library_file SET hidden_reason = 'duplicate', hidden_at = datetime('now') WHERE dup_review = 'aside' AND hidden_reason IS NULL;
