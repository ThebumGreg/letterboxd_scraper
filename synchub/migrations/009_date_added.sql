-- When Jellyfin added each item to its library (its DateCreated), for "Date Added". first_seen is only when SyncHub first read it, which is the
-- same day for a whole existing library. Filled by the next Jellyfin sync.
ALTER TABLE src_jellyfin_item ADD COLUMN date_created TEXT;
