from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from logger import logger

BASE_URL = "https://letterboxd.com"

def parse_movie_posters(html_content: str, username: str) -> List[Dict[str, str]]:
    """
    Parses HTML content and extracts movie records from LazyPoster or film containers.
    """
    soup = BeautifulSoup(html_content, "html.parser")
    movies = []

    # Letterboxd posters are typically wrapped in container elements with data attributes
    posters = soup.find_all(attrs={"data-component-class": "LazyPoster"})
    
    # Fallback to standard poster list items if LazyPoster attribute is not explicitly set
    if not posters:
        posters = soup.select("ul.poster-list div.film-poster, ul.poster-list div.react-component")

    for poster in posters:
        # Extract attributes with fallback options
        name = poster.get("data-item-name") or poster.get("data-film-name") or ""
        slug = poster.get("data-item-slug") or poster.get("data-film-slug") or ""
        link = poster.get("data-item-link") or poster.get("data-target-link") or ""
        full_display = poster.get("data-item-full-display-name") or name
        film_id = (
            poster.get("data-film-id")
            or poster.get("data-postered-identifier")
            or poster.get("data-target-id")
            or ""
        )

        # Skip invalid poster entries without minimal identification
        if not slug and not name:
            continue

        full_url = urljoin(BASE_URL, link) if link else ""

        movies.append({
            "username": username,
            "movie_title": name.strip(),
            "movie_slug": slug.strip(),
            "movie_url": full_url.strip(),
            "full_display_name": full_display.strip(),
            "film_identifier": film_id.strip(),
        })

    return movies


def parse_next_page_url(html_content: str, current_url: str) -> Optional[str]:
    """
    Parses pagination controls and returns the full absolute URL for the next page, or None.
    """
    soup = BeautifulSoup(html_content, "html.parser")
    next_link = soup.select_one("div.pagination a.next")

    if next_link and next_link.get("href"):
        href = next_link["href"]
        return urljoin(current_url, href)

    return None