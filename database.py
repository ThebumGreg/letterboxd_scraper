import sqlite3
from datetime import datetime, timezone
from typing import Dict, List
from config import DB_PATH
from logger import logger

def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db() -> None:
    """Initializes the database schema."""
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
                is_present INTEGER DEFAULT 1,
                first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(username, list_type, movie_slug)
            );
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_user_list 
            ON user_films(username, list_type);
        """)
        conn.commit()
    logger.info("Database initialized successfully.")

def sync_scraped_records(
    username: str, 
    list_type: str, 
    scraped_records: List[Dict[str, str]], 
    run_timestamp: str
) -> Tuple[int, int, int]:
    """
    Upserts newly scraped records and tombstones (marks is_present = 0) missing entries.
    Returns (inserted_or_updated_count, tombstoned_count, total_active_count).
    """
    with get_connection() as conn:
        cursor = conn.cursor()

        # Step 1: Upsert scraped records
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

        # Step 2: Mark records as removed if they weren't seen in this run
        cursor.execute("""
            UPDATE user_films
            SET is_present = 0
            WHERE username = ? 
              AND list_type = ? 
              AND last_seen_at < ?;
        """, (username, list_type, run_timestamp))
        
        tombstoned_count = cursor.rowcount

        # Step 3: Count active items
        cursor.execute("""
            SELECT COUNT(*) FROM user_films 
            WHERE username = ? AND list_type = ? AND is_present = 1;
        """, (username, list_type))
        active_count = cursor.fetchone()[0]

        conn.commit()

    return upserted_count, tombstoned_count, active_count