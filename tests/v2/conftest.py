import pytest


@pytest.fixture
def v2(tmp_path, monkeypatch):
    """The synchub.db module pointed at a fresh, migrated temporary database."""
    from synchub import db

    monkeypatch.setattr(db, "DB_FILE", str(tmp_path / "synchub_test.db"))
    db.migrate()
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
    from synchub.web import create_app
    flask_app = create_app()
    flask_app.config["TESTING"] = True
    return flask_app


@pytest.fixture
def client(app):
    """A browser that has already chosen 'just looking' on the Who-is-watching page (see test_who.py for the page itself)."""
    browser = app.test_client()
    browser.set_cookie("synchub_profile", "0")
    return browser
