-- Age ratings and audience scores, by TMDB ID so a title in a search result has one before it is ever added. certification is the US rating
-- (PG-13, TV-MA ...), NULL when TMDB has none; rating_rank puts every label on one scale (0 G/TV-Y/TV-G, 1 PG/TV-PG, 2 PG-13/TV-14, 3 R/TV-MA,
-- 4 NC-17), NULL when unrated. vote_average is TMDB's 0-10 audience score.
CREATE TABLE title_rating (
    kind          TEXT NOT NULL,
    tmdb_id       TEXT NOT NULL,
    certification TEXT,
    rating_rank   INTEGER,
    vote_average  REAL,
    checked_at    TEXT NOT NULL,
    PRIMARY KEY (kind, tmdb_id)
);

-- The most mature rating a profile may see (0-3 on the scale above); NULL = no limit.
ALTER TABLE profile ADD COLUMN max_rating INTEGER;
