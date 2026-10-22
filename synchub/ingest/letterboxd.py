"""Letterboxd lists -> src_letterboxd_entry.

Entries are kept forever. When a list is scraped completely, entries that are no longer on it get removed_at set
(they stay in the database); an incomplete or empty scrape never marks anything removed.
"""
import logging
import random
import time

from bs4 import BeautifulSoup

from config import DEFAULT_REQUEST_DELAY_RANGE, DEFAULT_USER_DELAY_RANGE, HTTP_TIMEOUT
from synchub import db, settings
from synchub.ingest.common import mark_unseen, touch
from synchub.text import split_title_year

logger = logging.getLogger(__name__)

LISTS = (("watchlist", "watchlist"), ("watched", "films"))   # (list name stored, URL path)
BLOCKED = (403, 429)


def default_fetch(url):
    """Returns (status_code, html). Letterboxd needs a browser-like TLS fingerprint."""
    db.assert_not_in_write_transaction(f"Letterboxd request to {url}")
    from curl_cffi import requests as cffi_requests
    response = cffi_requests.get(url, impersonate="chrome120", timeout=HTTP_TIMEOUT)
    return response.status_code, response.text


def parse_page(html):
    """Returns (entries, has_next_page). Each entry is {'slug', 'title', 'year'}."""
    soup = BeautifulSoup(html, "html.parser")
    posters = soup.select("div.react-component[data-item-slug]") or soup.select("[data-item-slug], [data-film-slug]")
    entries, seen = [], set()
    for poster in posters:
        slug = poster.get("data-item-slug") or poster.get("data-film-slug")
        if not slug or slug in seen:
            continue
        seen.add(slug)
        raw = poster.get("data-item-full-display-name") or poster.get("data-item-name")
        if not raw:
            img = poster.find("img")
            raw = img.get("alt") if img is not None and img.get("alt") else slug.replace("-", " ").title()
        title, year = split_title_year(raw)
        entries.append({"slug": slug, "title": title, "year": year})
    return entries, soup.select_one("a.next, .paginate-next a") is not None


def scrape_list(username, path, fetch=default_fetch, pause=lambda: None, progress=None):
    """Walks every page of one list. Returns (entries, complete); complete is False when the scrape was cut short."""
    progress = progress or (lambda message, force=False: None)
    entries, seen, page = [], set(), 1
    while True:
        url = f"https://letterboxd.com/{username}/{path}/" + ("" if page == 1 else f"page/{page}/")
        progress(f"Letterboxd {username}/{path}: page {page} ({len(entries)} film(s) so far)")
        status, html = fetch(url)
        if status in BLOCKED:
            logger.warning(f"Letterboxd returned HTTP {status} for {url}; stopping.")
            return entries, False
        if status == 404:
            return entries, page > 1          # page 1 missing = unknown/private user, not an empty list
        if status != 200:
            logger.warning(f"Letterboxd returned HTTP {status} for {url}; list is incomplete.")
            return entries, False
        page_entries, has_next = parse_page(html)
        for entry in page_entries:
            if entry["slug"] not in seen:
                seen.add(entry["slug"])
                entries.append(entry)
        if not page_entries or not has_next:
            return entries, True
        page += 1
        pause()


def store(conn, username, list_name, entries, complete, now=None):
    now = now or db.now()
    username = username.lower()
    for entry in entries:
        touch(conn, "src_letterboxd_entry", ("username", "slug", "list"), {
            "username": username, "slug": entry["slug"], "list": list_name, "title": entry["title"], "year": entry["year"],
        }, now, keep=("title", "year"))
    removed = 0
    if complete and entries:
        removed = mark_unseen(conn, "src_letterboxd_entry", ("slug",), [e["slug"] for e in entries], now,
                              scope_sql="username = ? AND list = ?", scope_params=(username, list_name))
    return {"seen": len(entries), "removed": removed}


def _delay(min_key, max_key, default):
    try:
        low, high = float(settings.get(min_key)), float(settings.get(max_key))
    except (TypeError, ValueError):
        return default
    return (min(low, high), max(low, high)) if low >= 0 and high >= 0 else default


def sync_all(fetch=default_fetch, sleep=time.sleep, progress=None):
    """Scrapes every active profile that has a Letterboxd username. Returns a per-user summary."""
    progress = progress or (lambda message, force=False: None)
    with db.connection() as conn:
        users = [r["letterboxd_username"] for r in conn.execute(
            "SELECT letterboxd_username FROM profile WHERE is_active = 1 AND letterboxd_username IS NOT NULL ORDER BY id")]
    request_delay = _delay("request_delay_min", "request_delay_max", DEFAULT_REQUEST_DELAY_RANGE)
    user_delay = _delay("user_delay_min", "user_delay_max", DEFAULT_USER_DELAY_RANGE)
    summary = {}
    for index, username in enumerate(users):
        if index:
            wait = random.uniform(*user_delay)
            progress(f"Waiting {wait:.0f}s before the next user ({index + 1} of {len(users)}: {username})", force=True)
            sleep(wait)
        summary[username] = {}
        for list_name, path in LISTS:
            entries, complete = scrape_list(username, path, fetch, pause=lambda: sleep(random.uniform(*request_delay)), progress=progress)
            with db.connection() as conn:
                result = store(conn, username, list_name, entries, complete)
            summary[username][list_name] = {**result, "complete": complete}
            progress(f"Letterboxd {username}/{list_name}: {result['seen']} film(s), {result['removed']} no longer listed" + ("" if complete else " (incomplete)"), force=True)
            sleep(random.uniform(*request_delay))
    return summary
