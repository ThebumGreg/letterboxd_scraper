-- What each Jellyfin user has watched and how far they got, copied from Jellyfin's own per-user data (Jellyfin is the source of truth:
-- every app that plays something writes it there). Keyed by Jellyfin user and item (both lower case, without dashes) and not by profile,
-- so deleting and recreating a profile re-attaches it. A row exists only for something played or part-way through; removed_at is set when
-- Jellyfin stops reporting one (it was marked unwatched). Like the other src_ tables, rows are never deleted.
CREATE TABLE src_jellyfin_userdata (
    jellyfin_user_id TEXT NOT NULL,
    item_id          TEXT NOT NULL,                           -- a movie or an episode
    position_ticks   INTEGER NOT NULL DEFAULT 0,              -- where they stopped (Jellyfin resets it to 0 when they finish)
    runtime_ticks    INTEGER,
    played           INTEGER NOT NULL DEFAULT 0,
    play_count       INTEGER NOT NULL DEFAULT 0,
    last_played_at   TEXT,                                    -- UTC, as Jellyfin's LastPlayedDate
    first_seen       TEXT NOT NULL,
    last_seen        TEXT NOT NULL,
    removed_at       TEXT,
    PRIMARY KEY (jellyfin_user_id, item_id)
);
CREATE INDEX ix_userdata_item ON src_jellyfin_userdata (item_id);
