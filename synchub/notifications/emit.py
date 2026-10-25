"""Recording that something happened and queueing a message for each recipient. Database writes only: sending is the dispatcher's job, so an
event can be raised from anywhere (a sync, a webhook, a page) without a network call or a held-up transaction."""
import json

from synchub import db
from synchub.notifications import events, store


def enqueue(conn, event_type, dedupe_key, payload, channels, now=None):
    """Records the event and one pending notification per enabled channel in `channels` (dicts from store.get_channel).

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
        ids.append(conn.execute(
            """INSERT INTO notification (event_id, profile_id, channel_id, channel_label, title, body, url, url_title, priority, ttl_seconds, expires_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (cur.lastrowid, channel["profile_id"], channel["id"], channel["label"], message.title, message.body, message.url, message.url_title,
             message.priority, message.ttl_seconds, expires, now)).lastrowid)
    return ids
