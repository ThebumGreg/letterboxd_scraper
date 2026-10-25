"""Where a person is in a movie or a show: what the title pages show, and where playback starts when they pick it up again.

The data is Jellyfin's own per-user data (see ingest/jellyfin_userdata.py); this only reads it back. A person is a Jellyfin user, so a profile
without one has nothing to show. Specials (season 0) are never counted, as everywhere else in SyncHub."""
from datetime import datetime

from synchub.ingest.jellyfin_userdata import norm

TICKS = 10_000_000          # Jellyfin counts time in 100-nanosecond ticks


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


def describe(row):
    """One movie or episode as a person sees it. `row` is a src_jellyfin_userdata row, or None when they have never started it."""
    row = dict(row) if row else {}
    position, runtime = int(row.get("position_ticks") or 0), int(row.get("runtime_ticks") or 0)
    watched_before = bool(row.get("played") or row.get("play_count"))
    state = "in_progress" if position > 0 else "watched" if watched_before else "unwatched"
    percent = None
    if runtime and state == "in_progress":
        percent = min(99, max(1, round(100 * position / runtime)))
    elif state == "watched":
        percent = 100
    return {"state": state, "watched_before": watched_before,
            "state_label": {"unwatched": "Not watched", "watched": "Watched", "in_progress": "Watching again" if watched_before else "In progress"}[state],
            "position_ticks": position, "runtime_ticks": runtime or None, "percent": percent,
            "position_label": fmt_ticks(position) if position else None, "runtime_label": fmt_ticks(runtime) if runtime else None,
            "last_played_at": row.get("last_played_at"), "last_played_label": date_label(row.get("last_played_at")), "play_count": int(row.get("play_count") or 0),
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
    return {i: describe(found.get(i)) for i in ids}


def movie_status(conn, user, media_id):
    """describe(...) of a movie for one Jellyfin user, plus `item_id`; None when the movie is not in the Jellyfin library."""
    link = conn.execute("""SELECT l.source_key FROM link l JOIN src_jellyfin_item j ON j.item_id = l.source_key AND j.removed_at IS NULL
                           WHERE l.source = 'jellyfin' AND l.entity_type = 'media' AND l.entity_id = ? LIMIT 1""", (media_id,)).fetchone()
    if not link:
        return None
    return {**describe(_row(conn, user, link["source_key"]) if user else None), "item_id": link["source_key"]}


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
    episodes, seen = [], set()
    for r in rows:
        if r["episode_id"] not in seen:                       # an episode with two files is one episode
            seen.add(r["episode_id"])
            episodes.append((r, describe(r if r["position_ticks"] is not None else None)))
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
