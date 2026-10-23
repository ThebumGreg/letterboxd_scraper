-- Remembers when a source record was last run through the (network-using) title search, so it is not repeated every sync.
CREATE TABLE link_attempt (
    source       TEXT NOT NULL,
    source_key   TEXT NOT NULL,
    attempted_at TEXT NOT NULL,
    PRIMARY KEY (source, source_key)
);

-- The kind (movie/show) a review item is about, so approving it can create the right media row.
ALTER TABLE match_review ADD COLUMN kind TEXT;
