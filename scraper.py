import json
import random
import time
from bs4 import BeautifulSoup
from curl_cffi import requests

# Import your settings from your config module
from config import (
    DEFAULT_REQUEST_DELAY_RANGE,
    DEFAULT_USER_DELAY_RANGE,
    DEFAULT_USERNAMES,
    HTTP_TIMEOUT,
)
from database import add_list_column_if_missing, get_setting, upsert_movie_with_list

def scrape_letterboxd_page(url, list_name, username):
    """Parser updated for Letterboxd React component grid structures, ensuring proper username/list association."""
    try:
        response = requests.get(url, impersonate="chrome120", timeout=HTTP_TIMEOUT)

        if response.status_code in [403, 404]:
            print(f"HTTP {response.status_code} encountered for {url}")
            return "end_of_pagination"
        if response.status_code != 200:
            print(f"Failed to load {url} (Status: {response.status_code})")
            return False

        soup = BeautifulSoup(response.text, "html.parser")
        
        # Target Letterboxd's React lazy-poster components
        posters = soup.select("div.react-component[data-item-slug]")

        if not posters:
            # Fallback to any element containing data-item-slug or data-film-slug
            posters = soup.select("[data-item-slug], [data-film-slug]")

        if not posters:
            return "end_of_pagination"

        count = 0
        seen_slugs = set()
        
        for poster in posters:
            slug = poster.get("data-item-slug") or poster.get("data-film-slug")
            if not slug or slug in seen_slugs:
                continue
                
            seen_slugs.add(slug)

            title = (
                poster.get("data-item-full-display-name") or
                poster.get("data-item-name")
            )

            if not title:
                img = poster.find("img")
                if img and img.get("alt"):
                    title = img.get("alt")
                else:
                    title = slug.replace("-", " ").title()

            print(f"Found movie -> User: {username} | Slug: {slug} | Title: {title} | List: {list_name}")
            upsert_movie_with_list(slug, title, list_name, username=username)
            count += 1

        print(f"Successfully processed {count} movies from {url}")

        next_button = soup.select_one("a.next, .paginate-next a")
        if not next_button:
            return "end_of_pagination"

        return True
    except Exception as e:
        print(f"Error scraping {url}: {e}")
        return False

def get_target_usernames():
    """Letterboxd usernames saved on the Settings page (a JSON list).

    Falls back to the config defaults only if the setting was never saved; an explicitly
    empty list means "scrape nobody"."""
    raw = get_setting("target_usernames", "") or ""
    if not raw.strip():
        return list(DEFAULT_USERNAMES)
    try:
        parsed = json.loads(raw)
        names = [str(n).strip() for n in parsed] if isinstance(parsed, list) else []
    except ValueError:
        names = [n.strip() for n in raw.replace("\n", ",").split(",")]
    return list(dict.fromkeys(n for n in names if n))


def _delay_range(min_key, max_key, default):
    """(low, high) seconds from two settings, falling back to the config default if unset or invalid."""
    try:
        low = float(get_setting(min_key, default[0]))
        high = float(get_setting(max_key, default[1]))
    except (TypeError, ValueError):
        return default
    if low < 0 or high < 0:
        return default
    return (low, high) if low <= high else (high, low)


def _pause(delay_range, reason=None):
    delay = random.uniform(delay_range[0], delay_range[1])
    if reason:
        print(f"Waiting {delay:.1f}s {reason}...")
    time.sleep(delay)


def _scrape_section(username, path, list_name, request_delay):
    """Walk every page of one section (watchlist / films) for one user."""
    page = 1
    while True:
        if page == 1:
            url = f"https://letterboxd.com/{username}/{path}/"
        else:
            url = f"https://letterboxd.com/{username}/{path}/page/{page}/"

        print(f"Scraping {list_name}: {url}")
        result = scrape_letterboxd_page(url, list_name, username)

        if result == "end_of_pagination" or result is False:
            break

        page += 1
        _pause(request_delay)


def run_all_scrapers(usernames=None):
    """Master scraper: every configured user, with polite delays between requests and between users."""
    print("Verifying database schema...")
    add_list_column_if_missing()

    usernames = usernames or get_target_usernames()
    if not usernames:
        print("No Letterboxd usernames configured. Add some on the Settings page.")
        return

    request_delay = _delay_range("request_delay_min", "request_delay_max", DEFAULT_REQUEST_DELAY_RANGE)
    user_delay = _delay_range("user_delay_min", "user_delay_max", DEFAULT_USER_DELAY_RANGE)
    print(f"Scraping {len(usernames)} user(s): {', '.join(usernames)}")

    for index, username in enumerate(usernames):
        if index > 0:
            _pause(user_delay, f"before starting {username}")

        print(f"\n--- Starting Scrape for {username} ({index + 1}/{len(usernames)}) ---")
        try:
            _scrape_section(username, "watchlist", "Watchlist", request_delay)
            _pause(request_delay)
            _scrape_section(username, "films", "Watched", request_delay)
        except Exception as e:
            # One bad user must not stop the rest of the run.
            print(f"Error while scraping {username}: {e}")

    print("\nLetterboxd multi-source scrape complete.")


if __name__ == "__main__":
    run_all_scrapers()