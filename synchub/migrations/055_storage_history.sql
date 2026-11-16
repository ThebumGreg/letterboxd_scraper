-- One reading a day of how much room SyncHub itself takes (database, previews, backups, logs, posters, work folders) and how much is free on the data volume.
-- Kept 90 days, so growth shows as a small chart on System > SyncHub storage.
CREATE TABLE IF NOT EXISTS storage_history (
    day             TEXT PRIMARY KEY,
    data_bytes      INTEGER,
    db_bytes        INTEGER,
    previews_bytes  INTEGER,
    backups_bytes   INTEGER,
    logs_bytes      INTEGER,
    work_bytes      INTEGER,
    free_bytes      INTEGER
);
