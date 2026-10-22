"""Poster images cached on disk by media ID. TMDB's image CDN first, then Jellyfin."""
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


def ensure_poster(conn, media_id):
    """Returns the path of a cached poster, downloading it if needed, or None when none is available."""
    target = cached_path(media_id)
    if target.exists() and target.stat().st_size >= MIN_BYTES:
        return target
    marker = _missing_marker(media_id)
    if marker.exists() and time.time() - marker.stat().st_mtime < RETRY_AFTER_SECONDS:
        return None
    media = conn.execute("SELECT poster_path FROM media WHERE id = ?", (media_id,)).fetchone()
    if media is None:
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
