-- Jobs are looked up by what they are about (the Record page lists a file's jobs, and several pages ask "is a job open for this file").
CREATE INDEX IF NOT EXISTS ix_job_ref ON job (ref_type, ref_id);

-- "Poor quality" on a movie or show page: who flagged the title, why, and what Radarr or Sonarr was asked to do about it.
-- A film's request is also kept in quality_request (per file); this is the title's own history and the only record for shows.
CREATE TABLE media_quality_request (
    id            INTEGER PRIMARY KEY,
    media_id      INTEGER NOT NULL REFERENCES media (id) ON DELETE CASCADE,
    service       TEXT NOT NULL CHECK (service IN ('radarr', 'sonarr')),
    scope         TEXT NOT NULL DEFAULT 'movie' CHECK (scope IN ('movie', 'episode', 'season')),
    season_number INTEGER,
    episode_id    INTEGER,                        -- episode.id for one episode
    mode          TEXT NOT NULL DEFAULT 'auto' CHECK (mode IN ('auto', 'pick')),
    note          TEXT,                           -- what the person said is wrong with it
    blocked       INTEGER NOT NULL DEFAULT 0,     -- how many grabbed releases were put on the blocklist
    chosen_release TEXT,
    status        TEXT NOT NULL CHECK (status IN ('requested', 'grabbed', 'failed')),
    message       TEXT,
    requested_by  TEXT,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now'))
);
CREATE INDEX ix_media_quality_request ON media_quality_request (media_id, id);
