-- A title someone asked for from the search page. The person is kept by profile ID without a foreign key on purpose:
-- deleting a profile keeps what it requested (the title stays in the catalog; the request just stops counting for anyone).
CREATE TABLE manual_request (
    media_id     INTEGER NOT NULL REFERENCES media (id) ON DELETE CASCADE,
    profile_id   INTEGER NOT NULL,
    requested_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now')),
    PRIMARY KEY (media_id, profile_id)
);
