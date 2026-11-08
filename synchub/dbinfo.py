"""How big SyncHub's database is, which tables take the room, and a Compact button (VACUUM) that gives deleted space back to the disk."""
import logging
import os
import sqlite3

from synchub import db

logger = logging.getLogger(__name__)


class CompactRefused(RuntimeError):
    """Compacting cannot run right now; the message is meant for the page."""


def _mb(n):
    return round((n or 0) / 1e6, 1)


def sizes(top=8):
    """The file size, the part VACUUM can give back, and the biggest tables. Read-only."""
    path = db.DB_FILE
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        page = conn.execute("PRAGMA page_size").fetchone()[0]
        total, free = conn.execute("PRAGMA page_count").fetchone()[0], conn.execute("PRAGMA freelist_count").fetchone()[0]
        owner = {r[0]: (r[2] if r[1] == "index" else r[0]) for r in conn.execute("SELECT name, type, tbl_name FROM sqlite_master")}
        by_table = {}
        try:
            for name, size in conn.execute("SELECT name, SUM(pgsize) FROM dbstat GROUP BY name"):
                key = owner.get(name, name)
                by_table[key] = by_table.get(key, 0) + size
            how = "size on disk, indexes included"
        except sqlite3.Error:
            how = "row counts only"
        tables = []
        for name in [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]:
            tables.append({"name": name, "rows": conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0], "mb": _mb(by_table.get(name, 0))})
    finally:
        conn.close()
    tables.sort(key=lambda t: (t["mb"], t["rows"]), reverse=True)
    wal = path + "-wal"
    return {"file_mb": _mb(os.path.getsize(path)), "wal_mb": _mb(os.path.getsize(wal)) if os.path.exists(wal) else 0.0, "reclaimable_mb": _mb(free * page),
            "total_mb": _mb(total * page), "how": how, "tables": tables[:top]}


def compact():
    """VACUUMs the database. Refused while a job is running, because the database is locked for the whole rewrite. Returns the size before and after."""
    with db.connection() as conn:
        busy = conn.execute("SELECT COUNT(*) FROM job WHERE status = 'running'").fetchone()[0]
    if busy:
        raise CompactRefused(f"{busy} job(s) are running. Compacting locks the database for a while; try again when the Jobs page is quiet.")
    before = os.path.getsize(db.DB_FILE)
    conn = sqlite3.connect(db.DB_FILE, timeout=60, isolation_level=None)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("VACUUM")
    except sqlite3.OperationalError as exc:
        raise CompactRefused(f"The database is busy ({exc}). Try again in a minute.")
    finally:
        conn.close()
    after = os.path.getsize(db.DB_FILE)
    logger.info(f"[database] compacted {_mb(before)} MB -> {_mb(after)} MB")
    return {"before_mb": _mb(before), "after_mb": _mb(after)}
