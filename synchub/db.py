"""SQLite access and numbered migrations for the v2 database."""
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from config import DATA_DIR

DB_FILE = str(DATA_DIR / "synchub.db")
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_MIGRATION_RE = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def connect():
    conn = sqlite3.connect(DB_FILE, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


@contextmanager
def connection():
    """Commits on success and rolls back on error."""
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _migration_files():
    files = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        match = _MIGRATION_RE.match(path.name)
        if not match:
            raise ValueError(f"Bad migration file name: {path.name} (expected NNN_name.sql)")
        files.append((int(match.group(1)), path))
    versions = [v for v, _ in files]
    if versions != list(range(1, len(versions) + 1)):
        raise ValueError(f"Migration numbers must be consecutive from 001, found {versions}")
    return files


def current_version(conn):
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'").fetchone()
    if not exists:
        return 0
    return conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()[0]


def migrate():
    """Applies every migration newer than the database's version. Each runs in one transaction. Returns the new version."""
    Path(DB_FILE).parent.mkdir(parents=True, exist_ok=True)
    conn = connect()
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)")
        conn.commit()
        applied = current_version(conn)
        for version, path in _migration_files():
            if version <= applied:
                continue
            script = f"BEGIN;\n{path.read_text(encoding='utf-8')}\nINSERT INTO schema_version (version, name, applied_at) VALUES ({version}, '{path.stem}', '{now()}');\nCOMMIT;"
            try:
                conn.executescript(script)
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        return current_version(conn)
    finally:
        conn.close()
