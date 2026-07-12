"""Scraper for imoti.net — Bulgarian real estate portal.

URL pattern: https://www.imoti.net/bg/obiavi/r/prodava/sofia/?page=N
Data: ~11,000 Sofia listings (366 pages × 30 listings)

Fix: Switched to Bulgarian version (/bg/) which has better coverage
and updated selectors to match current site structure.
"""

import re
from typing import List, Dict, Any, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from loguru import logger

from src.scrapers.base import BaseScraper
from src.config import EUR_BGN_RATE


class ImotiNetScraper(BaseScraper):
    """Scraper for imoti.net — all Sofia listings (Bulgarian version)."""
    
    BASE_URL = "https://www.imoti.net"
    # Bulgarian version has better coverage than English
    SOFIA_URL = "https://www.imoti.net/bg/obiavi/r/prodava/sofia/"
    RESULTS_PER_PAGE = 30
    
    def __init__(self, max_pages: int = 50):
        super().__init__("imotinet", self.BASE_URL)
        self.max_pages = max_pages

    @classmethod
    def parse_detail(cls, soup: BeautifulSoup) -> Dict[str, Any]:
        offer = soup.select_one("#js-ad-container") or soup.select_one(".real-estate-offer")
        description = offer.select_one(".text") if offer else None
        location = offer.select_one(".location") if offer else None
        agency = soup.select_one(".contact-agency-name")
        phone = next(
            (node.get_text(" ", strip=True) for node in soup.select(".hidden-phone") if re.search(r"\d{8,}", node.get_text())),
            None,
        )
        email_link = soup.select_one('a[href^="mailto:"]')
        images = [
            urljoin(cls.BASE_URL, image.get("src"))
            for image in soup.select(".gallery-slider-pics img[src]")
            if "/web/files/obiavi/" in image.get("src", "")
        ]
        coordinates = None
        map_frame = soup.select_one('iframe[src*="google.com/maps"]')
        if map_frame:
            coordinates = re.search(r"q=([0-9.]+),([0-9.]+)", map_frame.get("src", ""))
            if coordinates and float(coordinates.group(1)) == 0:
                coordinates = None
        return {
            "description_full": description.get_text(" ", strip=True) if description else None,
            "address": location.get_text(" ", strip=True) if location else None,
            "latitude": float(coordinates.group(1)) if coordinates else None,
            "longitude": float(coordinates.group(2)) if coordinates else None,
            "seller_type": "agency" if agency else None,
            "seller_name": agency.get_text(" ", strip=True) if agency else None,
            "contact_phone": phone,
            "contact_email": email_link.get("href", "")[7:] if email_link else None,
            "image_urls": images,
        }
    
    def _get_page_url(self, page: int) -> str:
        """Build page URL."""
        if page == 1:
            return self.SOFIA_URL
        return f"{self.SOFIA_URL}?page={page}"
    
    def _parse_price(self, text: str) -> tuple[Optional[float], Optional[float]]:
        """Parse price from text — handles EUR and BGN."""
        if not text:
            return None, None
        
        # Look for EUR amount - handle formats like "€ 185 000" or "185 000 EUR"
        eur_match = re.search(r'€\s*([\d\s]+)', text)
        if not eur_match:
            eur_match = re.search(r'([\d\s]+)\s*€', text, re.IGNORECASE)
        if eur_match:
            price_str = eur_match.group(1).replace(' ', '').replace('\xa0', '')
            try:
                price_eur = float(price_str)
                return price_eur * EUR_BGN_RATE, price_eur
            except ValueError:
                pass
        
        # Look for BGN amount and convert
        bgn_match = re.search(r'([\d\s]+)\s*лв', text)
        if bgn_match:
            price_str = bgn_match.group(1).replace(' ', '').replace('\xa0', '')
            try:
                price_bgn = float(price_str)
                return price_bgn, price_bgn / EUR_BGN_RATE
            except ValueError:
                pass
        
        return None, None
    
    def _extract_property_type(self, text: str, url: str) -> str:
        """Determine property type from text or URL."""
        text_lower = text.lower()
        url_lower = url.lower()
        
        # Check URL first (more reliable for Bulgarian version)
        if '/kashta/' in url_lower or '/house/' in url_lower:
            return 'house'
        elif '/parcel/' in url_lower or '/plot/' in url_lower:
            return 'plot'
        elif '/mezonet/' in url_lower or '/maisonette/' in url_lower:
            return 'maisonette'
        elif '/atelie/' in url_lower or '/studio/' in url_lower:
            return 'studio'
        elif any(x in url_lower for x in ['/dvustaen/', '/tristaen/', '/chetiristaen/', '/mnogostaen/']):
            return 'apartment'
        
        # Fallback to text
        if any(x in text_lower for x in ['studio', 'atelie', 'студио', 'ателие']):
            return 'studio'
        elif any(x in text_lower for x in ['maisonette', 'mezonet', 'мезонет']):
            return 'maisonette'
        elif any(x in text_lower for x in ['house', 'kashta', 'вила', 'къща']):
            return 'house'
        elif any(x in text_lower for x in ['plot', 'parcel', 'земя', 'парцел']):
            return 'plot'
        elif any(x in text_lower for x in ['office', 'офис', 'магазин']):
            return 'commercial'
        
        return 'apartment'
    
    def _extract_rooms(self, url: str) -> Optional[int]:
        """Extract room count from URL (Bulgarian version patterns)."""
        url_lower = url.lower()
        
        # Bulgarian URL patterns
        if 'ednostaen' in url_lower or 'ednostain' in url_lower or '1-staen' in url_lower:
            return 1
        elif 'dvustaen' in url_lower or 'dvustain' in url_lower or '2-staen' in url_lower:
            return 2
        elif 'tristaen' in url_lower or 'tristain' in url_lower or '3-staen' in url_lower:
            return 3
        elif 'chetiristaen' in url_lower or 'chetiristain' in url_lower or '4-staen' in url_lower:
            return 4
        elif 'mnogostaen' in url_lower or 'mnogostain' in url_lower:
            return 5
        
        return None
    
    def _extract_neighborhood(self, url: str) -> Optional[str]:
        """Extract neighborhood from URL."""
        # URL pattern: /prodava/sofia/NEIGHBORHOOD/type/id
        # Try different patterns
        match = re.search(r'/prodava(?:--[^/]+)?/sofia/([^/]+)/', url, re.IGNORECASE)
        if match:
            hood = match.group(1).replace('-', ' ').title()
            # Clean up common suffixes
            hood = re.sub(r'\s*District$', '', hood, flags=re.IGNORECASE)
            hood = re.sub(r'^Kkv\s+', '', hood, flags=re.IGNORECASE)  # Remove Kkv prefix
            return hood
        
        # Alternative pattern with query params
        match = re.search(r'location=([^&]+)', url)
        if match:
            from urllib.parse import unquote
            hood = unquote(match.group(1)).replace('-', ' ').title()
            return hood
        
        return None
    
    def _parse_listing(self, card: BeautifulSoup) -> Optional[Dict[str, Any]]:
        """Parse a single listing card from the search results."""
        try:
            # Find the main link
            link = card.find('a', href=re.compile(r'/obiava/'))
            if not link:
                return None
            
            url = link.get('href', '')
            if not url:
                return None
            
            if url.startswith('/'):
                url = f"{self.BASE_URL}{url}"

            image = card.find('img', src=True)
            image_url = urljoin(self.BASE_URL, image.get('src', '')) if image else None
            
            # Extract ID from URL: /bg/obiava/.../ID/
            id_match = re.search(r'/obiava/(?:[^/]+/)*(\d+)/', url)
            if not id_match:
                # Try alternative pattern at end of URL
                id_match = re.search(r'/(\d+)/?$', url.split('?')[0])
            source_id = id_match.group(1) if id_match else url
            
            # Get all text from the card
            text = card.get_text(separator=' ', strip=True)
            
            # Property type
            property_type = self._extract_property_type(text, url)
            
            # Rooms from URL
            rooms = self._extract_rooms(url)
            if property_type == 'studio':
                rooms = 1
            
            # Extract area - handle various formats
            area_match = re.search(r'(\d+(?:\.\d+)?)\s*(?:m\s*2|м\s*2|sq\.?\s*m|кв\.?\s*м)', text, re.IGNORECASE)
            area_sqm = float(area_match.group(1)) if area_match else None
            
            if not area_sqm:
                logger.debug(f"No area found in text")
                return None
            
            # Extract price - look for price elements
            price_elem = card.find(class_=re.compile(r'price|цена', re.I))
            price_text = price_elem.get_text(strip=True) if price_elem else text
            
            price_bgn, price_eur = self._parse_price(price_text)
            
            if not price_eur:
                logger.debug(f"No price found")
                return None
            
            # Extract neighborhood from URL
            neighborhood = self._extract_neighborhood(url)
            
            if not neighborhood:
                # Try to extract from text
                hood_match = re.search(r'град\s+София\s*,?\s*([^,\d]+)', text)
                if hood_match:
                    neighborhood = hood_match.group(1).strip()
                else:
                    logger.debug(f"No neighborhood found in URL: {url}")
                    return None
            
            # Clean up neighborhood
            neighborhood = re.sub(r'^(?:ж\.?к\.?|кв\.?|м-т)\s*', '', neighborhood, flags=re.IGNORECASE).strip()
            
            # Extract floor info (if available)
            floor = None
            total_floors = None
            
            # Look for floor patterns in text
            floor_match = re.search(r'(?:ет\.?|floor|етаж)\s*(\d+)', text, re.IGNORECASE)
            if floor_match:
                floor = int(floor_match.group(1))
            
            # Look for "от N" pattern for total floors
            total_match = re.search(r'(?:от|of)\s*(\d+)\s*(?:ет|floor)', text, re.IGNORECASE)
            if total_match:
                total_floors = int(total_match.group(1))
            
            return {
                'source': 'imotinet',
                'source_id': source_id,
                'url': url,
                'image_url': image_url or None,
                'title': text[:250] if len(text) > 250 else text,
                'price_bgn': price_bgn,
                'price_eur': price_eur,
                'area_sqm': area_sqm,
                'price_per_sqm_eur': round(price_eur / area_sqm, 2),
                'neighborhood': neighborhood,
                'property_type': property_type,
                'rooms': rooms,
                'floor': floor,
                'total_floors': total_floors,
                'construction_type': None,
            }
        
        except Exception as e:
            logger.error(f"Parse error: {e}")
            return None
    
    def scrape_page(self, page: int) -> List[Dict[str, Any]]:
        """Scrape a single page."""
        url = self._get_page_url(page)
        soup = self.fetch_page(url)
        
        if not soup:
            return []
        
        listings = []
        seen_ids = set()
        
        # Primary selector: list-view with clearfix items (actual site structure)
        list_view = soup.find('ul', class_='list-view')
        if list_view:
            cards = list_view.find_all('li', class_='clearfix')
            logger.debug(f"Using list-view selector, found {len(cards)} elements")
        else:
            # Fallback selectors
            selectors = [
                'div[data-listing-id]',  # Modern data attribute
                'article.listing-item',  # Article wrapper
                'div.listing-item',      # Div wrapper
                'div.property-card',     # Alternative class
            ]
            
            cards = []
            for selector in selectors:
                cards = soup.select(selector)
                if cards:
                    logger.debug(f"Using selector: {selector}, found {len(cards)} elements")
                    break
        
        # Final fallback: find all divs containing property links
        if not cards:
            property_links = soup.find_all('a', href=re.compile(r'/obiava/'))
            seen_parents = set()
            for link in property_links:
                # Skip pagination links
                href = link.get('href', '')
                if 'page=' in href:
                    continue
                
                # Find parent container
                parent = link.find_parent(['div', 'article', 'li'])
                if parent and parent not in seen_parents:
                    seen_parents.add(id(parent))
                    cards.append(parent)
            logger.debug(f"Fallback: found {len(cards)} cards via link parents")
        
        for card in cards:
            listing = self._parse_listing(card)
            if listing and listing['source_id'] not in seen_ids:
                seen_ids.add(listing['source_id'])
                listings.append(listing)
        
        return listings
    
    def scrape(self) -> List[Dict[str, Any]]:
        """Scrape all Sofia listings."""
        all_listings = []
        seen_ids = set()
        
        logger.info(f"Scraping imoti.net Sofia listings (Bulgarian version, up to {self.max_pages} pages)...")
        
        for page in range(1, self.max_pages + 1):
            listings = self.scrape_page(page)
            
            new_count = 0
            for listing in listings:
                if listing['source_id'] not in seen_ids:
                    seen_ids.add(listing['source_id'])
                    all_listings.append(listing)
                    new_count += 1
            
            logger.info(f"Page {page}: +{new_count} new listings (total: {len(all_listings)})")
            
            # Stop if no new listings
            if not listings:
                logger.info(f"No more listings at page {page}")
                break
            
            if page >= self.max_pages:
                break
        
        logger.info(f"Total from imoti.net: {len(all_listings)} unique listings")
        return all_listings
