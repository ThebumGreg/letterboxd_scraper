-- Where Radarr and Sonarr say each title's files are, so the Library check can compare them with Jellyfin and the files on disk.
-- Paths are kept exactly as the service reports them (its own folder spelling, its own mount path).
ALTER TABLE src_radarr_movie ADD COLUMN folder TEXT;           -- the movie's folder ("path")
ALTER TABLE src_radarr_movie ADD COLUMN file_path TEXT;        -- the file inside it (folder + movieFile.relativePath)
ALTER TABLE src_radarr_movie ADD COLUMN file_size INTEGER;
ALTER TABLE src_sonarr_series ADD COLUMN folder TEXT;          -- the series' folder ("path")
ALTER TABLE src_sonarr_episode ADD COLUMN file_path TEXT;      -- the episode file's full path
ALTER TABLE src_sonarr_episode ADD COLUMN file_size INTEGER;
