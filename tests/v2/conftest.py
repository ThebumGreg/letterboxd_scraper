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
    connection = v2.connect()
    yield connection
    connection.commit()
    connection.close()
