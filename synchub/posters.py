"""Poster images cached on disk by media ID. TMDB's image CDN first, then Jellyfin."""
import calendar
import logging
import time
from pathlib import Path

import requests

from config import HTTP_TIMEOUT, POSTER_DIR
from synchub import db, settings

logger = logging.getLogger(__name__)
TMDB_IMAGE = "https://image.tmdb.org/t/p/w342"
MIN_BYTES = 1500
RETRY_AFTER_SECONDS = 24 * 3600     # a failed lookup is retried after a day, not never
POSTER_DIR_OVERRIDE = None


def _dir():
    return Path(POSTER_DIR_OVERRIDE or POSTER_DIR)


def cached_path(media_id):
    return _dir() / f"v2-{int(media_id)}.jpg"


def _missing_marker(media_id):
    return _dir() / f"v2-{int(media_id)}.missing"


def _has_cached(media_id):
    target = cached_path(media_id)
    return target.exists() and target.stat().st_size >= MIN_BYTES


def _failure_still_counts(marker, refreshed_at):
    """A failed lookup is not retried for a day, unless the title was re-read from TMDB after it failed (it may have a poster now)."""
    if not marker.exists():
        return False
    failed_at = marker.stat().st_mtime
    if time.time() - failed_at >= RETRY_AFTER_SECONDS:
        return False
    if refreshed_at:
        try:
            return calendar.timegm(time.strptime(refreshed_at, "%Y-%m-%d %H:%M:%S")) <= failed_at
        except ValueError:
            pass
    return True


def clear_failures():
    """Forgets every remembered failure so the next request for those posters tries again. Returns how many there were."""
    markers = list(_dir().glob("v2-*.missing"))
    for marker in markers:
        marker.unlink(missing_ok=True)
    return len(markers)


def ensure_poster(conn, media_id):
    """Returns the path of a cached poster, downloading it if needed, or None when none is available."""
    target = cached_path(media_id)
    if _has_cached(media_id):
        return target
    marker = _missing_marker(media_id)
    media = conn.execute("SELECT poster_path, metadata_refreshed_at FROM media WHERE id = ?", (media_id,)).fetchone()
    if media is None or _failure_still_counts(marker, media["metadata_refreshed_at"]):
        return None
    _dir().mkdir(parents=True, exist_ok=True)
    db.assert_not_in_write_transaction(f"poster download for media {media_id}")

    def save(content):
        if len(content) >= MIN_BYTES:
            target.write_bytes(content)
            marker.unlink(missing_ok=True)
            return True
        return False

    if media["poster_path"]:
        try:
            res = requests.get(f"{TMDB_IMAGE}{media['poster_path']}", timeout=HTTP_TIMEOUT)
            if res.status_code == 200 and save(res.content):
                return target
        except requests.RequestException as exc:
            logger.info(f"TMDB poster download failed for media {media_id}: {exc}")

    jf_url, jf_key = settings.get("jellyfin_url").rstrip("/"), settings.get("jellyfin_api_key")
    if jf_url and jf_key and settings.get("disable_jellyfin_connection") != "1":
        item = conn.execute("""SELECT l.source_key FROM link l JOIN src_jellyfin_item j ON j.item_id = l.source_key
                               WHERE l.source = 'jellyfin' AND l.entity_type = 'media' AND l.entity_id = ? AND j.removed_at IS NULL LIMIT 1""", (media_id,)).fetchone()
        if item:
            try:
                res = requests.get(f"{jf_url}/Items/{item['source_key']}/Images/Primary", headers={"X-Emby-Token": jf_key}, timeout=HTTP_TIMEOUT)
                if res.status_code == 200 and save(res.content):
                    return target
            except requests.RequestException as exc:
                logger.info(f"Jellyfin poster download failed for media {media_id}: {exc}")

    marker.touch()
    return None


def refresh_all(conn, progress=None, limit_titles=25):
    """Tries to cache the poster of every title that has none yet. Returns counts and the first few titles with no poster anywhere."""
    progress = progress or (lambda message, force=False: None)
    rows = conn.execute("SELECT id, title, year FROM media ORDER BY title COLLATE NOCASE").fetchall()
    stats = {"cached": 0, "downloaded": 0, "none": 0, "none_titles": []}
    for number, row in enumerate(rows, 1):
        if _has_cached(row["id"]):
            stats["cached"] += 1
            continue
        progress(f"Poster: {row['title']} ({number} of {len(rows)})")
        if ensure_poster(conn, row["id"]):
            stats["downloaded"] += 1
        else:
            stats["none"] += 1
            if len(stats["none_titles"]) < limit_titles:
                stats["none_titles"].append(f"{row['title']} ({row['year']})" if row["year"] else row["title"])
    return stats
