"""Worker PCs: who they are, how they prove it (a token each), and what they report about themselves.

A worker never opens SyncHub's database; it calls the worker API with its token. Only a hash of the token is stored, so a
lost token is replaced (rotated), never recovered.
"""
import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone

from synchub import db

PROTOCOL = 1                  # bumped when the worker API changes incompatibly; older agents are refused
ONLINE_SECONDS = 60           # a worker that has not polled for this long shows as offline
MAX_SLOTS = 16
MAX_STATUS_BYTES = 8192
MAX_CAPABILITIES_BYTES = 32768   # what an agent says about itself once (the hardware details make this larger than a status summary)
MAX_LOG_CHARS = 1_000_000        # the tail of a worker's own log that is kept


class WorkerError(Exception):
    """A change that cannot be made; the message is meant for the user."""


class ProtocolError(WorkerError):
    """The agent speaks a protocol version SyncHub does not support."""


def new_token():
    return "sw_" + secrets.token_hex(24)


def hash_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _tags(value):
    if isinstance(value, str):
        value = value.replace(",", " ").split()
    out = []
    for tag in value or []:
        tag = str(tag).strip().lower()
        if tag and tag not in out:
            if len(tag) > 32 or not all(c.isalnum() or c in "-_." for c in tag):
                raise WorkerError(f"'{tag}' is not a usable tag (letters, numbers, - _ . and at most 32 characters).")
            out.append(tag)
    return out


def _slots(value):
    try:
        slots = int(value)
    except (TypeError, ValueError):
        raise WorkerError("Slots must be a whole number.")
    if not 1 <= slots <= MAX_SLOTS:
        raise WorkerError(f"Slots must be between 1 and {MAX_SLOTS}.")
    return slots


def _name(value):
    name = " ".join((value or "").split())
    if not name:
        raise WorkerError("A worker needs a name.")
    if len(name) > 60:
        raise WorkerError("The name can be at most 60 characters.")
    return name


def _load(text, default):
    try:
        value = json.loads(text) if text else default
    except ValueError:
        return default
    return value if isinstance(value, type(default)) else default


def row(r, now=None):
    """A worker as a dict, with the JSON columns decoded and whether it is online."""
    w = dict(r)
    for key, default in (("tags", []), ("capabilities", {}), ("status", {})):
        w[key] = _load(w.get(key), default)
    w["token_hash"] = bool(w["token_hash"])            # never hand the hash around
    w["revoked"] = bool(w.get("revoked_at"))
    w["online"] = is_online(w.get("last_seen"), now)
    return w


def is_online(last_seen, now=None):
    if not last_seen:
        return False
    try:
        seen = datetime.strptime(last_seen, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    current = datetime.strptime(now, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc) if now else datetime.now(timezone.utc)
    return (current - seen) < timedelta(seconds=ONLINE_SECONDS)


# ---------------------------------------------------------------- managing workers (the web UI)
def create(conn, name, tags=(), slots=1):
    """Adds a worker. Returns (worker_id, token); the token is shown once and cannot be read again."""
    name, tags, slots = _name(name), _tags(tags), _slots(slots)
    if conn.execute("SELECT 1 FROM worker WHERE name = ?", (name,)).fetchone():
        raise WorkerError(f"There is already a worker called '{name}'.")
    token = new_token()
    worker_id = conn.execute("INSERT INTO worker (name, token_hash, token_hint, tags, slots) VALUES (?, ?, ?, ?, ?)",
                             (name, hash_token(token), token[-4:], json.dumps(tags), slots)).lastrowid
    return worker_id, token


def get(conn, worker_id, now=None):
    r = conn.execute("SELECT * FROM worker WHERE id = ?", (worker_id,)).fetchone()
    return row(r, now) if r else None


def list_all(conn, now=None):
    workers = [row(r, now) for r in conn.execute("SELECT * FROM worker ORDER BY name COLLATE NOCASE")]
    running = {r["worker_id"]: r["n"] for r in conn.execute("SELECT worker_id, COUNT(*) AS n FROM job WHERE status = 'running' GROUP BY worker_id")}
    for w in workers:
        w["running"] = running.get(w["id"], 0)
    return workers


def update(conn, worker_id, name=None, tags=None, slots=None):
    w = get(conn, worker_id)
    if not w:
        raise WorkerError("That worker no longer exists.")
    new_name = _name(name) if name is not None else w["name"]
    if new_name != w["name"] and conn.execute("SELECT 1 FROM worker WHERE name = ? AND id <> ?", (new_name, worker_id)).fetchone():
        raise WorkerError(f"There is already a worker called '{new_name}'.")
    conn.execute("UPDATE worker SET name = ?, tags = ?, slots = ? WHERE id = ?",
                 (new_name, json.dumps(_tags(tags) if tags is not None else w["tags"]), _slots(slots) if slots is not None else w["slots"], worker_id))


def set_flag(conn, worker_id, flag, value):
    """flag: enabled, paused or draining."""
    if flag not in ("enabled", "paused", "draining"):
        raise WorkerError("Unknown setting.")
    if conn.execute(f"UPDATE worker SET {flag} = ? WHERE id = ?", (1 if value else 0, worker_id)).rowcount != 1:
        raise WorkerError("That worker no longer exists.")


def rotate_token(conn, worker_id):
    """Replaces the token (the old one stops working at once). Returns the new token, shown once."""
    token = new_token()
    if conn.execute("UPDATE worker SET token_hash = ?, token_hint = ?, revoked_at = NULL WHERE id = ?", (hash_token(token), token[-4:], worker_id)).rowcount != 1:
        raise WorkerError("That worker no longer exists.")
    return token


def revoke(conn, worker_id):
    """Stops the token working. The worker's name, history and jobs stay; rotate the token to let it back in."""
    now = db.now()
    if conn.execute("UPDATE worker SET token_hash = NULL, token_hint = NULL, revoked_at = ? WHERE id = ?", (now, worker_id)).rowcount != 1:
        raise WorkerError("That worker no longer exists.")


def delete(conn, worker_id):
    if conn.execute("SELECT 1 FROM job WHERE worker_id = ? AND status = 'running'", (worker_id,)).fetchone():
        raise WorkerError("This worker is running a job. Cancel the job or drain the worker first.")
    if conn.execute("DELETE FROM worker WHERE id = ?", (worker_id,)).rowcount != 1:
        raise WorkerError("That worker no longer exists.")


# ---------------------------------------------------------------- what a worker does (the worker API)
def authenticate(conn, token):
    """(worker, problem): the worker for a token, or None and a reason ('missing', 'unknown' or 'disabled')."""
    token = (token or "").strip()
    if not token:
        return None, "missing"
    r = conn.execute("SELECT * FROM worker WHERE token_hash = ?", (hash_token(token),)).fetchone()
    if r is None:
        return None, "unknown"
    if not r["enabled"]:
        return None, "disabled"
    return row(r), None


def register(conn, worker_id, info, now=None):
    """Records what the agent says about itself. Raises ProtocolError for an unsupported protocol version."""
    info = info if isinstance(info, dict) else {}
    if info.get("protocol") != PROTOCOL:
        raise ProtocolError(f"This SyncHub speaks worker protocol {PROTOCOL}; the agent sent {info.get('protocol')!r}. Update the agent.")
    capabilities = info.get("capabilities") if isinstance(info.get("capabilities"), dict) else {}
    capabilities["agent_slots"] = info.get("slots")
    text = json.dumps(capabilities)
    if len(text) > MAX_CAPABILITIES_BYTES:                  # cutting the text would leave invalid JSON; drop the bulky part instead
        capabilities.pop("host", None)
        text = json.dumps(capabilities)[:MAX_CAPABILITIES_BYTES]
    conn.execute("UPDATE worker SET protocol = ?, version = ?, os = ?, hostname = ?, capabilities = ?, last_seen = ? WHERE id = ?",
                 (PROTOCOL, str(info.get("version") or "")[:40], str(info.get("os") or "")[:80], str(info.get("hostname") or "")[:80],
                  text, now or db.now(), worker_id))


def touch(conn, worker_id, status=None, now=None):
    """Marks the worker as seen, and stores its latest status summary when it sent one."""
    if isinstance(status, dict):
        text = json.dumps(status)
        if len(text) <= MAX_STATUS_BYTES:
            conn.execute("UPDATE worker SET last_seen = ?, status = ? WHERE id = ?", (now or db.now(), text, worker_id))
            return
    conn.execute("UPDATE worker SET last_seen = ? WHERE id = ?", (now or db.now(), worker_id))


# ---------------------------------------------------------------- the worker's own log ("Get logs")
def request_log(conn, worker_id):
    """Asks a worker for the tail of its own log. It answers with its next poll."""
    if conn.execute("UPDATE worker SET log_requested = 1 WHERE id = ? AND revoked_at IS NULL", (worker_id,)).rowcount != 1:
        raise WorkerError("That worker no longer exists." if get(conn, worker_id) is None else "That worker's token is revoked, so it cannot answer. Make a new token first.")


def store_log(conn, worker_id, text, truncated=False, now=None):
    """Keeps the log a worker uploaded (the latest only) and closes the request."""
    text = str(text if text is not None else "")
    if len(text) > MAX_LOG_CHARS:
        text, truncated = "[earlier lines left out]\n" + text[-MAX_LOG_CHARS:], True
    conn.execute("INSERT INTO worker_log (worker_id, received_at, truncated, content) VALUES (?, ?, ?, ?) "
                 "ON CONFLICT(worker_id) DO UPDATE SET received_at = excluded.received_at, truncated = excluded.truncated, content = excluded.content",
                 (worker_id, now or db.now(), 1 if truncated else 0, text))
    conn.execute("UPDATE worker SET log_requested = 0 WHERE id = ?", (worker_id,))


def stored_log(conn, worker_id):
    r = conn.execute("SELECT * FROM worker_log WHERE worker_id = ?", (worker_id,)).fetchone()
    return dict(r) if r else None
