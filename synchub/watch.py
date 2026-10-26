"""Where a person is in a movie or a show: what the title pages show, and where playback starts when they pick it up again.

The data is Jellyfin's own per-user data (see ingest/jellyfin_userdata.py); this only reads it back. A person is a Jellyfin user, so a profile
without one has nothing to show. Specials (season 0) are never counted, as everywhere else in SyncHub."""
from datetime import datetime

from synchub.ingest.jellyfin_userdata import norm

TICKS = 10_000_000          # Jellyfin counts time in 100-nanosecond ticks
STARTED_MIN_TICKS = 15 * TICKS      # a few seconds in is a click, not a viewing (a TV's deep link opens a title at 0:01)
FINISHED_FRACTION = 0.9             # Jellyfin counts a title as finished from about here
EVENT_SLACK_SECONDS = 600           # an event this long before Jellyfin's "last played" still belongs to that viewing


def user_of(profile):
    """The Jellyfin user (normalised) a profile stands for, or None."""
    return norm((profile or {}).get("jellyfin_user_id")) or None


def fmt_ticks(ticks):
    """'42:10' or '1:12:30'."""
    seconds = max(0, int((ticks or 0) // TICKS))
    hours, minutes, seconds = seconds // 3600, seconds % 3600 // 60, seconds % 60
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


def date_label(utc):
    """'2026-10-20 19:03:11' -> 'Oct 20, 2026'."""
    try:
        when = datetime.strptime((utc or "")[:10], "%Y-%m-%d")
    except ValueError:
        return None
    return f"{when.strftime('%b')} {when.day}, {when.year}"


def resume_ticks(user_data):
    """Where playback should start from Jellyfin's UserData for an item: where they stopped, or 0 (Jellyfin resets it once something is finished)."""
    try:
        return max(0, int((user_data or {}).get("PlaybackPositionTicks") or 0))
    except (TypeError, ValueError):
        return 0


def _when(text):
    try:
        return datetime.strptime((text or "")[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def event_progress(conn, user, item_ids):
    """{jellyfin item id: the person's latest playback event for it} from the Jellyfin events log (webhook), for one Jellyfin user.

    Jellyfin forgets where someone stopped when it was before about 5% of the runtime, but the webhook saw it. Each value is
    {'position_ticks', 'runtime_ticks', 'at'}. Empty without the webhook."""
    ids = [norm(i) for i in item_ids if i]
    if not user or not ids:
        return {}
    found = {}
    for start in range(0, len(ids), 400):
        chunk = ids[start:start + 400]
        rows = conn.execute(
            f"""SELECT lower(replace(item_id, '-', '')) AS item, position_ticks, runtime_ticks, received_at, played_to_completion FROM jellyfin_event
                WHERE event_type IN ('PlaybackProgress', 'PlaybackStop') AND lower(replace(user_id, '-', '')) = ?
                  AND lower(replace(item_id, '-', '')) IN ({','.join('?' * len(chunk))}) ORDER BY id""", (user, *chunk))
        for r in rows:                                        # oldest first, so the last one wins
            found[r["item"]] = {"position_ticks": int(r["position_ticks"] or 0), "runtime_ticks": int(r["runtime_ticks"] or 0) or None,
                                "at": r["received_at"], "finished": bool(r["played_to_completion"])}
    return found


def event_ticks(event, last_played_at=None):
    """Where the latest playback event says the viewer was, or 0 when it does not describe a title left part-way: a click that went
    nowhere, a viewing that reached the end, or one older than the last time Jellyfin says the title was played."""
    if not event or event.get("finished"):
        return 0
    ticks, runtime = int(event.get("position_ticks") or 0), event.get("runtime_ticks")
    if ticks < STARTED_MIN_TICKS or (runtime and ticks >= runtime * FINISHED_FRACTION):
        return 0
    seen, last = _when(event.get("at")), _when(last_played_at)
    if seen and last and (last - seen).total_seconds() > EVENT_SLACK_SECONDS:
        return 0
    return ticks


def event_done(event, last_played_at=None):
    """True when the latest playback event is at the end of the title (Jellyfin counts that as finished once the player stops, which can be
    a while before its own played flag reaches SyncHub)."""
    if not event:
        return False
    if event.get("finished"):
        return True
    ticks, runtime = int(event.get("position_ticks") or 0), event.get("runtime_ticks")
    if not runtime or ticks < runtime * FINISHED_FRACTION:
        return False
    seen, last = _when(event.get("at")), _when(last_played_at)
    return not (seen and last and (last - seen).total_seconds() > EVENT_SLACK_SECONDS)


def describe(row, event=None):
    """One movie or episode as a person sees it. `row` is a src_jellyfin_userdata row, or None when they have never started it.
    `event` (see event_progress) fills in where they stopped when Jellyfin kept no position.

    Only Jellyfin's played flag counts as watched. Its play count goes up every time a title is opened, even for a second, so it
    cannot tell a viewing from a click."""
    row = dict(row) if row else {}
    position, runtime = int(row.get("position_ticks") or 0), int(row.get("runtime_ticks") or 0)
    source = "jellyfin"
    if position <= 0 and (extra := event_ticks(event, row.get("last_played_at"))):
        position, source = extra, "events"
        runtime = runtime or int((event or {}).get("runtime_ticks") or 0)
    watched_before = bool(row.get("played")) or (position <= 0 and event_done(event, row.get("last_played_at")))
    state = "in_progress" if position > 0 else "watched" if watched_before else "unwatched"
    percent = None
    if runtime and state == "in_progress":
        percent = min(99, max(1, round(100 * position / runtime)))
    elif state == "watched":
        percent = 100
    return {"state": state, "watched_before": watched_before,
            "state_label": {"unwatched": "Not watched", "watched": "Watched", "in_progress": "Watching again" if watched_before else "Started watching"}[state],
            "position_ticks": position, "runtime_ticks": runtime or None, "percent": percent, "position_source": source if position else None,
            "position_label": fmt_ticks(position) if position else None, "runtime_label": fmt_ticks(runtime) if runtime else None,
            "last_played_at": row.get("last_played_at") or ((event or {}).get("at") if position else None),
            "last_played_label": date_label(row.get("last_played_at") or ((event or {}).get("at") if position else None)),
            "play_count": int(row.get("play_count") or 0),
            "resume_label": fmt_ticks(position) if position else None}


def _row(conn, user, item_id):
    return conn.execute("SELECT * FROM src_jellyfin_userdata WHERE jellyfin_user_id = ? AND item_id = ? AND removed_at IS NULL", (user, norm(item_id))).fetchone()


def item_states(conn, user, item_ids):
    """{jellyfin item id: describe(...)} for the given items, for one Jellyfin user (a season's episode list)."""
    ids = [norm(i) for i in item_ids if i]
    if not user or not ids:
        return {}
    found = {}
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        for r in conn.execute(f"SELECT * FROM src_jellyfin_userdata WHERE jellyfin_user_id = ? AND removed_at IS NULL AND item_id IN ({','.join('?' * len(chunk))})", (user, *chunk)):
            found[r["item_id"]] = r
    events = event_progress(conn, user, [i for i in ids if i not in found or not found[i]["position_ticks"]])
    return {i: describe(found.get(i), events.get(i)) for i in ids}


def movie_status(conn, user, media_id):
    """describe(...) of a movie for one Jellyfin user, plus `item_id`; None when the movie is not in the Jellyfin library."""
    link = conn.execute("""SELECT l.source_key FROM link l JOIN src_jellyfin_item j ON j.item_id = l.source_key AND j.removed_at IS NULL
                           WHERE l.source = 'jellyfin' AND l.entity_type = 'media' AND l.entity_id = ? LIMIT 1""", (media_id,)).fetchone()
    if not link:
        return None
    event = event_progress(conn, user, [link["source_key"]]).get(norm(link["source_key"])) if user else None
    return {**describe(_row(conn, user, link["source_key"]) if user else None, event), "item_id": link["source_key"]}


def _code(r):
    return f"S{r['season_number']}E{r['episode_number']}"


def show_status(conn, user, media_id):
    """Where one Jellyfin user is in a show, counting the episodes that are in the library: how many they have watched, the episode to
    continue with (the one they are part-way through, else the next after the one they watched last) and what they watched last."""
    rows = conn.execute(
        """SELECT e.id AS episode_id, e.season_number, e.episode_number, e.title, l.source_key AS item_id,
                  u.position_ticks, u.runtime_ticks, u.played, u.play_count, u.last_played_at
           FROM episode e
           JOIN link l ON l.source = 'jellyfin_episode' AND l.entity_type = 'episode' AND l.entity_id = e.id
           JOIN src_jellyfin_item j ON j.item_id = l.source_key AND j.removed_at IS NULL
           LEFT JOIN src_jellyfin_userdata u ON u.item_id = l.source_key AND u.jellyfin_user_id = ? AND u.removed_at IS NULL
           WHERE e.media_id = ? AND e.season_number > 0 ORDER BY e.season_number, e.episode_number, u.last_played_at DESC""", (user or "", media_id)).fetchall()
    events = event_progress(conn, user, [r["item_id"] for r in rows if not r["position_ticks"]]) if user else {}
    episodes, seen = [], set()
    for r in rows:
        if r["episode_id"] not in seen:                       # an episode with two files is one episode
            seen.add(r["episode_id"])
            episodes.append((r, describe(r if r["position_ticks"] is not None else None, events.get(norm(r["item_id"])))))
    watched = sum(1 for _, d in episodes if d["watched_before"])
    result = {"total": len(episodes), "watched": watched, "percent": round(100 * watched / len(episodes)) if episodes else 0,
              "state": "caught_up" if episodes and watched == len(episodes) else "in_progress" if watched or any(d["state"] == "in_progress" for _, d in episodes) else "not_started",
              "continue": None, "last": None}
    if not episodes:
        return result
    played = [(r, d) for r, d in episodes if d["last_played_at"]]
    last = max(played, key=lambda pair: pair[1]["last_played_at"]) if played else None
    if last:
        r, d = last
        result["last"] = {"code": _code(r), "title": r["title"], "date_label": d["last_played_label"], "last_played_at": d["last_played_at"]}
    mid = [(r, d) for r, d in episodes if d["state"] == "in_progress"]
    if mid:
        r, d = max(mid, key=lambda pair: ((not pair[1]["watched_before"]), pair[1]["last_played_at"] or ""))      # unfinished first-time viewings win over re-watches
        result["continue"] = {"kind": "resume", **_pick(r, d)}
    else:
        order = [r["episode_id"] for r, _ in episodes]
        after = order.index(last[0]["episode_id"]) + 1 if last else 0
        upcoming = [(r, d) for (r, d) in episodes[after:] if not d["watched_before"]] or [(r, d) for (r, d) in episodes if not d["watched_before"]]
        if upcoming:
            r, d = upcoming[0]
            result["continue"] = {"kind": "next", **_pick(r, d)}
    return result


def _pick(r, d):
    return {"episode_id": r["episode_id"], "item_id": r["item_id"], "code": _code(r), "title": r["title"], "position_label": d["position_label"],
            "percent": d["percent"], "resume_label": d["resume_label"]}


def _in(column, ids):
    return f"{column} IN ({','.join('?' * len(ids))})"


def continue_watching(conn, user, limit=12, recent_days=60):
    """What one Jellyfin user is part-way through, newest first, one entry per movie or show, for the Continue Watching row:
    [{'media_id', 'kind', 'item_id', 'code', 'title', 'position_label', 'percent', 'at'}]. A show's entry is its most recently left episode.
    Uses Jellyfin's saved positions plus the webhook log for viewings Jellyfin forgot (see event_progress)."""
    if not user:
        return []
    found = {}
    for r in conn.execute("SELECT item_id, position_ticks, runtime_ticks, last_played_at FROM src_jellyfin_userdata "
                          "WHERE jellyfin_user_id = ? AND removed_at IS NULL AND position_ticks > 0 ORDER BY last_played_at DESC LIMIT 200", (user,)):
        found[r["item_id"]] = {"ticks": int(r["position_ticks"]), "runtime": int(r["runtime_ticks"] or 0) or None, "at": r["last_played_at"] or ""}
    recent = [r["item"] for r in conn.execute(
        """SELECT DISTINCT lower(replace(item_id, '-', '')) AS item FROM jellyfin_event WHERE event_type IN ('PlaybackProgress', 'PlaybackStop')
           AND lower(replace(user_id, '-', '')) = ? AND received_at >= datetime('now', ?) AND item_id IS NOT NULL LIMIT 200""", (user, f"-{recent_days} days"))]
    events = event_progress(conn, user, [i for i in recent if i not in found])
    for item_id, ev in events.items():
        ticks = event_ticks(ev)
        if ticks:
            found[item_id] = {"ticks": ticks, "runtime": ev.get("runtime_ticks"), "at": ev["at"] or ""}
    if not found:
        return []
    ids = list(found)[:400]
    media = {r["item"]: {"media_id": r["media_id"], "kind": "movie", "code": None, "title": None} for r in conn.execute(
        f"""SELECT l.source_key AS item, l.entity_id AS media_id FROM link l JOIN media m ON m.id = l.entity_id AND m.kind = 'movie'
            WHERE l.source = 'jellyfin' AND l.entity_type = 'media' AND {_in('l.source_key', ids)}""", ids)}
    for r in conn.execute(
            f"""SELECT l.source_key AS item, e.media_id, e.season_number, e.episode_number, e.title FROM link l JOIN episode e ON e.id = l.entity_id
                WHERE l.source = 'jellyfin_episode' AND l.entity_type = 'episode' AND e.season_number > 0 AND {_in('l.source_key', ids)}""", ids):
        media[r["item"]] = {"media_id": r["media_id"], "kind": "show", "code": f"S{r['season_number']}E{r['episode_number']}", "title": r["title"]}
    best = {}
    for item_id, info in media.items():
        where = found[item_id]
        pick = {**info, "item_id": item_id, "position_label": fmt_ticks(where["ticks"]), "at": where["at"],
                "percent": min(99, max(1, round(100 * where["ticks"] / where["runtime"]))) if where["runtime"] else None}
        if info["media_id"] not in best or pick["at"] > best[info["media_id"]]["at"]:
            best[info["media_id"]] = pick
    return sorted(best.values(), key=lambda p: p["at"], reverse=True)[:limit]


def library_states(conn, user):
    """{media id: 'finished' | 'in_progress'} for the titles this Jellyfin user has played or started, for the dashboard's watch filter.
    A show is finished when every episode in the library is played; a title not in the result is not started."""
    if not user:
        return {}
    states = {}
    for r in conn.execute(
            """SELECT l.entity_id AS media_id, MAX(u.played) AS played, MAX(u.position_ticks) AS position FROM link l
               JOIN src_jellyfin_userdata u ON u.item_id = l.source_key AND u.jellyfin_user_id = ? AND u.removed_at IS NULL
               WHERE l.source = 'jellyfin' AND l.entity_type = 'media' GROUP BY l.entity_id""", (user,)):
        states[r["media_id"]] = "in_progress" if r["position"] else "finished" if r["played"] else None
    for r in conn.execute(
            """SELECT e.media_id, COUNT(DISTINCT e.id) AS total, COUNT(DISTINCT CASE WHEN u.played = 1 THEN e.id END) AS played,
                      MAX(COALESCE(u.position_ticks, 0)) AS position
               FROM episode e JOIN link l ON l.source = 'jellyfin_episode' AND l.entity_type = 'episode' AND l.entity_id = e.id
               JOIN src_jellyfin_item j ON j.item_id = l.source_key AND j.removed_at IS NULL
               LEFT JOIN src_jellyfin_userdata u ON u.item_id = l.source_key AND u.jellyfin_user_id = ? AND u.removed_at IS NULL
               WHERE e.season_number > 0 GROUP BY e.media_id""", (user,)):
        states[r["media_id"]] = "finished" if r["total"] and r["played"] >= r["total"] else "in_progress" if r["played"] or r["position"] else None
    return {k: v for k, v in states.items() if v}
