"""Request a better copy of a low quality film from Radarr.

Two ways, both starting from a Library file:
  * `request_auto`: block the release that was grabbed for the file (Radarr's "mark as failed" puts it on the blocklist) and ask Radarr to search again. Radarr then
    applies its own quality profile. If the profile says the file already meets its cutoff, Radarr may grab nothing; the message says so.
  * `preview` then `grab`: list what the indexers have (read only, nothing is changed), and grab one release a person chose. This ignores the cutoff.
A new download is checked by the normal download workflow when Radarr imports it. The old file is never deleted here; Radarr replaces it on import when it treats the new one as
an upgrade.

Radarr's address and key come from Settings. The endpoints used are Radarr v3's (history, history/failed, command MoviesSearch, release); they are the documented ones but have not
been run against a live Radarr from here, so every step reports what happened instead of assuming.
"""
import logging
import posixpath

from synchub import db, quality
from synchub.clients import ApiError, ConfigError
from synchub.library import vocab as v

logger = logging.getLogger(__name__)

MAX_CANDIDATES = 8
RELEASE_FIELDS = ("guid", "indexerId", "title", "size", "seeders", "indexer", "protocol", "ageHours")


class BetterCopyError(ValueError):
    """Something that stops the request; the message is meant for the page."""


def _client(radarr=None):
    if radarr is not None:
        return radarr
    from synchub.clients.arr import RadarrClient
    return RadarrClient()


def _inside(path, folder):
    path, folder = (path or "").rstrip("/").casefold(), (folder or "").rstrip("/").casefold()
    return bool(folder) and (path == folder or path.startswith(folder + "/"))


def target(conn, file_id):
    """(record, the file's path as Radarr sees it). Raises BetterCopyError when the file or the share's Radarr path is not known."""
    r = conn.execute("SELECT * FROM library_file WHERE id = ?", (file_id,)).fetchone()
    if r is None:
        raise BetterCopyError("That file is not in the Library.")
    rec = dict(r)
    root = conn.execute("SELECT radarr_path FROM storage_root WHERE name = ?", (rec["root_name"],)).fetchone()
    base = ((root["radarr_path"] if root else "") or "").rstrip("/")
    if not base:
        raise BetterCopyError(f"Set the Radarr path of the share '{rec['root_name']}' on the Storage page, so SyncHub can tell which Radarr movie this file belongs to.")
    if rec.get("media_type") == "TV Episode":
        raise BetterCopyError("Requests for TV episodes are not built yet. Movies only.")
    return rec, posixpath.join(base, v.clean_relative(rec["relative_path"]))


def find_movie(client, path):
    movie = next((m for m in client.movies() if _inside(path, m.get("path"))), None)
    if movie is None:
        raise BetterCopyError("Radarr has no movie for this file, so there is nothing to ask it about.")
    return movie


def bad_release(client, movie_id):
    """(history id, release title) of the newest grab for the movie, or (None, None) when Radarr's history no longer has one."""
    events = [e for e in client.movie_history(movie_id) or [] if isinstance(e, dict) and str(e.get("eventType", "")).lower() == "grabbed"]
    if not events:
        return None, None
    events.sort(key=lambda e: str(e.get("date") or ""), reverse=True)
    return events[0].get("id"), events[0].get("sourceTitle")


def _resolution(release):
    try:
        return int(((release.get("quality") or {}).get("quality") or {}).get("resolution") or 0)
    except (TypeError, ValueError):
        return 0


def _blocklisted(release):
    return any("blocklist" in str(x).lower() for x in release.get("rejections") or [])


def candidates(releases, bad_title, wanted_height):
    """The releases worth offering: allowed to download, not on the blocklist, not the release being replaced, and at least the wanted picture height. Best first."""
    keep = []
    for r in releases or []:
        if not isinstance(r, dict) or r.get("downloadAllowed") is False or _blocklisted(r) or (bad_title and r.get("title") == bad_title) or not r.get("guid"):
            continue
        height = _resolution(r)
        if wanted_height and height and height < wanted_height:
            continue
        item = {k: r.get(k) for k in RELEASE_FIELDS}
        item.update(resolution=height or None, quality=((r.get("quality") or {}).get("quality") or {}).get("name"), notes=[str(x) for x in (r.get("rejections") or [])][:3])
        keep.append(item)
    keep.sort(key=lambda c: (c["resolution"] or 0, c["seeders"] or 0, c["size"] or 0), reverse=True)
    return keep[:MAX_CANDIDATES]


def wanted_height(conn, rec):
    """The smallest picture height a replacement should have: the minimum setting, or the file's own height when that is higher."""
    _, height, _, _, _ = quality._facts(rec)
    return max(quality.min_height(conn), int(height or 0))


def _record(file_id, rec, movie, mode, bad, history_id, blocklisted, status, message, by, chosen=None):
    with db.connection() as conn:
        return conn.execute(
            "INSERT INTO quality_request (library_file_id, service_item_id, title, mode, bad_release, history_id, blocklisted, chosen_release, status, message, requested_by) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (file_id, str(movie.get("id")), movie.get("title"), mode, bad, None if history_id is None else str(history_id), 1 if blocklisted else 0, chosen, status, message[:500], by)).lastrowid


def _already_blocked(conn, file_id, history_id):
    return history_id is not None and conn.execute("SELECT 1 FROM quality_request WHERE library_file_id = ? AND history_id = ? AND blocklisted = 1", (file_id, str(history_id))).fetchone() is not None


def _block(client, file_id, history_id):
    """True when the release is blocklisted now (or already was). Never raises: a failed block is reported, and the request carries on."""
    if history_id is None:
        return False, "Radarr's history has no grab for this file, so no release could be blocked."
    with db.connection() as conn:
        if _already_blocked(conn, file_id, history_id):
            return True, ""
    try:
        client.mark_history_failed(history_id)
        return True, ""
    except (ApiError, ConfigError) as exc:
        return False, f"The release could not be blocked ({exc})."


def preview(file_id, radarr=None):
    """Read only: the movie, the release that would be blocked and the releases to choose from. Nothing in Radarr changes."""
    with db.connection() as conn:
        rec, path = target(conn, file_id)
        wanted = wanted_height(conn, rec)
    try:
        client = _client(radarr)
        movie = find_movie(client, path)
        history_id, bad = bad_release(client, movie["id"])
        found = candidates(client.releases(movie["id"]), bad, wanted)
    except (ApiError, ConfigError) as exc:
        raise BetterCopyError(str(exc))
    note = ""
    if not found:
        note = f"The indexers have no other release of at least {wanted}p right now." if wanted else "The indexers have no other release right now."
    return {"movie": movie.get("title"), "bad_release": bad, "can_block": history_id is not None, "wanted_height": wanted, "candidates": found, "message": note}


def request_auto(file_id, by="someone", radarr=None):
    """Blocks the release that was grabbed for the file and asks Radarr to search again. Returns {'id', 'status', 'message'}."""
    with db.connection() as conn:
        rec, path = target(conn, file_id)
    try:
        client = _client(radarr)
        movie = find_movie(client, path)
        history_id, bad = bad_release(client, movie["id"])
        blocked, problem = _block(client, file_id, history_id)
        client.search_movie(movie["id"])
    except (ApiError, ConfigError, BetterCopyError) as exc:
        message = str(exc)
        _log_failure(file_id, by, "auto", message)
        raise BetterCopyError(message)
    parts = [f"Blocked “{bad}”." if blocked and bad else problem, "Radarr is searching for another release. If its quality profile says the file already meets its cutoff it may grab nothing; "
             "in that case use Show releases and pick one."]
    message = " ".join(p for p in parts if p)
    request_id = _record(file_id, rec, movie, "auto", bad, history_id, blocked, "requested", message, by)
    logger.info(f"[quality] better copy requested for {rec['file_name']} by {by}: {message}")
    return {"id": request_id, "status": "requested", "message": message}


def grab(file_id, guid, indexer_id, by="someone", radarr=None):
    """Grabs one release a person chose from `preview`, after blocking the one it replaces. The release is looked up again, so only a release Radarr is offering now can be grabbed."""
    with db.connection() as conn:
        rec, path = target(conn, file_id)
        wanted = wanted_height(conn, rec)
    try:
        client = _client(radarr)
        movie = find_movie(client, path)
        history_id, bad = bad_release(client, movie["id"])
        chosen = next((c for c in candidates(client.releases(movie["id"]), bad, wanted) if c["guid"] == guid and str(c["indexerId"]) == str(indexer_id)), None)
        if chosen is None:
            raise BetterCopyError("That release is not on offer any more. Press Show releases again.")
        blocked, problem = _block(client, file_id, history_id)
        client.grab_release(chosen["guid"], chosen["indexerId"])
    except (ApiError, ConfigError, BetterCopyError) as exc:
        message = str(exc)
        _log_failure(file_id, by, "pick", message)
        raise BetterCopyError(message)
    message = " ".join(p for p in (f"Blocked “{bad}”." if blocked and bad else problem, f"Sent “{chosen['title']}” to the download client.") if p)
    request_id = _record(file_id, rec, movie, "pick", bad, history_id, blocked, "grabbed", message, by, chosen=chosen["title"])
    logger.info(f"[quality] better copy grabbed for {rec['file_name']} by {by}: {message}")
    return {"id": request_id, "status": "grabbed", "message": message}


def _log_failure(file_id, by, mode, message):
    try:
        with db.connection() as conn:
            conn.execute("INSERT INTO quality_request (library_file_id, mode, status, message, requested_by) VALUES (?, ?, 'failed', ?, ?)", (file_id, mode, message[:500], by))
    except Exception as exc:                         # the log is a nicety; the person still gets the real message
        logger.debug(f"[quality] could not log the failed request: {exc}")


def history_for(conn, file_id, limit=5):
    return [dict(r) for r in conn.execute("SELECT * FROM quality_request WHERE library_file_id = ? ORDER BY id DESC LIMIT ?", (file_id, int(limit)))]
