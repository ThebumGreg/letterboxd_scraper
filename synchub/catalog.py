"""Read-side queries for the pages: who wants what, the dashboard list, and items that still need a human."""
from collections import defaultdict
from datetime import date, datetime

from synchub import ratings, settings, states


def demand(conn):
    """{media_id: {'wanted_by': [...], 'watched_by': [...]}} from the entries of active profiles.

    Someone *wants* a title when it is on their watchlist (and not removed from it) and they have not watched it, or when
    they asked for it from the search page (that stands even if they have watched it: they asked).
    Entries are never deleted, so a title dropped from a list keeps its row but stops counting as wanted.
    """
    rows = conn.execute("""
        SELECT l.entity_id AS media_id, e.list AS list, p.name AS name, e.removed_at AS removed_at
        FROM src_letterboxd_entry e
        JOIN link l ON l.source = 'letterboxd' AND l.source_key = e.slug AND l.entity_type = 'media'
        JOIN profile p ON p.letterboxd_username = e.username AND p.is_active = 1
        UNION ALL
        SELECT l.entity_id, 'watchlist', p.name, e.removed_at
        FROM src_serializd_entry e
        JOIN link l ON l.source = 'serializd' AND l.source_key = e.serializd_show_id AND l.entity_type = 'media'
        JOIN profile p ON p.serializd_username = e.username AND p.is_active = 1""").fetchall()
    watchlist, watched, asked = defaultdict(set), defaultdict(set), defaultdict(set)
    for r in conn.execute("SELECT m.media_id, p.name FROM manual_request m JOIN profile p ON p.id = m.profile_id AND p.is_active = 1"):
        asked[r["media_id"]].add(r["name"])
    for r in rows:
        if r["list"] == "watched":
            watched[r["media_id"]].add(r["name"])
        elif r["removed_at"] is None:
            watchlist[r["media_id"]].add(r["name"])
    result = {}
    for media_id in set(watchlist) | set(watched) | set(asked):
        result[media_id] = {"wanted_by": sorted((watchlist[media_id] - watched[media_id]) | asked[media_id], key=str.lower),
                            "watched_by": sorted(watched[media_id], key=str.lower)}
    return result


def _active_links(conn, source, table, key_col):
    """{media_id: source_key} for links whose source row is still reported by the source."""
    rows = conn.execute(f"""SELECT l.entity_id AS media_id, l.source_key AS key FROM link l JOIN {table} t ON t.{key_col} = l.source_key
                            WHERE l.source = ? AND l.entity_type = 'media' AND t.removed_at IS NULL""", (source,))
    return {r["media_id"]: r["key"] for r in rows}


def _jellyfin_link(base_url, item_id):
    return f"{base_url}/web/index.html#/details?id={item_id}" if base_url and item_id else None


def jellyfin_item_link(item_id):
    """The Jellyfin web page of an item (the details page, one click from Play), or None when Jellyfin's address is not set."""
    return _jellyfin_link(settings.get("jellyfin_url").rstrip("/"), item_id)


def _added_label(value):
    """'2026-10-21 18:04:11' -> ('2026-10-21', 'Oct 21, 2026'); (None, None) when there is no date."""
    try:
        when = datetime.strptime((value or "")[:10], "%Y-%m-%d")
    except ValueError:
        return None, None
    return when.strftime("%Y-%m-%d"), f"{when.strftime('%b')} {when.day}, {when.year}"


def _added_dates(conn):
    """Jellyfin item id -> when it was added to the library. A movie or series has its own date; a show is as new as its newest episode."""
    own = {r["item_id"]: r["date_created"] for r in conn.execute(
        "SELECT item_id, date_created FROM src_jellyfin_item WHERE type IN ('movie', 'series') AND removed_at IS NULL AND date_created IS NOT NULL")}
    newest = {r["series_id"]: r["newest"] for r in conn.execute(
        "SELECT series_id, MAX(date_created) AS newest FROM src_jellyfin_item WHERE type = 'episode' AND removed_at IS NULL AND date_created IS NOT NULL AND series_id IS NOT NULL GROUP BY series_id")}
    return {item: max(filter(None, (own.get(item), newest.get(item)))) for item in set(own) | set(newest)}


def jellyfin_app_config(conn):
    """What a browser needs to open an item in the Jellyfin iOS app: the server's ID and the Jellyfin users to choose from."""
    users = [{"id": r["jellyfin_user_id"], "name": r["name"]} for r in conn.execute(
        "SELECT name, jellyfin_user_id FROM profile WHERE jellyfin_user_id IS NOT NULL AND jellyfin_user_id <> '' ORDER BY name COLLATE NOCASE")]
    return {"server_id": settings.get("jellyfin_server_id"), "users": users}


def _show_rollups(conn, today):
    ranks = {r["code"]: r["rank"] for r in conn.execute("SELECT code, rank FROM state_def")}
    per_show = defaultdict(lambda: {"counts": {}, "episodes": 0})
    for r in conn.execute("""SELECT e.media_id, e.air_date, COALESCE(s.state_code, 'missing') AS state_code
                             FROM episode e LEFT JOIN item_state s ON s.target_type = 'episode' AND s.target_id = e.id
                             WHERE e.season_number <> 0"""):
        shown = states.display_state(r["state_code"], r["air_date"], today)
        bucket = per_show[r["media_id"]]
        bucket["counts"][shown] = bucket["counts"].get(shown, 0) + 1
        bucket["episodes"] += 1
    out = {}
    for media_id, b in per_show.items():
        aired = b["episodes"] - b["counts"].get(states.NOT_AIRED, 0)
        in_library = b["counts"].get("in_library", 0)
        out[media_id] = {"state": states.rollup(b["counts"], aired, ranks), "aired": aired, "in_library": in_library,
                         "percent": round(100 * in_library / aired) if aired else 0}
    return out


def _rating_info(conn):
    """{(kind, tmdb_id): (certification, vote_average)} for every title that has been looked up."""
    return {(r["kind"], r["tmdb_id"]): (r["certification"], r["vote_average"])
            for r in conn.execute("SELECT kind, tmdb_id, certification, vote_average FROM title_rating")}


def list_catalog(conn, today=None, only=None, max_rating=None):
    """One row per movie/show for the dashboard, with state, requesters and links. `only` limits the rows to a set of media IDs.
    `max_rating` (0-3, see synchub.ratings) hides what the person may not see; None shows everything."""
    today = today or date.today().isoformat()
    defs = states.definitions(conn)
    defs["partial"] = {"code": "partial", "label": "Partial", "color": "warning", "icon": "bi-pie-chart"}
    wanted = demand(conn)
    base = settings.get("jellyfin_url").rstrip("/")
    jf, radarr, sonarr = (_active_links(conn, "jellyfin", "src_jellyfin_item", "item_id"),
                          _active_links(conn, "radarr", "src_radarr_movie", "radarr_id"),
                          _active_links(conn, "sonarr", "src_sonarr_series", "sonarr_id"))
    rollups = _show_rollups(conn, today)
    added = _added_dates(conn)
    movie_states = {r["target_id"]: (r["state_code"], r["progress"]) for r in conn.execute("SELECT * FROM item_state WHERE target_type = 'movie'")}
    episode_counts = {r["media_id"]: r["n"] for r in conn.execute("SELECT media_id, COUNT(*) AS n FROM episode WHERE season_number <> 0 GROUP BY media_id")}

    permitted = ratings.media_ids_allowed(conn, max_rating)
    rated = _rating_info(conn)
    rows = []
    for m in conn.execute("SELECT * FROM media ORDER BY title COLLATE NOCASE"):
        if (only is not None and m["id"] not in only) or (permitted is not None and m["id"] not in permitted):
            continue
        d = wanted.get(m["id"], {"wanted_by": [], "watched_by": []})
        if m["kind"] == "movie":
            state, progress = movie_states.get(m["id"], ("missing", None))
            extra = {}
        else:
            roll = rollups.get(m["id"])
            state, progress = (roll["state"], None) if roll else ("missing", None)
            extra = {"episodes": episode_counts.get(m["id"], 0), "aired": roll["aired"] if roll else 0,
                     "in_library": roll["in_library"] if roll else 0, "percent": roll["percent"] if roll else 0}
        info = defs.get(state, defs["missing"])
        date_added, date_added_label = _added_label(added.get(jf.get(m["id"])))
        rows.append({
            "id": m["id"], "kind": m["kind"], "title": m["title"], "year": m["year"], "tmdb_id": m["tmdb_id"], "tvdb_id": m["tvdb_id"],
            "url": f"/{'movie' if m['kind'] == 'movie' else 'show'}/{m['id']}", "poster": f"/poster/{m['id']}",
            "state": state, "state_label": info["label"], "state_color": info["color"], "state_icon": info.get("icon"),
            "progress": progress, "progress_pct": round(progress * 100) if progress is not None else None,
            "wanted_by": d["wanted_by"], "watched_by": d["watched_by"], "user_count": len(d["wanted_by"]),
            "in_jellyfin": m["id"] in jf, "in_radarr": m["id"] in radarr, "in_sonarr": m["id"] in sonarr,
            "jellyfin_link": _jellyfin_link(base, jf.get(m["id"])),
            "jellyfin_item_id": jf.get(m["id"]),
            "date_added": date_added, "date_added_label": date_added_label,
            "certification": rated.get((m["kind"], m["tmdb_id"]), (None, None))[0],
            **extra,
        })
    return rows


def dashboard_stats(rows):
    movies, shows = [r for r in rows if r["kind"] == "movie"], [r for r in rows if r["kind"] == "show"]
    return {
        "total": len(rows), "movies": len(movies), "shows": len(shows),
        "in_library": sum(r["state"] == "in_library" for r in rows),
        "in_flight": sum(r["state"] in ("downloading", "downloaded", "transferring", "working", "encoding", "transferring_library") for r in rows),
        "queued": sum(r["state"] == "queued" for r in rows),
        "missing": sum(r["state"] == "missing" for r in rows),
        "partial": sum(r["state"] == "partial" for r in rows),
        "users": len({u for r in rows for u in r["wanted_by"]}),
    }


def requesters_of(conn, media_id):
    return demand(conn).get(media_id, {"wanted_by": [], "watched_by": []})


def media_detail(conn, media_id):
    m = conn.execute("SELECT * FROM media WHERE id = ?", (media_id,)).fetchone()
    if not m:
        return None
    base = settings.get("jellyfin_url").rstrip("/")
    jf = _active_links(conn, "jellyfin", "src_jellyfin_item", "item_id").get(media_id)
    d = dict(m)
    d.update(requesters_of(conn, media_id))
    d["in_jellyfin"] = bool(jf)
    d["jellyfin_link"] = _jellyfin_link(base, jf)
    d["jellyfin_item_id"] = jf
    d["date_added"], d["date_added_label"] = _added_label(_added_dates(conn).get(jf))
    d["in_radarr"] = media_id in _active_links(conn, "radarr", "src_radarr_movie", "radarr_id")
    d["in_sonarr"] = media_id in _active_links(conn, "sonarr", "src_sonarr_series", "sonarr_id")
    known = ratings.get(conn, m["kind"], m["tmdb_id"]) or {}
    d["certification"], d["vote_average"], d["rating_rank"] = known.get("certification"), known.get("vote_average"), known.get("rank")
    return d


def unmatched_sources(conn):
    """Letterboxd films / Serializd shows that have no media link yet, with the people who listed them."""
    rows = []
    for r in conn.execute("""
        SELECT 'letterboxd' AS source, e.slug AS key, MAX(e.title) AS title, MAX(e.year) AS year, 'movie' AS kind,
               GROUP_CONCAT(DISTINCT e.username) AS users, MAX(e.id_checked_at) AS checked
        FROM src_letterboxd_entry e
        WHERE NOT EXISTS (SELECT 1 FROM link l WHERE l.source = 'letterboxd' AND l.source_key = e.slug)
        GROUP BY e.slug
        UNION ALL
        SELECT 'serializd', s.serializd_show_id, MAX(s.title), MAX(s.year), 'show', GROUP_CONCAT(DISTINCT s.username), NULL
        FROM src_serializd_entry s
        WHERE NOT EXISTS (SELECT 1 FROM link l WHERE l.source = 'serializd' AND l.source_key = s.serializd_show_id)
        GROUP BY s.serializd_show_id
        ORDER BY title COLLATE NOCASE"""):
        rows.append(dict(r))
    return rows


def pending_reviews(conn):
    """Pending match suggestions grouped per source record, strongest candidate first."""
    grouped = {}
    for r in conn.execute("""SELECT r.*, m.title AS candidate_title, m.year AS candidate_year
                             FROM match_review r LEFT JOIN media m ON m.id = r.candidate_media_id
                             WHERE r.status = 'pending' ORDER BY r.confidence DESC, r.id"""):
        g = grouped.setdefault((r["source"], r["source_key"]), {"source": r["source"], "key": r["source_key"], "title": r["source_title"],
                                                                "year": r["source_year"], "kind": r["kind"], "candidates": []})
        g["candidates"].append(dict(r))
    return sorted(grouped.values(), key=lambda g: -g["candidates"][0]["confidence"])
