import pytest


@pytest.fixture
def v2(tmp_path, monkeypatch):
    """The synchub.db module pointed at a fresh, migrated temporary database."""
    from synchub import db

    monkeypatch.setattr(db, "DB_FILE", str(tmp_path / "synchub_test.db"))
    db.migrate()
    from synchub import settings
    settings.set_value("naming_style", "current")      # TV files named `Show - S01E01` as before; tests of Jellyfin's style switch it
    settings.set_value("audio_mode", "keep")             # the compatible-track rule is tested on its own; older tests keep the audio as it is
    settings.set_value("size_mode", "size")            # the older GB limits: the tests written for them keep their meaning; tests of the bitrate rule switch it back
    return db


@pytest.fixture
def conn(v2):
    """A plain connection for arranging test data. It is deliberately not tracked: its own uncommitted setup writes
    must not trip the network guard, which is meant for the code under test (that opens tracked connections)."""
    import sqlite3
    connection = sqlite3.connect(v2.DB_FILE, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    yield connection
    connection.commit()
    connection.close()


@pytest.fixture(autouse=True)
def strict_network_guard(monkeypatch):
    """A network call while a write transaction is open is a bug (it blocks every other writer): fail the test."""
    from synchub import db
    monkeypatch.setattr(db, "STRICT_NETWORK_GUARD", True)


@pytest.fixture(autouse=True)
def no_ddns_watcher(monkeypatch):
    """A real IP change starts a background watcher that asks DNS servers; no test wants that unless it says so."""
    from synchub import ddns
    monkeypatch.setattr(ddns, "WATCH_AFTER_UPDATE", False)


@pytest.fixture
def app(v2):
    """The app with guests allowed everything and no rating limit, so the many tests that add a profile and then browse as a guest are not about
    access. test_ratings.py and test_access.py say what the real defaults (the dashboard and remote only, nothing above PG) do."""
    from synchub import settings
    from synchub.web import create_app
    settings.set_value("access_guest_permissions", "*")
    settings.set_value("guest_max_rating", "")
    flask_app = create_app(csrf=False)
    flask_app.config["TESTING"] = True
    return flask_app


@pytest.fixture
def client(app):
    """A browser that has already chosen 'just looking' on the Who-is-watching page (see test_who.py for the page itself)."""
    browser = app.test_client()
    browser.set_cookie("synchub_profile", "0")
    return browser
