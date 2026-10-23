"""What Jellyfin's Webhook plugin tells us, kept for later: every event is stored with the fields worth querying and the body as it arrived."""
import json
import threading
import time

from synchub import db, settings

PAYLOAD_LIMIT = 256 * 1024
PROGRESS_SAMPLE_SECONDS = 30            # PlaybackProgress arrives every few seconds per player; one row per half minute keeps the story without the flood
PROGRESS_EVENT = "PlaybackProgress"

# Plugin field (any letter case) -> column. Ints and flags are converted; everything else is stored as text.
_TEXT = {"user_name": ("NotificationUsername", "Username"), "user_id": ("UserId",), "item_id": ("ItemId",), "item_type": ("ItemType",), "item_name": ("Name",),
         "series_name": ("SeriesName",), "series_id": ("SeriesId",), "device_name": ("DeviceName",), "device_id": ("DeviceId",), "client_name": ("ClientName", "Client"),
         "sent_at": ("UtcTimestamp",)}
_INT = {"season_number": ("SeasonNumber",), "episode_number": ("EpisodeNumber",), "year": ("Year",), "position_ticks": ("PlaybackPositionTicks",), "runtime_ticks": ("RunTimeTicks",)}
_FLAG = {"played_to_completion": ("PlayedToCompletion",), "is_paused": ("IsPaused",)}

_last_progress = {}
_lock = threading.Lock()


def _pick(lowered, names):
    for name in names:
        value = lowered.get(name.lower())
        if value not in (None, ""):
            return value
    return None


def _int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _flag(value):
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return int(value.strip().lower() == "true")
    return None


def parse_body(raw):
    """The sender's body as a dict, whatever Content-Type it claimed (the plugin sends text/plain unless told otherwise). {} when it is not a JSON object."""
    try:
        data = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def event_type(payload):
    value = _pick({str(k).lower(): v for k, v in payload.items()}, ("NotificationType", "eventType"))
    return str(value) if value else "WebhookTrigger"


def extract(payload):
    """The queryable fields of one payload."""
    lowered = {str(k).lower(): v for k, v in payload.items()}
    row = {column: (str(_pick(lowered, names)) if _pick(lowered, names) is not None else None) for column, names in _TEXT.items()}
    row.update({column: _int(_pick(lowered, names)) for column, names in _INT.items()})
    row.update({column: _flag(_pick(lowered, names)) for column, names in _FLAG.items()})
    return row


def _sampled_out(kind, row, now):
    """True for a progress tick that follows another one for the same player and item within the sampling window."""
    if kind != PROGRESS_EVENT:
        return False
    key = (row.get("user_id"), row.get("device_id"), row.get("item_id"))
    with _lock:
        if now - _last_progress.get(key, 0) < PROGRESS_SAMPLE_SECONDS:
            return True
        _last_progress[key] = now
        if len(_last_progress) > 2000:
            for old in [k for k, t in _last_progress.items() if now - t > 3600]:
                del _last_progress[old]
    return False


def store(raw, remote_addr=None, now=None):
    """Keeps one received event. Returns its type (the caller decides what it means), whether or not it was stored."""
    text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else (raw or "")
    payload = parse_body(text)
    kind = event_type(payload)
    row = extract(payload)
    if not _sampled_out(kind, row, now if now is not None else time.monotonic()):
        columns = ["received_at", "event_type", "remote_addr", "payload", *row]
        with db.connection() as conn:
            conn.execute(f"INSERT INTO jellyfin_event ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
                         (db.now(), kind, remote_addr, text[:PAYLOAD_LIMIT], *row.values()))
    return kind, payload


def keep_days(conn=None):
    try:
        return max(int(settings.get("jellyfin_events_keep_days", conn=conn)), 0)
    except (TypeError, ValueError):
        return 365


def prune(conn=None):
    """Deletes events older than the retention setting (0 keeps everything). Returns how many went."""
    def run(c):
        days = keep_days(c)
        if not days:
            return 0
        return c.execute("DELETE FROM jellyfin_event WHERE received_at < datetime('now', ?)", (f"-{days} days",)).rowcount
    if conn is not None:
        return run(conn)
    with db.connection() as c:
        return run(c)


# ---------------------------------------------------------------- reading them back
LIST_COLUMNS = ("id, received_at, event_type, user_name, item_type, item_name, series_name, season_number, episode_number, device_name, client_name, "
                "position_ticks, runtime_ticks, played_to_completion, is_paused, remote_addr")


def _where(kind=None, user=None, search=None):
    clauses, params = [], []
    if kind:
        clauses.append("event_type = ?")
        params.append(kind)
    if user:
        clauses.append("user_name = ?")
        params.append(user)
    if search:
        like = f"%{search}%"
        clauses.append("(item_name LIKE ? OR series_name LIKE ? OR device_name LIKE ? OR user_name LIKE ?)")
        params += [like] * 4
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def summary(conn):
    """Counts by type (all time, last 24 hours), the span covered and roughly how much space the events take."""
    types = [dict(r) for r in conn.execute(
        """SELECT event_type, COUNT(*) AS total, SUM(received_at >= datetime('now', '-1 day')) AS day, MAX(received_at) AS latest
           FROM jellyfin_event GROUP BY event_type ORDER BY total DESC""")]
    total = conn.execute("SELECT COUNT(*) AS n, MIN(received_at) AS first, COALESCE(SUM(LENGTH(payload)), 0) AS bytes FROM jellyfin_event").fetchone()
    users = [r["user_name"] for r in conn.execute("SELECT DISTINCT user_name FROM jellyfin_event WHERE user_name IS NOT NULL ORDER BY user_name COLLATE NOCASE")]
    return {"types": types, "total": total["n"], "first": total["first"], "bytes": total["bytes"], "users": users}


def recent(conn, kind=None, user=None, search=None, limit=200):
    where, params = _where(kind, user, search)
    return [dict(r) for r in conn.execute(f"SELECT {LIST_COLUMNS} FROM jellyfin_event{where} ORDER BY id DESC LIMIT ?", (*params, limit))]


def payload_of(conn, event_id):
    row = conn.execute("SELECT payload FROM jellyfin_event WHERE id = ?", (event_id,)).fetchone()
    return row["payload"] if row else None


def export_lines(conn, kind=None, user=None, search=None, chunk=500):
    """One JSON object per line, oldest first: the stored fields plus the payload as it arrived."""
    where, params = _where(kind, user, search)
    last = 0
    while True:
        rows = conn.execute(f"SELECT * FROM jellyfin_event{where}{' AND' if where else ' WHERE'} id > ? ORDER BY id LIMIT ?", (*params, last, chunk)).fetchall()
        if not rows:
            return
        for r in rows:
            record = dict(r)
            record["payload"] = parse_body(record["payload"]) or record["payload"]
            yield json.dumps(record, ensure_ascii=False) + "\n"
        last = rows[-1]["id"]


def clear(conn):
    return conn.execute("DELETE FROM jellyfin_event").rowcount
