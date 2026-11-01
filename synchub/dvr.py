"""Jellyfin as the DVR: which library items are recordings, what Jellyfin has scheduled to record, and a few numbers about both.

Reading never changes Jellyfin; the only things written to it are the Record button (record, below) and Cancel. A recording is an item whose path is inside one of the recording folders (setting dvr_path_prefixes,
spelled as Jellyfin sees the paths). Jellyfin's timer states are New (scheduled), InProgress, Completed, Cancelled, ConflictedOk, ConflictedNotOk and Error."""
from datetime import datetime, timedelta

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


def upcoming(conn, hours=24, now=None):
    """What is recording now and what Jellyfin will start recording within `hours` (times are UTC, 'YYYY-MM-DD HH:MM:SS')."""
    now = now or db.now()
    limit = (datetime.strptime(now, "%Y-%m-%d %H:%M:%S") + timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    cols = "timer_id, name, channel_name, start_at, end_at, status"
    recording = conn.execute(f"SELECT {cols} FROM dvr_timer WHERE kind = 'timer' AND removed_at IS NULL AND status = 'InProgress' ORDER BY start_at").fetchall()
    coming = conn.execute(f"SELECT {cols} FROM dvr_timer WHERE kind = 'timer' AND removed_at IS NULL AND status IN ('New', 'ConflictedOk', 'ConflictedNotOk') "
                          "AND start_at > ? AND start_at <= ? ORDER BY start_at", (now, limit)).fetchall()
    return {"recording": [dict(r) for r in recording], "coming": [dict(r) for r in coming], "hours": hours}


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


# ---------------------------------------------------------------- full or short
SHORT_BELOW = 0.9           # a recording under this share of its scheduled length is called short
START_EARLY = 600           # a recording file starts a little before the program (padding); seconds allowed
_FORMAT = "%Y-%m-%d %H:%M:%S"


def _seconds(value):
    from datetime import datetime
    return datetime.strptime(value, _FORMAT)


def completeness(conn, row):
    """('full' | 'short' | 'unknown', percent or None) for one recording (a src_jellyfin_item row).

    The recording is matched to the timer Jellyfin had for it: same show (or film) name, and a timer window that contains the moment the file was
    created. Jellyfin drops a timer shortly after it finishes, but SyncHub keeps its copy. The recorded length is compared with the timer's length.
    Whenever the length or the timer is missing, or two different timers fit, the answer is unknown rather than a guess."""
    runtime, created = row["runtime_seconds"], row["date_created"]
    if not runtime or not created:
        return "unknown", None
    names = {n.strip().lower() for n in (row["series_name"], row["name"]) if n and n.strip()}
    try:
        made = _seconds(created[:19])
    except ValueError:
        return "unknown", None
    fits = []
    for t in conn.execute("SELECT name, start_at, end_at FROM dvr_timer WHERE kind = 'timer' AND start_at IS NOT NULL AND end_at IS NOT NULL"):
        if (t["name"] or "").strip().lower() not in names:
            continue
        start, end = _seconds(t["start_at"]), _seconds(t["end_at"])
        if (start - made).total_seconds() <= START_EARLY and made <= end:
            fits.append((end - start).total_seconds())
    if len(set(fits)) != 1 or fits[0] <= 0:
        return "unknown", None
    percent = round(100 * runtime / fits[0])
    return ("short" if runtime < SHORT_BELOW * fits[0] else "full"), percent


# ---------------------------------------------------------------- what Jellyfin says about a live channel being watched
_ITEM_FIELDS = ("Id", "Name", "Type", "MediaType", "IsLive", "ChannelId", "ChannelName", "ChannelNumber", "SeriesName", "EpisodeTitle", "SeasonName",
                "IndexNumber", "ParentIndexNumber", "StartDate", "EndDate", "RunTimeTicks", "ProgramId", "TimerId", "SeriesTimerId")
_PROGRAM_FIELDS = ("Id", "Name", "Type", "EpisodeTitle", "SeriesName", "IndexNumber", "ParentIndexNumber", "StartDate", "EndDate", "RunTimeTicks",
                   "IsSeries", "IsMovie", "IsLive", "IsNews", "IsSports", "TimerId", "SeriesTimerId", "ChannelId", "ChannelName")


def _pick(item, fields):
    return {k: item[k] for k in fields if item.get(k) is not None}


def live_session_details(client):
    """The sessions that are playing live TV, cut down to the fields that matter for recording (no keys, tokens, addresses or device IDs), plus what
    Jellyfin says is on air on that channel now. Used to find out which fields Jellyfin really fills in; it only reads."""
    out = []
    for s in client.sessions():
        item = s.get("NowPlayingItem") or {}
        if not item or not (item.get("Type") in ("TvChannel", "LiveTvChannel", "Program", "LiveTvProgram") or item.get("ChannelId") or item.get("IsLive")):
            continue
        entry = {"user": s.get("UserName"), "client": s.get("Client"), "device": s.get("DeviceName"),
                 "play_state": _pick(s.get("PlayState") or {}, ("PositionTicks", "IsPaused", "PlayMethod")),
                 "now_playing": _pick(item, _ITEM_FIELDS), "now_playing_all_fields": sorted(item)}
        channel_id = item.get("ChannelId") or (item.get("Id") if item.get("Type") in ("TvChannel", "LiveTvChannel") else None)
        entry["channel_id"] = channel_id
        if channel_id:
            try:
                ch = client.channel(channel_id)
                entry["on_air_now"] = _pick(ch.get("CurrentProgram") or {}, _PROGRAM_FIELDS) or None
                entry["channel"] = _pick(ch, ("Id", "Name", "Number", "Type", "ServiceName"))
            except Exception as exc:                     # the session details are still worth showing
                entry["on_air_now_error"] = str(exc)
        out.append(entry)
    return out


# ---------------------------------------------------------------- the Record button
class DvrError(Exception):
    """A reason, in plain words, why a recording could not be scheduled or cancelled."""


def _parse(value):
    when = _when(value)
    return datetime.strptime(when, _FORMAT) if when else None


def program_on_air(client, channel_id):
    """(channel, program) for what Jellyfin's guide says is on a channel now."""
    channel = client.channel(channel_id)
    program = channel.get("CurrentProgram") or {}
    if not program.get("Id"):
        raise DvrError("Jellyfin's guide has nothing listed on this channel right now, so there is nothing to record.")
    return channel, program


def _timer_for(timers, program_id):
    return next((t for t in timers if t.get("ProgramId") == program_id), None)


def record(client, channel_id, series=False, now=None):
    """Schedules what is on a channel now: this airing, or (series=True) every new airing. Nothing is scheduled twice.

    Returns {result, title, late_minutes, message}; result is scheduled, already, conflict or unconfirmed. Raises DvrError with the reason when it cannot."""
    now = now or datetime.utcnow()
    channel, program = program_on_air(client, channel_id)
    title = program.get("Name") or channel.get("Name") or "this program"
    start = _parse(program.get("StartDate"))
    late = int((now - start).total_seconds() // 60) if start and (now - start).total_seconds() > 60 else 0
    out = {"title": title, "late_minutes": late}
    if series:
        if not program.get("IsSeries"):
            raise DvrError(f"{title} is not listed as a series (it looks like a film or a one-off), so there is no series to record. Use Record this airing.")
        rules = client.series_timers()
        if program.get("SeriesTimerId") or any((r.get("Name") or "").strip().lower() == title.strip().lower() for r in rules):
            return {**out, "result": "already", "message": f"A series recording for {title} already exists."}
        body = client.timer_defaults(program["Id"])
        body["RecordNewOnly"] = True                              # new episodes only, not every rerun
        client.create_series_timer(body)
        return {**out, "result": "scheduled", "message": f"Recording every new episode of {title}. It is a rule: cancel it from Jellyfin's Live TV schedule when you no longer want it."}
    if program.get("TimerId") or _timer_for(client.timers(), program["Id"]):
        return {**out, "result": "already", "message": f"{title} is already scheduled to record."}
    client.create_timer(client.timer_defaults(program["Id"]))
    made = _timer_for(client.timers(), program["Id"])
    where = f" It started {late} minute{'s' if late != 1 else ''} ago, so only the rest of it will be recorded." if late else ""
    if not made:
        return {**out, "result": "unconfirmed", "message": f"Asked Jellyfin to record {title}, but it does not show up in its schedule yet. Check the Live TV schedule.{where}"}
    if made.get("Status") in FAILED:
        return {**out, "result": "conflict", "message": f"Jellyfin scheduled {title} but reports a clash with another recording, so it may not record.{where}"}
    return {**out, "result": "scheduled", "message": f"Recording {title}.{where}"}


def cancel(client, timer_id, kind):
    """Cancels one scheduled recording ('timer') or a series rule ('series')."""
    if kind not in KINDS:
        raise DvrError("Unknown kind of recording.")
    (client.cancel_series_timer if kind == "series" else client.cancel_timer)(timer_id)


# ---------------------------------------------------------------- recordings that went wrong
GIVE_UP_HOURS = 48          # a finished recording nobody checked in this long is no longer news
SETTLE_MINUTES = 15         # how long after the end the file has to show up in a Jellyfin sync


def _recording_for(conn, timer):
    """The recording item whose creation time falls inside a timer's window and whose show or film name matches it, or None."""
    name = (timer["name"] or "").strip().lower()
    start, end = _seconds(timer["start_at"]), _seconds(timer["end_at"])
    found = []
    for r in conn.execute("SELECT * FROM src_jellyfin_item WHERE is_dvr = 1 AND removed_at IS NULL AND type IN ('movie', 'episode') AND date_created IS NOT NULL"):
        names = {(r["series_name"] or "").strip().lower(), (r["name"] or "").strip().lower()}
        if name in names and (start - _seconds(r["date_created"][:19])).total_seconds() <= START_EARLY and _seconds(r["date_created"][:19]) <= end:
            found.append(r)
    return found[0] if found else None


def recording_problems(conn, now=None):
    """Recordings that need a person: a clash or error Jellyfin reported, a recording that never appeared, or one much shorter than scheduled.

    Each timer is judged once (dvr_timer.alerted). A finished timer waits until a Jellyfin library sync has run after its end, because the file only
    exists for SyncHub after that. Returns [{timer_id, kind, name, detail}] and marks each timer."""
    now = now or db.now()
    problems = []

    def mark(timer_id, value):
        conn.execute("UPDATE dvr_timer SET alerted = ? WHERE timer_id = ?", (value, timer_id))

    for t in conn.execute("SELECT * FROM dvr_timer WHERE kind = 'timer' AND removed_at IS NULL AND status IN ('Error', 'ConflictedNotOk') AND alerted IS NULL").fetchall():
        problems.append({"timer_id": t["timer_id"], "kind": "conflict", "name": t["name"] or "A recording",
                         "detail": "Jellyfin reports a clash with another recording, or an error, so it may not record."})
        mark(t["timer_id"], "conflict")
    synced = conn.execute("SELECT MAX(last_seen) FROM src_jellyfin_item WHERE removed_at IS NULL").fetchone()[0]
    stale = (datetime.strptime(now, _FORMAT) - timedelta(hours=GIVE_UP_HOURS)).strftime(_FORMAT)
    for t in conn.execute("SELECT * FROM dvr_timer WHERE kind = 'timer' AND removed_at IS NOT NULL AND alerted IS NULL AND start_at IS NOT NULL AND end_at IS NOT NULL").fetchall():
        end = _seconds(t["end_at"])
        if t["removed_at"] < (end - timedelta(minutes=2)).strftime(_FORMAT):
            mark(t["timer_id"], "cancelled")                   # taken away before it finished: someone cancelled it
            continue
        if t["end_at"] < stale:
            mark(t["timer_id"], "stale")
            continue
        if not synced or synced < (end + timedelta(minutes=SETTLE_MINUTES)).strftime(_FORMAT):
            continue                                           # no library sync since it ended: look again next time
        item = _recording_for(conn, t)
        name = t["name"] or "A recording"
        if item is None:
            problems.append({"timer_id": t["timer_id"], "kind": "missing", "name": name, "detail": "No recording of it appeared in Jellyfin's library."})
            mark(t["timer_id"], "missing")
            continue
        label, percent = completeness(conn, item)
        if label == "short":
            problems.append({"timer_id": t["timer_id"], "kind": "short", "name": name, "detail": f"The recording is only about {percent}% of the scheduled length."})
        mark(t["timer_id"], label)
    return problems
