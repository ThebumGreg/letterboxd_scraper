"""Jellyfin as the DVR: which library items are recordings, what Jellyfin has scheduled to record, and a few numbers about both.

Everything here only reads from Jellyfin. A recording is an item whose path is inside one of the recording folders (setting dvr_path_prefixes,
spelled as Jellyfin sees the paths). Jellyfin's timer states are New (scheduled), InProgress, Completed, Cancelled, ConflictedOk, ConflictedNotOk and Error."""
from synchub import db, settings
from synchub.ingest.common import mark_unseen, touch

FAILED = ("Error", "ConflictedNotOk")
KINDS = {"timer": "Timer", "series": "SeriesTimer"}


def prefixes(conn=None):
    """The recording folders, as Jellyfin spells them, without a trailing slash."""
    value = settings.get("dvr_path_prefixes", conn=conn) or ""
    return [p.strip().rstrip("/") for p in value.replace("\n", ",").split(",") if p.strip().rstrip("/")]


def is_dvr_path(path, folders):
    """True when `path` is a recording folder or inside one. Paths on Linux are case-sensitive, so this is too."""
    path = (path or "").rstrip("/")
    return any(path == f or path.startswith(f + "/") for f in folders)


def _when(value):
    """'2026-10-28T07:00:00.0000000Z' -> '2026-10-28 07:00:00'."""
    return (value or "")[:19].replace("T", " ") or None


def sync_timers(conn, timers, series_timers, now=None):
    """Saves what Jellyfin reported (both lists are read before this runs). Timers it no longer lists are marked removed. Returns the counts."""
    now = now or db.now()
    for kind, items in (("timer", timers), ("series", series_timers)):
        seen = []
        for item in items:
            if not item.get("Id"):
                continue
            touch(conn, "dvr_timer", ("timer_id",), {
                "timer_id": item["Id"], "kind": kind, "name": item.get("Name"), "channel_name": item.get("ChannelName"),
                "program_id": item.get("ProgramId"), "series_timer_id": item.get("SeriesTimerId"), "status": item.get("Status"),
                "start_at": _when(item.get("StartDate")), "end_at": _when(item.get("EndDate")),
            }, now)
            seen.append(item["Id"])
        mark_unseen(conn, "dvr_timer", ("timer_id",), seen, now, scope_sql="kind = ?", scope_params=(kind,), allow_empty=True)
    return {"timers": len(timers), "series": len(series_timers)}


def stats(conn, now=None):
    """Numbers for the status page: the recordings in the library and Jellyfin's schedule."""
    now = now or db.now()
    out = {}
    rows = {r["type"]: r["n"] for r in conn.execute("SELECT type, COUNT(*) AS n FROM src_jellyfin_item WHERE is_dvr = 1 AND removed_at IS NULL AND type IN ('movie', 'episode') GROUP BY type")}
    out["movies"], out["episodes"] = rows.get("movie", 0), rows.get("episode", 0)
    out["shows"] = conn.execute("SELECT COUNT(DISTINCT series_id) FROM src_jellyfin_item WHERE is_dvr = 1 AND removed_at IS NULL AND type = 'episode' AND series_id IS NOT NULL").fetchone()[0]
    out["last_recording"] = conn.execute("SELECT MAX(date_created) FROM src_jellyfin_item WHERE is_dvr = 1 AND removed_at IS NULL AND type IN ('movie', 'episode')").fetchone()[0]
    status = {r["status"] or "": r["n"] for r in conn.execute("SELECT status, COUNT(*) AS n FROM dvr_timer WHERE kind = 'timer' AND removed_at IS NULL GROUP BY status")}
    out["scheduled"] = status.get("New", 0) + status.get("ConflictedOk", 0)
    out["recording_now"] = status.get("InProgress", 0)
    out["failed"] = sum(status.get(s, 0) for s in FAILED)
    out["series_rules"] = conn.execute("SELECT COUNT(*) FROM dvr_timer WHERE kind = 'series' AND removed_at IS NULL").fetchone()[0]
    out["next"] = conn.execute("SELECT name, start_at FROM dvr_timer WHERE kind = 'timer' AND removed_at IS NULL AND status = 'New' AND start_at >= ? ORDER BY start_at LIMIT 1", (now,)).fetchone()
    out["next"] = dict(out["next"]) if out["next"] else None
    return out


def summary(s):
    """One line: '2 recording now, 5 scheduled, 3 series rules; 41 recordings (9 shows, 2 movies)'."""
    parts = []
    if s["recording_now"]:
        parts.append(f"{s['recording_now']} recording now")
    parts.append(f"{s['scheduled']} scheduled")
    parts.append(f"{s['series_rules']} series rule{'' if s['series_rules'] == 1 else 's'}")
    if s["failed"]:
        parts.append(f"{s['failed']} failed or in conflict")
    total = s["movies"] + s["episodes"]
    text = ", ".join(parts) + f"; {total} recording{'' if total == 1 else 's'} in the library"
    if total:
        text += f" ({s['shows']} show{'' if s['shows'] == 1 else 's'}, {s['movies']} movie{'' if s['movies'] == 1 else 's'})"
    if s["next"] and s["next"]["start_at"]:
        text += f". Next: {s['next']['name'] or 'a recording'} at {s['next']['start_at']} UTC"
    return text
