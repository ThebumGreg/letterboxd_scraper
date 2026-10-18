"""
SQLite data layer for Letterboxd & Serializd SyncHub.

Single source of truth for the DB location is config.DB_PATH.
If an old <project>/data.db exists and the new DB does not, it is copied over
automatically on first init_db().
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
        status TEXT,
        in_library INTEGER DEFAULT 0,
        external_item_id TEXT,
        details_json TEXT,
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

        # Upgrade older user_films tables
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
        ):
            _ensure_column(conn, "user_films", col)

        # Collapse any remaining duplicates, then enforce identity going forward
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
    """Compatibility wrapper for scraper.py."""
    init_db()


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _split_title_year(title, year=None):
    """'The Social Network (2010)' -> ('The Social Network', 2010)."""
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
    """Blank strings -> None so empty form fields never overwrite real IDs."""
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _upsert_film(conn, username, slug, title, year=None, media_type=None,
                 tmdb_id=None, imdb_id=None, tvdb_id=None,
                 season_number=None, episode_number=None, poster_url=None, list_name=None):
    """Single place that writes user_films. Existing values are kept when new ones are None."""
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
    """Removes list_name from this user's films that were NOT seen in the latest scrape.
    A film left on no lists at all is deleted. Returns how many entries were removed."""
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
    """Per-item upsert used by scraper.py: adds the film for this user and tags the list."""
    with get_connection() as conn:
        _upsert_film(conn, username, slug, title, list_name=list_name)


def prune_list(username, list_name, seen_slugs):
    """Call after a COMPLETE, successful scrape of one list so removed titles drop off.
    Does nothing if seen_slugs is empty (an empty result usually means a failed scrape)."""
    seen_slugs = set(seen_slugs or [])
    if not seen_slugs:
        logger.warning(f"prune_list skipped for [{username}] {list_name}: no slugs seen.")
        return 0
    with get_connection() as conn:
        return _prune_list(conn, username, list_name, seen_slugs)


def sync_scraped_records(username, list_type, records, run_timestamp=None):
    """Bulk sync of one user's list. Returns (processed, removed, active).
    records: dicts with movie_slug/slug and movie_title/film_title/title (+ optional ids, media_type...)."""
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
    """Inserts or updates a single film/show entry for a user (no list tag)."""
    with get_connection() as conn:
        _upsert_film(conn, username, movie_slug, film_title, year=film_year, media_type=media_type,
                     tmdb_id=tmdb_id, imdb_id=imdb_id, tvdb_id=tvdb_id,
                     season_number=season_number, episode_number=episode_number, poster_url=poster_url)


def update_film_identifiers(movie_slug, tmdb_id=None, imdb_id=None, tvdb_id=None):
    """Manual ID fix. Blank values leave the existing ID untouched. Applies to every user's row."""
    with get_connection() as conn:
        conn.execute("""
            UPDATE user_films
            SET tmdb_id = COALESCE(?, tmdb_id),
                imdb_id = COALESCE(?, imdb_id),
                tvdb_id = COALESCE(?, tvdb_id)
            WHERE movie_slug = ?
        """, (_clean(tmdb_id), _clean(imdb_id), _clean(tvdb_id), movie_slug))


def get_film_by_slug(slug):
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM user_films WHERE movie_slug = ? ORDER BY tmdb_id IS NULL LIMIT 1", (slug,)
        ).fetchone()
        return dict(row) if row else None


def get_aggregated_active_films(list_type=None, media_type=None):
    """One row per film across all users.
    list_type='Watchlist' limits to films on that list (and counts only users who have it there).
    media_type='movie' or 'tv' filters by type."""
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
    """Rows for the CSV export (replaces the old scraped_records table)."""
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

def update_external_cache(service, tmdb_id=None, imdb_id=None, tvdb_id=None, status="unknown",
                          in_library=0, external_item_id=None, details_json=None):
    """Upsert keyed on (service, external_item_id). If no external id is supplied,
    falls back to a NULL-safe match on tmdb/tvdb."""
    tmdb_id, imdb_id, tvdb_id = _clean(tmdb_id), _clean(imdb_id), _clean(tvdb_id)
    external_item_id = _clean(external_item_id)

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
                status = ?,
                in_library = ?,
                details_json = COALESCE(?, details_json),
                last_updated = CURRENT_TIMESTAMP
            WHERE {where}
        """, (tmdb_id, imdb_id, tvdb_id, status, in_library, details_json) + where_params)

        if cur.rowcount == 0:
            conn.execute("""
                INSERT INTO external_library_cache
                    (service, tmdb_id, imdb_id, tvdb_id, status, in_library,
                     external_item_id, details_json, last_updated)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """, (service, tmdb_id, imdb_id, tvdb_id, status, in_library, external_item_id, details_json))


update_external_library_cache = update_external_cache


def get_external_cache(service):
    """All cached rows for a service. Adds a parsed 'title' key when details_json holds one."""
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM external_library_cache WHERE service = ?", (service,)).fetchall()

    results = []
    for r in rows:
        d = dict(r)
        d["title"] = None
        if d.get("details_json"):
            try:
                d["title"] = json.loads(d["details_json"]).get("title")
            except (ValueError, AttributeError):
                pass
        results.append(d)
    return results


get_external_library_cache = get_external_cache


def prune_external_cache(service, older_than):
    """Deletes cache rows for a service not refreshed since `older_than` ('YYYY-MM-DD HH:MM:SS', UTC).
    Call after a fully successful sync, passing the time the sync started."""
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
    """Saves many settings in one transaction."""
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
    default_tasks = ["scraper", "radarr", "sonarr", "jellyfin_movies", "jellyfin_tv", "all"]
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
    """Replaces the disk cache. Accepts disk_scanner keys (parsed_title/file_path/file_size)
    as well as the table-style keys (title/path/size)."""
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