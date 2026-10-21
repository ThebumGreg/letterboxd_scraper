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


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    """Tests must never reach the network: fail loudly instead of making a real HTTP call."""
    import socket
    real_connect = socket.socket.connect

    def guarded(self, address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) else address
        if isinstance(host, str) and host not in ("localhost", "127.0.0.1", "::1") and not host.startswith("/"):
            raise AssertionError(f"Test tried to open a network connection to {host!r}")
        return real_connect(self, address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded)

    # Environments that send traffic through a local proxy would pass the socket check above, so guard requests too.
    import requests
    from urllib.parse import urlparse

    def guarded_send(self, request, *args, **kwargs):
        host = urlparse(request.url).hostname
        if host not in ("localhost", "127.0.0.1", "::1"):
            raise AssertionError(f"Test tried to send an HTTP request to {request.url!r}")
        return real_send(self, request, *args, **kwargs)

    real_send = requests.adapters.HTTPAdapter.send
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", guarded_send)
