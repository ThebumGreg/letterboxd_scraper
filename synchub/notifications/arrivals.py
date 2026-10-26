"""Telling people that a title they wanted has reached the library. Called by `states.compute` with the items that just became In Library.

A person "wants" a title exactly as `catalog.demand()` says. A movie is announced when it arrives; a show when its first episode does ("a title I
wanted"). Later episodes of a show a person follows are "new episode" notices, grouped into one per show, and `announce_aired` tells those who asked
when an episode reaches its air date. An item seen for the first time is not an arrival, so the first sync never notifies. A single pass that finds more
than BULK arrivals is a first import or a library rescan, not news, and says nothing."""
import logging
from datetime import datetime, timedelta

from synchub import db, ratings
from synchub.notifications import emit, store

logger = logging.getLogger(__name__)
BULK = 50


def _people(conn):
    people = {}
    for r in conn.execute("SELECT id, name, max_rating FROM profile WHERE is_active = 1"):
        people.setdefault(r["name"], []).append(r)
    return people


def _followers(conn, media_id, entry, people, when):
    """The profile ids that follow this show and want new-episode notices at this time ('library' or 'aired'): they are on its watchlist or asked for it
    (as their own choice of source allows), are allowed to see it, have not muted it and have not switched the notice off."""
    found = []
    for name in sorted(set(entry.get("watchlisted_by", [])) | set(entry.get("requested_by", []))):
        for person in people.get(name, []):
            if not ratings.media_allowed(conn, media_id, ratings.parse_level(person["max_rating"])):
                continue
            pref = store.pref(conn, person["id"])
            if pref["episode_when"] not in (when, "both"):
                continue
            via_list, via_request = name in entry.get("watchlisted_by", []), name in entry.get("requested_by", [])
            if not ((pref["episode_source"] == "both" and (via_list or via_request)) or (pref["episode_source"] == "watchlist" and via_list)
                    or (pref["episode_source"] == "requested" and via_request)):
                continue
            if store.is_muted(conn, person["id"], media_id):
                continue
            found.append(person["id"])
    return found


def announce(conn, arrived, now=None):
    """`arrived`: (target_type, target_id) pairs that changed to In Library. Returns how many notifications were queued."""
    if not arrived:
        return 0
    if len(arrived) > BULK:
        logger.info(f"{len(arrived)} items reached the library at once; not notifying (looks like an import).")
        return 0
    from synchub import catalog                                       # catalog imports states, which calls this
    media_ids, episodes = {}, {}
    for target_type, target_id in arrived:
        if target_type == "movie":
            media_ids.setdefault(target_id, 0)
        else:
            r = conn.execute("SELECT id, media_id, season_number, episode_number, title FROM episode WHERE id = ?", (target_id,)).fetchone()
            if r is not None:
                media_ids[r["media_id"]] = media_ids.get(r["media_id"], 0) + 1
                episodes.setdefault(r["media_id"], []).append(r)
    # a show is announced for its first episode only: if others were already in the library before this pass, these are follow-up episodes
    first, later = {}, {}
    for media_id, count in media_ids.items():
        have = conn.execute("""SELECT COUNT(*) FROM item_state s JOIN episode e ON s.target_type = 'episode' AND s.target_id = e.id
                               WHERE e.media_id = ? AND s.state_code = 'in_library'""", (media_id,)).fetchone()[0] if count else 0
        (later if count and have > count else first)[media_id] = count
    if not first and not later:
        return 0
    demand = catalog.demand(conn, sources=True)
    people = _people(conn)
    queued = 0
    for media_id in first:
        wanted = (demand.get(media_id) or {}).get("wanted_by") or []
        media = conn.execute("SELECT kind, title, year FROM media WHERE id = ?", (media_id,)).fetchone()
        if media is None or not wanted:
            continue
        for name in wanted:
            for person in people.get(name, []):
                if not ratings.media_allowed(conn, media_id, ratings.parse_level(person["max_rating"])):
                    continue
                queued += len(emit.notify(conn, "request_available", f"media:{media_id}:profile:{person['id']}",
                                          {"media_id": media_id, "kind": media["kind"], "title": media["title"], "year": media["year"]},
                                          profile_ids=[person["id"]], now=now))
    for media_id in later:
        media = conn.execute("SELECT title, year FROM media WHERE id = ?", (media_id,)).fetchone()
        eps = sorted(episodes.get(media_id, []), key=lambda e: (e["season_number"], e["episode_number"]))
        if media is None or not eps:
            continue
        payload = {"media_id": media_id, "title": media["title"], "year": media["year"],
                   "episodes": [{"id": e["id"], "season": e["season_number"], "episode": e["episode_number"], "name": e["title"]} for e in eps]}
        key = "-".join(str(e["id"]) for e in eps)
        for pid in _followers(conn, media_id, demand.get(media_id) or {}, people, "library"):
            queued += len(emit.notify(conn, "new_episode", f"episodes:{media_id}:{key}:profile:{pid}", payload, profile_ids=[pid], now=now))
    return queued


def announce_aired(conn, now=None, days=1):
    """Tells the people who chose 'when it airs' about episodes whose air date is today or in the last `days` days. Safe to run often: an episode is
    announced to a person once. Returns how many notifications were queued."""
    from synchub import catalog
    now = now or db.now()
    today = datetime.strptime(now, "%Y-%m-%d %H:%M:%S").date()
    since = (today - timedelta(days=days)).isoformat()
    rows = conn.execute("""SELECT e.id, e.media_id, e.season_number, e.episode_number, e.title, e.air_date, m.title AS show_title, m.year,
                                  EXISTS (SELECT 1 FROM item_state s WHERE s.target_type = 'episode' AND s.target_id = e.id AND s.state_code = 'in_library') AS in_library
                           FROM episode e JOIN media m ON m.id = e.media_id
                           WHERE m.kind = 'show' AND e.air_date >= ? AND e.air_date <= ? AND e.season_number > 0 ORDER BY e.media_id, e.air_date, e.season_number, e.episode_number""",
                        (since, today.isoformat())).fetchall()
    if not rows:
        return 0
    demand = catalog.demand(conn, sources=True)
    people = _people(conn)
    groups = {}
    for r in rows:
        groups.setdefault((r["media_id"], r["air_date"]), []).append(r)
    queued = 0
    for (media_id, air_date), eps in groups.items():
        entry = demand.get(media_id)
        if not entry:
            continue
        payload = {"media_id": media_id, "title": eps[0]["show_title"], "air_date": air_date, "in_library": all(e["in_library"] for e in eps),
                   "episodes": [{"id": e["id"], "season": e["season_number"], "episode": e["episode_number"], "name": e["title"]} for e in eps]}
        for pid in _followers(conn, media_id, entry, people, "aired"):
            queued += len(emit.notify(conn, "episode_aired", f"aired:{media_id}:{air_date}:profile:{pid}", payload, profile_ids=[pid], now=now))
    return queued
