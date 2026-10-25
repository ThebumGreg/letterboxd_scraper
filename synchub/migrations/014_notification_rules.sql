-- Notifications, phase 2: what each person wants to hear about, and when. See docs/DESIGN_NOTIFICATIONS.md.
-- No row means the event type's own default; a row overrides it.

CREATE TABLE notification_rule (
    profile_id INTEGER NOT NULL REFERENCES profile (id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    enabled    INTEGER NOT NULL,
    PRIMARY KEY (profile_id, event_type)
);

CREATE TABLE notification_pref (
    profile_id  INTEGER PRIMARY KEY REFERENCES profile (id) ON DELETE CASCADE,
    quiet_on    INTEGER NOT NULL DEFAULT 0,
    quiet_start TEXT NOT NULL DEFAULT '22:00',     -- local time, HH:MM
    quiet_end   TEXT NOT NULL DEFAULT '07:00',
    timezone    TEXT NOT NULL DEFAULT 'UTC'        -- an IANA name such as America/Chicago
);
