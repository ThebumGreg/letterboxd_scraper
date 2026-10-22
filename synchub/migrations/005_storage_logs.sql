-- Storage roots (the shares workers mount) and job logs fetched from workers on request.

-- A root is a logical name for a share. Jobs say "media: Movies/X.mkv", never a machine's path; every worker mounts the root
-- at the same mount point, so a path means the same thing everywhere.
CREATE TABLE storage_root (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,              -- logical name: 'media', 'tfiles'
    label         TEXT,
    protocol      TEXT NOT NULL CHECK (protocol IN ('smb', 'nfs', 'local')),
    host          TEXT,                              -- the Unraid address (smb and nfs)
    share         TEXT NOT NULL,                     -- smb: share name; nfs: export path; local: the folder on the worker PC
    options       TEXT NOT NULL DEFAULT '',          -- extra mount options; never a user name or password
    mount_point   TEXT NOT NULL UNIQUE,              -- where workers see it, e.g. /mnt/tower/Media
    jellyfin_path TEXT,                              -- how Jellyfin, Radarr and Sonarr see the same folder (path translation, notifications)
    radarr_path   TEXT,
    sonarr_path   TEXT,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now'))
);

-- "Fetch full log": the web UI sets log_requested, the holder of the job's files uploads the log with its next poll.
ALTER TABLE job ADD COLUMN log_requested INTEGER NOT NULL DEFAULT 0;

CREATE TABLE job_log (
    job_id      INTEGER PRIMARY KEY REFERENCES job (id) ON DELETE CASCADE,
    worker_id   INTEGER REFERENCES worker (id) ON DELETE SET NULL,
    received_at TEXT NOT NULL,
    truncated   INTEGER NOT NULL DEFAULT 0,
    content     TEXT NOT NULL
);
