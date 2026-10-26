"""Jellyfin -> src_jellyfin_userdata: what each user has watched and where they stopped.

Jellyfin keeps this for every user (each app that plays something writes it), so SyncHub copies it instead of keeping a count of its own: a full
read per profile now and then, and a read of one item right after a playback ends. Only things played or part-way through get a row."""
from synchub import db
from synchub.ingest.common import mark_unseen, touch


def norm(value):
    """Jellyfin's ids as this table keeps them: lower case, no dashes (the API gives that; the webhook plugin may add dashes)."""
    return str(value or "").replace("-", "").lower()


def _utc(text):
    """'2026-10-20T19:03:11.5120000Z' -> '2026-10-20 19:03:11'."""
    return (text or "")[:19].replace("T", " ") or None


def row_of(item):
    """The columns of one Jellyfin item's UserData."""
    data = item.get("UserData") or {}
    return {"position_ticks": int(data.get("PlaybackPositionTicks") or 0), "runtime_ticks": item.get("RunTimeTicks"),
            "played": 1 if data.get("Played") else 0, "play_count": int(data.get("PlayCount") or 0), "last_played_at": _utc(data.get("LastPlayedDate"))}


def meaningful(row):
    """Finished or part-way. A play count alone is not enough: Jellyfin adds one every time a title is opened, even for a second."""
    return bool(row["played"] or row["position_ticks"])


def sync_user(conn, client, jellyfin_user_id, now=None):
    """Replaces what SyncHub knows about one Jellyfin user with what Jellyfin says now. Returns {'seen', 'removed'}.

    Everything is read before anything is written, so the database is never held while waiting on Jellyfin."""
    now = now or db.now()
    user = norm(jellyfin_user_id)
    found = {}
    for filters in ("IsPlayed", "IsResumable"):
        for item in client.iter_user_data(jellyfin_user_id, filters):
            if item.get("Id"):
                found[norm(item["Id"])] = item
    seen = []
    for item_id, item in found.items():
        row = row_of(item)
        if meaningful(row):
            touch(conn, "src_jellyfin_userdata", ("jellyfin_user_id", "item_id"), {"jellyfin_user_id": user, "item_id": item_id, **row}, now)
            seen.append((user, item_id))
    removed = mark_unseen(conn, "src_jellyfin_userdata", ("jellyfin_user_id", "item_id"), seen, now, scope_sql="jellyfin_user_id = ?", scope_params=(user,), allow_empty=True)
    return {"seen": len(seen), "removed": removed}


def refresh_item(conn, client, jellyfin_user_id, item_id, now=None):
    """Re-reads one item for one user (right after they stopped playing it). True if they now have something recorded for it."""
    now = now or db.now()
    user, item_key = norm(jellyfin_user_id), norm(item_id)
    item = client.user_item(jellyfin_user_id, item_id)
    row = row_of(item) if item else None
    if row and meaningful(row):
        touch(conn, "src_jellyfin_userdata", ("jellyfin_user_id", "item_id"), {"jellyfin_user_id": user, "item_id": item_key, **row}, now)
        return True
    conn.execute("UPDATE src_jellyfin_userdata SET removed_at = ? WHERE jellyfin_user_id = ? AND item_id = ? AND removed_at IS NULL", (now, user, item_key))
    return False
