import sqlite3
import os

DB_FILE = os.path.join(os.path.dirname(__file__), "data.db")

def get_connection():
    """Returns a SQLite connection with timeout and row factory configuration."""
    conn = sqlite3.connect(DB_FILE, timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Initializes the SQLite database with all required tables, indexes, and WAL mode."""
    with get_connection() as conn:
        cursor = conn.cursor()
        
        cursor.execute("PRAGMA journal_mode=WAL;")
        
        # Primary user watchlist and film entries with season/episode support
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_films (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT,
                film_title TEXT,
                film_year INTEGER,
                movie_slug TEXT,
                tmdb_id TEXT,
                imdb_id TEXT,
                tvdb_id TEXT,
                media_type TEXT DEFAULT 'movie',
                season_number INTEGER DEFAULT NULL,
                episode_number INTEGER DEFAULT NULL,
                poster_url TEXT,
                last_scraped TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(username, movie_slug)
            )
        """)

        # Scraped records archive table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS scraped_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT,
                username TEXT,
                item_title TEXT,
                item_year INTEGER,
                media_type TEXT,
                season_number INTEGER DEFAULT NULL,
                episode_number INTEGER DEFAULT NULL,
                item_slug TEXT,
                raw_data TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # External library cache
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS external_library_cache (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tmdb_id TEXT,
                imdb_id TEXT,
                tvdb_id TEXT,
                service TEXT,
                status TEXT,
                in_library INTEGER DEFAULT 0,
                external_item_id TEXT,
                details_json TEXT,
                last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(service, tmdb_id, tvdb_id)
            )
        """)

        # Settings key-value store
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)

        # Task execution state tracking
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS task_status (
                task_name TEXT PRIMARY KEY,
                status TEXT DEFAULT 'idle',
                last_run TIMESTAMP,
                message TEXT
            )
        """)

        # Safely attempt to add season/episode columns if upgrading existing database
        for col in ["season_number INTEGER DEFAULT NULL", "episode_number INTEGER DEFAULT NULL"]:
            try:
                cursor.execute(f"ALTER TABLE user_films ADD COLUMN {col};")
            except sqlite3.OperationalError:
                pass
            try:
                cursor.execute(f"ALTER TABLE scraped_records ADD COLUMN {col};")
            except sqlite3.OperationalError:
                pass

        cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_films_slug ON user_films(movie_slug);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_films_tmdb ON user_films(tmdb_id);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_films_tvdb ON user_films(tvdb_id);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_ext_cache_lookup ON external_library_cache(service, tmdb_id, tvdb_id);")
        
        conn.commit()

# --- Query & Data Operations ---

def save_user_film(username, film_title, film_year, movie_slug, tmdb_id=None, imdb_id=None, tvdb_id=None, media_type='movie', season_number=None, episode_number=None, poster_url=None):
    """Inserts or updates a film/show entry for a user."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO user_films (username, film_title, film_year, movie_slug, tmdb_id, imdb_id, tvdb_id, media_type, season_number, episode_number, poster_url, last_scraped)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(username, movie_slug) DO UPDATE SET
                film_title = excluded.film_title,
                film_year = excluded.film_year,
                tmdb_id = COALESCE(excluded.tmdb_id, user_films.tmdb_id),
                imdb_id = COALESCE(excluded.imdb_id, user_films.imdb_id),
                tvdb_id = COALESCE(excluded.tvdb_id, user_films.tvdb_id),
                media_type = excluded.media_type,
                season_number = COALESCE(excluded.season_number, user_films.season_number),
                episode_number = COALESCE(excluded.episode_number, user_films.episode_number),
                poster_url = COALESCE(excluded.poster_url, user_films.poster_url),
                last_scraped = CURRENT_TIMESTAMP
        """, (username, film_title, film_year, movie_slug, tmdb_id, imdb_id, tvdb_id, media_type, season_number, episode_number, poster_url))
        conn.commit()

def update_film_identifiers(movie_slug, tmdb_id=None, imdb_id=None, tvdb_id=None):
    """Updates metadata identifiers (TMDB, IMDb, TVDB) for a specific film entry by slug."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE user_films 
            SET tmdb_id = COALESCE(?, tmdb_id),
                imdb_id = COALESCE(?, imdb_id),
                tvdb_id = COALESCE(?, tvdb_id)
            WHERE movie_slug = ?
        """, (tmdb_id, imdb_id, tvdb_id, movie_slug))
        conn.commit()

def save_scraped_record(source, username, item_title, item_year, media_type, item_slug, season_number=None, episode_number=None, raw_data=""):
    """Archives a raw scraped item into scraped_records."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO scraped_records (source, username, item_title, item_year, media_type, season_number, episode_number, item_slug, raw_data)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (source, username, item_title, item_year, media_type, season_number, episode_number, item_slug, raw_data))
        conn.commit()

def sync_scraped_records(records):
    """Batch processes user_films and scraped_records with season/episode metadata."""
    with get_connection() as conn:
        cursor = conn.cursor()
        for item in records:
            username = item.get("username", "default")
            title = item.get("film_title") or item.get("title")
            year = item.get("film_year") or item.get("year")
            slug = item.get("movie_slug") or item.get("slug")
            media_type = item.get("media_type", "movie")
            season_num = item.get("season_number")
            episode_num = item.get("episode_number")
            tmdb_id = item.get("tmdb_id")
            imdb_id = item.get("imdb_id")
            tvdb_id = item.get("tvdb_id")
            poster_url = item.get("poster_url")
            source = item.get("source", "letterboxd")
            raw_data = str(item.get("raw_data", ""))

            cursor.execute("""
                INSERT INTO user_films (username, film_title, film_year, movie_slug, tmdb_id, imdb_id, tvdb_id, media_type, season_number, episode_number, poster_url, last_scraped)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(username, movie_slug) DO UPDATE SET
                    film_title = excluded.film_title,
                    film_year = excluded.film_year,
                    tmdb_id = COALESCE(excluded.tmdb_id, user_films.tmdb_id),
                    imdb_id = COALESCE(excluded.imdb_id, user_films.imdb_id),
                    tvdb_id = COALESCE(excluded.tvdb_id, user_films.tvdb_id),
                    media_type = excluded.media_type,
                    season_number = COALESCE(excluded.season_number, user_films.season_number),
                    episode_number = COALESCE(excluded.episode_number, user_films.episode_number),
                    poster_url = COALESCE(excluded.poster_url, user_films.poster_url),
                    last_scraped = CURRENT_TIMESTAMP
            """, (username, title, year, slug, tmdb_id, imdb_id, tvdb_id, media_type, season_num, episode_num, poster_url))

            cursor.execute("""
                INSERT INTO scraped_records (source, username, item_title, item_year, media_type, season_number, episode_number, item_slug, raw_data)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (source, username, title, year, media_type, season_num, episode_num, slug, raw_data))

        conn.commit()

def update_external_cache(service, tmdb_id=None, imdb_id=None, tvdb_id=None, status="unknown", in_library=0, external_item_id=None, details_json=None):
    """Updates status for Radarr, Sonarr, or Jellyfin external sync cache."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO external_library_cache (service, tmdb_id, imdb_id, tvdb_id, status, in_library, external_item_id, details_json, last_updated)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(service, tmdb_id, tvdb_id) DO UPDATE SET
                status = excluded.status,
                in_library = excluded.in_library,
                external_item_id = COALESCE(excluded.external_item_id, external_library_cache.external_item_id),
                details_json = COALESCE(excluded.details_json, external_library_cache.details_json),
                last_updated = CURRENT_TIMESTAMP
        """, (service, tmdb_id, imdb_id, tvdb_id, status, in_library, external_item_id, details_json))
        conn.commit()

update_external_library_cache = update_external_cache

def get_external_cache(service):
    """Retrieves external library cache records for a given service."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM external_library_cache WHERE service = ?", (service,))
        return [dict(row) for row in cursor.fetchall()]

get_external_library_cache = get_external_cache

def get_film_by_slug(slug):
    """Direct single-record query for poster routing and fast lookup."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM user_films WHERE movie_slug = ? LIMIT 1", (slug,))
        row = cursor.fetchone()
        return dict(row) if row else None

def get_aggregated_active_films():
    """Returns all active user films from database formatted for web interface."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT 
                movie_slug,
                film_title AS movie_title,
                film_title || ' (' || film_year || ')' AS full_display_name,
                media_type,
                season_number,
                episode_number,
                tmdb_id,
                imdb_id,
                tvdb_id,
                poster_url,
                GROUP_CONCAT(username, ', ') AS added_by_users,
                COUNT(DISTINCT username) AS user_count,
                'https://letterboxd.com/film/' || movie_slug || '/' AS movie_url,
                MAX(last_scraped) AS last_scraped
            FROM user_films 
            GROUP BY movie_slug
            ORDER BY last_scraped DESC
        """)
        return [dict(row) for row in cursor.fetchall()]

# --- Settings & Task Tracking ---

def get_setting(key, default_val=""):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM app_settings WHERE key = ?", (key,))
        row = cursor.fetchone()
        return row["value"] if row else default_val

def set_setting(key, value):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)", (key, str(value)))
        conn.commit()

def get_all_settings():
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT key, value FROM app_settings")
        return {row["key"]: row["value"] for row in cursor.fetchall()}

def update_task_status(task_name, status, message=""):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO task_status (task_name, status, last_run, message)
            VALUES (?, ?, CURRENT_TIMESTAMP, ?)
            ON CONFLICT(task_name) DO UPDATE SET
                status = excluded.status,
                last_run = CURRENT_TIMESTAMP,
                message = excluded.message
        """, (task_name, status, message))
        conn.commit()

def update_job_status(job_name, status, message=""):
    update_task_status(job_name, status, message)

def get_all_task_statuses():
    default_tasks = ["scraper", "radarr", "sonarr", "jellyfin_movies", "jellyfin_tv", "all"]
    statuses = {task: {"status": "idle", "last_run": None, "message": "No active logs"} for task in default_tasks}

    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM task_status")
        for row in cursor.fetchall():
            statuses[row["task_name"]] = dict(row)

    return statuses