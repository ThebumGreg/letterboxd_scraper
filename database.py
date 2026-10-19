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

from config import DB_PATH

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

        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_requests (
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
        conn.execute("CREATE TABLE IF NOT EXISTS user_profile_mappings (id INTEGER PRIMARY KEY AUTOINCREMENT, profile_name TEXT UNIQUE NOT NULL, letterboxd_username TEXT, serializd_username TEXT, jellyfin_user_id TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        conn.execute("CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS task_status (task_name TEXT PRIMARY KEY, status TEXT DEFAULT 'idle', started_at TIMESTAMP, last_run TIMESTAMP, message TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS disk_cache (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, path TEXT, size REAL, last_scanned TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")

        _ensure_column(conn, "user_requests", "ignore_match INTEGER DEFAULT 0")
        _ensure_column(conn, "service_cache", "ignore_match INTEGER DEFAULT 0")
        _ensure_column(conn, "task_status", "started_at TIMESTAMP")

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
    """Safely logs raw API payloads into the database, respecting the global toggle and cap limits."""
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
    """A wrapper for requests.request that measures timing and routes data to the payload logger."""
    start = time.time()
    try:
        res = requests.request(method, url, **kwargs)
        dur = int((time.time() - start) * 1000)
        
        is_error = res.status_code >= 400
        is_state_change = method.upper() in ('POST', 'PUT', 'DELETE', 'PATCH')
        
        if is_error or is_state_change:
            try:
                resp_data = res.json()
            except Exception:
                resp_data = res.text[:2000]
                
            log_api_payload("OUT", service, event_type, url, res.status_code, dur, dict(res.request.headers), kwargs.get('json') or kwargs.get('data') or kwargs.get('params'), resp_data)
        return res
    except requests.RequestException as e:
        dur = int((time.time() - start) * 1000)
        log_api_payload("OUT", service, event_type, url, 0, dur, kwargs.get('headers', {}), kwargs.get('json') or kwargs.get('data') or kwargs.get('params'), str(e))
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
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

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

def _upsert_film(conn, username, slug, title, year=None, media_type=None, tmdb_id=None, imdb_id=None, tvdb_id=None, season_number=None, episode_number=None, poster_url=None, list_name=None):
    title, year = _split_title_year(title, year)
    tmdb_id, imdb_id, tvdb_id = _clean(tmdb_id), _clean(imdb_id), _clean(tvdb_id)

    row = conn.execute("SELECT id, letterboxd_lists FROM user_requests WHERE username = ? AND movie_slug = ?", (username, slug)).fetchone()
    if row:
        lists = _parse_lists(row["letterboxd_lists"])
        if list_name: lists.add(list_name)
        conn.execute("""
            UPDATE user_requests SET film_title = COALESCE(?, film_title), film_year = COALESCE(?, film_year), media_type = COALESCE(?, media_type), tmdb_id = COALESCE(?, tmdb_id), imdb_id = COALESCE(?, imdb_id), tvdb_id = COALESCE(?, tvdb_id), season_number = COALESCE(?, season_number), episode_number = COALESCE(?, episode_number), poster_url = COALESCE(?, poster_url), letterboxd_lists = ?, last_scraped = CURRENT_TIMESTAMP WHERE id = ?
        """, (title, year, media_type, tmdb_id, imdb_id, tvdb_id, season_number, episode_number, poster_url, _format_lists(lists), row["id"]))
    else:
        conn.execute("""
            INSERT INTO user_requests (username, film_title, film_year, movie_slug, tmdb_id, imdb_id, tvdb_id, media_type, season_number, episode_number, poster_url, letterboxd_lists, last_scraped)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """, (username, title, year, slug, tmdb_id, imdb_id, tvdb_id, media_type or "movie", season_number, episode_number, poster_url, _format_lists({list_name} if list_name else set())))

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

def upsert_movie_with_list(slug, title, list_name, username="default"):
    with get_connection() as conn:
        _upsert_film(conn, username, slug, title, list_name=list_name)

def prune_list(username, list_name, seen_slugs):
    seen_slugs = set(seen_slugs or [])
    if not seen_slugs: return 0
    with get_connection() as conn:
        return _prune_list(conn, username, list_name, seen_slugs)

def sync_scraped_records(username, list_type, records, run_timestamp=None):
    seen = set()
    with get_connection() as conn:
        for item in records:
            slug = (item.get("movie_slug") or item.get("slug") or "").strip()
            if not slug: continue
            title = (item.get("film_title") or item.get("movie_title") or item.get("title") or item.get("full_display_name"))
            _upsert_film(conn, username, slug, title, year=item.get("film_year") or item.get("year"), media_type=item.get("media_type"), tmdb_id=item.get("tmdb_id"), imdb_id=item.get("imdb_id"), tvdb_id=item.get("tvdb_id"), season_number=item.get("season_number"), episode_number=item.get("episode_number"), poster_url=item.get("poster_url"), list_name=list_type)
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
            SET tmdb_id = COALESCE(?, tmdb_id), imdb_id = COALESCE(?, imdb_id), tvdb_id = COALESCE(?, tvdb_id), ignore_match = ?, id_source = CASE WHEN ? THEN 'manual' ELSE id_source END 
            WHERE movie_slug = ?
        """, (tmdb_id, imdb_id, tvdb_id, ignore_flag, any_given, movie_slug))
        conn.execute("""
            UPDATE service_cache 
            SET tmdb_id = COALESCE(?, tmdb_id), imdb_id = COALESCE(?, imdb_id), tvdb_id = COALESCE(?, tvdb_id), ignore_match = ?
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
        unmatched_services = conn.execute("SELECT id, title, year, media_type, external_item_id, service FROM service_cache WHERE (tmdb_id IS NULL AND tvdb_id IS NULL) AND ignore_match = 0 AND title IS NOT NULL").fetchall()

    for req in unmatched_requests:
        req_norm, req_year = normalize(req["film_title"]), int(req["film_year"]) if req["film_year"] and str(req["film_year"]).isdigit() else None
        for srv in unmatched_services:
            if req["media_type"] != srv["media_type"]: continue
            srv_norm, srv_year = normalize(srv["title"]), int(srv["year"]) if srv["year"] and str(srv["year"]).isdigit() else None
            if not req_norm or not srv_norm or req_norm != srv_norm: continue
            if req_year and srv_year and abs(req_year - srv_year) > 1: continue

            suggestions.append({
                "request_slug": req["movie_slug"], "request_title": req["film_title"], "request_year": req["film_year"],
                "service_title": srv["title"], "service_year": srv["year"], "service": srv["service"], "service_external_id": srv["external_item_id"]
            })
            break
    return suggestions

def get_unconfirmed_guesses():
    with get_connection() as conn:
        rows = conn.execute("SELECT movie_slug as request_slug, film_title as request_title, film_year as request_year, tmdb_id FROM user_requests WHERE id_source = 'tmdb-search' AND tmdb_id IS NOT NULL GROUP BY movie_slug").fetchall()
        return [dict(r) for r in rows]

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
    where_sql = "WHERE media_type = ? AND letterboxd_lists LIKE '%Watchlist%' AND (id_source IS NULL OR id_source != 'tmdb-search')"
    with get_connection() as conn:
        rows = conn.execute(f"SELECT tmdb_id, tvdb_id, movie_slug, MAX(film_title) as full_display_name, COUNT(DISTINCT username) as user_count, MAX(season_number) as season_number FROM user_requests {where_sql} GROUP BY movie_slug", (media_type,)).fetchall()
        return [dict(r) for r in rows]

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

def prune_service_cache(service, older_than):
    with get_connection() as conn:
        return conn.execute("DELETE FROM service_cache WHERE service = ? AND last_updated < ?", (service, older_than)).rowcount

def reconcile_master_catalog():
    with get_connection() as conn:
        conn.execute("DELETE FROM media_catalog")
        catalog = {}
        
        def _get_key(mtype, tmdb, tvdb, title, year, slug):
            if mtype == "tv" and tvdb: return ("tv", "tvdb", str(tvdb))
            if tmdb: return (mtype, "tmdb", str(tmdb))
            if slug: return ("slug", slug)
            return (mtype, "title_year", (title or "").lower().strip(), int(year) if year and str(year).isdigit() else 0)

        jf_seasons = {}
        jf_url = get_setting("jellyfin_url", "").rstrip("/")
        
        for r in conn.execute("SELECT * FROM service_cache WHERE service = 'jellyfin'").fetchall():
            mtype = r["media_type"] or "movie"
            if mtype in ("episode", "season"):
                tvdb = str(r["tvdb_id"]) if r["tvdb_id"] else None
                sn = r["season_number"]
                if tvdb and sn is not None:
                    jf_seasons.setdefault(tvdb, set()).add(int(sn))
                continue
                
            key = _get_key(mtype, r["tmdb_id"], r["tvdb_id"], r["title"], r["year"], r["movie_slug"])
            jellyfin_id = r["external_item_id"]
            
            catalog[key] = {
                "media_type": mtype, "tmdb_id": r["tmdb_id"], "imdb_id": r["imdb_id"], "tvdb_id": r["tvdb_id"],
                "movie_slug": r["movie_slug"] or f"jf-{jellyfin_id}", "title": r["title"] or f"Jellyfin Item {jellyfin_id}",
                "release_year": r["year"], "season_number": r["season_number"], "episode_number": r["episode_number"],
                "in_jellyfin": 1, "jellyfin_id": jellyfin_id, "jellyfin_link": f"{jf_url}/web/index.html#/details?id={jellyfin_id}" if jf_url else "",
                "in_radarr": 0, "radarr_id": None, "in_sonarr": 0, "sonarr_id": None, "status": "in_library",
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
                if catalog[key]["status"] != "in_library": catalog[key]["status"] = "queued"
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
                if catalog[key]["status"] != "in_library": catalog[key]["status"] = "queued"
                catalog[key]["tvdb_id"] = catalog[key]["tvdb_id"] or r["tvdb_id"]

        for r in conn.execute("SELECT * FROM user_requests").fetchall():
            mtype = r["media_type"] or "movie"
            key = _get_key(mtype, r["tmdb_id"], r["tvdb_id"], r["film_title"], r["film_year"], r["movie_slug"])
            if key not in catalog:
                catalog[key] = {
                    "media_type": mtype, "tmdb_id": r["tmdb_id"], "imdb_id": r["imdb_id"], "tvdb_id": r["tvdb_id"],
                    "movie_slug": r["movie_slug"], "title": r["film_title"] or r["movie_slug"], "release_year": r["film_year"],
                    "season_number": r["season_number"], "episode_number": r["episode_number"],
                    "in_jellyfin": 0, "jellyfin_id": None, "jellyfin_link": None, "in_radarr": 0, "radarr_id": None,
                    "in_sonarr": 0, "sonarr_id": None, "status": "missing", "requested_by_users": {r["username"]}, "poster_url": r["poster_url"]
                }
            else:
                catalog[key]["requested_by_users"].add(r["username"])
                catalog[key]["tmdb_id"] = catalog[key]["tmdb_id"] or r["tmdb_id"]
                catalog[key]["tvdb_id"] = catalog[key]["tvdb_id"] or r["tvdb_id"]
                if catalog[key]["movie_slug"].startswith("jf-") or catalog[key]["movie_slug"].startswith("radarr-") or catalog[key]["movie_slug"].startswith("sonarr-"):
                    catalog[key]["movie_slug"] = r["movie_slug"]

        rows_to_insert = []
        for key, item in catalog.items():
            avail_seasons = []
            if item["media_type"] == "tv" and item["tvdb_id"] and str(item["tvdb_id"]) in jf_seasons:
                avail_seasons = sorted(list(jf_seasons[str(item["tvdb_id"])]))
                if avail_seasons and item["status"] != "in_library":
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
    
def get_user_profile_mappings():
    with get_connection() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM user_profile_mappings ORDER BY profile_name ASC").fetchall()]
    
def save_user_profile_mapping(profile_name, letterboxd_username="", serializd_username="", jellyfin_user_id=""):
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO user_profile_mappings (profile_name, letterboxd_username, serializd_username, jellyfin_user_id)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(profile_name) DO UPDATE SET
                letterboxd_username = excluded.letterboxd_username,
                serializd_username = excluded.serializd_username,
                jellyfin_user_id = excluded.jellyfin_user_id
        """, (profile_name.strip(), letterboxd_username.strip(), serializd_username.strip(), jellyfin_user_id.strip()))

def delete_user_profile_mapping(profile_id):
    with get_connection() as conn:
        conn.execute("DELETE FROM user_profile_mappings WHERE id = ?", (profile_id,))

def get_tv_show_details(tvdb_id):
    with get_connection() as conn:
        show_row = conn.execute("SELECT * FROM media_catalog WHERE tvdb_id = ? AND media_type IN ('tv', 'episode') LIMIT 1", (tvdb_id,)).fetchone()
        if not show_row:
            return None, {}
        
        show = dict(show_row)
        show["available_seasons"] = json.loads(show["available_seasons"]) if show["available_seasons"] else []
        show["users_list"] = [u.strip() for u in (show["requested_by_users"] or "").split(",") if u.strip()]

        ep_rows = conn.execute("""
            SELECT season_number, episode_number, title, in_library, external_item_id as jellyfin_id 
            FROM service_cache 
            WHERE service = 'jellyfin' AND media_type = 'episode' AND tvdb_id = ?
        """, (tvdb_id,)).fetchall()

        seasons = {}
        for ep in ep_rows:
            ep_dict = dict(ep)
            sn = ep_dict["season_number"] if ep_dict["season_number"] is not None else 0
            seasons.setdefault(sn, []).append(ep_dict)

        for sn in seasons:
            seasons[sn].sort(key=lambda x: (x["episode_number"] if x["episode_number"] is not None else 0))

        return show, seasons

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