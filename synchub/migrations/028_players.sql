-- TV players: small PCs behind a TV that play media and are controlled from SyncHub (the master). Like a worker, a player has a token and only calls out.
CREATE TABLE player (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE,
    token_hash   TEXT UNIQUE,                       -- sha256 of the token; NULL once revoked (the token itself is never stored)
    token_hint   TEXT,                              -- last characters of the token, to tell tokens apart in the UI
    enabled      INTEGER NOT NULL DEFAULT 1,
    tv_name      TEXT NOT NULL DEFAULT '',          -- the TV it is plugged into (free text for now; linked to a remote TV in a later step)
    protocol     INTEGER,
    version      TEXT,
    os           TEXT,
    hostname     TEXT,
    capabilities TEXT NOT NULL DEFAULT '{}',        -- JSON: mpv version, graphics devices, connected screens
    status       TEXT NOT NULL DEFAULT '{}',        -- JSON: the summary sent with the latest poll (load, memory, uptime)
    now_playing  TEXT NOT NULL DEFAULT '{}',        -- JSON: what is playing right now (empty until playback exists)
    last_seen    TEXT,
    log_requested INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now')),
    revoked_at   TEXT
);

-- What SyncHub asked a player to do. A player takes the oldest undelivered command, does it and reports the result.
CREATE TABLE player_command (
    id           INTEGER PRIMARY KEY,
    player_id    INTEGER NOT NULL REFERENCES player (id) ON DELETE CASCADE,
    kind         TEXT NOT NULL,                     -- ping, update, restart (playback kinds come later)
    payload      TEXT NOT NULL DEFAULT '{}',        -- JSON
    status       TEXT NOT NULL DEFAULT 'queued',    -- queued, sent, done, failed, expired
    result       TEXT,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now')),
    sent_at      TEXT,
    finished_at  TEXT
);
CREATE INDEX player_command_open ON player_command (player_id, status);

CREATE TABLE player_log (
    player_id    INTEGER PRIMARY KEY REFERENCES player (id) ON DELETE CASCADE,
    received_at  TEXT NOT NULL,
    truncated    INTEGER NOT NULL DEFAULT 0,
    content      TEXT NOT NULL DEFAULT ''
);
