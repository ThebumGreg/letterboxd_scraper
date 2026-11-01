"""TV players: small PCs behind a TV that play media, with SyncHub as the master.

A player works like a worker: SyncHub never connects to it. The player calls the player API with its token (only a hash is stored, a lost token is
rotated, never recovered), reports what it is and what it is doing, and takes the commands SyncHub has queued for it. This step knows three commands
(ping, update, restart); playback commands come later and use the same queue.
"""
import json
from datetime import datetime, timedelta, timezone

from synchub import db
from synchub.workers import hash_token, new_token as _worker_token

PROTOCOL = 1                       # bumped when the player API changes incompatibly; older players are refused
ONLINE_SECONDS = 60                # a player that has not polled for this long shows as offline
QUEUED_EXPIRE_SECONDS = 600        # a command nobody took in ten minutes is dropped: a restart must not fire an hour later
SENT_EXPIRE_SECONDS = 300          # a command taken but never answered
MAX_STATUS_BYTES = 8192
MAX_CAPABILITIES_BYTES = 32768
MAX_NOW_PLAYING_BYTES = 8192
MAX_RESULT_CHARS = 2000
MAX_LOG_CHARS = 1_000_000
COMMANDS = ("ping", "update", "restart")


class PlayerError(Exception):
    """A change that cannot be made; the message is meant for the user."""


class ProtocolError(PlayerError):
    """The player speaks a protocol version SyncHub does not support."""


def new_token():
    return "sp_" + _worker_token()[3:]


def _name(value):
    name = " ".join((value or "").split())
    if not name:
        raise PlayerError("A player needs a name.")
    if len(name) > 60:
        raise PlayerError("The name can be at most 60 characters.")
    return name


def _tv_name(value):
    return " ".join((value or "").split())[:60]


def _load(text, default):
    try:
        value = json.loads(text) if text else default
    except ValueError:
        return default
    return value if isinstance(value, type(default)) else default


def row(r, now=None):
    """A player as a dict, with the JSON columns decoded and whether it is online."""
    p = dict(r)
    for key in ("capabilities", "status", "now_playing"):
        p[key] = _load(p.get(key), {})
    p["token_hash"] = bool(p["token_hash"])            # never hand the hash around
    p["revoked"] = bool(p.get("revoked_at"))
    p["online"] = is_online(p.get("last_seen"), now)
    return p


def is_online(last_seen, now=None):
    if not last_seen:
        return False
    try:
        seen = datetime.strptime(last_seen, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    current = datetime.strptime(now, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc) if now else datetime.now(timezone.utc)
    return (current - seen) < timedelta(seconds=ONLINE_SECONDS)


# ---------------------------------------------------------------- managing players (the web UI)
def create(conn, name, tv_name=""):
    """Adds a player. Returns (player_id, token); the token is shown once and cannot be read again."""
    name = _name(name)
    if conn.execute("SELECT 1 FROM player WHERE name = ?", (name,)).fetchone():
        raise PlayerError(f"There is already a player called '{name}'.")
    token = new_token()
    player_id = conn.execute("INSERT INTO player (name, token_hash, token_hint, tv_name) VALUES (?, ?, ?, ?)",
                             (name, hash_token(token), token[-4:], _tv_name(tv_name))).lastrowid
    return player_id, token


def get(conn, player_id, now=None):
    r = conn.execute("SELECT * FROM player WHERE id = ?", (player_id,)).fetchone()
    return row(r, now) if r else None


def list_all(conn, now=None):
    players = [row(r, now) for r in conn.execute("SELECT * FROM player ORDER BY name COLLATE NOCASE")]
    waiting = {r["player_id"]: r["n"] for r in conn.execute("SELECT player_id, COUNT(*) AS n FROM player_command WHERE status IN ('queued', 'sent') GROUP BY player_id")}
    for p in players:
        p["waiting"] = waiting.get(p["id"], 0)
    return players


def update(conn, player_id, name=None, tv_name=None):
    p = get(conn, player_id)
    if not p:
        raise PlayerError("That player no longer exists.")
    new_name = _name(name) if name is not None else p["name"]
    if new_name != p["name"] and conn.execute("SELECT 1 FROM player WHERE name = ? AND id <> ?", (new_name, player_id)).fetchone():
        raise PlayerError(f"There is already a player called '{new_name}'.")
    conn.execute("UPDATE player SET name = ?, tv_name = ? WHERE id = ?", (new_name, _tv_name(tv_name) if tv_name is not None else p["tv_name"], player_id))


def set_enabled(conn, player_id, value):
    if conn.execute("UPDATE player SET enabled = ? WHERE id = ?", (1 if value else 0, player_id)).rowcount != 1:
        raise PlayerError("That player no longer exists.")


def rotate_token(conn, player_id):
    """Replaces the token (the old one stops working at once). Returns the new token, shown once."""
    token = new_token()
    if conn.execute("UPDATE player SET token_hash = ?, token_hint = ?, revoked_at = NULL WHERE id = ?", (hash_token(token), token[-4:], player_id)).rowcount != 1:
        raise PlayerError("That player no longer exists.")
    return token


def revoke(conn, player_id):
    """Stops the token working. The player's name stays; rotate the token to let it back in."""
    if conn.execute("UPDATE player SET token_hash = NULL, token_hint = NULL, revoked_at = ? WHERE id = ?", (db.now(), player_id)).rowcount != 1:
        raise PlayerError("That player no longer exists.")


def delete(conn, player_id):
    if conn.execute("DELETE FROM player WHERE id = ?", (player_id,)).rowcount != 1:
        raise PlayerError("That player no longer exists.")


# ---------------------------------------------------------------- commands (SyncHub asks, the player does)
def send(conn, player_id, kind, payload=None):
    """Queues a command. Returns its id. The same command is not queued twice while one is still waiting."""
    if kind not in COMMANDS:
        raise PlayerError(f"There is no command called '{kind}'.")
    p = get(conn, player_id)
    if not p:
        raise PlayerError("That player no longer exists.")
    if p["revoked"]:
        raise PlayerError("That player's token is revoked, so it cannot take commands. Make a new token first.")
    expire(conn)
    open_one = conn.execute("SELECT id FROM player_command WHERE player_id = ? AND kind = ? AND status IN ('queued', 'sent') ORDER BY id LIMIT 1", (player_id, kind)).fetchone()
    if open_one and kind != "ping":
        return open_one["id"]
    return conn.execute("INSERT INTO player_command (player_id, kind, payload) VALUES (?, ?, ?)", (player_id, kind, json.dumps(payload or {}))).lastrowid


def take(conn, player_id, now=None):
    """The player's next queued command (oldest first), marked as sent, or None."""
    expire(conn, now)
    r = conn.execute("SELECT * FROM player_command WHERE player_id = ? AND status = 'queued' ORDER BY id LIMIT 1", (player_id,)).fetchone()
    if r is None:
        return None
    conn.execute("UPDATE player_command SET status = 'sent', sent_at = ? WHERE id = ?", (now or db.now(), r["id"]))
    return {"id": r["id"], "kind": r["kind"], "payload": _load(r["payload"], {})}


def has_queued(conn, player_id):
    return conn.execute("SELECT 1 FROM player_command WHERE player_id = ? AND status = 'queued' LIMIT 1", (player_id,)).fetchone() is not None


def finish(conn, player_id, command_id, ok, result="", now=None):
    """The player's answer to a command. Only a command that is this player's and was sent can be finished."""
    done = conn.execute("UPDATE player_command SET status = ?, result = ?, finished_at = ? WHERE id = ? AND player_id = ? AND status = 'sent'",
                        ("done" if ok else "failed", str(result or "")[:MAX_RESULT_CHARS], now or db.now(), command_id, player_id)).rowcount
    if done != 1:
        raise PlayerError("That command is not waiting for an answer from this player.")


def expire(conn, now=None):
    """Drops commands nobody took or answered in time."""
    current = datetime.strptime(now, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc) if now else datetime.now(timezone.utc)
    stamp = lambda seconds: (current - timedelta(seconds=seconds)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("UPDATE player_command SET status = 'expired', finished_at = ? WHERE status = 'queued' AND created_at < ?", (current.strftime("%Y-%m-%d %H:%M:%S"), stamp(QUEUED_EXPIRE_SECONDS)))
    conn.execute("UPDATE player_command SET status = 'expired', finished_at = ? WHERE status = 'sent' AND sent_at < ?", (current.strftime("%Y-%m-%d %H:%M:%S"), stamp(SENT_EXPIRE_SECONDS)))


def recent_commands(conn, player_id, limit=5):
    return [dict(r) for r in conn.execute("SELECT id, kind, status, result, created_at, finished_at FROM player_command WHERE player_id = ? ORDER BY id DESC LIMIT ?", (player_id, limit))]


# ---------------------------------------------------------------- what a player does (the player API)
def authenticate(conn, token):
    """(player, problem): the player for a token, or None and a reason ('missing', 'unknown' or 'disabled')."""
    token = (token or "").strip()
    if not token:
        return None, "missing"
    r = conn.execute("SELECT * FROM player WHERE token_hash = ?", (hash_token(token),)).fetchone()
    if r is None:
        return None, "unknown"
    if not r["enabled"]:
        return None, "disabled"
    return row(r), None


def register(conn, player_id, info, now=None):
    """Records what the player says about itself. Raises ProtocolError for an unsupported protocol version."""
    info = info if isinstance(info, dict) else {}
    if info.get("protocol") != PROTOCOL:
        raise ProtocolError(f"This SyncHub speaks player protocol {PROTOCOL}; the player sent {info.get('protocol')!r}. Update the player.")
    capabilities = info.get("capabilities") if isinstance(info.get("capabilities"), dict) else {}
    text = json.dumps(capabilities)
    if len(text) > MAX_CAPABILITIES_BYTES:
        text = json.dumps({"truncated": True})
    conn.execute("UPDATE player SET protocol = ?, version = ?, os = ?, hostname = ?, capabilities = ?, last_seen = ? WHERE id = ?",
                 (PROTOCOL, str(info.get("version") or "")[:40], str(info.get("os") or "")[:80], str(info.get("hostname") or "")[:80], text, now or db.now(), player_id))


def touch(conn, player_id, status=None, now_playing=None, now=None):
    """Marks the player as seen and stores its latest status summary and what it is playing (each only when it sent one)."""
    stamp = now or db.now()
    conn.execute("UPDATE player SET last_seen = ? WHERE id = ?", (stamp, player_id))
    for column, value, limit in (("status", status, MAX_STATUS_BYTES), ("now_playing", now_playing, MAX_NOW_PLAYING_BYTES)):
        if isinstance(value, dict):
            text = json.dumps(value)
            if len(text) <= limit:
                conn.execute(f"UPDATE player SET {column} = ? WHERE id = ?", (text, player_id))


# ---------------------------------------------------------------- the player's own log
def request_log(conn, player_id):
    if conn.execute("UPDATE player SET log_requested = 1 WHERE id = ? AND revoked_at IS NULL", (player_id,)).rowcount != 1:
        raise PlayerError("That player no longer exists." if get(conn, player_id) is None else "That player's token is revoked, so it cannot answer. Make a new token first.")


def store_log(conn, player_id, text, truncated=False, now=None):
    text = str(text if text is not None else "")
    if len(text) > MAX_LOG_CHARS:
        text, truncated = "[earlier lines left out]\n" + text[-MAX_LOG_CHARS:], True
    conn.execute("INSERT INTO player_log (player_id, received_at, truncated, content) VALUES (?, ?, ?, ?) "
                 "ON CONFLICT(player_id) DO UPDATE SET received_at = excluded.received_at, truncated = excluded.truncated, content = excluded.content",
                 (player_id, now or db.now(), 1 if truncated else 0, text))
    conn.execute("UPDATE player SET log_requested = 0 WHERE id = ?", (player_id,))


def stored_log(conn, player_id):
    r = conn.execute("SELECT * FROM player_log WHERE player_id = ?", (player_id,)).fetchone()
    return dict(r) if r else None
