import random
import time
from bs4 import BeautifulSoup
from curl_cffi import requests

# Import your settings from your config module
from config import DEFAULT_REQUEST_DELAY_RANGE, HTTP_TIMEOUT
from database import add_list_column_if_missing, upsert_movie_with_list

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

def run_all_scrapers():
    """Master scraper function executing the multi-source Letterboxd logic."""
    print("Verifying database schema...")
    add_list_column_if_missing()

    usernames = ["tristanisepic"]

    for username in usernames:
        print(f"\n--- Starting Scrape for {username} ---")

        # Phase 1: Scrape Watchlist
        page = 1
        while True:
            if page == 1:
                url = f"https://letterboxd.com/{username}/watchlist/"
            else:
                url = f"https://letterboxd.com/{username}/watchlist/page/{page}/"

            print(f"Scraping Watchlist: {url}")
            result = scrape_letterboxd_page(url, "Watchlist", username)
            
            if result == "end_of_pagination" or result is False:
                break
                
            page += 1
            delay = random.uniform(
                DEFAULT_REQUEST_DELAY_RANGE[0], DEFAULT_REQUEST_DELAY_RANGE[1]
            )
            time.sleep(delay)

        # Phase 2: Scrape Watched Films
        page = 1
        while True:
            if page == 1:
                url = f"https://letterboxd.com/{username}/films/"
            else:
                url = f"https://letterboxd.com/{username}/films/page/{page}/"

            print(f"Scraping Watched Films: {url}")
            result = scrape_letterboxd_page(url, "Watched", username)
            
            if result == "end_of_pagination" or result is False:
                break
                
            page += 1
            delay = random.uniform(
                DEFAULT_REQUEST_DELAY_RANGE[0], DEFAULT_REQUEST_DELAY_RANGE[1]
            )
            time.sleep(delay)

    print("\nLetterboxd multi-source scrape complete.")


if __name__ == "__main__":
    run_all_scrapers()