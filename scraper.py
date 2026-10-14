import random
import time
from typing import Dict, List, Optional
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config import DEFAULT_HEADERS, HTTP_TIMEOUT, MAX_RETRIES, REQUEST_DELAY_RANGE
from logger import logger
from parser import parse_movie_posters, parse_next_page_url

class LetterboxdScraper:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        
        # Configure retry strategy for transient errors
        retry_strategy = Retry(
            total=MAX_RETRIES,
            backoff_factor=2,  # Exponential backoff (2s, 4s, 8s...)
            status_forcelist=[429, 500, 502, 503, 504],
            raise_on_status=False
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def _polite_delay(self):
        delay = random.uniform(*REQUEST_DELAY_RANGE)
        time.sleep(delay)

    def fetch_page(self, url: str) -> Optional[str]:
        self._polite_delay()
        try:
            logger.debug(f"Fetching URL: {url}")
            response = self.session.get(url, timeout=HTTP_TIMEOUT)
            
            if response.status_code == 404:
                logger.warning(f"Page not found (404): {url}")
                return None
                
            response.raise_for_status()
            return response.text
        except requests.exceptions.RequestException as e:
            logger.error(f"HTTP request failed for {url}: {e}")
            return None

    def scrape_user_list(self, username: str, list_type: str) -> List[Dict[str, str]]:
        """
        Scrapes all pages for a given username and list type ('watchlist' or 'films').
        """
        current_url = f"https://letterboxd.com/{username}/{list_type}/"
        all_records = []
        page_num = 1

        logger.info(f"Starting scrape: [{username}] -> {list_type}")

        while current_url:
            logger.info(f"Scraping [{username}] {list_type} - Page {page_num}...")
            html = self.fetch_page(current_url)
            
            if not html:
                logger.warning(f"Stopping pagination early due to fetch failure at {current_url}")
                break

            records = parse_movie_posters(html, username)
            all_records.extend(records)
            logger.info(f"Extracted {len(records)} movies from Page {page_num}")

            # Check for next page
            current_url = parse_next_page_url(html, current_url)
            page_num += 1

        logger.info(f"Completed scrape: [{username}] {list_type}. Total extracted: {len(all_records)}")
        return all_records