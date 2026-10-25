"""A person's delivery methods ("channels") and the history of what was sent to them."""
import json
from datetime import datetime, timedelta

from synchub import db, settings
from synchub.notifications import events, providers
from synchub.notifications.providers.base import ProviderError

MAX_CHANNELS = 5
FORMAT = "%Y-%m-%d %H:%M:%S"
HISTORY_LIMIT = 50


def later(now, seconds):
    """A timestamp `seconds` after `now` (negative for before), both in the database's text form."""
    return (datetime.strptime(now, FORMAT) + timedelta(seconds=seconds)).strftime(FORMAT)


def _load(text):
    try:
        got = json.loads(text or "{}")
    except ValueError:
        return {}
    return got if isinstance(got, dict) else {}


def _channel(row):
    channel = dict(row)
    channel["config"] = _load(channel["config"])
    return channel


def get_channel(conn, channel_id, profile_id=None):
    """The full channel, secrets included (for sending, never for a page). With profile_id, only if it is that person's."""
    sql, params = "SELECT * FROM notification_channel WHERE id = ?", [channel_id]
    if profile_id is not None:
        sql, params = sql + " AND profile_id = ?", params + [profile_id]
    row = conn.execute(sql, params).fetchone()
    return _channel(row) if row else None


def list_channels(conn, profile_id):
    """A person's channels as a page may show them: secrets masked."""
    shown = []
    for row in conn.execute("SELECT * FROM notification_channel WHERE profile_id = ? ORDER BY id", (profile_id,)):
        channel = _channel(row)
        try:
            provider = providers.get(channel["provider"])
            summary, name = provider.summary(channel["config"]), provider.label
        except ProviderError:
            summary, name = {}, channel["provider"]
        shown.append({"id": channel["id"], "provider": channel["provider"], "provider_label": name, "label": channel["label"], "summary": summary,
                      "enabled": bool(channel["enabled"]), "verified_at": channel["verified_at"], "last_ok_at": channel["last_ok_at"],
                      "last_error": channel["last_error"], "failures": channel["failures"]})
    return shown


def add_channel(conn, profile_id, provider_key, label, config, now=None):
    """Saves a channel the caller has already checked with the provider. Returns its id."""
    provider = providers.get(provider_key)
    if provider.scope != "user":
        raise ProviderError("That delivery method is not available.")
    cleaned = provider.validate(config)
    if not conn.execute("SELECT 1 FROM profile WHERE id = ?", (profile_id,)).fetchone():
        raise ProviderError("That profile no longer exists.")
    mine = [_channel(r) for r in conn.execute("SELECT * FROM notification_channel WHERE profile_id = ?", (profile_id,))]
    if len(mine) >= MAX_CHANNELS:
        raise ProviderError(f"That is the most delivery methods one person can have ({MAX_CHANNELS}). Remove one first.")
    if any(c["provider"] == provider_key and c["config"] == cleaned for c in mine):
        raise ProviderError("You have already added that one.")
    label = (label or "").strip()[:60] or provider.default_label(cleaned)
    now = now or db.now()
    return conn.execute("INSERT INTO notification_channel (profile_id, provider, label, config, verified_at) VALUES (?, ?, ?, ?, ?)",
                        (profile_id, provider_key, label, json.dumps(cleaned), now)).lastrowid


def set_enabled(conn, channel_id, profile_id, enabled):
    """Switches a channel on or off. Switching it on forgets the failures that had switched it off. False if it is not that person's."""
    return conn.execute("UPDATE notification_channel SET enabled = ?, failures = CASE WHEN ? THEN 0 ELSE failures END, last_error = CASE WHEN ? THEN NULL ELSE last_error END "
                        "WHERE id = ? AND profile_id = ?", (1 if enabled else 0, 1 if enabled else 0, 1 if enabled else 0, channel_id, profile_id)).rowcount == 1


def delete_channel(conn, channel_id, profile_id):
    return conn.execute("DELETE FROM notification_channel WHERE id = ? AND profile_id = ?", (channel_id, profile_id)).rowcount == 1


def history(conn, profile_id, limit=HISTORY_LIMIT):
    """The latest notifications for one person, newest first, with how each went."""
    rows = conn.execute(
        """SELECT n.id, n.status, n.title, n.body, n.channel_label, n.priority, n.attempts, n.error, n.created_at, n.sent_at, e.event_type
           FROM notification n JOIN notification_event e ON e.id = n.event_id WHERE n.profile_id = ? ORDER BY n.id DESC LIMIT ?""", (profile_id, limit))
    shown = []
    for r in rows:
        item = dict(r)
        item["event_label"] = events.REGISTRY[r["event_type"]].label if r["event_type"] in events.REGISTRY else r["event_type"]
        shown.append(item)
    return shown


def keep_days():
    try:
        return max(0, int(settings.get("notify_history_keep_days")))
    except (TypeError, ValueError):
        return 90


def prune(conn, days=None, now=None):
    """Deletes events (and so their notifications) older than the retention setting; 0 keeps everything. Returns how many events went."""
    days = keep_days() if days is None else days
    if not days:
        return 0
    cutoff = later(now or db.now(), -days * 86400)
    return conn.execute("DELETE FROM notification_event WHERE created_at < ?", (cutoff,)).rowcount


def secret_values(conn):
    """Every stored key and token, so the log viewer can blank them out."""
    found = set()
    for row in conn.execute("SELECT provider, config FROM notification_channel"):
        try:
            secret_names = [f.name for f in providers.get(row["provider"]).fields if f.secret]
        except ProviderError:
            continue
        config = _load(row["config"])
        found.update(str(config[n]) for n in secret_names if len(str(config.get(n) or "")) >= 6)
    return sorted(found, key=len, reverse=True)
