import json
import sqlite3
from typing import Dict, List, Tuple

from config import (
    DB_PATH,
    DEFAULT_LIST_TYPES,
    DEFAULT_REQUEST_DELAY_RANGE,
    DEFAULT_SCRAPE_INTERVAL_HOURS,
    DEFAULT_USER_DELAY_RANGE,
    DEFAULT_USERNAMES,
)
from logger import logger


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_films (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                list_type TEXT NOT NULL,
                movie_slug TEXT NOT NULL,
                movie_title TEXT NOT NULL,
                movie_url TEXT NOT NULL,
                full_display_name TEXT,
                film_identifier TEXT,
                media_type TEXT DEFAULT 'movie',
                tmdb_id TEXT,
                imdb_id TEXT,
                tvdb_id TEXT,
                local_poster_path TEXT,
                is_present INTEGER DEFAULT 1,
                first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(username, list_type, movie_slug)
            );
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS external_library_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                service_name TEXT NOT NULL,
                item_identifier TEXT NOT NULL,
                title TEXT NOT NULL,
                normalized_title TEXT NOT NULL,
                media_type TEXT DEFAULT 'movie',
                tmdb_id TEXT,
                imdb_id TEXT,
                tvdb_id TEXT,
                external_item_id TEXT,
                details_json TEXT,
                last_synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(service_name, item_identifier)
            );
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)

        # Schema migrations
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(user_films);")
        user_cols = [row["name"] for row in cursor.fetchall()]
        if "tmdb_id" not in user_cols:
            cursor.execute("ALTER TABLE user_films ADD COLUMN tmdb_id TEXT;")
        if "imdb_id" not in user_cols:
            cursor.execute("ALTER TABLE user_films ADD COLUMN imdb_id TEXT;")
        if "tvdb_id" not in user_cols:
            cursor.execute("ALTER TABLE user_films ADD COLUMN tvdb_id TEXT;")
        if "media_type" not in user_cols:
            cursor.execute("ALTER TABLE user_films ADD COLUMN media_type TEXT DEFAULT 'movie';")

        cursor.execute("PRAGMA table_info(external_library_cache);")
        ext_cols = [row["name"] for row in cursor.fetchall()]
        if "tvdb_id" not in ext_cols:
            cursor.execute("ALTER TABLE external_library_cache ADD COLUMN tvdb_id TEXT;")

        conn.commit()

    default_settings = {
        "target_usernames": json.dumps(DEFAULT_USERNAMES),
        "target_serializd_usernames": json.dumps([]),
        "target_list_types": json.dumps(DEFAULT_LIST_TYPES),
        "request_delay_min": str(DEFAULT_REQUEST_DELAY_RANGE[0]),
        "request_delay_max": str(DEFAULT_REQUEST_DELAY_RANGE[1]),
        "user_delay_min": str(DEFAULT_USER_DELAY_RANGE[0]),
        "user_delay_max": str(DEFAULT_USER_DELAY_RANGE[1]),
        "scrape_interval_hours": str(DEFAULT_SCRAPE_INTERVAL_HOURS),
        "tmdb_api_key": "",
        "apprise_urls": "",
        "enable_notifications": "0",
        "disable_jellyfin_connection": "0",
        "jellyfin_url": "http://localhost:8096",
        "jellyfin_api_key": "",
        "radarr_url": "http://localhost:7878",
        "radarr_api_key": "",
        "radarr_root_folder": "/movies",
        "radarr_quality_profile_id": "1",
        "enable_radarr_automation": "0",
        "radarr_min_user_count": "1",
        "sonarr_url": "http://localhost:8989",
        "sonarr_api_key": "",
        "sonarr_root_folder": "/tv",
        "sonarr_quality_profile_id": "1",
        "enable_sonarr_automation": "0",
        "sonarr_min_user_count": "1",
        "last_run_timestamp": "Never",
        "is_scraping_active": "0",
        "job_status": json.dumps({
            "scraper": {"status": "idle", "last_run": "Never", "message": ""},
            "jellyfin_sync": {"status": "idle", "last_run": "Never", "message": ""},
            "radarr_automation": {"status": "idle", "last_run": "Never", "message": ""},
            "sonarr_automation": {"status": "idle", "last_run": "Never", "message": ""}
        }),
    }

    with get_connection() as conn:
        for key, val in default_settings.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?);",
                (key, val),
            )
        conn.commit()


def get_setting(key: str, default: str = "") -> str:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM settings WHERE key = ?;", (key,))
        row = cursor.fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?);",
            (key, str(value)),
        )
        conn.commit()


def get_all_settings() -> Dict[str, str]:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT key, value FROM settings;")
        return {row["key"]: row["value"] for row in cursor.fetchall()}


def update_job_status(job_name: str, status: str, message: str = "") -> None:
    import datetime
    current_status = json.loads(get_setting("job_status", "{}"))
    current_status[job_name] = {
        "status": status,
        "last_run": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "message": message,
    }
    set_setting("job_status", json.dumps(current_status))


def update_external_library_cache(service_name: str, items: List[Dict]) -> None:
    with get_connection() as conn:
        conn.execute(
            "DELETE FROM external_library_cache WHERE service_name = ?;",
            (service_name,),
        )
        for item in items:
            conn.execute(
                """
                INSERT OR REPLACE INTO external_library_cache (
                    service_name, item_identifier, title, normalized_title,
                    media_type, tmdb_id, imdb_id, tvdb_id, external_item_id, details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
                (
                    service_name,
                    item.get("item_identifier"),
                    item.get("title"),
                    item.get("normalized_title"),
                    item.get("media_type", "movie"),
                    item.get("tmdb_id"),
                    item.get("imdb_id"),
                    item.get("tvdb_id"),
                    item.get("external_item_id"),
                    json.dumps(item.get("details", {})),
                ),
            )
        conn.commit()


def get_external_library_cache(service_name: str, media_type: str = None) -> List[Dict]:
    query = "SELECT * FROM external_library_cache WHERE service_name = ?"
    params = [service_name]
    if media_type:
        query += " AND media_type = ?"
        params.append(media_type)

    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]


def update_film_identifiers(movie_slug: str, tmdb_id: str = None, imdb_id: str = None, tvdb_id: str = None) -> None:
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE user_films
            SET tmdb_id = COALESCE(?, tmdb_id),
                imdb_id = COALESCE(?, imdb_id),
                tvdb_id = COALESCE(?, tvdb_id)
            WHERE movie_slug = ?;
        """,
            (tmdb_id, imdb_id, tvdb_id, movie_slug),
        )
        conn.commit()


def sync_scraped_records(
    username: str,
    list_type: str,
    scraped_records: List[Dict[str, str]],
    run_timestamp: str,
) -> Tuple[int, int, int]:
    with get_connection() as conn:
        cursor = conn.cursor()
        for record in scraped_records:
            cursor.execute(
                """
                INSERT INTO user_films (
                    username, list_type, movie_slug, movie_title, 
                    movie_url, full_display_name, film_identifier,
                    media_type, tmdb_id, imdb_id, tvdb_id, is_present,
                    first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(username, list_type, movie_slug) DO UPDATE SET
                    movie_title = excluded.movie_title,
                    movie_url = excluded.movie_url,
                    full_display_name = excluded.full_display_name,
                    film_identifier = excluded.film_identifier,
                    media_type = excluded.media_type,
                    tmdb_id = COALESCE(excluded.tmdb_id, user_films.tmdb_id),
                    imdb_id = COALESCE(excluded.imdb_id, user_films.imdb_id),
                    tvdb_id = COALESCE(excluded.tvdb_id, user_films.tvdb_id),
                    is_present = 1,
                    last_seen_at = excluded.last_seen_at;
            """,
                (
                    username,
                    list_type,
                    record["movie_slug"],
                    record["movie_title"],
                    record["movie_url"],
                    record["full_display_name"],
                    record["film_identifier"],
                    record.get("media_type", "movie"),
                    record.get("tmdb_id"),
                    record.get("imdb_id"),
                    record.get("tvdb_id"),
                    run_timestamp,
                    run_timestamp,
                ),
            )
        upserted_count = len(scraped_records)
        cursor.execute(
            """
            UPDATE user_films
            SET is_present = 0
            WHERE username = ? AND list_type = ? AND last_seen_at < ?;
        """,
            (username, list_type, run_timestamp),
        )
        tombstoned_count = cursor.rowcount
        cursor.execute(
            """
            SELECT COUNT(*) FROM user_films 
            WHERE username = ? AND list_type = ? AND is_present = 1;
        """,
            (username, list_type),
        )
        active_count = cursor.fetchone()[0]
        conn.commit()
    return upserted_count, tombstoned_count, active_count


def get_aggregated_active_films() -> List[Dict]:
    query = """
        SELECT 
            movie_slug,
            movie_title,
            full_display_name,
            movie_url,
            film_identifier,
            media_type,
            tmdb_id,
            imdb_id,
            tvdb_id,
            GROUP_CONCAT(DISTINCT username) as added_by_users,
            COUNT(DISTINCT username) as user_count,
            MAX(last_seen_at) as latest_seen
        FROM user_films
        WHERE is_present = 1
        GROUP BY movie_slug
        ORDER BY user_count DESC, movie_title ASC;
    """
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(query)
        return [dict(row) for row in cursor.fetchall()]