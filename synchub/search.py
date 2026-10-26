"""Searching TMDB and adding what is found to Radarr (movies) or Sonarr (shows)."""
import logging
import re

from synchub import automation, catalog, db, metadata
from synchub.automation import RequestError

logger = logging.getLogger(__name__)

POSTER = "https://image.tmdb.org/t/p/w185"
RESULTS_PER_KIND = 10
KINDS = ("all", "movie", "show")
# "Heat 1995" and "Heat (1995)": the year narrows the search. A title that merely ends in a year-like number is retried whole.
_YEAR_AT_END = re.compile(r"^(?P<title>.*\S)\s*[\s(]\s*(?P<year>(?:19|20)\d{2})\)?\s*$")


def parse_query(text):
    """(title, year) with the year None unless the text ends in one."""
    found = _YEAR_AT_END.match(text or "")
    return (found.group("title").strip(), int(found.group("year"))) if found else (text, None)


def _poster(path):
    return f"{POSTER}{path}" if path else None


def _movie(r):
    return {"kind": "movie", "tmdb_id": str(r["id"]), "title": r.get("title") or r.get("original_title") or f"TMDB {r['id']}",
            "year": metadata._year(r.get("release_date")), "overview": r.get("overview") or "", "poster": _poster(r.get("poster_path")),
            "rating": round(r["vote_average"], 1) if r.get("vote_average") else None}


def _show(r):
    return {"kind": "show", "tmdb_id": str(r["id"]), "title": r.get("name") or r.get("original_name") or f"TMDB {r['id']}",
            "year": metadata._year(r.get("first_air_date")), "overview": r.get("overview") or "", "poster": _poster(r.get("poster_path")),
            "rating": round(r["vote_average"], 1) if r.get("vote_average") else None}


def find(tmdb, text, kind="all"):
    """Movies and/or shows matching the text, best matches first within each kind (movies, then shows)."""
    text = " ".join((text or "").split())
    if not text:
        return []
    if kind not in KINDS:
        kind = "all"

    def run(query, year):
        found = []
        if kind in ("all", "movie"):
            found += [_movie(r) for r in tmdb.search_movie(query, year)[:RESULTS_PER_KIND] if r.get("id")]
        if kind in ("all", "show"):
            found += [_show(r) for r in tmdb.search_tv(query, year)[:RESULTS_PER_KIND] if r.get("id")]
        return found

    title, year = parse_query(text)
    results = run(title, year)
    if year and not results:
        results = run(text, None)
    return results


def annotate(conn, results):
    """Adds what SyncHub already knows about each result: where it stands, its page, and whether it can be added from here."""
    ids = {}
    for r in results:
        row = conn.execute("SELECT id FROM media WHERE kind = ? AND tmdb_id = ?", (r["kind"], r["tmdb_id"])).fetchone()
        if row:
            ids[(r["kind"], r["tmdb_id"])] = row["id"]
    known = {c["id"]: c for c in catalog.list_catalog(conn, only=set(ids.values()))} if ids else {}
    for r in results:
        media_id = ids.get((r["kind"], r["tmdb_id"]))
        c = known.get(media_id)
        r["media_id"] = media_id
        r["url"] = c["url"] if c else None
        r["state"], r["state_label"], r["state_color"], r["state_icon"] = (c["state"], c["state_label"], c["state_color"], c["state_icon"]) if c else (None, None, None, None)
        r["progress_pct"] = c["progress_pct"] if c else None
        r["in_radarr"], r["in_sonarr"] = (c["in_radarr"], c["in_sonarr"]) if c else (False, False)
        # A movie already in Radarr cannot be added again; a show already in Sonarr can still have more seasons requested.
        r["addable"] = c is None or (c["state"] == "missing" and not (r["kind"] == "movie" and c["in_radarr"]))
    return results


def season_choices(tmdb, tmdb_id):
    """What the season picker shows for a show: its seasons (Specials last) and whether Sonarr will be able to add it."""
    details = tmdb.show(tmdb_id)
    if not details:
        raise RequestError("TMDB does not know that show.")
    seasons = []
    for s in details.get("seasons") or []:
        number = s.get("season_number")
        if number is None:
            continue
        seasons.append({"number": number, "name": "Specials (Season 0)" if number == 0 else (s.get("name") or f"Season {number}"),
                        "episodes": s.get("episode_count") or 0, "air_date": s.get("air_date")})
    seasons.sort(key=lambda s: (s["number"] == 0, s["number"]))
    return {"title": details.get("name"), "has_tvdb": bool((details.get("external_ids") or {}).get("tvdb_id")), "seasons": seasons}


# ---------------------------------------------------------------- adding
def _existing(conn, kind, tmdb_id):
    row = conn.execute("SELECT id FROM media WHERE kind = ? AND tmdb_id = ?", (kind, str(tmdb_id))).fetchone()
    return row["id"] if row else None


def _discard_if_unused(conn, media_id):
    """Removes a title created for a request that then failed, so a failed add leaves nothing behind (unless something else uses it)."""
    in_use = (conn.execute("SELECT 1 FROM link WHERE entity_type = 'media' AND entity_id = ?", (media_id,)).fetchone()
              or conn.execute("SELECT 1 FROM manual_request WHERE media_id = ?", (media_id,)).fetchone()
              or conn.execute("SELECT 1 FROM watchlist_item WHERE media_id = ?", (media_id,)).fetchone())
    if in_use:
        return False
    conn.execute("DELETE FROM item_state WHERE target_type = 'episode' AND target_id IN (SELECT id FROM episode WHERE media_id = ?)", (media_id,))
    conn.execute("DELETE FROM item_state WHERE target_type = 'movie' AND target_id = ?", (media_id,))
    conn.execute("DELETE FROM media WHERE id = ?", (media_id,))
    return True


def _remember(conn, media_id, profile_id):
    if profile_id:
        conn.execute("INSERT OR IGNORE INTO manual_request (media_id, profile_id) VALUES (?, ?)", (media_id, profile_id))


def add_movie(tmdb, radarr, tmdb_id, profile_id=None):
    """Sends a movie to Radarr and credits the profile. Network calls never happen while a database write is pending."""
    details = tmdb.movie(tmdb_id)
    if not details:
        raise RequestError("TMDB does not know that movie.")
    with db.connection() as conn:
        created = _existing(conn, "movie", tmdb_id) is None
        media_id = metadata.ensure_media(conn, "movie", tmdb_id, details.get("title") or details.get("original_title"), metadata._year(details.get("release_date")))
        metadata.apply_movie_details(conn, media_id, details)
    try:
        with db.connection() as conn:
            title = automation.request_movie(conn, radarr, media_id)
            _remember(conn, media_id, profile_id)
    except Exception:
        if created:
            with db.connection() as conn:
                _discard_if_unused(conn, media_id)
        raise
    return {"media_id": media_id, "title": title}


def add_show(tmdb, sonarr, tmdb_id, seasons, profile_id=None):
    """Reads the show (and every episode) from TMDB, sends the chosen seasons (TMDB numbering) to Sonarr and credits the profile."""
    details = tmdb.show(tmdb_id)
    if not details:
        raise RequestError("TMDB does not know that show.")
    with db.connection() as conn:
        created = _existing(conn, "show", tmdb_id) is None
        media_id = metadata.ensure_media(conn, "show", tmdb_id, details.get("name") or details.get("original_name"), metadata._year(details.get("first_air_date")))
    try:
        with db.connection() as conn:
            metadata.refresh_show(conn, tmdb, media_id)
        with db.connection() as conn:
            if not conn.execute("SELECT tvdb_id FROM media WHERE id = ?", (media_id,)).fetchone()["tvdb_id"]:
                raise RequestError("Sonarr needs a TVDB ID and TMDB has none for this show, so it cannot be added from here.")
            result = automation.request_seasons(conn, sonarr, media_id, seasons)
            _remember(conn, media_id, profile_id)
    except Exception:
        if created:
            with db.connection() as conn:
                _discard_if_unused(conn, media_id)
        raise
    return {"media_id": media_id, "title": details.get("name"), **result}
