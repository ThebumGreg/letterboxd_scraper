import json
import logging
import random
import re
import time
from bs4 import BeautifulSoup
from curl_cffi import requests

from database import (
    get_setting,
    save_scraped_record,
    save_user_film,
    sync_scraped_records,
    update_task_status,
)

logger = logging.getLogger(__name__)

def random_delay():
    try:
        min_delay = float(get_setting("request_delay_min", "1.0"))
        max_delay = float(get_setting("request_delay_max", "3.0"))
        time.sleep(random.uniform(min_delay, max_delay))
    except Exception:
        time.sleep(1.5)

def parse_season_episode(title_text, slug_text):
    """Regex extracts season and episode numbers from title or slug."""
    season_num = None
    episode_num = None
    media_type = "movie"

    combined = f"{title_text} {slug_text}"
    
    # Check for S01E04 pattern
    se_match = re.search(r"[Ss](\d{1,2})[Ee](\d{1,2})", combined)
    if se_match:
        season_num = int(se_match.group(1))
        episode_num = int(se_match.group(2))
        media_type = "tv"
    else:
        # Check for Season 1 pattern
        s_match = re.search(r"[Ss]eason\s*(\d{1,2})", combined)
        if s_match:
            season_num = int(s_match.group(1))
            media_type = "tv"
        
        # Check for Episode 4 pattern
        e_match = re.search(r"[Ee]pisode\s*(\d{1,2})", combined)
        if e_match:
            episode_num = int(e_match.group(1))
            media_type = "tv"

    return media_type, season_num, episode_num

def scrape_letterboxd_user(username):
    base_url = f"https://letterboxd.com/{username}/watchlist/page/"
    page = 1
    scraped_items = []

    while True:
        url = f"{base_url}{page}/"
        logger.info(f"Scraping Letterboxd [{username}] Page {page}: {url}")

        try:
            response = requests.get(url, impersonate="chrome110", timeout=15)
            if response.status_code == 404:
                break
            elif response.status_code != 200:
                break

            soup = BeautifulSoup(response.text, "html.parser")
            posters = soup.select("div.react-component[data-component-class='LazyPoster'], div[data-item-slug], div[data-target-link], li.poster-container, div.film-poster")

            if not posters:
                break

            items_found = 0
            seen_slugs = set()

            for poster in posters:
                slug = (
                    poster.get("data-item-slug")
                    or poster.get("data-film-slug")
                    or poster.get("data-target-link", "").strip("/").split("/")[-1]
                )

                if not slug or slug in seen_slugs:
                    continue

                seen_slugs.add(slug)

                full_display = poster.get("data-item-full-display-name") or poster.get("data-item-name")
                title = ""
                year = None

                if full_display:
                    year_match = re.search(r"^(.*?)\s*\((\d{4})\)$", full_display.strip())
                    if year_match:
                        title = year_match.group(1).strip()
                        year = int(year_match.group(2))
                    else:
                        title = full_display.strip()
                else:
                    img = poster.select_one("img")
                    title = img["alt"].strip() if img and img.get("alt") else slug.replace("-", " ").title()

                media_type, season_num, episode_num = parse_season_episode(title, slug)
                movie_url = f"https://letterboxd.com/film/{slug}/"

                scraped_items.append({
                    "username": username,
                    "source": "letterboxd",
                    "film_title": title,
                    "film_year": year,
                    "movie_slug": slug,
                    "media_type": media_type,
                    "season_number": season_num,
                    "episode_number": episode_num,
                    "movie_url": movie_url,
                    "raw_data": json.dumps({"slug": slug, "title": title, "year": year, "season": season_num, "episode": episode_num})
                })
                items_found += 1

            if items_found == 0:
                break

            page += 1
            random_delay()

        except Exception as e:
            logger.error(f"Error scraping Letterboxd user {username} on page {page}: {e}")
            break

    return scraped_items

def run_watchlist_scrapers():
    update_task_status("scraper", "in_progress", "Parsing target watchlists...")

    raw_usernames = get_setting("target_usernames", "[]")
    try:
        usernames = json.loads(raw_usernames)
        if isinstance(usernames, str):
            usernames = [u.strip() for u in usernames.split(",") if u.strip()]
    except Exception:
        usernames = [u.strip() for u in raw_usernames.split(",") if u.strip()]

    if not usernames:
        update_task_status("scraper", "completed", "No target usernames configured.")
        return

    all_scraped_records = []

    for username in usernames:
        user_records = scrape_letterboxd_user(username)
        all_scraped_records.extend(user_records)

    if all_scraped_records:
        sync_scraped_records(all_scraped_records)
        update_task_status(
            "scraper",
            "completed",
            f"Successfully scraped {len(all_scraped_records)} items across {len(usernames)} user(s)."
        )
    else:
        update_task_status("scraper", "completed", "Scrape completed. No items found.")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_watchlist_scrapers()