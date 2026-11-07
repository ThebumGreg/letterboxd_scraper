-- A share that Jellyfin mounts in several pieces (/media is Media/Movies, /OldMovie is Media/Old Movies...) cannot be described by one Jellyfin path. Each line here is
-- "/path Jellyfin sees = folder inside the share", for example "/OldMovie = Old Movies". The single Jellyfin path still works and is used for the whole share.
ALTER TABLE storage_root ADD COLUMN jellyfin_extra_paths TEXT;
