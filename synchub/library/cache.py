"""Keeps the Library's worked-out recommendations in memory until something they depend on changes.

Working out every file's recommendation is the slow part of the Library pages (seconds on a big library), and it gave the same answer on every page view
and every refresh. `change_counter` (migration 041) goes up whenever a file, a quality result, a profile, a setting or a show changes; the result is
reused while that number is the same. `max_age` lets a background refresh accept a result that is a few seconds old while a scan keeps changing files;
anything a person just did is read exactly. Nothing is stored on disk: a restart starts empty."""
import threading
import time

from synchub import db

TTL = 900                     # seconds: even when the counter says nothing changed, work it out again this often

_lock = threading.Lock()
_entries = {}                 # key -> (generation, stored_at, value, started_at)
_key_locks = {}


def generation(conn):
    row = conn.execute("SELECT n FROM change_counter WHERE name = 'library'").fetchone()
    return row[0] if row else 0


def _fresh(entry, gen, now, max_age):
    if entry is None or now - entry[1] > TTL:
        return False
    return entry[0] == gen or (max_age and now - entry[1] < max_age)


def get(conn, name, compute, extra="", max_age=0):
    """The value for `name` (and `extra`, such as the settings it was made with), from memory when nothing changed, else `compute()`."""
    key = (db.DB_FILE, name, extra)
    gen = generation(conn)
    arrived = now = time.monotonic()
    with _lock:
        entry = _entries.get(key)
        if _fresh(entry, gen, now, max_age):
            return entry[2]
        key_lock = _key_locks.setdefault(key, threading.Lock())
        old_value = entry
    if max_age and old_value is not None and now - old_value[1] <= TTL:
        if not key_lock.acquire(blocking=False):      # a background refresh does not queue behind someone else's work: it shows the last result, which is a few seconds old
            return old_value[2]
    else:
        key_lock.acquire()
    try:                                              # one request works it out; the others wait and reuse it
        now = time.monotonic()
        with _lock:
            entry = _entries.get(key)
        if _fresh(entry, gen, now, max_age):
            return entry[2]
        if entry is not None and entry[3] >= arrived and now - entry[1] <= TTL:
            return entry[2]                           # worked out entirely after this request arrived, so it shows everything this request could have seen:
                                                      # without this, files changing every second made every waiting request work it all out again, one after another
        started = time.monotonic()
        value = compute()
        with _lock:
            _entries[key] = (gen, time.monotonic(), value, started)
        return value
    finally:
        key_lock.release()


def clear():
    with _lock:
        _entries.clear()
