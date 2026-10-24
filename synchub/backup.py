"""Consistent copies of the database: for backups, for moving it to another machine, and for checking one before it is used.

A copy is made with SQLite's own backup, so it is whole and correct even while SyncHub is running (copying the file by hand is not: recent changes
may still be in the -wal file next to it). Every copy is a single self-contained file."""
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from config import BACKUP_DIR
from synchub import db, settings

PREFIX, PREMIGRATE_PREFIX = "synchub-", "premigrate-"        # daily copies are pruned to the newest few; the copy before an upgrade is kept
ADDRESS_SETTINGS = ("jellyfin_url", "radarr_url", "sonarr_url", "synchub_url")
THIS_MACHINE = ("localhost", "127.0.0.1", "::1", "0.0.0.0")
COUNTED = (("profiles", "profile"), ("titles", "media"), ("episodes", "episode"), ("workers", "worker"), ("library files", "library_file"))


class BackupError(Exception):
    """Something that cannot be done; the message is for the person running it."""


def latest_version():
    return max(version for version, _ in db._migration_files())


def _stamp(now=None):
    return (now or datetime.now()).strftime("%Y%m%d-%H%M%S")


def create(dest=None, source=None, now=None):
    """Saves a consistent copy and returns its path. Refuses to overwrite, and removes the copy if it does not pass SQLite's integrity check."""
    source = Path(source or db.DB_FILE)
    if not source.exists() or source.stat().st_size == 0:
        raise BackupError(f"There is no database at {source} to copy.")
    dest = Path(dest) if dest else Path(BACKUP_DIR) / f"{PREFIX}{_stamp(now)}.db"
    if dest.exists():
        raise BackupError(f"{dest} already exists; choose another name.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + ".partial")
    reader = sqlite3.connect(str(source), timeout=30)
    writer = sqlite3.connect(str(partial))
    try:
        reader.backup(writer)
        writer.execute("PRAGMA journal_mode = DELETE")             # one file, nothing waiting in a -wal beside it
        verdict = writer.execute("PRAGMA integrity_check").fetchone()[0]
    except sqlite3.Error as exc:
        verdict = None
        failure = str(exc)
    finally:
        writer.close()
        reader.close()
    if verdict is None:
        partial.unlink(missing_ok=True)
        raise BackupError(f"The database could not be copied ({failure}); nothing was kept.")
    if verdict != "ok":
        partial.unlink(missing_ok=True)
        raise BackupError(f"The copy did not pass SQLite's integrity check ({verdict}); nothing was kept.")
    partial.replace(dest)
    return dest


def keep_count():
    try:
        return max(int(settings.get("backup_keep")), 0)
    except (TypeError, ValueError):
        return 7


def prune(keep=None, directory=None):
    """Deletes all but the newest `keep` daily copies (0 keeps them all). Returns the names removed."""
    keep = keep_count() if keep is None else keep
    if keep <= 0:
        return []
    files = sorted(Path(directory or BACKUP_DIR).glob(f"{PREFIX}*.db"))
    gone = files[:-keep] if len(files) > keep else []
    for path in gone:
        path.unlink(missing_ok=True)
    return [p.name for p in gone]


def latest(directory=None):
    """(path, modified time) of the newest copy of either kind, or None."""
    files = [p for p in Path(directory or BACKUP_DIR).glob("*.db")]
    if not files:
        return None
    newest = max(files, key=lambda p: p.stat().st_mtime)
    return newest, datetime.fromtimestamp(newest.stat().st_mtime)


def addresses_on_this_machine(values):
    """The settings among `values` ({name: address}) that point at this machine itself: fine on the machine that runs everything, wrong inside a container."""
    bad = []
    for key in ADDRESS_SETTINGS:
        host = (urlparse(str(values.get(key) or "")).hostname or "").lower()
        if host in THIS_MACHINE:
            bad.append(key)
    return bad


def check(path):
    """What is in a database file, without changing it: whether it is sound, its version against this app's, what it holds, and what will not work after a move."""
    path = Path(path)
    if not path.exists() or path.stat().st_size == 0:
        raise BackupError(f"{path} is missing or empty.")
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    except sqlite3.Error as exc:
        raise BackupError(f"{path} could not be opened: {exc}")
    try:
        try:
            verdict = conn.execute("PRAGMA integrity_check").fetchone()[0]
            has_versions = conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'schema_version'").fetchone()
        except sqlite3.DatabaseError as exc:
            raise BackupError(f"{path} is not a usable SQLite database: {exc}")
        report = {"path": str(path), "size": path.stat().st_size, "integrity": verdict, "app_schema": latest_version(), "schema": None, "counts": {}, "settings": {}}
        if has_versions:
            report["schema"] = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        for label, table in COUNTED:
            try:
                report["counts"][label] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except sqlite3.Error:
                pass                                                   # a table a newer version adds
        try:
            report["settings"] = {r[0]: r[1] for r in conn.execute("SELECT key, value FROM app_setting WHERE key IN (%s)" % ",".join("?" * len(ADDRESS_SETTINGS)), ADDRESS_SETTINGS)}
        except sqlite3.Error:
            pass
    finally:
        conn.close()
    wal = path.with_name(path.name + "-wal")
    report["wal_beside"] = wal.exists() and wal.stat().st_size > 0
    report["newer_than_app"] = bool(report["schema"] and report["schema"] > report["app_schema"])
    report["local_addresses"] = addresses_on_this_machine(report["settings"])
    report["ok"] = verdict == "ok" and report["schema"] is not None and not report["newer_than_app"]
    return report


def restore(path, dest=None, now=None):
    """Puts a checked copy in place of the database, keeping what was there beside it. Stop SyncHub first. Returns (new path, where the old one went or None)."""
    report = check(path)
    if report["integrity"] != "ok":
        raise BackupError(f"That file did not pass SQLite's integrity check ({report['integrity']}); nothing was changed.")
    if report["schema"] is None:
        raise BackupError("That file is a SQLite database but not a SyncHub one (it has no schema version); nothing was changed.")
    if report["newer_than_app"]:
        raise BackupError(f"That database is version {report['schema']} but this SyncHub only knows up to {report['app_schema']}. Update SyncHub first; nothing was changed.")
    target = Path(dest or db.DB_FILE)
    kept = None
    if target.exists():
        kept = target.with_name(f"{target.name}.before-restore-{_stamp(now)}")
        target.replace(kept)
    for side in ("-wal", "-shm"):
        beside = target.with_name(target.name + side)
        if beside.exists():
            if kept is not None:
                beside.replace(kept.with_name(kept.name + side))        # they belong to the old database (the -wal can hold its newest changes): they go with it
            else:
                beside.unlink()                                          # nothing to belong to: leftovers must not be read with the new file
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, target)
    return target, kept


def copy_before_upgrade(now=None):
    """Called at start-up: if the database is older than this version of SyncHub, saves it as it is before the upgrade changes it. Returns the path or None."""
    source = Path(db.DB_FILE)
    if not source.exists() or source.stat().st_size == 0:
        return None
    try:
        conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        try:
            present = conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'schema_version'").fetchone()
            version = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] if present else None
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    if not version or version >= latest_version():
        return None
    return create(Path(BACKUP_DIR) / f"{PREMIGRATE_PREFIX}v{version}-{_stamp(now)}.db", source)


# ---------------------------------------------------------------- command line
def _size(n):
    return f"{n / 1048576:.1f} MB" if n >= 1048576 else f"{n / 1024:.0f} KB"


def describe(report):
    lines = [f"{report['path']}  ({_size(report['size'])})",
             f"  integrity: {report['integrity']}",
             f"  database version: {report['schema'] if report['schema'] is not None else 'none (not a SyncHub database)'}  (this SyncHub: {report['app_schema']})"]
    if report["counts"]:
        lines.append("  holds: " + ", ".join(f"{n} {label}" for label, n in report["counts"].items()))
    if report["schema"] is not None and report["schema"] < report["app_schema"]:
        lines.append(f"  note: it will be upgraded to version {report['app_schema']} the first time SyncHub opens it (a copy is kept first)")
    if report["newer_than_app"]:
        lines.append("  PROBLEM: this database is newer than this SyncHub. Update SyncHub first.")
    if report["wal_beside"]:
        lines.append("  WARNING: a non-empty -wal file sits beside it, so some recent changes are not in this file. Use 'create' to make a complete copy instead.")
    for key in report["local_addresses"]:
        lines.append(f"  FIX AFTER MOVING: {key} is {report['settings'][key]}, which means 'this machine'. Inside a container that is the container itself; use the real address (Settings).")
    return "\n".join(lines)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    usage = "usage: python -m synchub.backup create [FILE] | check FILE | restore FILE | list"
    if not argv or argv[0] not in ("create", "check", "restore", "list") or (argv[0] in ("check", "restore") and len(argv) != 2) or len(argv) > 2:
        print(usage)
        return 2
    command = argv[0]
    try:
        if command == "create":
            path = create(argv[1] if len(argv) == 2 else None)
            print(f"Saved {path}  ({_size(path.stat().st_size)})")
        elif command == "check":
            report = check(argv[1])
            print(describe(report))
            return 0 if report["ok"] else 1
        elif command == "restore":
            target, kept = restore(argv[1])
            print(f"Restored {argv[1]} as {target}." + (f" The database that was there is kept as {kept}." if kept else ""))
        else:
            for path in sorted(Path(BACKUP_DIR).glob("*.db")):
                print(f"{path.name}  {_size(path.stat().st_size)}  {datetime.fromtimestamp(path.stat().st_mtime):%Y-%m-%d %H:%M}")
    except BackupError as exc:
        print(exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
