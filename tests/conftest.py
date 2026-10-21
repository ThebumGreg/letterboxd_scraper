import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def db(tmp_path, monkeypatch):
    """The database module pointed at a fresh, initialised temporary SQLite file."""
    import database

    monkeypatch.setattr(database, "DB_FILE", str(tmp_path / "test.db"))
    database.init_db()
    return database


@pytest.fixture
def client(db):
    # Imported lazily so the module-level init_db() in app.py runs against the temporary database.
    import app as app_module

    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


@pytest.fixture
def add_request(db):
    """Inserts a row into user_requests."""
    def _add(username, slug, title="Film", year=2000, media_type="movie", lists="Watchlist", tmdb_id=None, tvdb_id=None, profile_id=None):
        with db.get_connection() as conn:
            conn.execute(
                "INSERT INTO user_requests (username, profile_id, film_title, film_year, movie_slug, tmdb_id, tvdb_id, media_type, letterboxd_lists) VALUES (?,?,?,?,?,?,?,?,?)",
                (username, profile_id, title, year, slug, tmdb_id, tvdb_id, media_type, lists),
            )
    return _add
