"""Scraper for property.bg — Bulgarian real estate agency network.

URL: https://www.property.bg/en/properties-for-sale/sofia/
Data: 7,243 Sofia listings
Structure: Clean HTML with property cards

Fix: Proper UTF-8 encoding handling and updated selectors for current site structure.
"""

import re
import html
import json
from typing import List, Dict, Any, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from loguru import logger

from src.scrapers.base import BaseScraper
from src.config import EUR_BGN_RATE


class PropertyBGScraper(BaseScraper):
    """Scraper for property.bg with fixed encoding and selectors."""
    
    BASE_URL = "https://www.property.bg"
    SEARCH_URL = "https://www.property.bg/en/properties-for-sale/sofia/"
    
    def __init__(self, max_pages: int = 30):
        super().__init__("propertybg", self.BASE_URL)
        self.max_pages = max_pages

    @classmethod
    def parse_detail(cls, soup: BeautifulSoup) -> Dict[str, Any]:
        description = None
        offer_image = None
        for script in soup.select('script[type="application/ld+json"]'):
            text = script.get_text()
            if '"@type": "Offer"' not in text and '"@type":"Offer"' not in text:
                continue
            try:
                data = json.loads(text)
                description = data.get("description")
                offer_image = data.get("image")
            except (json.JSONDecodeError, TypeError):
                desc_match = re.search(r'"description"\s*:\s*"(.*?)"\s*[,}]', text, re.DOTALL)
                image_match = re.search(r'"image"\s*:\s*"(.*?)"', text)
                description = html.unescape(desc_match.group(1)) if desc_match else None
                offer_image = image_match.group(1) if image_match else None
            break

        location_label = next(
            (node for node in soup.find_all("span") if node.get_text(" ", strip=True) == "Location"),
            None,
        )
        location = location_label.find_next("b") if location_label else None
        map_frame = soup.select_one('iframe[src*="google.com/maps"]')
        coordinates = re.search(r"q=([0-9.]+),([0-9.]+)", map_frame.get("src", "")) if map_frame else None
        images = [link.get("href") for link in soup.select("#prop_gallery_grid a[href]") if link.get("href")]
        if offer_image and offer_image not in images:
            images.insert(0, offer_image)
        avatar = soup.select_one(".avatar")
        agent = avatar.find_next("b", class_="font-large") if avatar else None
        email_link = soup.select_one('a[href^="mailto:"]')
        return {
            "description_full": description,
            "address": location.get_text(" ", strip=True) if location else None,
            "latitude": float(coordinates.group(1)) if coordinates else None,
            "longitude": float(coordinates.group(2)) if coordinates else None,
            "seller_type": "agency",
            "seller_name": agent.get_text(" ", strip=True) if agent else None,
            # Listing-agent phone is JS-gated as "show number"; do not copy
            # the unrelated site-wide header telephone.
            "contact_phone": None,
            "contact_email": email_link.get("href", "")[7:] if email_link else None,
            "image_urls": images,
        }
    
    def _parse_price(self, text: str) -> tuple[Optional[float], Optional[float]]:
        """Parse price from text."""
        if not text:
            return None, None
        
        # Look for EUR amount - handle formats like "€ 185,000" or "175,000 €"
        # For discounted prices, take the last number (current price)
        matches = re.findall(r'€\s*([\d,\s]+)', text)
        if matches:
            # Take the last match (current price after discount)
            price_str = matches[-1].replace(',', '').replace(' ', '').strip()
            try:
                price_eur = float(price_str)
                return price_eur * EUR_BGN_RATE, price_eur
            except ValueError:
                pass
        
        # Alternative: number followed by EUR
        match = re.search(r'([\d,\s]+)\s*€', text)
        if match:
            price_str = match.group(1).replace(',', '').replace(' ', '').strip()
            try:
                price_eur = float(price_str)
                return price_eur * EUR_BGN_RATE, price_eur
            except ValueError:
                pass
        
        return None, None
    
    def _parse_listing_from_element(self, element) -> Optional[Dict[str, Any]]:
        """Parse a listing from a BeautifulSoup element."""
        try:
            # Get all text
            text = element.get_text(separator=' ', strip=True)
            
            # Find property link - try multiple patterns
            link = element.find('a', href=re.compile(r'/property-\d+'))
            if not link:
                # Try alternative pattern
                link = element.find('a', href=re.compile(r'/properties-for-sale/'))
            if not link:
                return None
            
            url = link.get('href', '')
            if url.startswith('/'):
                url = f"{self.BASE_URL}{url}"

            image = element.select_one('.prop_image_url[data-blazy]')
            image_src = image.get('data-blazy') if image else None
            image_url = urljoin(self.BASE_URL, image_src) if image_src else None
            
            # Extract ID from URL: /property-{ID}-description
            id_match = re.search(r'/property-(\d+)-', url)
            if not id_match:
                # Try alternative ID pattern
                id_match = re.search(r'/property/(\d+)/', url)
            if not id_match:
                return None
            source_id = id_match.group(1)
            
            # Extract property type from text
            property_type = 'apartment'
            text_lower = text.lower()
            if any(x in text_lower for x in ['studio', 'bedsit', 'ателие']):
                property_type = 'studio'
            elif any(x in text_lower for x in ['maisonette', 'townhouse', 'мезонет']):
                property_type = 'maisonette'
            elif any(x in text_lower for x in ['house', 'villa', 'къща', 'вила']):
                property_type = 'house'
            elif any(x in text_lower for x in ['plot', 'land', 'парцел', 'земя']):
                property_type = 'plot'
            elif any(x in text_lower for x in ['large apartment', 'penthouse']):
                property_type = 'apartment'
            
            # Extract rooms
            rooms = None
            room_match = re.search(r'(\d+)\s*bedroom', text, re.IGNORECASE)
            if room_match:
                rooms = int(room_match.group(1))
            
            if property_type == 'studio':
                rooms = 1
            
            # Extract area - pattern: "Area: XX sq.m" or "Building area: XX sq.m" or just "XX sq.m"
            area_match = re.search(r'(?:Area|Building area):?\s*([\d.]+)\s*sq\.?m', text, re.IGNORECASE)
            if not area_match:
                area_match = re.search(r'([\d.]+)\s*sq\.?m', text, re.IGNORECASE)
            area_sqm = float(area_match.group(1)) if area_match else None
            
            if not area_sqm:
                return None
            
            # Extract price - try to find price element first
            price_elem = element.find(class_=re.compile(r'price|цена', re.I))
            price_text = price_elem.get_text(strip=True) if price_elem else text
            
            price_bgn, price_eur = self._parse_price(price_text)
            
            if not price_eur:
                return None
            
            # Extract neighborhood - pattern: "Sofia / DISTRICT district"
            # Also handle Bulgarian: "София / РАЙОН"
            neighborhood = None
            
            # Try English pattern
            hood_match = re.search(r'Sofia\s*/\s*([^/]+?)(?:\s+district|\s+район|\s*$)', text, re.IGNORECASE)
            if hood_match:
                neighborhood = hood_match.group(1).strip()
            
            # Try Bulgarian pattern
            if not neighborhood:
                hood_match = re.search(r'София\s*/\s*([^/\d]+)', text)
                if hood_match:
                    neighborhood = hood_match.group(1).strip()
            
            if not neighborhood:
                return None
            
            # Clean up neighborhood name
            neighborhood = re.sub(r'\s+district$|\s+район$', '', neighborhood, flags=re.IGNORECASE).strip()
            
            # Extract floor info
            floor = None
            total_floors = None
            
            floor_match = re.search(r'Floor:\s*(\d+)', text, re.IGNORECASE)
            if floor_match:
                floor = int(floor_match.group(1))
            
            total_match = re.search(r'Number of floors:\s*(\d+)', text, re.IGNORECASE)
            if total_match:
                total_floors = int(total_match.group(1))
            
            # Construction type (if mentioned)
            construction_type = None
            if 'brick' in text_lower or 'тухла' in text_lower:
                construction_type = 'brick'
            elif 'panel' in text_lower or 'панел' in text_lower:
                construction_type = 'panel'
            elif 'epk' in text_lower or 'епк' in text_lower:
                construction_type = 'epk'
            
            return {
                'source': 'propertybg',
                'source_id': source_id,
                'url': url,
                'image_url': image_url,
                'title': text[:300] if len(text) > 300 else text,
                'price_bgn': price_bgn,
                'price_eur': price_eur,
                'area_sqm': area_sqm,
                'price_per_sqm_eur': round(price_eur / area_sqm, 2),
                'neighborhood': neighborhood,
                'property_type': property_type,
                'rooms': rooms,
                'floor': floor,
                'total_floors': total_floors,
                'construction_type': construction_type,
            }
        
        except Exception as e:
            logger.debug(f"Parse error: {e}")
            return None
    
    def scrape_page(self, page: int) -> List[Dict[str, Any]]:
        """Scrape a single page."""
        if page == 1:
            url = self.SEARCH_URL
        else:
            url = f"{self.SEARCH_URL}?page={page}"
        
        # Use UTF-8 encoding (site has been modernized)
        soup = self.fetch_page(url, encoding='utf-8')
        
        if not soup:
            # Fallback: try without explicit encoding
            soup = self.fetch_page(url)
        
        if not soup:
            return []
        
        listings = []
        seen_ids = set()
        
        # Try various container selectors
        selectors = [
            '.panel.offer',
            '.property-item',
            '.listing-item',
            '.property',
            'article.property',
            '.col-md-6 .property',
            '.col-lg-4 .property',
            'div[data-property-id]',
            '.property-card',
        ]
        
        for selector in selectors:
            elements = soup.select(selector)
            if elements:
                logger.debug(f"Using selector: {selector}, found {len(elements)} elements")
                for elem in elements:
                    listing = self._parse_listing_from_element(elem)
                    if listing and listing['source_id'] not in seen_ids:
                        seen_ids.add(listing['source_id'])
                        listings.append(listing)
                if listings:
                    break
        
        # Fallback: parse by finding property links
        if not listings:
            for link in soup.find_all('a', href=re.compile(r'/property-\d+|/properties-for-sale/')):
                url = link.get('href', '')
                if 'page=' in url:
                    continue
                    
                id_match = re.search(r'/property-(\d+)-', url)
                if not id_match:
                    id_match = re.search(r'/property/(\d+)/', url)
                if not id_match:
                    continue
                
                source_id = id_match.group(1)
                if source_id in seen_ids:
                    continue
                
                # Get parent container
                parent = link.find_parent(['div', 'article', 'li', 'section'])
                if parent:
                    listing = self._parse_listing_from_element(parent)
                    if listing:
                        seen_ids.add(listing['source_id'])
                        listings.append(listing)
        
        return listings
    
    def scrape(self) -> List[Dict[str, Any]]:
        """Scrape all pages."""
        all_listings = []
        seen_ids = set()
        
        logger.info(f"Scraping property.bg (up to {self.max_pages} pages)...")
        
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
            if not listings or not new_count:
                logger.info(f"No more listings at page {page}")
                break
            
            if page >= self.max_pages:
                break
        
        logger.info(f"Total from property.bg: {len(all_listings)} unique listings")
        return all_listings
