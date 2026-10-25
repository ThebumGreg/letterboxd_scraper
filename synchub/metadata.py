"""TMDB metadata -> media and episode rows. Every episode of a show is stored (aired or not, Season 0 included)."""
import logging
from datetime import datetime, timedelta

from synchub import db, ratings, settings
from synchub.text import clean_id

logger = logging.getLogger(__name__)
ENDED_STATUSES = {"Ended", "Canceled"}
ENDED_REFRESH_DAYS = 30


def _year(date_text):
    return int(date_text[:4]) if date_text and date_text[:4].isdigit() else None


def _set_ids(conn, media_id, kind, tvdb_id=None, imdb_id=None):
    """Fills missing TVDB/IMDb IDs without ever taking an ID that belongs to another media row."""
    row = conn.execute("SELECT tvdb_id, imdb_id FROM media WHERE id = ?", (media_id,)).fetchone()
    if tvdb_id and not row["tvdb_id"]:
        taken = conn.execute("SELECT 1 FROM media WHERE kind = ? AND tvdb_id = ? AND id <> ?", (kind, tvdb_id, media_id)).fetchone()
        if taken:
            logger.warning(f"TVDB {tvdb_id} already belongs to another {kind}; not assigned to media {media_id}.")
        else:
            conn.execute("UPDATE media SET tvdb_id = ? WHERE id = ?", (tvdb_id, media_id))
    if imdb_id and not row["imdb_id"]:
        conn.execute("UPDATE media SET imdb_id = ? WHERE id = ?", (imdb_id, media_id))


def ensure_media(conn, kind, tmdb_id, title, year=None, tvdb_id=None, imdb_id=None):
    """The media row for (kind, tmdb_id), created from the supplied data if it does not exist yet."""
    row = conn.execute("SELECT id FROM media WHERE kind = ? AND tmdb_id = ?", (kind, str(tmdb_id))).fetchone()
    if row:
        _set_ids(conn, row["id"], kind, clean_id(tvdb_id), clean_id(imdb_id))
        return row["id"]
    media_id = conn.execute("INSERT INTO media (kind, title, year, tmdb_id, imdb_id) VALUES (?, ?, ?, ?, ?)",
                            (kind, title or f"TMDB {tmdb_id}", year, str(tmdb_id), clean_id(imdb_id))).lastrowid
    _set_ids(conn, media_id, kind, clean_id(tvdb_id), None)
    return media_id


def _store_rating(conn, kind, details, appended, now):
    """Keeps the age rating and audience score that came with the details (only when TMDB included the ratings, so a bare response is not read as 'unrated')."""
    if details.get("id") and appended in details:
        ratings.store(conn, kind, details["id"], details, now)


def apply_movie_details(conn, media_id, details, now=None):
    conn.execute(
        "UPDATE media SET title = ?, original_title = ?, year = ?, overview = ?, status = ?, first_air_date = ?, poster_path = ?, metadata_refreshed_at = ? WHERE id = ?",
        (details.get("title") or details.get("original_title"), details.get("original_title"), _year(details.get("release_date")),
         details.get("overview"), details.get("status"), details.get("release_date") or None, details.get("poster_path"), now or db.now(), media_id))
    _set_ids(conn, media_id, "movie", None, clean_id(details.get("imdb_id")))
    _store_rating(conn, "movie", details, "release_dates", now)


def apply_show_details(conn, media_id, details, now=None):
    conn.execute(
        "UPDATE media SET title = ?, original_title = ?, year = ?, overview = ?, status = ?, first_air_date = ?, poster_path = ?, metadata_refreshed_at = ? WHERE id = ?",
        (details.get("name") or details.get("original_name"), details.get("original_name"), _year(details.get("first_air_date")),
         details.get("overview"), details.get("status"), details.get("first_air_date") or None, details.get("poster_path"), now or db.now(), media_id))
    external = details.get("external_ids") or {}
    _set_ids(conn, media_id, "show", clean_id(external.get("tvdb_id")), clean_id(external.get("imdb_id")))
    _store_rating(conn, "show", details, "content_ratings", now)


def upsert_episodes(conn, media_id, season_number, episodes, now):
    count = 0
    for ep in episodes:
        number = ep.get("episode_number")
        if number is None:
            continue
        conn.execute(
            """INSERT INTO episode (media_id, season_number, episode_number, title, overview, air_date, runtime, tmdb_episode_id, last_refreshed)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(media_id, season_number, episode_number) DO UPDATE SET
                 title = excluded.title, overview = excluded.overview, air_date = excluded.air_date,
                 runtime = excluded.runtime, tmdb_episode_id = excluded.tmdb_episode_id, last_refreshed = excluded.last_refreshed""",
            (media_id, season_number, number, ep.get("name"), ep.get("overview"), ep.get("air_date") or None, ep.get("runtime"),
             clean_id(ep.get("id")), now))
        count += 1
    return count


def refresh_movie(conn, tmdb, media_id, now=None):
    row = conn.execute("SELECT tmdb_id FROM media WHERE id = ? AND kind = 'movie'", (media_id,)).fetchone()
    details = tmdb.movie(row["tmdb_id"]) if row else None
    if not details:
        return False
    apply_movie_details(conn, media_id, details, now)
    return True


def refresh_show(conn, tmdb, media_id, now=None):
    """Show details plus every season (including Season 0 Specials) and episode. Never deletes episodes."""
    now = now or db.now()
    row = conn.execute("SELECT tmdb_id FROM media WHERE id = ? AND kind = 'show'", (media_id,)).fetchone()
    # All TMDB calls first, then all writes: the database is never locked while waiting on the network.
    details = tmdb.show(row["tmdb_id"]) if row else None
    if not details:
        return {"found": False, "episodes": 0}
    numbers = [s["season_number"] for s in details.get("seasons") or [] if s.get("season_number") is not None]
    fetched = tmdb.seasons(row["tmdb_id"], numbers)
    apply_show_details(conn, media_id, details, now)
    episodes = 0
    for number, season in fetched.items():
        episodes += upsert_episodes(conn, media_id, number, season.get("episodes") or [], now)
    return {"found": True, "episodes": episodes, "seasons": len(numbers)}


def needs_refresh(row, now=None):
    """row: media row. Never-refreshed -> yes; ended shows rarely; everything else after metadata_refresh_hours."""
    if not row["metadata_refreshed_at"]:
        return True
    now = datetime.strptime(now or db.now(), "%Y-%m-%d %H:%M:%S")
    refreshed = datetime.strptime(row["metadata_refreshed_at"], "%Y-%m-%d %H:%M:%S")
    if row["kind"] == "show" and row["status"] in ENDED_STATUSES:
        return now - refreshed >= timedelta(days=ENDED_REFRESH_DAYS)
    try:
        hours = int(settings.get("metadata_refresh_hours") or 24)
    except ValueError:
        hours = 24
    return now - refreshed >= timedelta(hours=hours)


def fill_missing_poster_paths(conn, tmdb, progress=None):
    """Asks TMDB for just the poster of every title that has none (one call each; episodes and other details are not touched)."""
    progress = progress or (lambda message, force=False: None)
    rows = conn.execute("SELECT id, kind, tmdb_id, title FROM media WHERE tmdb_id IS NOT NULL AND poster_path IS NULL ORDER BY title COLLATE NOCASE").fetchall()
    stats = {"checked": 0, "found": 0, "none": 0, "errors": 0}
    for number, row in enumerate(rows, 1):
        progress(f"TMDB poster: {row['title']} ({number} of {len(rows)})")
        try:
            details = tmdb.movie(row["tmdb_id"]) if row["kind"] == "movie" else tmdb.show(row["tmdb_id"])
            path = (details or {}).get("poster_path")
            if path:
                conn.execute("UPDATE media SET poster_path = ? WHERE id = ? AND poster_path IS NULL", (path, row["id"]))
                conn.commit()
                stats["found"] += 1
            else:
                stats["none"] += 1
        except Exception as exc:
            conn.rollback()
            logger.warning(f"Could not read the poster of media {row['id']} ({row['title']}) from TMDB: {exc}")
            stats["errors"] += 1
        stats["checked"] += 1
    return stats


def refresh_pending(conn, tmdb, limit=200, now=None, progress=None):
    """Refreshes media that has never been refreshed or is stale, oldest first. Errors on one item do not stop the run."""
    now = now or db.now()
    progress = progress or (lambda message, force=False: None)
    rows = conn.execute("SELECT * FROM media WHERE tmdb_id IS NOT NULL ORDER BY COALESCE(metadata_refreshed_at, '') ASC, id ASC").fetchall()
    due = [r for r in rows if needs_refresh(r, now)][:limit]
    progress(f"{len(due)} title(s) to refresh from TMDB", force=True)
    stats = {"movies": 0, "shows": 0, "episodes": 0, "errors": 0}
    for number, row in enumerate(due, 1):
        progress(f"TMDB: {row['title']} ({number} of {len(due)})")
        try:
            if row["kind"] == "movie":
                stats["movies"] += int(refresh_movie(conn, tmdb, row["id"], now))
            else:
                result = refresh_show(conn, tmdb, row["id"], now)
                stats["shows"] += int(result["found"])
                stats["episodes"] += result["episodes"]
            conn.commit()
        except Exception as exc:
            conn.rollback()      # never leave a half-finished transaction open
            logger.warning(f"Metadata refresh failed for media {row['id']}: {exc}")
            stats["errors"] += 1
    return stats
