"""A snapshot of what the app is doing right now, for troubleshooting stuck or locked tasks."""
import os
import sys
import threading
import time
import traceback
from pathlib import Path

from synchub import db, tasks

PROJECT_DIR = Path(__file__).resolve().parent.parent
STACK_DEPTH = 8


def _size_mb(path):
    try:
        return round(Path(path).stat().st_size / 1_048_576, 2)
    except OSError:
        return None


def _thread_stacks():
    names = {t.ident: t.name for t in threading.enumerate()}
    current = threading.get_ident()
    result = []
    for ident, frame in sys._current_frames().items():
        if ident == current:
            continue
        frames = traceback.extract_stack(frame)
        mine = [f for f in frames if PROJECT_DIR in Path(f.filename).resolve().parents]
        if not mine:
            continue          # a pure library/idle thread (scheduler sleeping, server loop...)
        shown = (mine[-STACK_DEPTH:] + [frames[-1]]) if frames[-1] not in mine else mine[-STACK_DEPTH:]
        result.append({"thread": names.get(ident, str(ident)),
                       "stack": [f"{Path(f.filename).name}:{f.lineno} in {f.name}" for f in shown],
                       "waiting_in": f"{Path(frames[-1].filename).name}:{frames[-1].lineno} in {frames[-1].name}"})
    return result


def snapshot():
    with db.connection() as conn:
        version = db.current_version(conn)
    statuses = tasks.all_statuses()
    running = []
    for name, row in statuses.items():
        if row["status"] in ("in_progress", "pending"):
            started = tasks._STARTED.get(name)
            running.append({"task": name, "status": row["status"], "message": row["message"],
                            "running_for_seconds": round(time.monotonic() - started) if started else None, "since": row["started_at"]})
    return {
        "time": db.now(), "pid": os.getpid(),
        "database": {"path": db.DB_FILE, "journal_mode": db.journal_mode(), "schema_version": version,
                     "size_mb": _size_mb(db.DB_FILE), "wal_mb": _size_mb(db.DB_FILE + "-wal")},
        "running_tasks": running,
        "database_connections": db.open_connections(),
        "threads": _thread_stacks(),
    }
