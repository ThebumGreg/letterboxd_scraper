"""SQLite access and numbered migrations for the v2 database.

Every connection is tracked, so the app can say *who* holds the database when something reports "database is locked",
and so a network call made while a write transaction is open (the usual cause of such locks) is caught.
"""
import logging
import re
import sqlite3
import threading
import time
import traceback
import weakref
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from config import DATA_DIR

logger = logging.getLogger(__name__)

DB_FILE = str(DATA_DIR / "synchub.db")
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_MIGRATION_RE = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")

# Tests switch this on so a network call inside a write transaction raises instead of only logging a warning.
STRICT_NETWORK_GUARD = False
DEFAULT_TIMEOUT = 30.0      # seconds a statement waits for another writer before "database is locked"


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- connection tracking
_registry = {}
_registry_lock = threading.Lock()


def _caller():
    """The first stack frame outside this module and contextlib, as 'file.py:123 in function'."""
    for frame in reversed(traceback.extract_stack()[:-1]):
        name = Path(frame.filename).name
        if name not in ("db.py", "contextlib.py"):
            return f"{name}:{frame.lineno} in {frame.name}"
    return "unknown"


class TrackedConnection(sqlite3.Connection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._info = {"thread": threading.current_thread().name, "ident": threading.get_ident(), "caller": _caller(), "opened": time.monotonic()}
        with _registry_lock:
            _registry[id(self)] = (weakref.ref(self), self._info)

    def close(self):
        with _registry_lock:
            _registry.pop(id(self), None)
        super().close()


def open_connections():
    """What is open right now: caller, thread, age in seconds and whether a write transaction is pending."""
    rows = []
    with _registry_lock:
        entries = list(_registry.values())
    for ref, info in entries:
        conn = ref()
        if conn is None:
            continue
        try:
            writing = conn.in_transaction
        except sqlite3.ProgrammingError:
            continue          # closed meanwhile
        rows.append({"caller": info["caller"], "thread": info["thread"], "age_seconds": round(time.monotonic() - info["opened"], 1), "writing": writing})
    return sorted(rows, key=lambda r: -r["age_seconds"])


def describe_holders(exclude=None):
    holders = [r for r in open_connections() if r["writing"]]
    if not holders:
        return "no other connection has a pending write (the lock may belong to another process, e.g. a second copy of the app)"
    return "; ".join(f"{r['caller']} in thread '{r['thread']}' for {r['age_seconds']}s" for r in holders)


class NetworkInWriteTransaction(RuntimeError):
    """A network call was attempted while this thread had an open write transaction."""


def assert_not_in_write_transaction(what):
    """Call before any slow/network operation. Holding a write transaction across it blocks every other writer
    (and, with payload logging, the request's own log write). Raises in tests; logs a warning in the app."""
    ident = threading.get_ident()
    offenders = []
    with _registry_lock:
        entries = list(_registry.values())
    for ref, info in entries:
        conn = ref()
        if conn is not None and info["ident"] == ident:
            try:
                if conn.in_transaction:
                    offenders.append(info["caller"])
            except sqlite3.ProgrammingError:
                pass
    if not offenders:
        return
    message = f"{what} was started while a database write transaction (opened at {', '.join(offenders)}) was still open; commit first."
    if STRICT_NETWORK_GUARD:
        raise NetworkInWriteTransaction(message)
    logger.warning(message)


# ---------------------------------------------------------------- connections
def connect(timeout=None):
    conn = sqlite3.connect(DB_FILE, timeout=DEFAULT_TIMEOUT if timeout is None else timeout, factory=TrackedConnection, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


@contextmanager
def connection(timeout=None):
    """Commits on success and rolls back on error. A 'database is locked' error says who is holding the database."""
    conn = connect(timeout)
    try:
        yield conn
        conn.commit()
    except sqlite3.OperationalError as exc:
        _rollback(conn)
        if "locked" in str(exc).lower() and "(held by:" not in str(exc):
            holders = describe_holders()
            logger.error(f"Database is locked (waiting at {_caller()}). Currently holding it: {holders}")
            raise sqlite3.OperationalError(f"{exc} (held by: {holders})") from exc
        raise
    except Exception:
        _rollback(conn)
        raise
    finally:
        conn.close()


def _rollback(conn):
    try:
        conn.rollback()
    except sqlite3.Error:
        pass


# ---------------------------------------------------------------- migrations
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


def journal_mode():
    conn = connect()
    try:
        return conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()


def migrate():
    """Applies every migration newer than the database's version. Each runs in one transaction. Returns the new version."""
    Path(DB_FILE).parent.mkdir(parents=True, exist_ok=True)
    conn = connect()
    try:
        mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        if mode.lower() != "wal":
            logger.warning(f"SQLite could not switch to WAL mode (using '{mode}'). Readers will block writers and 'database is locked' errors "
                           f"become likely. This happens when the data folder is on a network or Windows-mounted drive (e.g. /mnt/c in WSL): "
                           f"keep the project on the Linux filesystem.")
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
