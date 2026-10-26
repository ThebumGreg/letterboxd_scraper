-- A watchlist for each person that they can change in SyncHub. It is seeded from their Letterboxd and Serializd watchlists, but is its own list:
-- nothing reads it to request or download anything (auto-add and the notifications still use the source lists, see catalog.demand).
-- Removing a title sets hidden_at instead of deleting the row, so a later sync does not add it back; adding it again clears hidden_at.
-- Rows are kept by profile ID without a foreign key, like manual_request, so deleting a profile keeps its list.
CREATE TABLE watchlist_item (
    profile_id    INTEGER NOT NULL,
    media_id      INTEGER NOT NULL REFERENCES media (id) ON DELETE CASCADE,
    source        TEXT NOT NULL CHECK (source IN ('letterboxd', 'serializd', 'manual')),
    added_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now')),
    hidden_at     TEXT,
    hidden_reason TEXT CHECK (hidden_reason IS NULL OR hidden_reason IN ('removed', 'watched')),
    PRIMARY KEY (profile_id, media_id)
);
CREATE INDEX ix_watchlist_media ON watchlist_item (media_id);

-- Per person: take a title off the list once they have watched it.
ALTER TABLE profile ADD COLUMN watchlist_drop_watched INTEGER NOT NULL DEFAULT 0;

-- Start everyone with what their Letterboxd and Serializd watchlists already say.
INSERT OR IGNORE INTO watchlist_item (profile_id, media_id, source, added_at)
SELECT p.id, l.entity_id, 'letterboxd', MIN(e.first_seen)
FROM src_letterboxd_entry e
JOIN link l ON l.source = 'letterboxd' AND l.source_key = e.slug AND l.entity_type = 'media'
JOIN profile p ON p.letterboxd_username = e.username
WHERE e.list = 'watchlist' AND e.removed_at IS NULL
GROUP BY p.id, l.entity_id;

INSERT OR IGNORE INTO watchlist_item (profile_id, media_id, source, added_at)
SELECT p.id, l.entity_id, 'serializd', MIN(e.first_seen)
FROM src_serializd_entry e
JOIN link l ON l.source = 'serializd' AND l.source_key = e.serializd_show_id AND l.entity_type = 'media'
JOIN profile p ON p.serializd_username = e.username
WHERE e.removed_at IS NULL
GROUP BY p.id, l.entity_id;
