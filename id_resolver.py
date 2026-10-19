"""Finds TMDB IDs for Letterboxd films that were scraped without one.

Order of attack for each film that has no TMDB ID:
  1. Read the ID off the film's own Letterboxd page (exact; source 'letterboxd').
  2. Search TMDB by title + year and accept ONLY a single exact match
     (source 'tmdb-search', marked for review and not sent to Radarr until confirmed).
Anything else is left unmatched and retried after a few days.
"""
import re
import threading
import unicodedata
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup
from curl_cffi import requests as cffi_requests

from config import DEFAULT_REQUEST_DELAY_RANGE, HTTP_TIMEOUT
from database import (
    get_films_needing_ids,
    get_setting,
    mark_id_check_failed,
    set_resolved_ids,
    update_task_status,
)
from logger import logger
from projects.letterboxd_scraper.letterboxd_scraper import _delay_range, _pause

TASK_NAME = "id_resolver"
MAX_PER_RUN = 150            # keeps a single run to roughly ten minutes at the default delays
RETRY_AFTER_DAYS = 7         # how long to wait before re-checking a film that could not be resolved
MAX_CONSECUTIVE_ERRORS = 5

_TMDB_LINK_RE = re.compile(r"themoviedb\.org/(movie|tv)/(\d+)")
_IMDB_LINK_RE = re.compile(r"imdb\.com/title/(tt\d+)")
_ARTICLE_RE = re.compile(r"^(the|a|an) ")
_RUN_LOCK = threading.Lock()


def is_running():
    return _RUN_LOCK.locked()


def normalize_title(title):
    """'The Matrix: Reloaded!' -> 'matrix reloaded'. Used for exact title comparisons only."""
    text = unicodedata.normalize("NFKD", title or "")
    text = text.encode("ascii", "ignore").decode("ascii").lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text).strip()
    return _ARTICLE_RE.sub("", text)


def _status(status, message):
    try:
        update_task_status(TASK_NAME, status, message)
    except Exception:  # status reporting must never break the run
        pass


# --------------------------------------------------------------------------
# Step 1: Letterboxd film page
# --------------------------------------------------------------------------

def parse_letterboxd_film_page(html):
    """Pulls TMDB/IMDb IDs out of a Letterboxd film page. Tries the <body> data attributes first,
    then falls back to the TMDb / IMDb links in the page footer."""
    soup = BeautifulSoup(html, "html.parser")
    tmdb_id = tmdb_type = imdb_id = None

    body = soup.body
    if body is not None:
        tmdb_id = body.get("data-tmdb-id")
        tmdb_type = body.get("data-tmdb-type")

    for link in soup.find_all("a", href=True):
        href = link["href"]
        if not tmdb_id:
            m = _TMDB_LINK_RE.search(href)
            if m:
                tmdb_type, tmdb_id = m.group(1), m.group(2)
        if not imdb_id:
            m = _IMDB_LINK_RE.search(href)
            if m:
                imdb_id = m.group(1)

    if tmdb_id is not None and not str(tmdb_id).isdigit():
        tmdb_id = None
    return {"tmdb_id": str(tmdb_id) if tmdb_id else None, "tmdb_type": tmdb_type, "imdb_id": imdb_id}


def _fetch_letterboxd_ids(slug):
    """Returns (status, ids). status: ok | missing | blocked | error."""
    url = f"https://letterboxd.com/film/{quote(slug, safe='-')}/"
    try:
        response = cffi_requests.get(url, impersonate="chrome120", timeout=HTTP_TIMEOUT)
    except Exception as e:
        logger.warning(f"ID resolver: request failed for {url}: {e}")
        return "error", None

    if response.status_code in (403, 429):
        return "blocked", None
    if response.status_code == 404:
        return "missing", None
    if response.status_code != 200:
        return "error", None
    return "ok", parse_letterboxd_film_page(response.text)


# --------------------------------------------------------------------------
# Step 2: TMDB title search (fallback)
# --------------------------------------------------------------------------

def search_tmdb_movie(title, year, api_key):
    """Returns a TMDB ID only if exactly one result matches the title AND release year exactly."""
    wanted = normalize_title(title)
    if not wanted or not year:
        return None

    headers, params = {}, {"query": title, "year": year, "include_adult": "false"}
    if api_key.startswith("eyJ"):          # v4 read-access token
        headers["Authorization"] = f"Bearer {api_key}"
    else:
        params["api_key"] = api_key

    try:
        response = requests.get("https://api.themoviedb.org/3/search/movie", headers=headers,
                                params=params, timeout=HTTP_TIMEOUT)
        if response.status_code != 200:
            return None
        results = response.json().get("results", [])
    except (requests.RequestException, ValueError):
        return None

    hits = [
        m for m in results
        if (m.get("release_date") or "")[:4] == str(year)
        and wanted in {normalize_title(m.get("title")), normalize_title(m.get("original_title"))}
    ]
    return str(hits[0]["id"]) if len(hits) == 1 else None


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------

def resolve_missing_ids(limit=MAX_PER_RUN, retry_after_days=RETRY_AFTER_DAYS, use_search=True):
    """Resolve IDs for films that have none. Returns a stats dict, or None if a run is already active."""
    if not _RUN_LOCK.acquire(blocking=False):
        logger.info("ID resolver is already running; skipping.")
        return None

    stats = {"checked": 0, "letterboxd": 0, "search": 0, "unresolved": 0, "errors": 0, "blocked": False}
    try:
        films = get_films_needing_ids(retry_after_days, limit)
        if not films:
            _status("completed", "No films are missing IDs.")
            return stats

        request_delay = _delay_range("request_delay_min", "request_delay_max", DEFAULT_REQUEST_DELAY_RANGE)
        api_key = (get_setting("tmdb_api_key", "") or "").strip()
        _status("in_progress", f"Resolving IDs for {len(films)} film(s)...")
        logger.info(f"ID resolver: {len(films)} film(s) without a TMDB ID.")

        consecutive_errors = 0
        for n, film in enumerate(films, 1):
            if n > 1:
                _pause(request_delay)
            if n % 10 == 0:
                _status("in_progress", f"Resolved {n - 1} of {len(films)} films so far...")

            slug, title, year = film["movie_slug"], film.get("title"), film.get("year")
            status, ids = _fetch_letterboxd_ids(slug)

            if status == "blocked":
                stats["blocked"] = True
                logger.warning("ID resolver: Letterboxd is blocking requests; stopping. Will retry next run.")
                break
            if status == "error":
                stats["errors"] += 1
                consecutive_errors += 1
                if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    logger.warning("ID resolver: too many consecutive errors; stopping.")
                    break
                continue
            consecutive_errors = 0
            stats["checked"] += 1

            # Step 1: exact ID from the Letterboxd page (only for movies; TMDB TV IDs would not work in Radarr)
            if ids and ids["tmdb_id"] and ids["tmdb_type"] in (None, "movie"):
                set_resolved_ids(slug, ids["tmdb_id"], ids["imdb_id"], source="letterboxd")
                stats["letterboxd"] += 1
                logger.info(f"ID resolver: {slug} -> TMDB {ids['tmdb_id']} (Letterboxd page)")
                continue

            # Step 2: title + year search, flagged for review
            if use_search and api_key:
                tmdb_id = search_tmdb_movie(title, year, api_key)
                if tmdb_id:
                    set_resolved_ids(slug, tmdb_id, ids["imdb_id"] if ids else None, source="tmdb-search")
                    stats["search"] += 1
                    logger.info(f"ID resolver: {slug} -> TMDB {tmdb_id} (title search, needs review)")
                    continue

            mark_id_check_failed(slug)
            stats["unresolved"] += 1
            logger.info(f"ID resolver: could not resolve {slug}")

        summary = (f"Checked {stats['checked']} film(s): {stats['letterboxd']} found on Letterboxd, "
                   f"{stats['search']} guessed by title search (review needed), {stats['unresolved']} unresolved.")
        if stats["blocked"]:
            summary += " Stopped early: Letterboxd blocked the requests."
        if stats["errors"]:
            summary += f" {stats['errors']} request error(s)."
        _status("completed", summary)
        return stats
    except Exception as e:
        logger.error(f"ID resolver failed: {e}")
        _status("failed", str(e))
        return stats
    finally:
        _RUN_LOCK.release()
