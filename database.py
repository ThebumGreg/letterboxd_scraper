"""
SQLite data layer for Letterboxd & Serializd SyncHub.
Single source of truth for the DB location is config.DB_PATH.
If an old <project>/data.db exists and the new DB does not, it is copied over automatically on first init_db().
"""
import json
import logging
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from config import DB_PATH

logger = logging.getLogger(__name__)

DB_FILE = str(DB_PATH)
LEGACY_DB_FILE = Path(__file__).resolve().parent / "data.db"

_YEAR_RE = re.compile(r"^(.*?)\s*\((\d{4})\)\s*$")

# --------------------------------------------------------------------------
# Connections
# --------------------------------------------------------------------------

def _connect():
    conn = sqlite3.connect(DB_FILE, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")  # safe with WAL, much faster bulk writes
    return conn

@contextmanager
def get_connection():
    """Context manager: commits on success, rolls back on error, ALWAYS closes."""
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def get_db_connection():
    """Raw connection (caller closes it). Kept for export_to_csv.py compatibility."""
    return _connect()

# --------------------------------------------------------------------------
# Schema / migrations
# --------------------------------------------------------------------------

EXTERNAL_CACHE_DDL = """
    CREATE TABLE IF NOT EXISTS external_library_cache (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        service TEXT NOT NULL,
        tmdb_id TEXT,
        imdb_id TEXT,
        tvdb_id TEXT,
        movie_slug TEXT,
        title TEXT,
        year INTEGER,
        media_type TEXT,
        season_number INTEGER,
        episode_number INTEGER,
        status TEXT,
        in_library INTEGER DEFAULT 0,
        external_item_id TEXT,
        details_json TEXT,
        queue_status TEXT,
        queue_progress REAL,
        last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
"""

def _migrate_legacy_db():
    """One-time copy of <project>/data.db into config.DB_PATH."""
    target = Path(DB_FILE)
    if (target.exists() and target.stat().st_size > 0) or not LEGACY_DB_FILE.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    
    src = sqlite3.connect(str(LEGACY_DB_FILE))
    dst = sqlite3.connect(DB_FILE)
    try:
        src.backup(dst)  # WAL-safe copy
        logger.info(f"Migrated legacy database {LEGACY_DB_FILE} -> {DB_FILE}")
    finally:
        dst.close()
        src.close()

def _ensure_column(conn, table, col_def):
    name = col_def.split()[0]
    existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    if name not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_def}")

def _rebuild_external_cache_if_needed(conn):
    """Old schema had UNIQUE(service, tmdb_id, tvdb_id), which never matches when an ID is NULL,
    so every sync inserted duplicate rows. Rebuild the table without it and de-duplicate."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='external_library_cache'"
    ).fetchone()
    if not row or "UNIQUE(service,tmdb_id,tvdb_id)" not in re.sub(r"\s+", "", row["sql"] or ""):
        return

    logger.info("Rebuilding external_library_cache (removing broken UNIQUE constraint, de-duplicating).")
    conn.execute("ALTER TABLE external_library_cache RENAME TO external_library_cache_old")
    conn.execute(EXTERNAL_CACHE_DDL)
    
    conn.execute("""
        INSERT INTO external_library_cache
            (tmdb_id, imdb_id, tvdb_id, service, status, in_library, external_item_id, details_json, last_updated)
        SELECT tmdb_id, imdb_id, tvdb_id, service, status, in_library, external_item_id, details_json, last_updated
        FROM external_library_cache_old
        WHERE id IN (
            SELECT MAX(id) FROM external_library_cache_old
            GROUP BY service,
                     COALESCE(external_item_id, 'n:' || IFNULL(tmdb_id, '') || '|' || IFNULL(tvdb_id, ''))
        )
    """)
    conn.execute("DROP TABLE external_library_cache_old")

def init_db():
    """Creates/upgrades all tables and indexes. Safe to call repeatedly."""
    _migrate_legacy_db()
    conn = _connect()
    try:
        conn.execute("PRAGMA journal_mode=WAL")

        # Per-user watchlist / watched entries
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_films (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                film_title TEXT,
                film_year INTEGER,
                movie_slug TEXT NOT NULL,
                tmdb_id TEXT,
                imdb_id TEXT,
                tvdb_id TEXT,
                media_type TEXT DEFAULT 'movie',
                season_number INTEGER DEFAULT NULL,
                episode_number INTEGER DEFAULT NULL,
                poster_url TEXT,
                letterboxd_lists TEXT DEFAULT '',
                last_scraped TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(username, movie_slug)
            )
        """)

        # Radarr / Sonarr / Jellyfin cache
        conn.execute(EXTERNAL_CACHE_DDL)
        _rebuild_external_cache_if_needed(conn)

        # Settings key-value store
        conn.execute("""
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)

        conn.execute("""
            INSERT OR IGNORE INTO app_settings (key, value)
            SELECT 'radarr_enable_queue_addition', value FROM app_settings WHERE key = 'enable_radarr_automation'
        """)
        conn.execute("""
            INSERT OR IGNORE INTO app_settings (key, value)
            SELECT 'radarr_enable_library_sync', '1' FROM app_settings WHERE key = 'enable_radarr_automation'
        """)
        conn.execute("DELETE FROM app_settings WHERE key = 'enable_radarr_automation'")

        conn.execute("""
            INSERT OR IGNORE INTO app_settings (key, value)
            SELECT 'sonarr_enable_queue_addition', value FROM app_settings WHERE key = 'enable_sonarr_automation'
        """)
        conn.execute("""
            INSERT OR IGNORE INTO app_settings (key, value)
            SELECT 'sonarr_enable_library_sync', '1' FROM app_settings WHERE key = 'enable_sonarr_automation'
        """)
        conn.execute("DELETE FROM app_settings WHERE key = 'enable_sonarr_automation'")

        # Task execution state
        conn.execute("""
            CREATE TABLE IF NOT EXISTS task_status (
                task_name TEXT PRIMARY KEY,
                status TEXT DEFAULT 'idle',
                last_run TIMESTAMP,
                message TEXT
            )
        """)

        # Disk scanner cache
        conn.execute("""
            CREATE TABLE IF NOT EXISTS disk_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT,
                path TEXT,
                size REAL,
                last_scanned TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        for col in (
            "film_year INTEGER",
            "media_type TEXT DEFAULT 'movie'",
            "tmdb_id TEXT",
            "imdb_id TEXT",
            "tvdb_id TEXT",
            "poster_url TEXT",
            "season_number INTEGER DEFAULT NULL",
            "episode_number INTEGER DEFAULT NULL",
            "letterboxd_lists TEXT DEFAULT ''",
            "id_source TEXT",           
            "id_checked_at TIMESTAMP",
        ):
            _ensure_column(conn, "user_films", col)

        # Upgrade external library cache for robust granularity
        for col in (
            "queue_status TEXT",
            "queue_progress REAL",
            "movie_slug TEXT",
            "title TEXT",
            "year INTEGER",
            "media_type TEXT",
            "season_number INTEGER",
            "episode_number INTEGER"
        ):
            _ensure_column(conn, "external_library_cache", col)

        # Migrate JSON data to explicit columns to fix granularity issues
        conn.execute("""
            UPDATE external_library_cache
            SET title = json_extract(details_json, '$.title'),
                media_type = CASE
                    WHEN json_extract(details_json, '$.type') = 'Movie' THEN 'movie'
                    WHEN json_extract(details_json, '$.type') = 'Series' THEN 'tv'
                    WHEN json_extract(details_json, '$.type') = 'Episode' THEN 'episode'
                    WHEN service = 'radarr' THEN 'movie'
                    WHEN service = 'sonarr' THEN 'tv'
                    ELSE NULL
                END,
                season_number = json_extract(details_json, '$.season_number'),
                episode_number = json_extract(details_json, '$.episode_number')
            WHERE title IS NULL AND details_json IS NOT NULL AND details_json != ''
        """)

        conn.execute("""
            DELETE FROM external_library_cache
            WHERE external_item_id IS NOT NULL
              AND id NOT IN (
                  SELECT MAX(id) FROM external_library_cache
                  WHERE external_item_id IS NOT NULL
                  GROUP BY service, external_item_id
              )
        """)
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_ext_cache_item
            ON external_library_cache(service, external_item_id)
            WHERE external_item_id IS NOT NULL
        """)

        conn.execute("CREATE INDEX IF NOT EXISTS idx_user_films_user ON user_films(username)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_user_films_slug ON user_films(movie_slug)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_user_films_tmdb ON user_films(tmdb_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_user_films_tvdb ON user_films(tvdb_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ext_cache_service ON external_library_cache(service, tmdb_id, tvdb_id)")
        
        conn.commit()
    finally:
        conn.close()

def add_list_column_if_missing():
    init_db()

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _split_title_year(title, year=None):
    if year is not None:
        year = int(year) if str(year).isdigit() else None
    if not title:
        return title, year

    m = _YEAR_RE.match(title.strip())
    if m:
        title = m.group(1)
        year = year or int(m.group(2))
    return title.strip(), year

def _parse_lists(value):
    return {x.strip() for x in (value or "").split(",") if x.strip()}

def _format_lists(lists):
    return ", ".join(sorted(lists))

def _clean(value):
    if value is None:
        return None
    value = str(value).strip()
    return value or None

def _upsert_film(conn, username, slug, title, year=None, media_type=None,
                 tmdb_id=None, imdb_id=None, tvdb_id=None,
                 season_number=None, episode_number=None, poster_url=None, list_name=None):
    title, year = _split_title_year(title, year)
    tmdb_id, imdb_id, tvdb_id = _clean(tmdb_id), _clean(imdb_id), _clean(tvdb_id)

    row = conn.execute(
        "SELECT id, letterboxd_lists FROM user_films WHERE username = ? AND movie_slug = ?",
        (username, slug),
    ).fetchone()

    if row:
        lists = _parse_lists(row["letterboxd_lists"])
        if list_name:
            lists.add(list_name)
        
        conn.execute("""
            UPDATE user_films SET
                film_title = COALESCE(?, film_title),
                film_year = COALESCE(?, film_year),
                media_type = COALESCE(?, media_type),
                tmdb_id = COALESCE(?, tmdb_id),
                imdb_id = COALESCE(?, imdb_id),
                tvdb_id = COALESCE(?, tvdb_id),
                season_number = COALESCE(?, season_number),
                episode_number = COALESCE(?, episode_number),
                poster_url = COALESCE(?, poster_url),
                letterboxd_lists = ?,
                last_scraped = CURRENT_TIMESTAMP
            WHERE id = ?
        """, (title, year, media_type, tmdb_id, imdb_id, tvdb_id,
              season_number, episode_number, poster_url, _format_lists(lists), row["id"]))
    else:
        conn.execute("""
            INSERT INTO user_films
                (username, film_title, film_year, movie_slug, tmdb_id, imdb_id, tvdb_id,
                 media_type, season_number, episode_number, poster_url, letterboxd_lists, last_scraped)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """, (username, title, year, slug, tmdb_id, imdb_id, tvdb_id,
              media_type or "movie", season_number, episode_number, poster_url,
              _format_lists({list_name} if list_name else set())))

def _prune_list(conn, username, list_name, seen_slugs):
    removed = 0
    rows = conn.execute(
        "SELECT id, movie_slug, letterboxd_lists FROM user_films WHERE username = ?", (username,)
    ).fetchall()

    for r in rows:
        if r["movie_slug"] in seen_slugs:
            continue
        
        lists = _parse_lists(r["letterboxd_lists"])
        if list_name not in lists:
            continue
            
        lists.discard(list_name)
        if lists:
            conn.execute("UPDATE user_films SET letterboxd_lists = ? WHERE id = ?",
                         (_format_lists(lists), r["id"]))
        else:
            conn.execute("DELETE FROM user_films WHERE id = ?", (r["id"],))
        removed += 1

    return removed

# --------------------------------------------------------------------------
# Film / watchlist operations
# --------------------------------------------------------------------------

def upsert_movie_with_list(slug, title, list_name, username="default"):
    with get_connection() as conn:
        _upsert_film(conn, username, slug, title, list_name=list_name)

def prune_list(username, list_name, seen_slugs):
    seen_slugs = set(seen_slugs or [])
    if not seen_slugs:
        logger.warning(f"prune_list skipped for [{username}] {list_name}: no slugs seen.")
        return 0

    with get_connection() as conn:
        return _prune_list(conn, username, list_name, seen_slugs)

def sync_scraped_records(username, list_type, records, run_timestamp=None):
    seen = set()
    with get_connection() as conn:
        for item in records:
            slug = (item.get("movie_slug") or item.get("slug") or "").strip()
            if not slug:
                continue
            
            title = (item.get("film_title") or item.get("movie_title")
                     or item.get("title") or item.get("full_display_name"))
            
            _upsert_film(
                conn, username, slug, title,
                year=item.get("film_year") or item.get("year"),
                media_type=item.get("media_type"),
                tmdb_id=item.get("tmdb_id"), imdb_id=item.get("imdb_id"), tvdb_id=item.get("tvdb_id"),
                season_number=item.get("season_number"), episode_number=item.get("episode_number"),
                poster_url=item.get("poster_url"), list_name=list_type,
            )
            seen.add(slug)

        removed = _prune_list(conn, username, list_type, seen) if seen else 0
        if not seen:
            logger.warning(f"sync_scraped_records: no records for [{username}] {list_type}; nothing pruned.")
            
    return len(seen), removed, len(seen)

def save_user_film(username, film_title, film_year, movie_slug, tmdb_id=None, imdb_id=None,
                   tvdb_id=None, media_type="movie", season_number=None, episode_number=None,
                   poster_url=None):
    with get_connection() as conn:
        _upsert_film(conn, username, movie_slug, film_title, year=film_year, media_type=media_type,
                     tmdb_id=tmdb_id, imdb_id=imdb_id, tvdb_id=tvdb_id,
                     season_number=season_number, episode_number=episode_number, poster_url=poster_url)

def update_film_identifiers(movie_slug, tmdb_id=None, imdb_id=None, tvdb_id=None):
    tmdb_id, imdb_id, tvdb_id = _clean(tmdb_id), _clean(imdb_id), _clean(tvdb_id)
    any_given = 1 if (tmdb_id or imdb_id or tvdb_id) else 0

    with get_connection() as conn:
        conn.execute("""
            UPDATE user_films
            SET tmdb_id = COALESCE(?, tmdb_id),
                imdb_id = COALESCE(?, imdb_id),
                tvdb_id = COALESCE(?, tvdb_id),
                id_source = CASE WHEN ? THEN 'manual' ELSE id_source END
            WHERE movie_slug = ?
        """, (tmdb_id, imdb_id, tvdb_id, any_given, movie_slug))

# --------------------------------------------------------------------------
# ID resolver support
# --------------------------------------------------------------------------

def get_films_needing_ids(retry_after_days=7, limit=None):
    sql = """
        SELECT movie_slug,
               MAX(film_title) AS title,
               MAX(film_year) AS year,
               MAX(id_checked_at) AS checked
        FROM user_films
        WHERE movie_slug NOT LIKE 'serializd-%'
        GROUP BY movie_slug
        HAVING MAX(NULLIF(tmdb_id, '')) IS NULL
           AND COALESCE(MAX(id_source), '') != 'rejected'
           AND (MAX(id_checked_at) IS NULL OR MAX(id_checked_at) < datetime('now', ?))
        ORDER BY MAX(id_checked_at) IS NOT NULL, MAX(id_checked_at)
    """
    params = [f"-{int(retry_after_days)} days"]
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
        
    with get_connection() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]

def set_resolved_ids(movie_slug, tmdb_id, imdb_id=None, source="letterboxd"):
    with get_connection() as conn:
        conn.execute("""
            UPDATE user_films
            SET tmdb_id = COALESCE(?, tmdb_id),
                imdb_id = COALESCE(?, imdb_id),
                id_source = ?,
                id_checked_at = CURRENT_TIMESTAMP
            WHERE movie_slug = ?
        """, (_clean(tmdb_id), _clean(imdb_id), source, movie_slug))

def mark_id_check_failed(movie_slug):
    with get_connection() as conn:
        conn.execute("UPDATE user_films SET id_checked_at = CURRENT_TIMESTAMP WHERE movie_slug = ?", (movie_slug,))

def get_films_needing_review():
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT movie_slug,
                   MAX(film_title) AS title,
                   MAX(film_year) AS year,
                   MAX(tmdb_id) AS tmdb_id,
                   GROUP_CONCAT(DISTINCT username) AS added_by_users
            FROM user_films
            WHERE id_source = 'tmdb-search'
            GROUP BY movie_slug
            ORDER BY MAX(film_title)
        """).fetchall()
        return [dict(r) for r in rows]

def review_film_ids(movie_slug, decision):
    with get_connection() as conn:
        if decision == "confirm":
            conn.execute("UPDATE user_films SET id_source = 'confirmed' WHERE movie_slug = ? AND id_source = 'tmdb-search'",
                         (movie_slug,))
        elif decision == "reject":
            conn.execute("""
                UPDATE user_films SET tmdb_id = NULL, imdb_id = NULL, id_source = 'rejected',
                       id_checked_at = CURRENT_TIMESTAMP
                WHERE movie_slug = ? AND id_source = 'tmdb-search'
            """, (movie_slug,))
        else:
            return False
    return True

def get_film_by_slug(slug):
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM user_films WHERE movie_slug = ? ORDER BY tmdb_id IS NULL LIMIT 1", (slug,)
        ).fetchone()
        return dict(row) if row else None

def get_aggregated_active_films(list_type=None, media_type=None):
    where, params = [], []
    
    if list_type:
        where.append("(',' || REPLACE(IFNULL(letterboxd_lists, ''), ' ', '') || ',') LIKE ?")
        params.append(f"%,{list_type.replace(' ', '')},%")
    if media_type:
        where.append("COALESCE(media_type, 'movie') = ?")
        params.append(media_type)
        
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    
    with get_connection() as conn:
        rows = conn.execute(f"""
            SELECT
                movie_slug,
                MAX(film_title) AS movie_title,
                MAX(film_year) AS film_year,
                CASE WHEN MAX(film_year) IS NOT NULL
                     THEN MAX(film_title) || ' (' || MAX(film_year) || ')'
                     ELSE MAX(film_title) END AS full_display_name,
                COALESCE(MAX(media_type), 'movie') AS media_type,
                MAX(season_number) AS season_number,
                MAX(episode_number) AS episode_number,
                MAX(NULLIF(tmdb_id, '')) AS tmdb_id,
                MAX(NULLIF(imdb_id, '')) AS imdb_id,
                MAX(NULLIF(tvdb_id, '')) AS tvdb_id,
                MAX(poster_url) AS poster_url,
                MAX(id_source) AS id_source,
                GROUP_CONCAT(letterboxd_lists, ',') AS letterboxd_lists,
                GROUP_CONCAT(DISTINCT username) AS added_by_users,
                COUNT(DISTINCT username) AS user_count,
                CASE WHEN movie_slug LIKE 'serializd-%'
                     THEN 'https://www.serializd.com/show/' || SUBSTR(movie_slug, 11)
                     ELSE 'https://letterboxd.com/film/' || movie_slug || '/' END AS movie_url,
                MAX(last_scraped) AS last_scraped
            FROM user_films
            {where_sql}
            GROUP BY movie_slug
            ORDER BY MAX(last_scraped) DESC
        """, params).fetchall()

    films = [dict(r) for r in rows]
    for f in films:
        f["letterboxd_lists"] = _format_lists(_parse_lists(f["letterboxd_lists"]))
    return films

def get_export_rows():
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT username, film_title, film_year, movie_slug, media_type,
                   season_number, episode_number, tmdb_id, imdb_id, tvdb_id,
                   letterboxd_lists, last_scraped
            FROM user_films
            ORDER BY username, film_title
        """).fetchall()
        return [dict(r) for r in rows]

# --------------------------------------------------------------------------
# Radarr / Sonarr / Jellyfin cache
# --------------------------------------------------------------------------

def update_external_cache(service, tmdb_id=None, imdb_id=None, tvdb_id=None, 
                          movie_slug=None, title=None, year=None, media_type=None,
                          season_number=None, episode_number=None, status="unknown",
                          in_library=0, external_item_id=None, details_json=None):
    """Upsert keyed on (service, external_item_id) updating to explicit schema columns."""
    tmdb_id, imdb_id, tvdb_id = _clean(tmdb_id), _clean(imdb_id), _clean(tvdb_id)
    external_item_id = _clean(external_item_id)
    movie_slug = _clean(movie_slug)

    if external_item_id:
        where = "service = ? AND external_item_id = ?"
        where_params = (service, external_item_id)
    else:
        where = "service = ? AND external_item_id IS NULL AND tmdb_id IS ? AND tvdb_id IS ?"
        where_params = (service, tmdb_id, tvdb_id)

    with get_connection() as conn:
        cur = conn.execute(f"""
            UPDATE external_library_cache SET
                tmdb_id = COALESCE(?, tmdb_id),
                imdb_id = COALESCE(?, imdb_id),
                tvdb_id = COALESCE(?, tvdb_id),
                movie_slug = COALESCE(?, movie_slug),
                title = COALESCE(?, title),
                year = COALESCE(?, year),
                media_type = COALESCE(?, media_type),
                season_number = COALESCE(?, season_number),
                episode_number = COALESCE(?, episode_number),
                status = ?,
                in_library = ?,
                details_json = COALESCE(?, details_json),
                last_updated = CURRENT_TIMESTAMP
            WHERE {where}
        """, (tmdb_id, imdb_id, tvdb_id, movie_slug, title, year, media_type, season_number, episode_number, status, in_library, details_json) + where_params)
        
        if cur.rowcount == 0:
            conn.execute("""
                INSERT INTO external_library_cache
                    (service, tmdb_id, imdb_id, tvdb_id, movie_slug, title, year, media_type, season_number, episode_number, status, in_library,
                     external_item_id, details_json, last_updated)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """, (service, tmdb_id, imdb_id, tvdb_id, movie_slug, title, year, media_type, season_number, episode_number, status, in_library, external_item_id, details_json))

update_external_library_cache = update_external_cache

def get_external_cache(service):
    """All cached rows for a service using explicit database columns."""
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM external_library_cache WHERE service = ?", (service,)).fetchall()
        
    results = []
    for r in rows:
        results.append(dict(r))
    return results

get_external_library_cache = get_external_cache

def prune_external_cache(service, older_than):
    with get_connection() as conn:
        cur = conn.execute(
            "DELETE FROM external_library_cache WHERE service = ? AND last_updated < ?",
            (service, older_than),
        )
        return cur.rowcount

# --------------------------------------------------------------------------
# Settings & task tracking
# --------------------------------------------------------------------------

def get_setting(key, default_val=""):
    with get_connection() as conn:
        row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row and row["value"] is not None else default_val

def set_setting(key, value):
    with get_connection() as conn:
        conn.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)", (key, str(value)))

def set_settings(mapping):
    with get_connection() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)",
            [(k, str(v)) for k, v in mapping.items()],
        )

def get_all_settings():
    with get_connection() as conn:
        return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM app_settings")}

def update_task_status(task_name, status, message=""):
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO task_status (task_name, status, last_run, message)
            VALUES (?, ?, CURRENT_TIMESTAMP, ?)
            ON CONFLICT(task_name) DO UPDATE SET
                status = excluded.status,
                last_run = CURRENT_TIMESTAMP,
                message = excluded.message
        """, (task_name, status, message))

def update_job_status(job_name, status, message=""):
    update_task_status(job_name, status, message)

def get_all_task_statuses():
    default_tasks = ["scraper", "radarr", "sonarr", "jellyfin_movies", "jellyfin_tv", "id_resolver", "all"]
    statuses = {t: {"task_name": t, "status": "idle", "last_run": None, "message": "No active logs"}
                for t in default_tasks}
    
    with get_connection() as conn:
        for row in conn.execute("SELECT * FROM task_status"):
            statuses[row["task_name"]] = dict(row)
    return statuses

# --------------------------------------------------------------------------
# Disk scanner cache
# --------------------------------------------------------------------------

def save_disk_file_cache(file_data_list):
    rows = []
    for item in file_data_list:
        size = item.get("file_size") if item.get("file_size") is not None else item.get("size")
        rows.append((
            item.get("parsed_title") or item.get("title") or item.get("file_name"),
            item.get("file_path") or item.get("path"),
            size,
        ))
        
    with get_connection() as conn:
        conn.execute("DELETE FROM disk_cache")
        conn.executemany("INSERT INTO disk_cache (title, path, size) VALUES (?, ?, ?)", rows)

def get_disk_file_cache():
    with get_connection() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM disk_cache ORDER BY title")]

if __name__ == "__main__":
    init_db()
    print(f"Database schema verified and ready at {DB_FILE}")