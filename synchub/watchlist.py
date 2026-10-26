"""Each person's own watchlist: seeded from their Letterboxd and Serializd watchlists, and changed by them in SyncHub.

This list is separate from the demand that drives Radarr/Sonarr auto-add and the notifications (catalog.demand): adding to it or removing from it never
requests, downloads or deletes anything. A removed title is hidden, not deleted, so a later sync does not put it back.
A person can also choose to have watched titles taken off their list (profile.watchlist_drop_watched)."""
from datetime import date

from synchub import db, metadata, ratings
from synchub.clients import ApiError
from synchub.ingest.jellyfin_userdata import norm


class WatchlistError(Exception):
    """A change that cannot be made; the message is meant for the user."""


# ---------------------------------------------------------------- seeding from the source lists
def seed(conn, now=None):
    """Adds titles that are on an active person's Letterboxd/Serializd watchlist and not yet on theirs. Titles they removed stay removed.
    Returns how many were added."""
    now = now or db.now()
    added = 0
    for source, sql in (
        ("letterboxd", """SELECT p.id AS profile_id, l.entity_id AS media_id, MIN(e.first_seen) AS added_at FROM src_letterboxd_entry e
                          JOIN link l ON l.source = 'letterboxd' AND l.source_key = e.slug AND l.entity_type = 'media'
                          JOIN profile p ON p.letterboxd_username = e.username AND p.is_active = 1
                          WHERE e.list = 'watchlist' AND e.removed_at IS NULL GROUP BY p.id, l.entity_id"""),
        ("serializd", """SELECT p.id AS profile_id, l.entity_id AS media_id, MIN(e.first_seen) AS added_at FROM src_serializd_entry e
                         JOIN link l ON l.source = 'serializd' AND l.source_key = e.serializd_show_id AND l.entity_type = 'media'
                         JOIN profile p ON p.serializd_username = e.username AND p.is_active = 1
                         WHERE e.removed_at IS NULL GROUP BY p.id, l.entity_id"""),
    ):
        for r in conn.execute(sql).fetchall():
            added += conn.execute("INSERT OR IGNORE INTO watchlist_item (profile_id, media_id, source, added_at) VALUES (?, ?, ?, ?)",
                                  (r["profile_id"], r["media_id"], source, r["added_at"] or now)).rowcount
    return added


# ---------------------------------------------------------------- what counts as watched
def watched_among(conn, profile, media_ids, today=None):
    """The subset of media_ids this person has watched (a dict row of profile: id, jellyfin_user_id, letterboxd_username).

    A movie is watched when their Jellyfin user played it or it is in their Letterboxd 'watched' list. A show is watched when every episode that
    has aired (specials left out) is played in Jellyfin. Only Jellyfin's played flag counts, never its play count (see watch.describe)."""
    ids = list(media_ids)
    if not ids:
        return set()
    today = today or date.today().isoformat()
    user = norm(profile.get("jellyfin_user_id")) if profile.get("jellyfin_user_id") else None
    marks = ",".join("?" * len(ids))
    done = set()
    if user:
        done |= {r["id"] for r in conn.execute(
            f"""SELECT DISTINCT l.entity_id AS id FROM link l JOIN src_jellyfin_userdata u ON u.item_id = l.source_key AND u.jellyfin_user_id = ? AND u.removed_at IS NULL AND u.played = 1
                JOIN media m ON m.id = l.entity_id AND m.kind = 'movie'
                WHERE l.source = 'jellyfin' AND l.entity_type = 'media' AND l.entity_id IN ({marks})""", (user, *ids))}
        done |= {r["media_id"] for r in conn.execute(
            f"""SELECT e.media_id, COUNT(*) AS aired, COUNT(CASE WHEN u.played = 1 THEN 1 END) AS seen FROM episode e
                LEFT JOIN link l ON l.source = 'jellyfin_episode' AND l.entity_type = 'episode' AND l.entity_id = e.id
                LEFT JOIN src_jellyfin_userdata u ON u.item_id = l.source_key AND u.jellyfin_user_id = ? AND u.removed_at IS NULL
                WHERE e.season_number > 0 AND e.air_date IS NOT NULL AND e.air_date <= ? AND e.media_id IN ({marks}) GROUP BY e.media_id
                HAVING aired > 0 AND seen >= aired""", (user, today, *ids))}
    if profile.get("letterboxd_username"):
        done |= {r["id"] for r in conn.execute(
            f"""SELECT DISTINCT l.entity_id AS id FROM src_letterboxd_entry e JOIN link l ON l.source = 'letterboxd' AND l.source_key = e.slug AND l.entity_type = 'media'
                WHERE e.username = ? AND e.list = 'watched' AND e.removed_at IS NULL AND l.entity_id IN ({marks})""", (profile["letterboxd_username"].lower(), *ids))}
    return done & set(ids)


def drop_watched(conn, now=None, today=None):
    """For everyone who switched it on, hides the titles on their list that they have watched. Returns how many were hidden."""
    now = now or db.now()
    hidden = 0
    for person in conn.execute("SELECT id, jellyfin_user_id, letterboxd_username FROM profile WHERE watchlist_drop_watched = 1").fetchall():
        person = dict(person)
        ids = [r["media_id"] for r in conn.execute("SELECT media_id FROM watchlist_item WHERE profile_id = ? AND hidden_at IS NULL", (person["id"],))]
        for media_id in watched_among(conn, person, ids, today):
            hidden += conn.execute("UPDATE watchlist_item SET hidden_at = ?, hidden_reason = 'watched' WHERE profile_id = ? AND media_id = ? AND hidden_at IS NULL",
                                   (now, person["id"], media_id)).rowcount
    return hidden


def refresh(conn, now=None):
    """Brings every list up to date after a sync: new titles from the source lists, then the watched ones off the lists that want that."""
    return {"added": seed(conn, now), "dropped": drop_watched(conn, now)}


# ---------------------------------------------------------------- one person's list
def add(conn, profile_id, media_id, now=None):
    """Puts a title on the list (or back on it). Returns 'added', 'restored' or 'already'."""
    row = conn.execute("SELECT hidden_at FROM watchlist_item WHERE profile_id = ? AND media_id = ?", (profile_id, media_id)).fetchone()
    if row is None:
        conn.execute("INSERT INTO watchlist_item (profile_id, media_id, source, added_at) VALUES (?, ?, 'manual', ?)", (profile_id, media_id, now or db.now()))
        return "added"
    if row["hidden_at"] is None:
        return "already"
    conn.execute("UPDATE watchlist_item SET hidden_at = NULL, hidden_reason = NULL WHERE profile_id = ? AND media_id = ?", (profile_id, media_id))
    return "restored"


def remove(conn, profile_id, media_id, now=None):
    """Takes a title off the list; True if it was on it. It stays hidden through later syncs until it is added again."""
    return conn.execute("UPDATE watchlist_item SET hidden_at = ?, hidden_reason = 'removed' WHERE profile_id = ? AND media_id = ? AND hidden_at IS NULL",
                        (now or db.now(), profile_id, media_id)).rowcount == 1


def on_list(conn, profile_id, media_id):
    return conn.execute("SELECT 1 FROM watchlist_item WHERE profile_id = ? AND media_id = ? AND hidden_at IS NULL", (profile_id, media_id)).fetchone() is not None


def drop_watched_enabled(conn, profile_id):
    row = conn.execute("SELECT watchlist_drop_watched FROM profile WHERE id = ?", (profile_id,)).fetchone()
    return bool(row and row["watchlist_drop_watched"])


def set_drop_watched(conn, profile_id, enabled):
    conn.execute("UPDATE profile SET watchlist_drop_watched = ? WHERE id = ?", (1 if enabled else 0, profile_id))


def items(conn, profile_id, hidden=False, limit=None):
    """The person's list (or their removed titles), newest first, within their rating limit (None: no limit):
    [{'media_id', 'kind', 'title', 'year', 'source', 'added_at', 'hidden_reason', 'in_library'}]."""
    rows = conn.execute(
        f"""SELECT w.media_id, m.kind, m.title, m.year, w.source, w.added_at, w.hidden_reason,
                   EXISTS (SELECT 1 FROM link l JOIN src_jellyfin_item j ON j.item_id = l.source_key AND j.removed_at IS NULL
                           WHERE l.source = 'jellyfin' AND l.entity_type = 'media' AND l.entity_id = m.id) AS in_library
            FROM watchlist_item w JOIN media m ON m.id = w.media_id
            WHERE w.profile_id = ? AND w.hidden_at IS {'NOT NULL' if hidden else 'NULL'}
            ORDER BY {'w.hidden_at' if hidden else 'w.added_at'} DESC, m.title COLLATE NOCASE""", (profile_id,)).fetchall()
    out = [dict(r) for r in rows if limit is None or ratings.media_allowed(conn, r["media_id"], limit)]
    for r in out:
        r["in_library"] = bool(r["in_library"])
    return out


def add_from_tmdb(tmdb, kind, tmdb_id, profile_id):
    """Adds a title found on TMDB, creating it in SyncHub first when it is new. Nothing is sent to Radarr or Sonarr.
    Network calls never happen while a database write is pending. Returns {'media_id', 'title', 'result'}."""
    try:
        details = tmdb.movie(tmdb_id) if kind == "movie" else tmdb.show(tmdb_id)
    except ApiError as exc:
        raise WatchlistError(str(exc))
    if not details:
        raise WatchlistError(f"TMDB does not know that {'movie' if kind == 'movie' else 'show'}.")
    with db.connection() as conn:
        if kind == "movie":
            media_id = metadata.ensure_media(conn, "movie", tmdb_id, details.get("title") or details.get("original_title"), metadata._year(details.get("release_date")))
            metadata.apply_movie_details(conn, media_id, details)
        else:
            media_id = metadata.ensure_media(conn, "show", tmdb_id, details.get("name") or details.get("original_name"), metadata._year(details.get("first_air_date")))
    if kind == "show":
        with db.connection() as conn:
            metadata.refresh_show(conn, tmdb, media_id)         # every episode, so "watched" can be worked out later
    with db.connection() as conn:
        result = add(conn, profile_id, media_id)
        title = conn.execute("SELECT title FROM media WHERE id = ?", (media_id,)).fetchone()["title"]
    return {"media_id": media_id, "title": title, "result": result}
