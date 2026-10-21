# database.py
import json
import logging
import re
import sqlite3
import time
import requests
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from config import DB_PATH, HTTP_TIMEOUT

logger = logging.getLogger(__name__)

DB_FILE = str(DB_PATH)
LEGACY_DB_FILE = Path(__file__).resolve().parent / "data.db"

_YEAR_RE = re.compile(r"^(.*?)\s*\((\d{4})\)\s*$")

def _connect():
    conn = sqlite3.connect(DB_FILE, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn

@contextmanager
def get_connection():
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
    return _connect()

def _migrate_legacy_db():
    target = Path(DB_FILE)
    if (target.exists() and target.stat().st_size > 0) or not LEGACY_DB_FILE.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    
    src = sqlite3.connect(str(LEGACY_DB_FILE))
    dst = sqlite3.connect(DB_FILE)
    try:
        src.backup(dst)
        logger.info(f"Migrated legacy database {LEGACY_DB_FILE} -> {DB_FILE}")
    finally:
        dst.close()
        src.close()

def _ensure_column(conn, table, col_def):
    name = col_def.split()[0]
    existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    if name not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_def}")

def reset_stale_tasks():
    with get_connection() as conn:
        conn.execute("""
            UPDATE task_status 
            SET status = 'failed', message = 'Job interrupted by system restart.'
            WHERE status IN ('in_progress', 'pending')
        """)

def init_db():
    _migrate_legacy_db()
    conn = _connect()
    try:
        conn.execute("PRAGMA journal_mode=WAL")

        existing_tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        
        if "user_films" in existing_tables and "user_requests" not in existing_tables:
            conn.execute("ALTER TABLE user_films RENAME TO user_requests")
            
        if "external_library_cache" in existing_tables and "service_cache" not in existing_tables:
            conn.execute("ALTER TABLE external_library_cache RENAME TO service_cache")

        # Migrate tv_episodes cache table to new hardened UNIQUE constraint if needed
        if "tv_episodes" in existing_tables:
            schema = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='tv_episodes'").fetchone()[0]
            if "UNIQUE(tvdb_id, tmdb_id" not in schema:
                conn.execute("DROP TABLE tv_episodes")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS tv_episodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tvdb_id TEXT,
                tmdb_id TEXT,
                season_number INTEGER,
                episode_number INTEGER,
                title TEXT,
                overview TEXT,
                air_date TEXT,
                show_title TEXT,
                last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(tvdb_id, tmdb_id, season_number, episode_number)
            )
        """)

        conn.execute("CREATE INDEX IF NOT EXISTS idx_tv_episodes_lookup ON tv_episodes(tvdb_id, tmdb_id, season_number)")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                profile_id INTEGER,
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
                id_source TEXT,
                id_checked_at TIMESTAMP,
                ignore_match INTEGER DEFAULT 0,
                last_scraped TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(username, movie_slug)
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS service_cache (
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
                ignore_match INTEGER DEFAULT 0,
                last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS media_catalog (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tmdb_id TEXT,
                imdb_id TEXT,
                tvdb_id TEXT,
                movie_slug TEXT,
                title TEXT NOT NULL,
                release_year INTEGER,
                media_type TEXT DEFAULT 'movie',
                season_number INTEGER,
                episode_number INTEGER,
                poster_url TEXT,
                in_jellyfin INTEGER DEFAULT 0,
                jellyfin_id TEXT,
                jellyfin_link TEXT,
                in_radarr INTEGER DEFAULT 0,
                radarr_id TEXT,
                in_sonarr INTEGER DEFAULT 0,
                sonarr_id TEXT,
                available_seasons TEXT,
                status TEXT DEFAULT 'missing',
                requested_by_users TEXT,
                user_count INTEGER DEFAULT 0,
                last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS api_payload_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                direction TEXT,
                service TEXT,
                event_type TEXT,
                endpoint TEXT,
                status_code INTEGER,
                duration_ms INTEGER,
                headers_json TEXT,
                payload_json TEXT,
                response_json TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        conn.execute("CREATE TABLE IF NOT EXISTS ddns_cache (id INTEGER PRIMARY KEY AUTOINCREMENT, last_known_ip TEXT, last_update_status TEXT, raw_dynu_json_payload TEXT, last_sync_timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        conn.execute("CREATE TABLE IF NOT EXISTS user_profile_mappings (id INTEGER PRIMARY KEY AUTOINCREMENT, profile_name TEXT UNIQUE NOT NULL, letterboxd_username TEXT, serializd_username TEXT, jellyfin_user_id TEXT, is_active INTEGER DEFAULT 1, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        conn.execute("CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS task_status (task_name TEXT PRIMARY KEY, status TEXT DEFAULT 'idle', started_at TIMESTAMP, last_run TIMESTAMP, message TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS disk_cache (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, path TEXT, size REAL, last_scanned TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")

        _ensure_column(conn, "user_requests", "ignore_match INTEGER DEFAULT 0")
        _ensure_column(conn, "user_requests", "profile_id INTEGER")
        _ensure_column(conn, "service_cache", "ignore_match INTEGER DEFAULT 0")
        _ensure_column(conn, "task_status", "started_at TIMESTAMP")
        _ensure_column(conn, "user_profile_mappings", "is_active INTEGER DEFAULT 1")
        _ensure_column(conn, "tv_episodes", "show_title TEXT")

        # One-time seed of profiles from legacy requests. Re-running it on every start would resurrect deleted profiles.
        if not conn.execute("SELECT 1 FROM app_settings WHERE key = 'profile_seed_done'").fetchone():
            if conn.execute("SELECT COUNT(*) FROM user_profile_mappings").fetchone()[0] == 0:
                conn.execute("""
                    INSERT INTO user_profile_mappings (profile_name, letterboxd_username, serializd_username)
                    SELECT DISTINCT username, username, username FROM user_requests
                """)
            conn.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES ('profile_seed_done', '1')")
        conn.execute("""
            UPDATE user_requests 
            SET profile_id = (SELECT id FROM user_profile_mappings WHERE profile_name = user_requests.username) 
            WHERE profile_id IS NULL
        """)

        conn.execute("CREATE INDEX IF NOT EXISTS idx_user_requests_user ON user_requests(username)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_user_requests_slug ON user_requests(movie_slug)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_user_requests_tmdb ON user_requests(tmdb_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_service_cache_service ON service_cache(service, tmdb_id, tvdb_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_service_cache_ext ON service_cache(service, external_item_id)")
        
        conn.commit()
    finally:
        conn.close()
    
    reset_stale_tasks()

def log_api_payload(direction, service, event_type, endpoint, status_code, duration_ms, headers, payload, response):
    if get_setting("payload_logging_enabled", "0") != "1":
        return
    if get_setting(f"log_service_{service}", "0") != "1":
        return
        
    limit = int(get_setting("payload_retention_limit", "500"))
    headers_str = json.dumps(headers) if isinstance(headers, dict) else str(headers)
    payload_str = json.dumps(payload) if isinstance(payload, dict) else str(payload)
    resp_str = json.dumps(response) if isinstance(response, dict) else str(response)

    with get_connection() as conn:
        conn.execute("""
            INSERT INTO api_payload_logs 
            (direction, service, event_type, endpoint, status_code, duration_ms, headers_json, payload_json, response_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (direction, service, event_type, endpoint, status_code, duration_ms, headers_str, payload_str, resp_str))
        
        conn.execute(f"""
            DELETE FROM api_payload_logs 
            WHERE id NOT IN (
                SELECT id FROM api_payload_logs ORDER BY id DESC LIMIT {limit}
            )
        """)

def logged_request(service, event_type, method, url, **kwargs):
    start = time.time()
    try:
        res = requests.request(method, url, **kwargs)
        dur = int((time.time() - start) * 1000)
        
        try:
            resp_data = res.json()
        except Exception:
            resp_data = res.text[:2000]

        req_payload = kwargs.get('json') or kwargs.get('data') or kwargs.get('params')

        log_api_payload(
            "OUT",
            service,
            event_type,
            url,
            res.status_code,
            dur,
            dict(res.request.headers),
            req_payload,
            resp_data
        )
        return res
    except requests.RequestException as e:
        dur = int((time.time() - start) * 1000)
        req_payload = kwargs.get('json') or kwargs.get('data') or kwargs.get('params')
        req_headers = kwargs.get('headers') or {}
        log_api_payload(
            "OUT",
            service,
            event_type,
            url,
            0,
            dur,
            req_headers if isinstance(req_headers, dict) else dict(req_headers),
            req_payload,
            str(e)
        )
        raise

def get_payload_logs(limit=100, service=None, errors_only=False):
    sql = "SELECT * FROM api_payload_logs WHERE 1=1"
    params = []
    if service:
        sql += " AND service = ?"
        params.append(service)
    if errors_only:
        sql += " AND status_code >= 400"
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(int(limit))
    with get_connection() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]

def clear_payload_logs():
    with get_connection() as conn:
        conn.execute("DELETE FROM api_payload_logs")

def _split_title_year(title, year=None):
    if year is not None: year = int(year) if str(year).isdigit() else None
    if not title: return title, year
    m = _YEAR_RE.match(title.strip())
    if m: title, year = m.group(1), year or int(m.group(2))
    return title.strip(), year

def _parse_lists(value):
    return {x.strip() for x in (value or "").split(",") if x.strip()}

def _format_lists(lists):
    return ", ".join(sorted(lists))

def _clean(value):
    if value is None: return None
    value = str(value).strip()
    return value or None

def _upsert_film(conn, username, slug, title, profile_id=None, year=None, media_type=None, tmdb_id=None, imdb_id=None, tvdb_id=None, season_number=None, episode_number=None, poster_url=None, list_name=None):
    title, year = _split_title_year(title, year)
    tmdb_id, imdb_id, tvdb_id = _clean(tmdb_id), _clean(imdb_id), _clean(tvdb_id)

    row = conn.execute("SELECT id, letterboxd_lists FROM user_requests WHERE username = ? AND movie_slug = ?", (username, slug)).fetchone()
    if row:
        lists = _parse_lists(row["letterboxd_lists"])
        if list_name: lists.add(list_name)
        conn.execute("""
            UPDATE user_requests SET profile_id = COALESCE(?, profile_id), film_title = COALESCE(?, film_title), film_year = COALESCE(?, film_year), media_type = COALESCE(?, media_type), tmdb_id = COALESCE(?, tmdb_id), imdb_id = COALESCE(?, imdb_id), tvdb_id = COALESCE(?, tvdb_id), season_number = COALESCE(?, season_number), episode_number = COALESCE(?, episode_number), poster_url = COALESCE(?, poster_url), letterboxd_lists = ?, last_scraped = CURRENT_TIMESTAMP WHERE id = ?
        """, (profile_id, title, year, media_type, tmdb_id, imdb_id, tvdb_id, season_number, episode_number, poster_url, _format_lists(lists), row["id"]))
    else:
        conn.execute("""
            INSERT INTO user_requests (username, profile_id, film_title, film_year, movie_slug, tmdb_id, imdb_id, tvdb_id, media_type, season_number, episode_number, poster_url, letterboxd_lists, last_scraped)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """, (username, profile_id, title, year, slug, tmdb_id, imdb_id, tvdb_id, media_type or "movie", season_number, episode_number, poster_url, _format_lists({list_name} if list_name else set())))

def _prune_list(conn, username, list_name, seen_slugs):
    removed = 0
    rows = conn.execute("SELECT id, movie_slug, letterboxd_lists FROM user_requests WHERE username = ?", (username,)).fetchall()
    for r in rows:
        if r["movie_slug"] in seen_slugs: continue
        lists = _parse_lists(r["letterboxd_lists"])
        if list_name not in lists: continue
        lists.discard(list_name)
        if lists: conn.execute("UPDATE user_requests SET letterboxd_lists = ? WHERE id = ?", (_format_lists(lists), r["id"]))
        else: conn.execute("DELETE FROM user_requests WHERE id = ?", (r["id"],))
        removed += 1
    return removed

def upsert_movie_with_list(slug, title, list_name, username="default", profile_id=None):
    with get_connection() as conn:
        _upsert_film(conn, username, slug, title, profile_id=profile_id, list_name=list_name)

def sync_scraped_records(username, profile_id, list_type, records):
    seen = set()
    with get_connection() as conn:
        for item in records:
            slug = (item.get("movie_slug") or item.get("slug") or "").strip()
            if not slug: continue
            title = (item.get("film_title") or item.get("movie_title") or item.get("title") or item.get("full_display_name"))
            _upsert_film(conn, username, slug, title, profile_id=profile_id, year=item.get("film_year") or item.get("year"), media_type=item.get("media_type"), tmdb_id=item.get("tmdb_id"), imdb_id=item.get("imdb_id"), tvdb_id=item.get("tvdb_id"), season_number=item.get("season_number"), episode_number=item.get("episode_number"), poster_url=item.get("poster_url"), list_name=list_type)
            seen.add(slug)
        removed = _prune_list(conn, username, list_type, seen) if seen else 0
    return len(seen), removed, len(seen)

def update_film_identifiers(movie_slug, tmdb_id=None, imdb_id=None, tvdb_id=None, set_ignore=False):
    tmdb_id, imdb_id, tvdb_id = _clean(tmdb_id), _clean(imdb_id), _clean(tvdb_id)
    any_given = 1 if (tmdb_id or imdb_id or tvdb_id) else 0
    ignore_flag = 1 if set_ignore else 0
    with get_connection() as conn:
        conn.execute("""
            UPDATE user_requests 
            SET tmdb_id = COALESCE(?, tmdb_id), imdb_id = COALESCE(?, imdb_id), tvdb_id = COALESCE(?, tvdb_id), ignore_match = CASE WHEN ? THEN 1 ELSE ignore_match END, id_source = CASE WHEN ? THEN 'manual' ELSE id_source END 
            WHERE movie_slug = ?
        """, (tmdb_id, imdb_id, tvdb_id, ignore_flag, any_given, movie_slug))
        conn.execute("""
            UPDATE service_cache 
            SET tmdb_id = COALESCE(?, tmdb_id), imdb_id = COALESCE(?, imdb_id), tvdb_id = COALESCE(?, tvdb_id), ignore_match = CASE WHEN ? THEN 1 ELSE ignore_match END
            WHERE movie_slug = ?
        """, (tmdb_id, imdb_id, tvdb_id, ignore_flag, movie_slug))

def link_service_item_to_request(slug, action, service=None, external_id=None, tmdb_id=None, tvdb_id=None):
    with get_connection() as conn:
        if action == "dismiss":
            conn.execute("UPDATE user_requests SET ignore_match = 1 WHERE movie_slug = ?", (slug,))
            if service and external_id:
                conn.execute("UPDATE service_cache SET ignore_match = 1 WHERE service = ? AND external_item_id = ?", (service, external_id))
            return True
        if action == "approve":
            tmdb_id, tvdb_id = _clean(tmdb_id), _clean(tvdb_id)
            if service and external_id:
                srv_row = conn.execute("SELECT * FROM service_cache WHERE service = ? AND external_item_id = ?", (service, external_id)).fetchone()
                if srv_row:
                    tmdb_id, tvdb_id = tmdb_id or srv_row["tmdb_id"], tvdb_id or srv_row["tvdb_id"]
                    conn.execute("UPDATE service_cache SET movie_slug = ?, tmdb_id = COALESCE(?, tmdb_id), tvdb_id = COALESCE(?, tvdb_id) WHERE service = ? AND external_item_id = ?", (slug, tmdb_id, tvdb_id, service, external_id))
            if tmdb_id or tvdb_id:
                conn.execute("UPDATE user_requests SET tmdb_id = COALESCE(?, tmdb_id), tvdb_id = COALESCE(?, tvdb_id), id_source = 'manual' WHERE movie_slug = ?", (tmdb_id, tvdb_id, slug))
            return True
    return False

def get_fuzzy_matches():
    def normalize(text):
        if not text: return ""
        text = re.sub(r'[^\w\s]', '', text.lower())
        return re.sub(r'\b(the|a|an)\b', '', text).strip()
    suggestions = []
    with get_connection() as conn:
        unmatched_requests = conn.execute("SELECT id, movie_slug, film_title, film_year, media_type FROM user_requests WHERE (tmdb_id IS NULL AND tvdb_id IS NULL) AND ignore_match = 0 GROUP BY movie_slug").fetchall()
        # Fetch ALL valid items from the service cache, even those with TMDB IDs.
        unmatched_services = conn.execute("SELECT id, title, year, media_type, external_item_id, service, tmdb_id, tvdb_id FROM service_cache WHERE ignore_match = 0 AND title IS NOT NULL").fetchall()
    for req in unmatched_requests:
        req_norm, req_year = normalize(req["film_title"]), int(req["film_year"]) if req["film_year"] and str(req["film_year"]).isdigit() else None
        for srv in unmatched_services:
            if req["media_type"] != srv["media_type"]: continue
            srv_norm, srv_year = normalize(srv["title"]), int(srv["year"]) if srv["year"] and str(srv["year"]).isdigit() else None
            
            if not req_norm or not srv_norm or req_norm != srv_norm: continue
            if req_year and srv_year and abs(req_year - srv_year) > 1: continue
            
            suggestions.append({
                "request_slug": req["movie_slug"], 
                "request_title": req["film_title"], 
                "request_year": req["film_year"],
                "service_title": srv["title"], 
                "service_year": srv["year"], 
                "service": srv["service"], 
                "service_external_id": srv["external_item_id"],
                "tmdb_id": srv["tmdb_id"], 
                "tvdb_id": srv["tvdb_id"]
            })
            break
    return suggestions

def get_unconfirmed_guesses():
    with get_connection() as conn:
        return [dict(r) for r in conn.execute("SELECT movie_slug as request_slug, film_title as request_title, film_year as request_year, tmdb_id FROM user_requests WHERE id_source = 'tmdb-search' AND tmdb_id IS NOT NULL GROUP BY movie_slug").fetchall()]

def confirm_guessed_id(slug):
    with get_connection() as conn:
        conn.execute("UPDATE user_requests SET id_source = 'tmdb-search-confirmed' WHERE movie_slug = ?", (slug,))

def reject_guessed_id(slug):
    with get_connection() as conn:
        conn.execute("UPDATE user_requests SET tmdb_id = NULL, id_source = 'rejected' WHERE movie_slug = ?", (slug,))

def get_films_needing_ids(retry_after_days=7, limit=None):
    sql = "SELECT movie_slug, MAX(film_title) AS title, MAX(film_year) AS year, MAX(id_checked_at) AS checked FROM user_requests WHERE movie_slug NOT LIKE 'serializd-%' GROUP BY movie_slug HAVING MAX(NULLIF(tmdb_id, '')) IS NULL AND COALESCE(MAX(id_source), '') != 'rejected' AND (MAX(id_checked_at) IS NULL OR MAX(id_checked_at) < datetime('now', ?)) ORDER BY MAX(id_checked_at) IS NOT NULL, MAX(id_checked_at)"
    params = [f"-{int(retry_after_days)} days"]
    if limit:
        sql += " LIMIT ?"; params.append(int(limit))
    with get_connection() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]

def set_resolved_ids(movie_slug, tmdb_id, imdb_id=None, source="letterboxd"):
    with get_connection() as conn:
        conn.execute("UPDATE user_requests SET tmdb_id = COALESCE(?, tmdb_id), imdb_id = COALESCE(?, imdb_id), id_source = ?, id_checked_at = CURRENT_TIMESTAMP WHERE movie_slug = ?", (_clean(tmdb_id), _clean(imdb_id), source, movie_slug))

def mark_id_check_failed(movie_slug):
    with get_connection() as conn:
        conn.execute("UPDATE user_requests SET id_checked_at = CURRENT_TIMESTAMP WHERE movie_slug = ?", (movie_slug,))

def get_film_by_slug(slug):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM user_requests WHERE movie_slug = ? ORDER BY tmdb_id IS NULL LIMIT 1", (slug,)).fetchone()
        return dict(row) if row else None

def get_pending_requests(media_type="movie"):
    sql = """
        SELECT tmdb_id, tvdb_id, movie_slug, MAX(film_title) as full_display_name, COUNT(DISTINCT upm.id) as user_count, MAX(season_number) as season_number 
        FROM user_requests ur
        LEFT JOIN user_profile_mappings upm ON ur.profile_id = upm.id OR ur.username = upm.profile_name
        WHERE media_type = ? 
          AND letterboxd_lists LIKE '%Watchlist%' 
          AND letterboxd_lists NOT LIKE '%Watched%'
          AND (upm.is_active = 1 OR upm.id IS NULL)
          AND (id_source IS NULL OR id_source != 'tmdb-search')
        GROUP BY movie_slug
        ORDER BY user_count DESC, full_display_name ASC
    """
    with get_connection() as conn:
        return [dict(r) for r in conn.execute(sql, (media_type,)).fetchall()]

def update_service_cache(service, tmdb_id=None, imdb_id=None, tvdb_id=None, movie_slug=None, title=None, year=None, media_type=None, season_number=None, episode_number=None, status="unknown", in_library=0, external_item_id=None, details_json=None):
    tmdb_id, imdb_id, tvdb_id, external_item_id, movie_slug = _clean(tmdb_id), _clean(imdb_id), _clean(tvdb_id), _clean(external_item_id), _clean(movie_slug)
    where = "service = ? AND external_item_id = ?" if external_item_id else "service = ? AND external_item_id IS NULL AND tmdb_id IS ? AND tvdb_id IS ?"
    where_params = (service, external_item_id) if external_item_id else (service, tmdb_id, tvdb_id)
    with get_connection() as conn:
        cur = conn.execute(f"UPDATE service_cache SET tmdb_id = COALESCE(?, tmdb_id), imdb_id = COALESCE(?, imdb_id), tvdb_id = COALESCE(?, tvdb_id), movie_slug = COALESCE(?, movie_slug), title = COALESCE(?, title), year = COALESCE(?, year), media_type = COALESCE(?, media_type), season_number = COALESCE(?, season_number), episode_number = COALESCE(?, episode_number), status = ?, in_library = ?, details_json = COALESCE(?, details_json), last_updated = CURRENT_TIMESTAMP WHERE {where}", (tmdb_id, imdb_id, tvdb_id, movie_slug, title, year, media_type, season_number, episode_number, status, in_library, details_json) + where_params)
        if cur.rowcount == 0:
            conn.execute("INSERT INTO service_cache (service, tmdb_id, imdb_id, tvdb_id, movie_slug, title, year, media_type, season_number, episode_number, status, in_library, external_item_id, details_json, last_updated) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)", (service, tmdb_id, imdb_id, tvdb_id, movie_slug, title, year, media_type, season_number, episode_number, status, in_library, external_item_id, details_json))

def get_service_cache(service):
    with get_connection() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM service_cache WHERE service = ?", (service,)).fetchall()]

def prune_service_cache(service, older_than, media_type=None):
    with get_connection() as conn:
        if service == 'jellyfin':
            sql = "UPDATE service_cache SET in_library = 0, status = 'jf_missing' WHERE service = ? AND last_updated < ?"
            params = [service, older_than]
            if media_type and media_type != "all":
                if media_type == "tv":
                    sql += " AND media_type IN ('tv', 'series', 'episode')"
                elif media_type == "movie":
                    sql += " AND media_type = 'movie'"
                else:
                    sql += " AND media_type = ?"
                    params.append(media_type)
            return conn.execute(sql, params).rowcount
        else:
            sql = "DELETE FROM service_cache WHERE service = ? AND last_updated < ?"
            params = [service, older_than]
            if media_type and media_type != "all":
                if media_type == "tv":
                    sql += " AND media_type IN ('tv', 'series', 'episode')"
                elif media_type == "movie":
                    sql += " AND media_type = 'movie'"
                else:
                    sql += " AND media_type = ?"
                    params.append(media_type)
            return conn.execute(sql, params).rowcount

def reconcile_master_catalog():
    with get_connection() as conn:
        conn.execute("DELETE FROM media_catalog")
        catalog = {}
        
        def _get_key(mtype, tmdb, tvdb, title, year, slug):
            if mtype == "tv" and tvdb: return ("tv", "tvdb", str(tvdb))
            if tmdb: return (mtype, "tmdb", str(tmdb))
            if slug: return ("slug", slug)
            return (mtype, "title_year", (title or "").lower().strip(), int(year) if year and str(year).isdigit() else 0)

        # Updated to track Jellyfin seasons natively by multiple identifiers
        jf_seasons_tvdb = {}
        jf_seasons_tmdb = {}
        jf_url = get_setting("jellyfin_url", "").rstrip("/")
        
        for r in conn.execute("SELECT * FROM service_cache WHERE service = 'jellyfin'").fetchall():
            mtype = r["media_type"] or "movie"
            if mtype in ("episode", "season"):
                sn = r["season_number"]
                if sn is not None and r["status"] != "jf_missing":
                    if r["tvdb_id"]: jf_seasons_tvdb.setdefault(str(r["tvdb_id"]), set()).add(int(sn))
                    if r["tmdb_id"]: jf_seasons_tmdb.setdefault(str(r["tmdb_id"]), set()).add(int(sn))
                continue
            key = _get_key(mtype, r["tmdb_id"], r["tvdb_id"], r["title"], r["year"], r["movie_slug"])
            jellyfin_id = r["external_item_id"]
            
            is_jf_missing = (r["status"] == "jf_missing")
            
            catalog[key] = {
                "media_type": mtype, "tmdb_id": r["tmdb_id"], "imdb_id": r["imdb_id"], "tvdb_id": r["tvdb_id"],
                "movie_slug": r["movie_slug"] or f"jf-{jellyfin_id}", "title": r["title"] or f"Jellyfin Item {jellyfin_id}",
                "release_year": r["year"], "season_number": r["season_number"], "episode_number": r["episode_number"],
                "in_jellyfin": 0 if is_jf_missing else 1, 
                "jellyfin_id": jellyfin_id, "jellyfin_link": f"{jf_url}/web/index.html#/details?id={jellyfin_id}" if jf_url and not is_jf_missing else "",
                "in_radarr": 0, "radarr_id": None, "in_sonarr": 0, "sonarr_id": None, "status": "jf_missing" if is_jf_missing else "in_library",
                "requested_by_users": set(), "poster_url": None
            }

        for r in conn.execute("SELECT * FROM service_cache WHERE service = 'radarr'").fetchall():
            key = _get_key("movie", r["tmdb_id"], r["tvdb_id"], r["title"], r["year"], r["movie_slug"])
            if key not in catalog:
                catalog[key] = {
                    "media_type": "movie", "tmdb_id": r["tmdb_id"], "imdb_id": r["imdb_id"], "tvdb_id": r["tvdb_id"],
                    "movie_slug": f"radarr-{r['external_item_id']}", "title": r["title"] or f"Radarr Movie {r['tmdb_id']}",
                    "release_year": r["year"], "season_number": None, "episode_number": None,
                    "in_jellyfin": 0, "jellyfin_id": None, "jellyfin_link": None, "in_radarr": 1, "radarr_id": r["external_item_id"],
                    "in_sonarr": 0, "sonarr_id": None, "status": "queued", "requested_by_users": set(), "poster_url": None
                }
            else:
                catalog[key]["in_radarr"] = 1
                catalog[key]["radarr_id"] = r["external_item_id"]
                if catalog[key]["status"] not in ("in_library", "jf_missing"): catalog[key]["status"] = "queued"
                catalog[key]["tmdb_id"] = catalog[key]["tmdb_id"] or r["tmdb_id"]

        for r in conn.execute("SELECT * FROM service_cache WHERE service = 'sonarr'").fetchall():
            key = _get_key("tv", r["tmdb_id"], r["tvdb_id"], r["title"], r["year"], r["movie_slug"])
            if key not in catalog:
                catalog[key] = {
                    "media_type": "tv", "tmdb_id": r["tmdb_id"], "imdb_id": r["imdb_id"], "tvdb_id": r["tvdb_id"],
                    "movie_slug": f"sonarr-{r['external_item_id']}", "title": r["title"] or f"Sonarr Show {r['tvdb_id']}",
                    "release_year": r["year"], "season_number": None, "episode_number": None,
                    "in_jellyfin": 0, "jellyfin_id": None, "jellyfin_link": None, "in_radarr": 0, "radarr_id": None,
                    "in_sonarr": 1, "sonarr_id": r["external_item_id"], "status": "queued", "requested_by_users": set(), "poster_url": None
                }
            else:
                catalog[key]["in_sonarr"] = 1
                catalog[key]["sonarr_id"] = r["external_item_id"]
                if catalog[key]["status"] not in ("in_library", "jf_missing"): catalog[key]["status"] = "queued"
                catalog[key]["tvdb_id"] = catalog[key]["tvdb_id"] or r["tvdb_id"]

        for r in conn.execute("""
            SELECT ur.*, upm.profile_name as linked_profile 
            FROM user_requests ur 
            LEFT JOIN user_profile_mappings upm ON ur.profile_id = upm.id OR ur.username = upm.profile_name 
            WHERE upm.is_active = 1 OR upm.id IS NULL
        """).fetchall():
            mtype = r["media_type"] or "movie"
            key = _get_key(mtype, r["tmdb_id"], r["tvdb_id"], r["film_title"], r["film_year"], r["movie_slug"])
            req_name = r["linked_profile"] or r["username"]
            if key not in catalog:
                catalog[key] = {
                    "media_type": mtype, "tmdb_id": r["tmdb_id"], "imdb_id": r["imdb_id"], "tvdb_id": r["tvdb_id"],
                    "movie_slug": r["movie_slug"], "title": r["film_title"] or r["movie_slug"], "release_year": r["film_year"],
                    "season_number": r["season_number"], "episode_number": r["episode_number"],
                    "in_jellyfin": 0, "jellyfin_id": None, "jellyfin_link": None, "in_radarr": 0, "radarr_id": None,
                    "in_sonarr": 0, "sonarr_id": None, "status": "missing", "requested_by_users": {req_name}, "poster_url": r["poster_url"]
                }
            else:
                catalog[key]["requested_by_users"].add(req_name)
                catalog[key]["tmdb_id"] = catalog[key]["tmdb_id"] or r["tmdb_id"]
                catalog[key]["tvdb_id"] = catalog[key]["tvdb_id"] or r["tvdb_id"]
                if catalog[key]["movie_slug"].startswith("jf-") or catalog[key]["movie_slug"].startswith("radarr-") or catalog[key]["movie_slug"].startswith("sonarr-"):
                    catalog[key]["movie_slug"] = r["movie_slug"]

        rows_to_insert = []
        for key, item in catalog.items():
            avail_seasons = []
            if item["media_type"] == "tv":
                temp_seasons = set()
                if item["tvdb_id"] and str(item["tvdb_id"]) in jf_seasons_tvdb:
                    temp_seasons.update(jf_seasons_tvdb[str(item["tvdb_id"])])
                if item["tmdb_id"] and str(item["tmdb_id"]) in jf_seasons_tmdb:
                    temp_seasons.update(jf_seasons_tmdb[str(item["tmdb_id"])])
                
                avail_seasons = sorted(list(temp_seasons))
                if avail_seasons and item["status"] not in ("in_library", "jf_missing"):
                    item["status"] = "partially_available"
                    
            users = sorted(list(item["requested_by_users"]), key=str.lower)
            if item["in_jellyfin"] and not users: users = ["Jellyfin"]
            elif item["in_radarr"] and not users: users = ["Radarr"]
            elif item["in_sonarr"] and not users: users = ["Sonarr"]
            
            rows_to_insert.append((
                item["tmdb_id"], item["imdb_id"], item["tvdb_id"], item["movie_slug"], item["title"], item["release_year"], item["media_type"],
                item["season_number"], item["episode_number"], item["poster_url"], item["in_jellyfin"], item["jellyfin_id"], item["jellyfin_link"],
                item["in_radarr"], item["radarr_id"], item["in_sonarr"], item["sonarr_id"], json.dumps(avail_seasons), item["status"], ",".join(users), len(users)
            ))
            
        conn.executemany("INSERT INTO media_catalog (tmdb_id, imdb_id, tvdb_id, movie_slug, title, release_year, media_type, season_number, episode_number, poster_url, in_jellyfin, jellyfin_id, jellyfin_link, in_radarr, radarr_id, in_sonarr, sonarr_id, available_seasons, status, requested_by_users, user_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows_to_insert)

def get_media_catalog():
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM media_catalog ORDER BY user_count DESC, title ASC").fetchall()
        results = []
        for r in rows:
            d = dict(r)
            d["available_seasons"] = json.loads(d["available_seasons"]) if d["available_seasons"] else []
            d["users_list"] = [u.strip() for u in (d["requested_by_users"] or "").split(",") if u.strip()]
            results.append(d)
        return results

def get_catalog_item_by_slug(slug):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM media_catalog WHERE movie_slug = ? LIMIT 1", (slug,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["available_seasons"] = json.loads(d["available_seasons"]) if d["available_seasons"] else []
        d["users_list"] = [u.strip() for u in (d["requested_by_users"] or "").split(",") if u.strip()]
        return d

def get_setting(key, default_val=""):
    with get_connection() as conn:
        row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row and row["value"] is not None else default_val

def set_setting(key, value):
    with get_connection() as conn:
        conn.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)", (key, str(value)))

def get_all_settings():
    with get_connection() as conn:
        return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM app_settings")}

def update_task_status(task_name, status, message=""):
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with get_connection() as conn:
        row = conn.execute("SELECT started_at FROM task_status WHERE task_name = ?", (task_name,)).fetchone()
        started_at = now_str if status == "in_progress" else (row["started_at"] if row else None)
        conn.execute("""
            INSERT INTO task_status (task_name, status, started_at, last_run, message) 
            VALUES (?, ?, ?, ?, ?) 
            ON CONFLICT(task_name) DO UPDATE SET 
                status = excluded.status, 
                started_at = COALESCE(excluded.started_at, task_status.started_at),
                last_run = CASE WHEN excluded.status IN ('completed', 'failed') THEN ? ELSE task_status.last_run END, 
                message = excluded.message
        """, (task_name, status, started_at, now_str, message, now_str))

def update_job_status(job_name, status, message=""):
    update_task_status(job_name, status, message)

def get_all_task_statuses():
    default_tasks = ["scraper_letterboxd", "scraper_serializd", "radarr", "sonarr", "jellyfin_movies", "jellyfin_tv", "id_resolver", "disk_scanner", "all"]
    statuses = {t: {"task_name": t, "status": "idle", "started_at": None, "last_run": None, "message": "No active logs"} for t in default_tasks}
    with get_connection() as conn:
        for row in conn.execute("SELECT * FROM task_status"):
            statuses[row["task_name"]] = dict(row)
    return statuses

def get_ddns_cache():
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM ddns_cache ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None

def get_disk_file_cache():
    with get_connection() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM disk_cache ORDER BY title")]

def get_active_profiles():
    with get_connection() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM user_profile_mappings WHERE is_active = 1 ORDER BY profile_name ASC").fetchall()]

def get_user_profile_mappings():
    with get_connection() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM user_profile_mappings ORDER BY profile_name ASC").fetchall()]
    
def save_user_profile_mapping(profile_name, letterboxd_username=None, serializd_username=None, jellyfin_user_id=None, is_active=None):
    """Creates a profile, or updates an existing one. Arguments left as None keep their stored value."""
    profile_name = profile_name.strip()
    values = {"letterboxd_username": letterboxd_username, "serializd_username": serializd_username, "jellyfin_user_id": jellyfin_user_id, "is_active": is_active}
    changes = {k: (v.strip() if isinstance(v, str) else v) for k, v in values.items() if v is not None}
    with get_connection() as conn:
        row = conn.execute("SELECT id FROM user_profile_mappings WHERE profile_name = ?", (profile_name,)).fetchone()
        if row:
            if changes:
                assignments = ", ".join(f"{k} = ?" for k in changes)
                conn.execute(f"UPDATE user_profile_mappings SET {assignments} WHERE id = ?", (*changes.values(), row["id"]))
        else:
            conn.execute(
                "INSERT INTO user_profile_mappings (profile_name, letterboxd_username, serializd_username, jellyfin_user_id, is_active) VALUES (?, ?, ?, ?, ?)",
                (profile_name, changes.get("letterboxd_username", ""), changes.get("serializd_username", ""), changes.get("jellyfin_user_id", ""), changes.get("is_active", 1)),
            )

def toggle_profile_mapping(profile_id, is_active):
    with get_connection() as conn:
        conn.execute("UPDATE user_profile_mappings SET is_active = ? WHERE id = ?", (is_active, profile_id))

def delete_user_profile_mapping(profile_id):
    with get_connection() as conn:
        conn.execute("DELETE FROM user_profile_mappings WHERE id = ?", (profile_id,))
        conn.execute("UPDATE user_requests SET profile_id = NULL WHERE profile_id = ?", (profile_id,))

def _fetch_tmdb_episodes(tmdb_id):
    api_key = get_setting("tmdb_api_key", "").strip()
    if not api_key: 
        return None, None
    
    headers, params = {}, {}
    if api_key.startswith("eyJ"): 
        headers["Authorization"] = f"Bearer {api_key}"
    else: 
        params["api_key"] = api_key
    
    params["append_to_response"] = "external_ids"
    try:
        r = requests.get(f"https://api.themoviedb.org/3/tv/{tmdb_id}", headers=headers, params=params, timeout=HTTP_TIMEOUT)
        if r.status_code != 200: 
            return None, None
        
        data = r.json()
        fetched_tvdb_id = data.get("external_ids", {}).get("tvdb_id")
        
        episodes = []
        for season in data.get("seasons", []):
            sn = season.get("season_number")
            if sn is None: 
                continue 
            
            sr = requests.get(f"https://api.themoviedb.org/3/tv/{tmdb_id}/season/{sn}", headers=headers, params={"api_key": params.get("api_key")}, timeout=HTTP_TIMEOUT)
            if sr.status_code == 200:
                s_data = sr.json()
                for ep in s_data.get("episodes", []):
                    episodes.append({
                        "seasonNumber": ep.get("season_number"),
                        "episodeNumber": ep.get("episode_number"),
                        "title": ep.get("name"),
                        "overview": ep.get("overview"),
                        "airDate": ep.get("air_date")
                    })
        return episodes, fetched_tvdb_id
    except Exception as e:
        logger.warning(f"Failed fetching TMDB episodes for TMDB {tmdb_id}: {e}")
        return None, None

def _tv_id_clause(tvdb_id, tmdb_id, column_prefix=""):
    """SQL predicate matching a show by whichever IDs it actually has.

    Missing IDs must not be compared at all: tv_episodes stores the literal 'NONE' for a missing
    ID (to keep its UNIQUE constraint working), so comparing against it would match every other
    show that also lacks that ID.
    """
    clauses, params = [], []
    if tvdb_id:
        clauses.append(f"{column_prefix}tvdb_id = ?")
        params.append(str(tvdb_id))
    if tmdb_id:
        clauses.append(f"{column_prefix}tmdb_id = ?")
        params.append(str(tmdb_id))
    if not clauses:
        return "0", []
    return "(" + " OR ".join(clauses) + ")", params

def get_full_tv_show_episodes(identifier):
    with get_connection() as conn:
        show_row = conn.execute("SELECT * FROM media_catalog WHERE tvdb_id = ? OR movie_slug = ? LIMIT 1", (identifier, identifier)).fetchone()
        if not show_row: 
            return {}
        show = dict(show_row)
    
    tvdb_id = show.get("tvdb_id")
    tmdb_id = show.get("tmdb_id")
    slug = show.get("movie_slug")
    
    jf_episodes = set()
    jf_missing_episodes = set()
    with get_connection() as conn:
        jf_clause, jf_params = _tv_id_clause(tvdb_id, tmdb_id)
        rows = conn.execute(
            f"SELECT season_number, episode_number, status FROM service_cache WHERE service = 'jellyfin' AND media_type = 'episode' AND ({jf_clause} OR movie_slug LIKE ?)",
            jf_params + [f"{slug}%"]
        ).fetchall()
        for r in rows:
            if r["season_number"] is not None and r["episode_number"] is not None:
                if r["status"] == "jf_missing":
                    jf_missing_episodes.add((int(r["season_number"]), int(r["episode_number"])))
                else:
                    jf_episodes.add((int(r["season_number"]), int(r["episode_number"])))
                
    provider = get_setting("tv_metadata_provider", "tmdb")
    base_eps = []
    sonarr_eps_map = {}
    
    if tvdb_id:
        try:
            import sonarr_client
            s_eps = sonarr_client.get_episodes_by_tvdb(tvdb_id)
            for ep in s_eps:
                sonarr_eps_map[(ep.get("seasonNumber", 0), ep.get("episodeNumber", 0))] = ep
            if provider == "sonarr":
                base_eps = s_eps
        except Exception as e:
            logger.warning(f"Failed fetching Sonarr episodes for TVDB {tvdb_id}: {e}")
            
    if provider == "tmdb" or (not base_eps and tmdb_id):
        if tmdb_id:
            tmdb_eps, auto_tvdb_id = _fetch_tmdb_episodes(tmdb_id)
            if tmdb_eps:
                base_eps = tmdb_eps
            if auto_tvdb_id and str(auto_tvdb_id) != str(tvdb_id):
                update_film_identifiers(slug, tvdb_id=auto_tvdb_id)
                tvdb_id = auto_tvdb_id
                try:
                    import sonarr_client
                    s_eps = sonarr_client.get_episodes_by_tvdb(tvdb_id)
                    for ep in s_eps:
                        sonarr_eps_map[(ep.get("seasonNumber", 0), ep.get("episodeNumber", 0))] = ep
                except Exception:
                    pass
                    
    seasons = {}
    if not base_eps:
        for (sn, en) in jf_episodes:
            base_eps.append({"seasonNumber": sn, "episodeNumber": en, "title": f"Episode {en}"})
            
    processed = set()
    for ep in base_eps:
        sn = ep.get("seasonNumber", 0)
        en = ep.get("episodeNumber", 0)
        processed.add((sn, en))
        
        s_ep = sonarr_eps_map.get((sn, en), {})
        
        ep_data = {
            "episode_id": s_ep.get("id"),
            "season_number": sn,
            "episode_number": en,
            "title": ep.get("title") or ep.get("name") or s_ep.get("title") or f"Episode {en}",
            "overview": ep.get("overview") or s_ep.get("overview", ""),
            "has_file": s_ep.get("hasFile", False),
            "monitored": s_ep.get("monitored", False),
            "in_jellyfin": (sn, en) in jf_episodes,
            "jf_missing": (sn, en) in jf_missing_episodes,
            "air_date": ep.get("airDate") or ep.get("air_date") or s_ep.get("airDate")
        }
        seasons.setdefault(sn, []).append(ep_data)
        
    for (sn, en) in jf_episodes:
        if (sn, en) not in processed:
            seasons.setdefault(sn, []).append({
                "episode_id": None, "season_number": sn, "episode_number": en,
                "title": f"Episode {en}", "overview": "", "has_file": False, "monitored": False,
                "in_jellyfin": True, "jf_missing": False, "air_date": None
            })
            processed.add((sn, en))
            
    for (sn, en) in jf_missing_episodes:
        if (sn, en) not in processed:
            seasons.setdefault(sn, []).append({
                "episode_id": None, "season_number": sn, "episode_number": en,
                "title": f"Episode {en}", "overview": "", "has_file": False, "monitored": False,
                "in_jellyfin": False, "jf_missing": True, "air_date": None
            })
            processed.add((sn, en))
            
    for sn in seasons:
        seasons[sn].sort(key=lambda x: x["episode_number"])

    return seasons

def get_tv_show_details(identifier):
    """Fast, lightweight lookup for top-level show details and season list w/ accurate counts."""
    with get_connection() as conn:
        show_row = conn.execute(
            "SELECT * FROM media_catalog WHERE tvdb_id = ? OR movie_slug = ? LIMIT 1",
            (identifier, identifier)
        ).fetchone()
        if not show_row:
            return None, []
        
        show = dict(show_row)
        show["available_seasons"] = json.loads(show["available_seasons"]) if show["available_seasons"] else []
        show["users_list"] = [u.strip() for u in (show["requested_by_users"] or "").split(",") if u.strip()]

    # Determine total season numbers (from Jellyfin cache, Sonarr, or TMDB)
    season_numbers = set([int(s) for s in show["available_seasons"] if s is not None])
    
    # Query cached tv_episodes table for known seasons
    with get_connection() as conn:
        id_clause, id_params = _tv_id_clause(show.get("tvdb_id"), show.get("tmdb_id"))
        s_rows = conn.execute(
            f"SELECT DISTINCT season_number FROM tv_episodes WHERE {id_clause}", id_params
        ).fetchall()
        for r in s_rows:
            if r["season_number"] is not None:
                season_numbers.add(int(r["season_number"]))

    # If no seasons are known yet, fetch high-level season count from TMDB
    if not season_numbers and show.get("tmdb_id"):
        api_key = get_setting("tmdb_api_key", "").strip()
        if api_key:
            headers, params = {}, {}
            if api_key.startswith("eyJ"): headers["Authorization"] = f"Bearer {api_key}"
            else: params["api_key"] = api_key
            try:
                r = requests.get(f"https://api.themoviedb.org/3/tv/{show['tmdb_id']}", headers=headers, params=params, timeout=5)
                if r.status_code == 200:
                    for s in r.json().get("seasons", []):
                        if s.get("season_number") is not None and s.get("season_number") > 0:
                            season_numbers.add(s["season_number"])
            except Exception as e:
                logger.warning(f"Failed fetching season summary from TMDB: {e}")

    # Fallback to Sonarr if TMDB yields 0 results or is unconfigured
    if not season_numbers and show.get("tvdb_id"):
        try:
            import sonarr_client
            series_info = sonarr_client.get_series_by_tvdb(show["tvdb_id"])
            if series_info and "seasons" in series_info:
                for s in series_info["seasons"]:
                    if s.get("seasonNumber") is not None and s.get("seasonNumber") > 0:
                        season_numbers.add(s["seasonNumber"])
        except Exception as e:
            logger.warning(f"Failed fetching season summary from Sonarr: {e}")

    # Compute accurate stats per season
    seasons_data = []
    jf_clause, jf_params = _tv_id_clause(show.get("tvdb_id"), show.get("tmdb_id"))
    jf_clause = f"({jf_clause} OR movie_slug LIKE ?)"
    jf_params = jf_params + [f"{show.get('movie_slug')}%"]
    with get_connection() as conn:
        for sn in sorted(list(season_numbers)):
            # Active in Jellyfin (Fully Synced)
            jf_count = conn.execute(f"SELECT COUNT(*) FROM service_cache WHERE service='jellyfin' AND media_type='episode' AND season_number=? AND status != 'jf_missing' AND {jf_clause}", [sn] + jf_params).fetchone()[0]
            # Total strictly known locally
            tot_count = conn.execute(f"SELECT COUNT(*) FROM (SELECT DISTINCT episode_number FROM tv_episodes WHERE season_number=? AND {id_clause})", [sn] + id_params).fetchone()[0]
            # Formally marked missing during a previous scan
            jf_missing = conn.execute(f"SELECT COUNT(*) FROM service_cache WHERE service='jellyfin' AND media_type='episode' AND season_number=? AND status = 'jf_missing' AND {jf_clause}", [sn] + jf_params).fetchone()[0]
            
            # Combine logic. Always respect max known episodes (even if tv_episodes cache hasn't loaded yet)
            actual_total = max(tot_count, jf_count + jf_missing)
            missing_count = max(0, actual_total - jf_count)
            
            seasons_data.append({
                "season_number": sn,
                "in_jellyfin": jf_count,
                "missing": missing_count,
                "total": actual_total
            })

    return show, seasons_data

def get_season_episodes_lazy(identifier, season_number):
    """Lazy-loads and caches episodes for a single season on demand."""
    with get_connection() as conn:
        show_row = conn.execute(
            "SELECT * FROM media_catalog WHERE tvdb_id = ? OR movie_slug = ? LIMIT 1",
            (identifier, identifier)
        ).fetchone()
        if not show_row:
            return []
        show = dict(show_row)

    tvdb_id = show.get("tvdb_id")
    tmdb_id = show.get("tmdb_id")
    slug = show.get("movie_slug")
    season_num = int(season_number)

    # 1. Check local persistent SQLite cache for this specific season
    cached_by_episode = {}
    id_clause, id_params = _tv_id_clause(tvdb_id, tmdb_id)
    with get_connection() as conn:
        rows = conn.execute(
            f"SELECT season_number, episode_number, title, overview, air_date FROM tv_episodes WHERE {id_clause} AND season_number = ? ORDER BY last_updated, id",
            id_params + [season_num]
        ).fetchall()
        # A show whose TVDB/TMDB ID was resolved later can have the same episode cached under two ID pairs; the newest row wins.
        for r in rows:
            cached_by_episode[r["episode_number"]] = {
                "seasonNumber": r["season_number"],
                "episodeNumber": r["episode_number"],
                "title": r["title"],
                "overview": r["overview"],
                "airDate": r["air_date"]
            }
    cached_eps = [cached_by_episode[en] for en in sorted(cached_by_episode, key=lambda n: (n is None, n))]

    # 2. On Cache Miss: Fetch metadata for this single season from TMDB or Sonarr
    if not cached_eps:
        fetched_eps = []
        provider = get_setting("tv_metadata_provider", "tmdb")

        if provider == "tmdb" and tmdb_id:
            api_key = get_setting("tmdb_api_key", "").strip()
            if api_key:
                headers, params = {}, {}
                if api_key.startswith("eyJ"): headers["Authorization"] = f"Bearer {api_key}"
                else: params["api_key"] = api_key
                try:
                    r = requests.get(f"https://api.themoviedb.org/3/tv/{tmdb_id}/season/{season_num}", headers=headers, params=params, timeout=5)
                    if r.status_code == 200:
                        for ep in r.json().get("episodes", []):
                            fetched_eps.append({
                                "seasonNumber": ep.get("season_number"),
                                "episodeNumber": ep.get("episode_number"),
                                "title": ep.get("name"),
                                "overview": ep.get("overview"),
                                "airDate": ep.get("air_date")
                            })
                except Exception as e:
                    logger.warning(f"Failed fetching Season {season_num} from TMDB: {e}")

        # Fallback to Sonarr if TMDB yielded no results
        if not fetched_eps and tvdb_id:
            try:
                import sonarr_client
                all_s_eps = sonarr_client.get_episodes_by_tvdb(tvdb_id)
                fetched_eps = [
                    {"seasonNumber": ep["seasonNumber"], "episodeNumber": ep["episodeNumber"], "title": ep.get("title"), "overview": ep.get("overview"), "airDate": ep.get("airDate")}
                    for ep in all_s_eps if ep.get("seasonNumber") == season_num
                ]
            except Exception as e:
                logger.warning(f"Failed fetching Season {season_num} from Sonarr: {e}")

        # Write fetched season episodes to local tv_episodes persistent cache
        if fetched_eps:
            with get_connection() as conn:
                for ep in fetched_eps:
                    conn.execute("""
                        INSERT INTO tv_episodes (tvdb_id, tmdb_id, season_number, episode_number, title, overview, air_date, show_title, last_updated)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                        ON CONFLICT(tvdb_id, tmdb_id, season_number, episode_number) DO UPDATE SET
                            title = excluded.title,
                            overview = excluded.overview,
                            air_date = excluded.air_date,
                            show_title = excluded.show_title,
                            last_updated = CURRENT_TIMESTAMP
                    """, (str(tvdb_id) if tvdb_id else "NONE", str(tmdb_id) if tmdb_id else "NONE", season_num, ep["episodeNumber"], ep["title"], ep["overview"], ep["airDate"], show.get("title")))
            cached_eps = fetched_eps

    # 3. Overlay Live Operational Status (Jellyfin cache & Sonarr live flags)
    jf_episodes = set()
    jf_missing_episodes = set()
    with get_connection() as conn:
        jf_clause, jf_params = _tv_id_clause(tvdb_id, tmdb_id)
        rows = conn.execute(
            f"SELECT episode_number, status FROM service_cache WHERE service = 'jellyfin' AND media_type = 'episode' AND season_number = ? AND ({jf_clause} OR movie_slug LIKE ?)",
            [season_num] + jf_params + [f"{slug}%"]
        ).fetchall()
        for r in rows:
            if r["episode_number"] is not None:
                if r["status"] == "jf_missing":
                    jf_missing_episodes.add(int(r["episode_number"]))
                else:
                    jf_episodes.add(int(r["episode_number"]))

    sonarr_eps_map = {}
    if tvdb_id:
        try:
            import sonarr_client
            for ep in sonarr_client.get_episodes_by_tvdb(tvdb_id):
                if ep.get("seasonNumber") == season_num:
                    sonarr_eps_map[ep.get("episodeNumber", 0)] = ep
        except Exception:
            pass

    season_data = []
    processed_eps = set()

    for ep in cached_eps:
        en = ep.get("episodeNumber", 0)
        processed_eps.add(en)
        s_ep = sonarr_eps_map.get(en, {})

        season_data.append({
            "episode_id": s_ep.get("id"),
            "season_number": season_num,
            "episode_number": en,
            "title": ep.get("title") or s_ep.get("title") or f"Episode {en}",
            "overview": ep.get("overview") or s_ep.get("overview", ""),
            "has_file": s_ep.get("hasFile", False),
            "monitored": s_ep.get("monitored", False),
            "in_jellyfin": en in jf_episodes,
            "jf_missing": en in jf_missing_episodes,
            "air_date": ep.get("airDate") or s_ep.get("airDate")
        })

    # Include Jellyfin local files if missing from official guide
    for en in jf_episodes:
        if en not in processed_eps:
            season_data.append({
                "episode_id": None, "season_number": season_num, "episode_number": en,
                "title": f"Episode {en}", "overview": "", "has_file": False, "monitored": False,
                "in_jellyfin": True, "jf_missing": False, "air_date": None
            })
            processed_eps.add(en)
            
    for en in jf_missing_episodes:
        if en not in processed_eps:
            season_data.append({
                "episode_id": None, "season_number": season_num, "episode_number": en,
                "title": f"Episode {en}", "overview": "", "has_file": False, "monitored": False,
                "in_jellyfin": False, "jf_missing": True, "air_date": None
            })
            processed_eps.add(en)

    season_data.sort(key=lambda x: x["episode_number"])
    return season_data

def get_movie_details_by_slug(slug):
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM media_catalog WHERE movie_slug = ? AND media_type = 'movie' LIMIT 1", (slug,)).fetchone()
        if not row: return None
        movie = dict(row)
        movie["users_list"] = [u.strip() for u in (movie["requested_by_users"] or "").split(",") if u.strip()]
        return movie

def init_ddns_db():
    with get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ddns_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                old_ip TEXT,
                new_ip TEXT,
                status TEXT NOT NULL,
                message TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

def log_ddns_history(old_ip, new_ip, status, message):
    init_ddns_db()
    with get_connection() as conn:
        conn.execute("INSERT INTO ddns_history (old_ip, new_ip, status, message) VALUES (?, ?, ?, ?)", (old_ip or "N/A", new_ip or "N/A", status, message))

def get_ddns_history(limit=10):
    init_ddns_db()
    with get_connection() as conn:
        return [dict(row) for row in conn.execute("SELECT old_ip, new_ip, status, message, created_at FROM ddns_history ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]

def add_list_column_if_missing():
    init_db()

def save_disk_file_cache(file_data_list):
    rows = [(item.get("parsed_title") or item.get("title") or item.get("file_name"), item.get("file_path") or item.get("path"), item.get("file_size") if item.get("file_size") is not None else item.get("size")) for item in file_data_list]
    with get_connection() as conn:
        conn.execute("DELETE FROM disk_cache")
        conn.executemany("INSERT INTO disk_cache (title, path, size) VALUES (?, ?, ?)", rows)

if __name__ == "__main__":
    init_db()
    reconcile_master_catalog()