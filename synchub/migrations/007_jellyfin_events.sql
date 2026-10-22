-- Everything Jellyfin's Webhook plugin sends, kept as it arrived (raw) plus the fields worth querying.
CREATE TABLE jellyfin_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at TEXT NOT NULL,
    event_type TEXT NOT NULL,                         -- the plugin's NotificationType, or WebhookTrigger when the sender did not say
    user_name TEXT, user_id TEXT,
    item_id TEXT, item_type TEXT, item_name TEXT,
    series_name TEXT, series_id TEXT, season_number INTEGER, episode_number INTEGER, year INTEGER,
    device_name TEXT, device_id TEXT, client_name TEXT,
    position_ticks INTEGER, runtime_ticks INTEGER, played_to_completion INTEGER, is_paused INTEGER,
    sent_at TEXT,                                     -- Jellyfin's own timestamp (UTC), when it gave one
    remote_addr TEXT,
    payload TEXT NOT NULL                             -- the body, verbatim (cut at 256 KB)
);
CREATE INDEX ix_jellyfin_event_received ON jellyfin_event (received_at);
CREATE INDEX ix_jellyfin_event_type ON jellyfin_event (event_type, id);
CREATE INDEX ix_jellyfin_event_item ON jellyfin_event (item_id);
CREATE INDEX ix_jellyfin_event_user ON jellyfin_event (user_id);
