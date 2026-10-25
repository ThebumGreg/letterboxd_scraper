-- Notifications. See docs/DESIGN_NOTIFICATIONS.md.
-- A channel is one way of reaching one person (a Pushover user key and device); an event is something that happened, recorded once;
-- a notification is one message for one channel about one event, and doubles as the outbox and the history.

CREATE TABLE notification_channel (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id  INTEGER NOT NULL REFERENCES profile (id) ON DELETE CASCADE,       -- the keys go with the person
    provider    TEXT NOT NULL,
    label       TEXT NOT NULL DEFAULT '',
    config      TEXT NOT NULL DEFAULT '{}',                                      -- JSON: what the provider needs (Pushover: user_key, device)
    enabled     INTEGER NOT NULL DEFAULT 1,
    verified_at TEXT,
    last_ok_at  TEXT,
    last_error  TEXT,
    failures    INTEGER NOT NULL DEFAULT 0,                                      -- answers in a row saying this channel is no good; at 3 it is switched off
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now'))
);
CREATE INDEX ix_notification_channel_profile ON notification_channel (profile_id);

CREATE TABLE notification_event (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    dedupe_key TEXT NOT NULL,                                                    -- e.g. 'request_available:movie:42'; the same key is never recorded twice
    payload    TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now')),
    UNIQUE (event_type, dedupe_key)
);
CREATE INDEX ix_notification_event_created ON notification_event (created_at);

CREATE TABLE notification (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id        INTEGER NOT NULL REFERENCES notification_event (id) ON DELETE CASCADE,
    profile_id      INTEGER NOT NULL REFERENCES profile (id) ON DELETE CASCADE,
    channel_id      INTEGER REFERENCES notification_channel (id) ON DELETE SET NULL,
    channel_label   TEXT NOT NULL DEFAULT '',                                    -- kept so the history still says where it went after the channel is removed
    title           TEXT NOT NULL DEFAULT '',
    body            TEXT NOT NULL DEFAULT '',
    url             TEXT,
    url_title       TEXT,
    priority        INTEGER NOT NULL DEFAULT 0,
    ttl_seconds     INTEGER,
    status          TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'held', 'sent', 'failed', 'dropped')),
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,                                                        -- NULL: due now. Also a short lease while a send is under way.
    expires_at      TEXT,                                                        -- undelivered by then: dropped, so old news is not sent
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now')),
    sent_at         TEXT,
    provider_ref    TEXT,                                                        -- the provider's own id for the message (Pushover: the request id)
    error           TEXT,
    UNIQUE (event_id, channel_id)
);
CREATE INDEX ix_notification_due ON notification (status, next_attempt_at);
CREATE INDEX ix_notification_profile ON notification (profile_id, id DESC);
