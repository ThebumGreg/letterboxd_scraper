"""Serializd watchlists -> src_serializd_entry (same keep-forever rules as Letterboxd)."""
import logging

import requests

from config import DEFAULT_HEADERS, HTTP_TIMEOUT
from synchub import db
from synchub.ingest.common import mark_unseen, touch
from synchub.text import clean_id

logger = logging.getLogger(__name__)


def default_fetch(username):
    """Returns (status_code, parsed_json_or_None)."""
    response = requests.get(f"https://www.serializd.com/api/user/{username}/watchlist", headers=DEFAULT_HEADERS, timeout=HTTP_TIMEOUT)
    try:
        return response.status_code, (response.json() if response.status_code == 200 else None)
    except ValueError:
        return response.status_code, None


def store(conn, username, shows, now=None):
    now = now or db.now()
    username = username.lower()
    seen = []
    for show in shows:
        show_id = clean_id(show.get("id"))
        if not show_id:
            continue
        touch(conn, "src_serializd_entry", ("username", "serializd_show_id"), {
            "username": username, "serializd_show_id": show_id, "title": show.get("name"),
            "tmdb_id": clean_id(show.get("tmdbId")), "tvdb_id": clean_id(show.get("tvdbId")), "imdb_id": clean_id(show.get("imdbId")),
            "season_number": show.get("season_number") or show.get("season"),
        }, now, keep=("tmdb_id", "tvdb_id", "imdb_id"))
        seen.append(show_id)
    removed = mark_unseen(conn, "src_serializd_entry", ("serializd_show_id",), seen, now,
                          scope_sql="username = ?", scope_params=(username,))
    return {"seen": len(seen), "removed": removed}


def sync_all(fetch=default_fetch):
    with db.connection() as conn:
        users = [r["serializd_username"] for r in conn.execute(
            "SELECT serializd_username FROM profile WHERE is_active = 1 AND serializd_username IS NOT NULL ORDER BY id")]
    summary = {}
    for username in users:
        try:
            status, body = fetch(username)
        except Exception as exc:
            logger.error(f"Serializd request for {username} failed: {exc}")
            summary[username] = {"error": str(exc)}
            continue
        if status != 200 or body is None:
            logger.warning(f"Serializd returned HTTP {status} for {username}; nothing changed.")
            summary[username] = {"error": f"HTTP {status}"}
            continue
        with db.connection() as conn:
            summary[username] = store(conn, username, body.get("shows") or [])
    return summary
