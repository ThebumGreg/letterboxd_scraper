"""Finds the TMDB ID of Letterboxd films from their film pages (Letterboxd publishes it on every page)."""
import logging
import re
import time
from datetime import datetime, timedelta
from urllib.parse import quote

from bs4 import BeautifulSoup

from synchub import db
from synchub.ingest.letterboxd import BLOCKED, default_fetch
from synchub.text import clean_id

logger = logging.getLogger(__name__)
MAX_PER_RUN = 150
RETRY_DAYS = 7
MAX_CONSECUTIVE_ERRORS = 5
_TMDB_LINK = re.compile(r"themoviedb\.org/(movie|tv)/(\d+)")
_IMDB_LINK = re.compile(r"imdb\.com/title/(tt\d+)")


def parse_film_page(html):
    soup = BeautifulSoup(html, "html.parser")
    tmdb_id = tmdb_type = imdb_id = None
    if soup.body is not None:
        tmdb_id, tmdb_type = soup.body.get("data-tmdb-id"), soup.body.get("data-tmdb-type")
    for link in soup.find_all("a", href=True):
        if not tmdb_id and (m := _TMDB_LINK.search(link["href"])):
            tmdb_type, tmdb_id = m.group(1), m.group(2)
        if not imdb_id and (m := _IMDB_LINK.search(link["href"])):
            imdb_id = m.group(1)
    if tmdb_id is not None and not str(tmdb_id).isdigit():
        tmdb_id = None
    return {"tmdb_id": clean_id(tmdb_id), "tmdb_type": tmdb_type, "imdb_id": imdb_id}


def slugs_needing_ids(conn, limit, retry_days=RETRY_DAYS, now=None):
    cutoff = (datetime.strptime(now or db.now(), "%Y-%m-%d %H:%M:%S") - timedelta(days=retry_days)).strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute(
        """SELECT slug FROM src_letterboxd_entry e GROUP BY slug
           HAVING MAX(tmdb_id) IS NULL AND (MAX(id_checked_at) IS NULL OR MAX(id_checked_at) < ?)
              AND NOT EXISTS (SELECT 1 FROM link WHERE source = 'letterboxd' AND source_key = e.slug)
           ORDER BY MAX(id_checked_at) IS NOT NULL, MAX(id_checked_at) LIMIT ?""", (cutoff, limit))
    return [r["slug"] for r in rows]


def resolve(conn, fetch=default_fetch, limit=MAX_PER_RUN, pause=lambda: None, now=None):
    now = now or db.now()
    stats = {"checked": 0, "found": 0, "no_id": 0, "errors": 0, "blocked": False}
    consecutive_errors = 0
    for n, slug in enumerate(slugs_needing_ids(conn, limit, now=now)):
        if n:
            pause()
        try:
            status, html = fetch(f"https://letterboxd.com/film/{quote(slug, safe='-')}/")
        except Exception as exc:
            logger.warning(f"Letterboxd film page for {slug} failed: {exc}")
            status, html = 0, ""
        if status in BLOCKED:
            stats["blocked"] = True
            break
        if status not in (200, 404):
            stats["errors"] += 1
            consecutive_errors += 1
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                break
            continue
        consecutive_errors = 0
        stats["checked"] += 1
        ids = parse_film_page(html) if status == 200 else {"tmdb_id": None, "tmdb_type": None, "imdb_id": None}
        usable = ids["tmdb_id"] and ids["tmdb_type"] in (None, "movie")
        conn.execute(
            """UPDATE src_letterboxd_entry SET tmdb_id = COALESCE(?, tmdb_id), imdb_id = COALESCE(?, imdb_id),
                      id_source = CASE WHEN ? THEN 'letterboxd' ELSE id_source END, id_checked_at = ? WHERE slug = ?""",
            (ids["tmdb_id"] if usable else None, ids["imdb_id"], 1 if usable else 0, now, slug))
        stats["found" if usable else "no_id"] += 1
        conn.commit()      # release the write lock before the next (slow, paced) page fetch
    return stats
