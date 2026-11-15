-- A Library file of a TV episode is linked to its episode (episode.id); media_id is the show. Films keep episode_id NULL.
ALTER TABLE library_file ADD COLUMN episode_id INTEGER;
CREATE INDEX ix_library_file_episode ON library_file (episode_id) WHERE episode_id IS NOT NULL;
