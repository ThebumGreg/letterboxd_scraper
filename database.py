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
                local_poster_path TEXT,
                is_present INTEGER DEFAULT 1,
                first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(username, list_type, movie_slug)
            );
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS local_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_name TEXT NOT NULL,
                file_path TEXT UNIQUE NOT NULL,
                parsed_title TEXT,
                file_size INTEGER,
                scanned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)
        conn.commit()

    default_settings = {
        "target_usernames": json.dumps(DEFAULT_USERNAMES),
        "target_list_types": json.dumps(DEFAULT_LIST_TYPES),
        "request_delay_min": str(DEFAULT_REQUEST_DELAY_RANGE[0]),
        "request_delay_max": str(DEFAULT_REQUEST_DELAY_RANGE[1]),
        "user_delay_min": str(DEFAULT_USER_DELAY_RANGE[0]),
        "user_delay_max": str(DEFAULT_USER_DELAY_RANGE[1]),
        "scrape_interval_hours": str(DEFAULT_SCRAPE_INTERVAL_HOURS),
        "radarr_url": "http://localhost:7878",
        "radarr_api_key": "",
        "radarr_root_folder": "/movies",
        "radarr_quality_profile_id": "1",
        "auto_sync_radarr": "0",
        "sonarr_url": "http://localhost:8989",
        "sonarr_api_key": "",
        "sonarr_root_folder": "/tv",
        "sonarr_quality_profile_id": "1",
        "auto_sync_sonarr": "0",
        "jellyfin_url": "http://localhost:8096",
        "jellyfin_api_key": "",
        "enable_disk_scanner": "0",
        "disk_scanner_path": "/media/movies",
        "last_run_timestamp": "Never",
        "is_scraping_active": "0",
    }

    with get_connection() as conn:
        for key, val in default_settings.items():
            conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?);", (key, val))
        conn.commit()

def get_setting(key: str, default: str = "") -> str:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM settings WHERE key = ?;", (key,))
        row = cursor.fetchone()
        return row["value"] if row else default

def set_setting(key: str, value: str) -> None:
    with get_connection() as conn:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?);", (key, str(value)))
        conn.commit()

def get_all_settings() -> Dict[str, str]:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT key, value FROM settings;")
        return {row["key"]: row["value"] for row in cursor.fetchall()}

def save_disk_file_cache(files: List[Dict]) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM local_files;")
        for f in files:
            conn.execute("""
                INSERT OR REPLACE INTO local_files (file_name, file_path, parsed_title, file_size)
                VALUES (?, ?, ?, ?);
            """, (f["file_name"], f["file_path"], f["parsed_title"], f["file_size"]))
        conn.commit()

def sync_scraped_records(
    username: str, 
    list_type: str, 
    scraped_records: List[Dict[str, str]], 
    run_timestamp: str
) -> Tuple[int, int, int]:
    with get_connection() as conn:
        cursor = conn.cursor()
        for record in scraped_records:
            cursor.execute("""
                INSERT INTO user_films (
                    username, list_type, movie_slug, movie_title, 
                    movie_url, full_display_name, film_identifier, 
                    is_present, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(username, list_type, movie_slug) DO UPDATE SET
                    movie_title = excluded.movie_title,
                    movie_url = excluded.movie_url,
                    full_display_name = excluded.full_display_name,
                    film_identifier = excluded.film_identifier,
                    is_present = 1,
                    last_seen_at = excluded.last_seen_at;
            """, (
                username,
                list_type,
                record["movie_slug"],
                record["movie_title"],
                record["movie_url"],
                record["full_display_name"],
                record["film_identifier"],
                run_timestamp,
                run_timestamp
            ))
        upserted_count = len(scraped_records)

        cursor.execute("""
            UPDATE user_films
            SET is_present = 0
            WHERE username = ? AND list_type = ? AND last_seen_at < ?;
        """, (username, list_type, run_timestamp))
        tombstoned_count = cursor.rowcount

        cursor.execute("""
            SELECT COUNT(*) FROM user_films 
            WHERE username = ? AND list_type = ? AND is_present = 1;
        """, (username, list_type))
        active_count = cursor.fetchone()[0]
        conn.commit()
    return upserted_count, tombstoned_count, active_count

def get_aggregated_active_films(list_type: str = None) -> List[Dict]:
    query = """
        SELECT 
            movie_slug,
            movie_title,
            full_display_name,
            movie_url,
            film_identifier,
            GROUP_CONCAT(DISTINCT username) as added_by_users,
            COUNT(DISTINCT username) as user_count,
            MAX(last_seen_at) as latest_seen
        FROM user_films
        WHERE is_present = 1
    """
    params = []
    if list_type:
        query += " AND list_type = ?"
        params.append(list_type)

    query += " GROUP BY movie_slug ORDER BY user_count DESC, movie_title ASC;"

    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]