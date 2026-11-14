-- Which film a Library file belongs to, and what the file is to the film (docs: Files section of the film page).
-- media_id is saved the first time a film's files are found (Jellyfin path, same folder, or name) so Home and Library can count and filter without guessing again.
-- media_link_source: 'auto' (found by SyncHub, may be recomputed) or 'manual' (a person said so or said "not this film": never changed by a scan).
ALTER TABLE library_file ADD COLUMN media_id INTEGER;
ALTER TABLE library_file ADD COLUMN media_link_source TEXT;
ALTER TABLE library_file ADD COLUMN edition_name TEXT;
ALTER TABLE library_file ADD COLUMN is_edition INTEGER NOT NULL DEFAULT 0;
ALTER TABLE library_file ADD COLUMN is_main INTEGER NOT NULL DEFAULT 0;
-- NULL, 'keep_both' or 'review_later' (a person's note on an exact duplicate), or 'aside' (the file was renamed out of the way)
ALTER TABLE library_file ADD COLUMN dup_review TEXT;
-- duplicate, mobile, converted, original, edition or unexplained; NULL when the film has only one file
ALTER TABLE library_file ADD COLUMN file_label TEXT;
CREATE INDEX idx_library_file_media ON library_file (media_id);
CREATE INDEX idx_library_file_label ON library_file (file_label);
