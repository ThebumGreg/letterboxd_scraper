-- SyncHub v2 initial schema. See docs/DESIGN_V2.md.
-- Timestamps are UTC text in the form YYYY-MM-DD HH:MM:SS.

-- ---------------------------------------------------------------- core
CREATE TABLE app_setting (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

CREATE TABLE task_status (
    task_name  TEXT PRIMARY KEY,
    status     TEXT NOT NULL DEFAULT 'idle',
    started_at TEXT,
    last_run   TEXT,
    message    TEXT
);

-- One Letterboxd / Serializd / Jellyfin identity can belong to only one profile.
-- NULL means "not set"; an empty string is rejected so uniqueness cannot be dodged.
CREATE TABLE profile (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT NOT NULL UNIQUE COLLATE NOCASE CHECK (name <> ''),
    letterboxd_username TEXT UNIQUE COLLATE NOCASE CHECK (letterboxd_username IS NULL OR letterboxd_username <> ''),
    serializd_username  TEXT UNIQUE COLLATE NOCASE CHECK (serializd_username IS NULL OR serializd_username <> ''),
    jellyfin_user_id    TEXT UNIQUE CHECK (jellyfin_user_id IS NULL OR jellyfin_user_id <> ''),
    is_active           INTEGER NOT NULL DEFAULT 1,
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now'))
);

-- ---------------------------------------------------------------- source layer
-- Rows are inserted/updated by syncs and never deleted. removed_at is set when a source stops reporting a row.

-- Entries are keyed by username (not profile id) so deleting and recreating a profile re-attaches them.
CREATE TABLE src_letterboxd_entry (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL COLLATE NOCASE,
    slug          TEXT NOT NULL,
    list          TEXT NOT NULL CHECK (list IN ('watchlist', 'watched')),
    title         TEXT,
    year          INTEGER,
    tmdb_id       TEXT,
    imdb_id       TEXT,
    id_source     TEXT,
    id_checked_at TEXT,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    removed_at    TEXT,
    UNIQUE (username, slug, list)
);
CREATE INDEX ix_src_lb_slug ON src_letterboxd_entry (slug);

CREATE TABLE src_serializd_entry (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    username          TEXT NOT NULL COLLATE NOCASE,
    serializd_show_id TEXT NOT NULL,
    title             TEXT,
    year              INTEGER,
    tmdb_id           TEXT,
    tvdb_id           TEXT,
    imdb_id           TEXT,
    season_number     INTEGER,
    first_seen        TEXT NOT NULL,
    last_seen         TEXT NOT NULL,
    removed_at        TEXT,
    UNIQUE (username, serializd_show_id)
);

CREATE TABLE src_jellyfin_item (
    item_id        TEXT PRIMARY KEY,
    type           TEXT NOT NULL CHECK (type IN ('movie', 'series', 'season', 'episode')),
    series_id      TEXT,
    season_id      TEXT,
    name           TEXT,
    series_name    TEXT,
    year           INTEGER,
    season_number  INTEGER,
    episode_number INTEGER,
    premiere_date  TEXT,
    tmdb_id        TEXT,
    tvdb_id        TEXT,
    imdb_id        TEXT,
    first_seen     TEXT NOT NULL,
    last_seen      TEXT NOT NULL,
    removed_at     TEXT
);
CREATE INDEX ix_src_jf_series ON src_jellyfin_item (series_id);
CREATE INDEX ix_src_jf_type ON src_jellyfin_item (type);

CREATE TABLE src_radarr_movie (
    radarr_id  TEXT PRIMARY KEY,
    tmdb_id    TEXT,
    imdb_id    TEXT,
    title      TEXT,
    year       INTEGER,
    monitored  INTEGER NOT NULL DEFAULT 0,
    has_file   INTEGER NOT NULL DEFAULT 0,
    first_seen TEXT NOT NULL,
    last_seen  TEXT NOT NULL,
    removed_at TEXT
);

CREATE TABLE src_sonarr_series (
    sonarr_id  TEXT PRIMARY KEY,
    tvdb_id    TEXT,
    tmdb_id    TEXT,
    imdb_id    TEXT,
    title      TEXT,
    year       INTEGER,
    monitored  INTEGER NOT NULL DEFAULT 0,
    first_seen TEXT NOT NULL,
    last_seen  TEXT NOT NULL,
    removed_at TEXT
);

CREATE TABLE src_sonarr_episode (
    sonarr_episode_id TEXT PRIMARY KEY,
    sonarr_id         TEXT NOT NULL,
    season_number     INTEGER,
    episode_number    INTEGER,
    tvdb_episode_id   TEXT,
    title             TEXT,
    air_date          TEXT,
    monitored         INTEGER NOT NULL DEFAULT 0,
    has_file          INTEGER NOT NULL DEFAULT 0,
    first_seen        TEXT NOT NULL,
    last_seen         TEXT NOT NULL,
    removed_at        TEXT
);
CREATE INDEX ix_src_sonarr_ep_series ON src_sonarr_episode (sonarr_id);

-- Current Radarr/Sonarr download queue. target_id is the radarr_id (kind=movie) or sonarr_episode_id (kind=episode).
CREATE TABLE src_arr_queue (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    service    TEXT NOT NULL CHECK (service IN ('radarr', 'sonarr')),
    queue_id   TEXT NOT NULL,
    kind       TEXT NOT NULL CHECK (kind IN ('movie', 'episode')),
    target_id  TEXT NOT NULL,
    title      TEXT,
    status     TEXT,
    progress   REAL,
    size       REAL,
    time_left  TEXT,
    first_seen TEXT NOT NULL,
    last_seen  TEXT NOT NULL,
    removed_at TEXT,
    UNIQUE (service, queue_id)
);

-- ---------------------------------------------------------------- identity layer
CREATE TABLE media (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    kind                  TEXT NOT NULL CHECK (kind IN ('movie', 'show')),
    title                 TEXT NOT NULL,
    original_title        TEXT,
    year                  INTEGER,
    tmdb_id               TEXT,
    tvdb_id               TEXT,
    imdb_id               TEXT,
    overview              TEXT,
    status                TEXT,
    first_air_date        TEXT,
    poster_path           TEXT,
    metadata_refreshed_at TEXT,
    created_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now'))
);
CREATE UNIQUE INDEX ux_media_tmdb ON media (kind, tmdb_id) WHERE tmdb_id IS NOT NULL;
CREATE UNIQUE INDEX ux_media_tvdb ON media (kind, tvdb_id) WHERE tvdb_id IS NOT NULL;
CREATE INDEX ix_media_title ON media (kind, title);

-- All episodes of a show, including ones not in the library and not yet aired. Season 0 = Specials.
-- season_number / episode_number use TMDB numbering; tvdb_* hold Sonarr's numbering where it differs.
CREATE TABLE episode (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    media_id            INTEGER NOT NULL REFERENCES media (id) ON DELETE CASCADE,
    season_number       INTEGER NOT NULL,
    episode_number      INTEGER NOT NULL,
    title               TEXT,
    overview            TEXT,
    air_date            TEXT,
    runtime             INTEGER,
    tmdb_episode_id     TEXT,
    tvdb_episode_id     TEXT,
    tvdb_season_number  INTEGER,
    tvdb_episode_number INTEGER,
    numbering_status    TEXT NOT NULL DEFAULT 'same' CHECK (numbering_status IN ('same', 'mapped', 'unmapped')),
    last_refreshed      TEXT,
    UNIQUE (media_id, season_number, episode_number)
);
CREATE INDEX ix_episode_tvdb ON episode (tvdb_episode_id);
CREATE INDEX ix_episode_tmdb ON episode (tmdb_episode_id);

-- Links a source record to a media item or episode. One row per source record.
CREATE TABLE link (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL CHECK (entity_type IN ('media', 'episode')),
    entity_id   INTEGER NOT NULL,
    source      TEXT NOT NULL,
    source_key  TEXT NOT NULL,
    method      TEXT NOT NULL,
    confidence  REAL NOT NULL,
    status      TEXT NOT NULL DEFAULT 'auto' CHECK (status IN ('auto', 'manual')),
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now')),
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now')),
    UNIQUE (source, source_key)
);
CREATE INDEX ix_link_entity ON link (entity_type, entity_id);

-- Matches that need a human decision. 'dismissed' rows stop the same suggestion from coming back.
CREATE TABLE match_review (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    source             TEXT NOT NULL,
    source_key         TEXT NOT NULL,
    source_title       TEXT,
    source_year        INTEGER,
    candidate_media_id INTEGER REFERENCES media (id) ON DELETE CASCADE,
    candidate_tmdb_id  TEXT,
    confidence         REAL,
    reason             TEXT,
    status             TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'dismissed')),
    created_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now')),
    UNIQUE (source, source_key, candidate_tmdb_id)
);

-- ---------------------------------------------------------------- state layer
-- States are rows: adding one (e.g. a future "encoding" tweak) needs no schema change.
CREATE TABLE state_def (
    code        TEXT PRIMARY KEY,
    label       TEXT NOT NULL,
    rank        INTEGER NOT NULL UNIQUE,
    color       TEXT NOT NULL DEFAULT 'secondary',
    icon        TEXT,
    is_terminal INTEGER NOT NULL DEFAULT 0
);

INSERT INTO state_def (code, label, rank, color, icon, is_terminal) VALUES
    ('missing',      'Missing',      0,   'danger',    'bi-x-circle',          0),
    ('queued',       'Queued',       10,  'info',      'bi-hourglass-split',   0),
    ('downloading',  'Downloading',  20,  'primary',   'bi-cloud-arrow-down',  0),
    ('downloaded',   'Downloaded',   30,  'warning',   'bi-hdd',               0),
    ('transferring', 'Transferring', 40,  'warning',   'bi-arrow-left-right',  0),
    ('encoding',     'Encoding',     50,  'warning',   'bi-cpu',               0),
    ('in_library',   'In Library',   100, 'success',   'bi-check-circle-fill', 1);

-- Open events written by providers (Radarr/Sonarr queue now; transfer and encode scripts later).
-- A provider closes an event by setting ended_at.
CREATE TABLE processing_event (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    target_type TEXT NOT NULL CHECK (target_type IN ('movie', 'episode')),
    target_id   INTEGER NOT NULL,
    provider    TEXT NOT NULL,
    state_code  TEXT NOT NULL REFERENCES state_def (code),
    progress    REAL,
    detail      TEXT,
    external_ref TEXT,
    started_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now')),
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now')),
    ended_at    TEXT
);
CREATE INDEX ix_pe_target ON processing_event (target_type, target_id) WHERE ended_at IS NULL;
CREATE UNIQUE INDEX ux_pe_open ON processing_event (provider, external_ref) WHERE ended_at IS NULL AND external_ref IS NOT NULL;

-- Current state of every movie and episode, updated in place by the state engine.
CREATE TABLE item_state (
    target_type TEXT NOT NULL CHECK (target_type IN ('movie', 'episode')),
    target_id   INTEGER NOT NULL,
    state_code  TEXT NOT NULL REFERENCES state_def (code),
    progress    REAL,
    updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now')),
    PRIMARY KEY (target_type, target_id)
);

-- ---------------------------------------------------------------- misc
CREATE TABLE api_payload_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    direction    TEXT,
    service      TEXT,
    event_type   TEXT,
    endpoint     TEXT,
    status_code  INTEGER,
    duration_ms  INTEGER,
    headers_json TEXT,
    payload_json TEXT,
    response_json TEXT,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now'))
);

CREATE TABLE ddns_history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    old_ip     TEXT,
    new_ip     TEXT,
    status     TEXT NOT NULL,
    message    TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%S', 'now'))
);
