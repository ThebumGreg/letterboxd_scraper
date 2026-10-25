"""Recording that something happened and queueing a message for each recipient. Database writes only: sending is the dispatcher's job, so an
event can be raised from anywhere (a sync, a webhook, a page) without a network call or a held-up transaction."""
import json
import logging

from synchub import db
from synchub.notifications import events, store

logger = logging.getLogger(__name__)


def enqueue(conn, event_type, dedupe_key, payload, channels, now=None, hold_until=None):
    """Records the event and one pending notification per enabled channel in `channels` (dicts from store.get_channel).

    `hold_until` maps a profile id to a time before which a message that is not urgent should wait (that person's quiet hours).

    Returns the new notification ids, or None when this event (same type and dedupe key) was recorded before: the duplicate guard."""
    event = events.get(event_type)
    now = now or db.now()
    cur = conn.execute("INSERT OR IGNORE INTO notification_event (event_type, dedupe_key, payload, created_at) VALUES (?, ?, ?, ?)",
                       (event_type, str(dedupe_key), json.dumps(payload or {}), now))
    if not cur.rowcount:
        return None
    message = event.render(payload or {})
    expires = store.later(now, event.expires_hours * 3600)
    ids = []
    for channel in channels:
        if not channel["enabled"]:
            continue
        hold = (hold_until or {}).get(channel["profile_id"]) if message.priority < 1 else None
        ids.append(conn.execute(
            """INSERT INTO notification (event_id, profile_id, channel_id, channel_label, title, body, url, url_title, priority, ttl_seconds, expires_at, created_at, next_attempt_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (cur.lastrowid, channel["profile_id"], channel["id"], channel["label"], message.title, message.body, message.url, message.url_title,
             message.priority, message.ttl_seconds, max(expires, store.later(hold, 3600)) if hold else expires, now, hold)).lastrowid)
    return ids


def notify(conn, event_type, dedupe_key, payload, profile_ids=None, now=None):
    """Raises an event for the people it concerns: `profile_ids` for a user event, the administrators chosen in Settings for an admin event.

    Each person's own choices apply: an event they switched off is skipped, and one that is not urgent waits out their quiet hours.
    Never raises: a notification problem must not break the thing that happened. Returns the new notification ids (empty if none)."""
    try:
        event = events.get(event_type)
        now = now or db.now()
        wanted = store.admin_ids(conn) if event.audience == events.ADMIN else list(profile_ids or [])
        channels, hold = [], {}
        for profile_id in wanted:
            if not store.wants(conn, profile_id, event_type):
                continue
            mine = [store.get_channel(conn, r["id"]) for r in conn.execute("SELECT id FROM notification_channel WHERE profile_id = ? AND enabled = 1", (profile_id,))]
            if not mine:
                continue
            channels += mine
            until = store.hold_until(store.pref(conn, profile_id), now)
            if until:
                hold[profile_id] = until
        if not channels:
            return []
        return enqueue(conn, event_type, dedupe_key, payload, channels, now, hold) or []
    except Exception as exc:
        logger.error(f"Could not raise the '{event_type}' notification: {exc.__class__.__name__}: {exc}")
        return []
