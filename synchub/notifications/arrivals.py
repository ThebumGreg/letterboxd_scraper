"""Telling people that a title they wanted has reached the library. Called by `states.compute` with the items that just became In Library.

A person "wants" a title exactly as `catalog.demand()` says. A movie is announced when it arrives; a show when its first episode does (later
episodes are not announced). An item seen for the first time is not an arrival, so the first sync never notifies. A single pass that finds more
than BULK arrivals is a first import or a library rescan, not news, and says nothing."""
import logging

from synchub import ratings
from synchub.notifications import emit

logger = logging.getLogger(__name__)
BULK = 50


def announce(conn, arrived, now=None):
    """`arrived`: (target_type, target_id) pairs that changed to In Library. Returns how many notifications were queued."""
    if not arrived:
        return 0
    if len(arrived) > BULK:
        logger.info(f"{len(arrived)} items reached the library at once; not notifying (looks like an import).")
        return 0
    from synchub import catalog                                       # catalog imports states, which calls this
    media_ids = {}
    for target_type, target_id in arrived:
        if target_type == "movie":
            media_ids.setdefault(target_id, 0)
        else:
            r = conn.execute("SELECT media_id FROM episode WHERE id = ?", (target_id,)).fetchone()
            if r is not None:
                media_ids[r["media_id"]] = media_ids.get(r["media_id"], 0) + 1
    # a show is announced for its first episode only: if others were already in the library before this pass, it is old news
    for media_id, count in list(media_ids.items()):
        if count:
            have = conn.execute("""SELECT COUNT(*) FROM item_state s JOIN episode e ON s.target_type = 'episode' AND s.target_id = e.id
                                   WHERE e.media_id = ? AND s.state_code = 'in_library'""", (media_id,)).fetchone()[0]
            if have > count:
                del media_ids[media_id]
    if not media_ids:
        return 0
    demand = catalog.demand(conn)
    people = {}
    for r in conn.execute("SELECT id, name, max_rating FROM profile WHERE is_active = 1"):
        people.setdefault(r["name"], []).append(r)
    queued = 0
    for media_id in media_ids:
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
    return queued
