"""Progress notices for a title a person asked for (the Request button on Find & request): the download started, or it failed and needs a person.

Radarr and Sonarr say so by webhook; the title is found by its TMDB id (movies) or TVDB id (shows). Only the people who asked for it are told, and a
person who muted it or switched the notice off is skipped by `emit.notify` as for any other notice. Best effort: nothing here may break the webhook."""
import logging

from synchub import db, ratings
from synchub.notifications import emit

logger = logging.getLogger(__name__)
STARTED_EVENTS = {"Grab"}
FAILED_EVENTS = {"ManualInteractionRequired", "DownloadFailure", "ImportFailure"}


def _media(conn, payload):
    movie, series = payload.get("movie") or {}, payload.get("series") or {}
    row = None
    if movie.get("tmdbId"):
        row = conn.execute("SELECT id, kind, title, year FROM media WHERE kind = 'movie' AND tmdb_id = ?", (str(movie["tmdbId"]),)).fetchone()
    elif series.get("tvdbId"):
        row = conn.execute("SELECT id, kind, title, year FROM media WHERE kind = 'show' AND tvdb_id = ?", (str(series["tvdbId"]),)).fetchone()
    if row is None and series.get("tmdbId"):
        row = conn.execute("SELECT id, kind, title, year FROM media WHERE kind = 'show' AND tmdb_id = ?", (str(series["tmdbId"]),)).fetchone()
    return row


def from_webhook(conn, event, payload, now=None):
    """Queues the notices a Radarr/Sonarr webhook event calls for. Returns how many were queued."""
    kind = "request_started" if event in STARTED_EVENTS else "request_failed" if event in FAILED_EVENTS else None
    if kind is None:
        return 0
    media = _media(conn, payload)
    if media is None:
        return 0
    asked = conn.execute("SELECT p.id, p.max_rating FROM manual_request m JOIN profile p ON p.id = m.profile_id AND p.is_active = 1 WHERE m.media_id = ?", (media["id"],)).fetchall()
    release = (payload.get("downloadId") or (payload.get("release") or {}).get("releaseTitle") or (now or db.now())[:13])
    queued = 0
    for person in asked:
        if not ratings.media_allowed(conn, media["id"], ratings.parse_level(person["max_rating"])):
            continue
        message = str(payload.get("message") or "")[:200]
        queued += len(emit.notify(conn, kind, f"{kind}:{media['id']}:{release}:profile:{person['id']}",
                                  {"media_id": media["id"], "kind": media["kind"], "title": media["title"], "year": media["year"], "reason": message},
                                  profile_ids=[person["id"]], now=now))
    return queued
