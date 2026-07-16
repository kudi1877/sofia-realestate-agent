"""Base scraper with common functionality."""

import random
import time
from abc import ABC, abstractmethod
from typing import Optional, Dict, Any, List
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from src.utils.soup import make_soup
from loguru import logger

from src.config import USER_AGENTS, SCRAPE_DELAY_MIN, SCRAPE_DELAY_MAX


class BaseScraper(ABC):
    """Abstract base class for real estate scrapers."""
    
    def __init__(self, source_name: str, base_url: str):
        self.source_name = source_name
        self.base_url = base_url
        self.session = None
        self._request_count = 0
    
    def _get_headers(self) -> Dict[str, str]:
        """Get headers. Keep minimal to avoid Cloudflare bot detection."""
        return {
            "User-Agent": random.choice(USER_AGENTS),
        }
    
    def _init_session(self):
        """Initialize HTTP session with retries."""
        transport = httpx.HTTPTransport(retries=3)
        self.session = httpx.Client(
            transport=transport,
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=True,
        )
    
    def _rate_limit(self):
        """Apply rate limiting with random jitter."""
        delay = random.uniform(SCRAPE_DELAY_MIN, SCRAPE_DELAY_MAX)
        time.sleep(delay)
    
    def fetch_page(self, url: str, encoding: Optional[str] = None) -> Optional[BeautifulSoup]:
        """Fetch page and return BeautifulSoup object."""
        if not self.session:
            self._init_session()
        
        self._rate_limit()
        
        for attempt in range(3):
            try:
                logger.debug(f"Fetching {url} (attempt {attempt + 1})")
                
                response = self.session.get(url, headers=self._get_headers())
                response.raise_for_status()
                
                # Handle encoding properly - decode bytes manually to avoid httpx's auto-detection
                if encoding:
                    text = response.content.decode(encoding, errors='replace')
                else:
                    text = response.text
                
                self._request_count += 1
                
                return make_soup(text)
                
            except httpx.HTTPStatusError as e:
                logger.warning(f"HTTP error {e.response.status_code} for {url}: {e}")
                if e.response.status_code == 404:
                    return None
                if attempt < 2:
                    time.sleep(2 ** attempt)  # Exponential backoff
                    
            except Exception as e:
                logger.error(f"Error fetching {url}: {e}")
                if attempt < 2:
                    time.sleep(2 ** attempt)
        
        return None
    
    def parse_listing(self, soup: BeautifulSoup) -> Optional[Dict[str, Any]]:
        """Parse a single listing from BeautifulSoup object."""
        raise NotImplementedError("Subclasses must implement parse_listing")
    
    def scrape(self) -> List[Dict[str, Any]]:
        """Scrape all listings from source."""
        raise NotImplementedError("Subclasses must implement scrape")
    
    def close(self):
        """Close HTTP session."""
        if self.session:
            self.session.close()
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()