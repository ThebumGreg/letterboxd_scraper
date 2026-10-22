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
