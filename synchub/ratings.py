"""Age ratings: reading TMDB's US certification, putting every label on one scale, and the parental-control rule that uses it.

The scale: 0 = G, TV-Y, TV-Y7, TV-G; 1 = PG, TV-PG; 2 = PG-13, TV-14; 3 = R, TV-MA; 4 = NC-17. A person's limit is one of 0-3, or None for no limit.
A title with no US rating, or one SyncHub has not looked up yet, is unrated, and unrated titles are hidden from anyone who has a limit.
"""
import logging
import re

from synchub import db, settings

logger = logging.getLogger(__name__)

# the choices on the People and Settings pages: (limit, short name, what it covers)
LEVELS = (
    (0, "G", "G, TV-Y, TV-G"),
    (1, "PG", "PG, TV-PG"),
    (2, "PG-13", "PG-13, TV-14"),
    (3, "R", "R, TV-MA"),
)
LEVEL_NAMES = {level: name for level, name, _ in LEVELS}
NO_LIMIT = "No limit (everything, unrated titles too)"
GUEST_DEFAULT = 1                    # PG
RANKS = {"G": 0, "TV-Y": 0, "TV-Y7": 0, "TV-Y7-FV": 0, "TV-G": 0, "PG": 1, "TV-PG": 1, "PG-13": 2, "TV-14": 2, "R": 3, "TV-MA": 3, "NC-17": 4}
RECHECK_DAYS = 30                    # a title TMDB had no rating for is asked about again after this long


def normalize(text):
    """'Rated PG-13', 'us-tv14', 'TV-MA' -> 'PG-13', 'TV-14', 'TV-MA'; anything that is not a known US label -> None."""
    label = re.sub(r"^(RATED|US)[\s:_-]*", "", str(text or "").strip().upper())
    label = re.sub(r"^TV[\s_-]*(Y7-FV|Y7|Y|G|PG|14|MA)$", r"TV-\1", label)
    label = re.sub(r"\s+", "", label)
    return label if label in RANKS else None


def rank(text):
    label = normalize(text)
    return RANKS[label] if label else None


def level_name(level):
    return LEVEL_NAMES.get(level, "No limit")


def parse_level(value):
    """A stored or submitted limit -> 0-3 or None (no limit). '', 'none' and anything unknown mean no limit."""
    try:
        level = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return level if level in LEVEL_NAMES else None


def permitted(title_rank, limit):
    """Whether a title with this rank (None = unrated) may be shown to someone with this limit (None = no limit)."""
    return limit is None or (title_rank is not None and title_rank <= limit)


def guest_limit(conn=None):
    value = settings.get("guest_max_rating", conn=conn)
    return GUEST_DEFAULT if value is None else parse_level(value)


def limit_for(conn, profile_row, has_profiles):
    """The limit that applies to whoever is using the app. Before any profile exists nobody is limited (setup has to be possible)."""
    if profile_row is not None:
        return parse_level(profile_row["max_rating"])
    return guest_limit(conn) if has_profiles else None


# ---------------------------------------------------------------- reading TMDB
def _first_certification(entries):
    """From a movie's US release list: the theatrical rating if there is one, else whichever other release carries one."""
    entries = [e for e in entries or [] if str(e.get("certification") or "").strip()]
    entries.sort(key=lambda e: {3: 0, 4: 1, 2: 2, 1: 3, 5: 4, 6: 5}.get(e.get("type"), 9))
    return entries[0]["certification"].strip() if entries else None


def from_movie(details):
    for country in (details.get("release_dates") or {}).get("results") or []:
        if country.get("iso_3166_1") == "US":
            return _first_certification(country.get("release_dates"))
    return None


def from_show(details):
    for country in (details.get("content_ratings") or {}).get("results") or []:
        if country.get("iso_3166_1") == "US" and str(country.get("rating") or "").strip():
            return country["rating"].strip()
    return None


def from_details(kind, details):
    """(certification text or None, vote_average or None) from a TMDB movie/show response that carried release_dates / content_ratings."""
    certification = from_movie(details) if kind == "movie" else from_show(details)
    vote = details.get("vote_average")
    return certification, (round(float(vote), 1) if vote else None)


# ---------------------------------------------------------------- the table
def store(conn, kind, tmdb_id, details, now=None):
    certification, vote = from_details(kind, details)
    label = normalize(certification)
    conn.execute(
        """INSERT INTO title_rating (kind, tmdb_id, certification, rating_rank, vote_average, checked_at) VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(kind, tmdb_id) DO UPDATE SET certification = excluded.certification, rating_rank = excluded.rating_rank,
                                                     vote_average = excluded.vote_average, checked_at = excluded.checked_at""",
        (kind, str(tmdb_id), label or (certification or None), RANKS.get(label), vote, now or db.now()))


def get(conn, kind, tmdb_id):
    """{'certification', 'rank', 'vote_average'} for a title, or None when SyncHub has not looked it up."""
    row = conn.execute("SELECT certification, rating_rank, vote_average FROM title_rating WHERE kind = ? AND tmdb_id = ?", (kind, str(tmdb_id))).fetchone()
    return {"certification": row["certification"], "rank": row["rating_rank"], "vote_average": row["vote_average"]} if row else None


def ranks(conn):
    """{(kind, tmdb_id): rank or None} for every title that has been looked up."""
    return {(r["kind"], r["tmdb_id"]): r["rating_rank"] for r in conn.execute("SELECT kind, tmdb_id, rating_rank FROM title_rating")}


def media_rank(conn, media_id):
    """The rank of one of our own titles (None = unrated or not looked up yet)."""
    row = conn.execute("SELECT r.rating_rank FROM media m JOIN title_rating r ON r.kind = m.kind AND r.tmdb_id = m.tmdb_id WHERE m.id = ?", (media_id,)).fetchone()
    return row["rating_rank"] if row else None


def media_allowed(conn, media_id, limit):
    return limit is None or permitted(media_rank(conn, media_id), limit)


def media_ids_allowed(conn, limit):
    """The IDs of the titles someone with this limit may see, or None when they may see all of them."""
    if limit is None:
        return None
    rows = conn.execute("SELECT m.id FROM media m JOIN title_rating r ON r.kind = m.kind AND r.tmdb_id = m.tmdb_id "
                        "WHERE r.rating_rank IS NOT NULL AND r.rating_rank <= ?", (limit,)).fetchall()
    return {r["id"] for r in rows}


def of_jellyfin_item(item, series=None):
    """The rank of a Jellyfin item: the strictest of Jellyfin's own OfficialRating and TMDB's rating (SyncHub's table, by the item's TMDB ID).
    For an episode pass its series too. `item` may be a Jellyfin item or a summary of one. None = unrated, which a limited person may not see."""
    found = []
    for one in (item, series):
        if not one:
            continue
        found.append(rank(one.get("OfficialRating")))
        tmdb = (one.get("ProviderIds") or {}).get("Tmdb") or one.get("Tmdb")
        if tmdb and one.get("Type") in ("Movie", "Series"):
            with db.connection() as conn:
                info = get(conn, "show" if one["Type"] == "Series" else "movie", tmdb)
            found.append(info["rank"] if info else None)
    known = [r for r in found if r is not None]
    return max(known) if known else None


# ---------------------------------------------------------------- looking titles up
def lookup(tmdb, kind, tmdb_id):
    """Asks TMDB about one title (no database access). Returns the response, or None when TMDB does not know it."""
    return tmdb.movie(tmdb_id) if kind == "movie" else tmdb.show(tmdb_id)


def fill_missing(conn, tmdb, limit=300, progress=None, now=None):
    """Looks up the rating of titles that have none yet, and again those TMDB had no rating for (rarely). One TMDB call each, committed one at a time."""
    progress = progress or (lambda message, force=False: None)
    rows = conn.execute(
        """SELECT m.id, m.kind, m.tmdb_id, m.title FROM media m LEFT JOIN title_rating r ON r.kind = m.kind AND r.tmdb_id = m.tmdb_id
           WHERE m.tmdb_id IS NOT NULL AND (r.tmdb_id IS NULL OR (r.rating_rank IS NULL AND r.checked_at < datetime('now', ?)))
           ORDER BY r.tmdb_id IS NOT NULL, m.title COLLATE NOCASE LIMIT ?""", (f"-{RECHECK_DAYS} days", limit)).fetchall()
    stats = {"checked": 0, "rated": 0, "unrated": 0, "errors": 0}
    for number, row in enumerate(rows, 1):
        progress(f"Rating: {row['title']} ({number} of {len(rows)})")
        try:
            details = lookup(tmdb, row["kind"], row["tmdb_id"])
            if details:
                store(conn, row["kind"], row["tmdb_id"], details, now)
                conn.commit()
                stats["rated" if normalize(from_details(row["kind"], details)[0]) else "unrated"] += 1
        except Exception as exc:
            conn.rollback()
            logger.warning(f"Could not read the rating of media {row['id']} ({row['title']}) from TMDB: {exc}")
            stats["errors"] += 1
        stats["checked"] += 1
    return stats


def ensure(tmdb, kind, tmdb_id):
    """The stored rating of a title by TMDB ID, asking TMDB (and remembering the answer) when SyncHub has not seen it. None when TMDB does not know it."""
    with db.connection() as conn:
        known = get(conn, kind, tmdb_id)
    if known is None:
        details = lookup(tmdb, kind, tmdb_id)
        if not details:
            return None
        with db.connection() as conn:
            store(conn, kind, tmdb_id, details)
            known = get(conn, kind, tmdb_id)
    return known


def for_results(tmdb, results, limit):
    """Adds `certification` to each search result (looking up and remembering the ones SyncHub has not seen) and drops what `limit` hides.
    Network calls happen with no database write open."""
    with db.connection() as conn:
        known = {(r["kind"], r["tmdb_id"]): r for r in results if get(conn, r["kind"], r["tmdb_id"]) is not None}
    fetched = {}
    for r in results if limit is not None else []:         # someone with no limit gets the ratings SyncHub already has, without a TMDB call per result
        key = (r["kind"], r["tmdb_id"])
        if key not in known and key not in fetched:
            try:
                fetched[key] = lookup(tmdb, r["kind"], r["tmdb_id"])
            except Exception as exc:
                logger.warning(f"Could not read the rating of {r['kind']} {r['tmdb_id']} from TMDB: {exc}")
    with db.connection() as conn:
        for (kind, tmdb_id), details in fetched.items():
            if details:
                store(conn, kind, tmdb_id, details)
        shown = []
        for r in results:
            info = get(conn, r["kind"], r["tmdb_id"]) or {"certification": None, "rank": None}
            r["certification"] = info["certification"]
            if permitted(info["rank"], limit):
                shown.append(r)
    return shown
