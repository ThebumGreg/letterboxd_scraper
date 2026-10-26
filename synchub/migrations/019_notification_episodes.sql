-- New-episode alerts: each person chooses when (when an episode reaches the library, when it airs, or both) and for which shows
-- (their watchlist, only what they requested, or both), and can mute a single show.

ALTER TABLE notification_pref ADD COLUMN episode_when   TEXT NOT NULL DEFAULT 'library' CHECK (episode_when IN ('library', 'aired', 'both'));
ALTER TABLE notification_pref ADD COLUMN episode_source TEXT NOT NULL DEFAULT 'both'    CHECK (episode_source IN ('both', 'watchlist', 'requested'));

CREATE TABLE notification_show_mute (
    profile_id INTEGER NOT NULL REFERENCES profile (id) ON DELETE CASCADE,
    media_id   INTEGER NOT NULL REFERENCES media (id) ON DELETE CASCADE,
    PRIMARY KEY (profile_id, media_id)
);
