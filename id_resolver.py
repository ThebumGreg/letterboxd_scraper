# id_resolver.py
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
from letterboxd_scraper import _delay_range, _pause
from logger import logger

TASK_NAME = "id_resolver"
MAX_PER_RUN = 150
RETRY_AFTER_DAYS = 7
MAX_CONSECUTIVE_ERRORS = 5

_TMDB_LINK_RE = re.compile(r"themoviedb\.org/(movie|tv)/(\d+)")
_IMDB_LINK_RE = re.compile(r"imdb\.com/title/(tt\d+)")
_ARTICLE_RE = re.compile(r"^(the|a|an) ")
_RUN_LOCK = threading.Lock()

def is_running():
    return _RUN_LOCK.locked()

def normalize_title(title):
    text = unicodedata.normalize("NFKD", title or "")
    text = text.encode("ascii", "ignore").decode("ascii").lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text).strip()
    return _ARTICLE_RE.sub("", text)

def _status(status, message):
    try:
        update_task_status(TASK_NAME, status, message)
    except Exception:
        pass

def parse_letterboxd_film_page(html):
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
    url = f"https://letterboxd.com/film/{quote(slug, safe='-')}/"
    try:
        response = cffi_requests.get(url, impersonate="chrome120", timeout=HTTP_TIMEOUT)
    except Exception as e:
        logger.warning(f"ID resolver: request failed for {url}: {e}")
        return "error", None

    if response.status_code in (403, 429): return "blocked", None
    if response.status_code == 404: return "missing", None
    if response.status_code != 200: return "error", None
    return "ok", parse_letterboxd_film_page(response.text)

def search_tmdb_movie(title, year, api_key):
    wanted = normalize_title(title)
    if not wanted or not year:
        return None

    headers, params = {}, {"query": title, "year": year, "include_adult": "false"}
    if api_key.startswith("eyJ"): headers["Authorization"] = f"Bearer {api_key}"
    else: params["api_key"] = api_key

    try:
        response = requests.get("https://api.themoviedb.org/3/search/movie", headers=headers, params=params, timeout=HTTP_TIMEOUT)
        if response.status_code != 200: return None
        results = response.json().get("results", [])
    except (requests.RequestException, ValueError):
        return None

    hits = [
        m for m in results
        if (m.get("release_date") or "")[:4] == str(year)
        and wanted in {normalize_title(m.get("title")), normalize_title(m.get("original_title"))}
    ]
    return str(hits[0]["id"]) if len(hits) == 1 else None

def resolve_missing_ids(limit=MAX_PER_RUN, retry_after_days=RETRY_AFTER_DAYS, use_search=True):
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

        consecutive_errors = 0
        for n, film in enumerate(films, 1):
            if n > 1: _pause(request_delay)
            if n % 10 == 0: _status("in_progress", f"Resolved {n - 1} of {len(films)} films so far...")

            slug, title, year = film["movie_slug"], film.get("title"), film.get("year")
            status, ids = _fetch_letterboxd_ids(slug)

            if status == "blocked":
                stats["blocked"] = True
                logger.warning("ID resolver: Letterboxd blocked requests.")
                break
            if status == "error":
                stats["errors"] += 1
                consecutive_errors += 1
                if consecutive_errors >= MAX_CONSECUTIVE_ERRORS: break
                continue

            consecutive_errors = 0
            stats["checked"] += 1

            if ids and ids["tmdb_id"] and ids["tmdb_type"] in (None, "movie"):
                set_resolved_ids(slug, ids["tmdb_id"], ids["imdb_id"], source="letterboxd")
                stats["letterboxd"] += 1
                continue

            if use_search and api_key:
                tmdb_id = search_tmdb_movie(title, year, api_key)
                if tmdb_id:
                    set_resolved_ids(slug, tmdb_id, ids["imdb_id"] if ids else None, source="tmdb-search")
                    stats["search"] += 1
                    continue

            mark_id_check_failed(slug)
            stats["unresolved"] += 1

        summary = f"Checked {stats['checked']} film(s): {stats['letterboxd']} found on Letterboxd, {stats['search']} guessed (review needed), {stats['unresolved']} unresolved."
        _status("completed", summary)
        return stats
    except Exception as e:
        logger.error(f"ID resolver failed: {e}")
        _status("failed", str(e))
        return stats
    finally:
        _RUN_LOCK.release()