import random
import time
from bs4 import BeautifulSoup
from curl_cffi import requests

from config import DEFAULT_REQUEST_DELAY_RANGE, DEFAULT_USER_DELAY_RANGE, HTTP_TIMEOUT
from logger import logger
from database import add_list_column_if_missing, get_setting, upsert_movie_with_list, get_active_profiles

def scrape_letterboxd_page(url, list_name, username, profile_id):
    try:
        response = requests.get(url, impersonate="chrome120", timeout=HTTP_TIMEOUT)
        if response.status_code in [403, 404, 429]:
            logger.warning(f"HTTP {response.status_code} encountered for {url}")
            return "abort_scrape" if response.status_code in [403, 429] else "end_of_pagination"
        if response.status_code != 200:
            logger.warning(f"Failed to load {url} (Status: {response.status_code})")
            return False

        soup = BeautifulSoup(response.text, "html.parser")
        posters = soup.select("div.react-component[data-item-slug]") or soup.select("[data-item-slug], [data-film-slug]")
        if not posters: return "end_of_pagination"

        count = 0
        seen_slugs = set()
        for poster in posters:
            slug = poster.get("data-item-slug") or poster.get("data-film-slug")
            if not slug or slug in seen_slugs: continue
            seen_slugs.add(slug)

            title = poster.get("data-item-full-display-name") or poster.get("data-item-name")
            if not title:
                img = poster.find("img")
                title = img.get("alt") if img and img.get("alt") else slug.replace("-", " ").title()

            logger.info(f"Found movie -> User: {username} | Slug: {slug} | Title: {title} | List: {list_name}")
            upsert_movie_with_list(slug, title, list_name, username=username, profile_id=profile_id)
            count += 1

        logger.info(f"Successfully processed {count} movies from {url}")
        return True if soup.select_one("a.next, .paginate-next a") else "end_of_pagination"
    except Exception as e:
        logger.error(f"Error scraping {url}: {e}")
        return False

def _delay_range(min_key, max_key, default):
    try:
        low, high = float(get_setting(min_key, default[0])), float(get_setting(max_key, default[1]))
    except (TypeError, ValueError): return default
    return (low, high) if 0 <= low <= high else (high, low) if low > high else default

def _pause(delay_range, reason=None):
    delay = random.uniform(delay_range[0], delay_range[1])
    if reason: logger.info(f"Waiting {delay:.1f}s {reason}...")
    time.sleep(delay)

def _scrape_section(username, profile_id, path, list_name, request_delay):
    page = 1
    while True:
        url = f"https://letterboxd.com/{username}/{path}/" if page == 1 else f"https://letterboxd.com/{username}/{path}/page/{page}/"
        logger.info(f"Scraping {list_name}: {url}")
        result = scrape_letterboxd_page(url, list_name, username, profile_id)
        if result in ("end_of_pagination", "abort_scrape") or result is False: return result
        page += 1
        _pause(request_delay)

def run_all_scrapers():
    logger.info("Verifying database schema...")
    add_list_column_if_missing()

    active_profiles = get_active_profiles()
    target_profiles = [p for p in active_profiles if p.get('letterboxd_username')]
    if not target_profiles:
        logger.info("No active Letterboxd profiles configured.")
        return

    request_delay = _delay_range("request_delay_min", "request_delay_max", DEFAULT_REQUEST_DELAY_RANGE)
    user_delay = _delay_range("user_delay_min", "user_delay_max", DEFAULT_USER_DELAY_RANGE)
    logger.info(f"Scraping {len(target_profiles)} profile(s)...")

    for index, profile in enumerate(target_profiles):
        if index > 0: _pause(user_delay, f"before starting {profile['profile_name']}")
        username, profile_id = profile['letterboxd_username'], profile['id']
        
        logger.info(f"--- Starting Scrape for {username} ({index + 1}/{len(target_profiles)}) ---")
        try:
            if _scrape_section(username, profile_id, "watchlist", "Watchlist", request_delay) == "abort_scrape": continue
            _pause(request_delay)
            _scrape_section(username, profile_id, "films", "Watched", request_delay)
        except Exception as e:
            logger.error(f"Error while scraping {username}: {e}")

    logger.info("Letterboxd multi-source scrape complete.")

if __name__ == "__main__":
    run_all_scrapers()