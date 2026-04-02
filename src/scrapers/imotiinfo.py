"""Scraper for imoti.info — uses their internal JSON API.

The site embeds structured JSON data in the page from their internal API.
URL: https://imoti.info/prodazhbi/grad-sofiya/page-N
Pagination: 20 results per page, ~33K total Sofia listings.
Data is clean JSON with: id, url, price, currency, pubtypetxt, nraions, floor, tbuild, ybuild, summary.
"""

import re
import json
from typing import List, Dict, Any, Optional

from bs4 import BeautifulSoup
from loguru import logger

from src.scrapers.base import BaseScraper
from src.config import EUR_BGN_RATE


class ImotiInfoScraper(BaseScraper):
    """Scraper for imoti.info — Sofia listings via embedded JSON."""
    
    BASE_URL = "https://imoti.info"
    SOFIA_URL = "https://imoti.info/prodazhbi/grad-sofiya"
    RESULTS_PER_PAGE = 20
    
    # Property type slugs for scraping by type (to get more coverage)
    PROPERTY_TYPE_URLS = [
        ('ednostaini', 'apartment', 1),
        ('dvustaini', 'apartment', 2),
        ('tristaini', 'apartment', 3),
        ('chetiristaini', 'apartment', 4),
        ('mnogostaini', 'apartment', 5),
        ('mezoneti', 'maisonette', 4),
        ('kashti', 'house', None),
        ('vili', 'villa', None),
        ('atelieta-tavani', 'studio', 1),
        ('etazhi-ot-kashta', 'house', None),
    ]
    
    def __init__(self, max_pages_per_type: int = 25):
        super().__init__("imotiinfo", self.BASE_URL)
        self.max_pages_per_type = max_pages_per_type
    
    def _extract_json_data(self, soup: BeautifulSoup) -> Optional[dict]:
        """Extract the embedded JSON data from the page's script tags."""
        # The data is in a script tag containing "ads-results-slug=" or "results"
        for script in soup.find_all('script'):
            text = script.string or ''
            # Look for the JSON payload with results array
            if '"results":[' in text and '"count"' in text:
                try:
                    # The whole page state is JSON — find the relevant chunk
                    # Look for the ads-results pattern
                    data = json.loads(text)
                    
                    # Navigate the nested structure to find results
                    for key, val in data.items():
                        if isinstance(val, dict) and 'results' in val:
                            return val
                    return data
                except json.JSONDecodeError:
                    pass
        
        # Alternative: try to find JSON in data attributes or inline scripts
        for script in soup.find_all('script'):
            text = script.string or ''
            if 'ads-results-slug' in text or '"count":"' in text:
                # Extract the JSON object containing results
                match = re.search(r'"ads-results-slug[^"]*":\s*(\{[^}]*"results":\[.+?\]\s*(?:,\s*"[^"]*":\s*(?:\{[^}]*\}|"[^"]*"|null)\s*)*\})', text, re.DOTALL)
                if match:
                    try:
                        return json.loads(match.group(1))
                    except json.JSONDecodeError:
                        pass
                
                # Try to parse the whole thing as JSON
                try:
                    data = json.loads(text)
                    # Find results in nested structure  
                    if isinstance(data, dict):
                        for key, val in data.items():
                            if isinstance(val, dict) and 'results' in val and isinstance(val['results'], list):
                                return val
                except (json.JSONDecodeError, TypeError):
                    pass
        
        return None
    
    def _parse_listing(self, item: dict) -> Optional[Dict[str, Any]]:
        """Parse a single listing from the JSON data."""
        try:
            # Source ID from URL (e.g., /obiava/57793465/prodava-1-staen-grad-sofiya-banishora)
            url_path = item.get('url', '')
            id_match = re.search(r'/obiava/(\d+)', url_path)
            source_id = id_match.group(1) if id_match else item.get('id', str(hash(url_path)))
            
            full_url = f"{self.BASE_URL}{url_path}" if url_path.startswith('/') else url_path
            
            # Price (handle non-breaking spaces \xa0)
            price_str = (item.get('price') or '0').replace('\xa0', '').replace(' ', '').replace(',', '')
            currency = item.get('currency', '€')
            
            try:
                price = float(price_str)
            except ValueError:
                return None
            
            if currency == '€' or currency == 'EUR':
                price_eur = price
                price_bgn = price * EUR_BGN_RATE
            elif currency == 'лв' or currency == 'лв.' or currency == 'BGN':
                price_bgn = price
                price_eur = price / EUR_BGN_RATE
            else:
                price_eur = price
                price_bgn = price * EUR_BGN_RATE
            
            if price_eur <= 0:
                return None
            
            # Neighborhood from nraionsMob (e.g., "Банишора, град София" → "Банишора")
            location = item.get('nraionsMob') or item.get('nraions') or ''
            neighborhood = re.sub(r',?\s*град\s+София\s*$', '', location).strip()
            neighborhood = re.sub(r'^град\s+София\s*,?\s*', '', neighborhood).strip()
            if not neighborhood:
                neighborhood = 'Unknown'
            
            # Area from summary (e.g., "40 кв.м, 1980 г., ЕПК, 11-ти ет. ")
            summary = item.get('summary', '')
            area_match = re.search(r'(\d+(?:\.\d+)?)\s*кв\.?\s*м', summary)
            area_sqm = float(area_match.group(1)) if area_match else 0.0
            
            if area_sqm <= 0:
                return None
            
            price_per_sqm = price_eur / area_sqm
            
            # Property type from pubtypetxt
            pubtypetxt = (item.get('pubtypetxt') or '').lower()
            type_map = {
                '1-стаен': ('apartment', 1),
                '2-стаен': ('apartment', 2),
                '3-стаен': ('apartment', 3),
                '4-стаен': ('apartment', 4),
                'многостаен': ('apartment', 5),
                'мезонет': ('maisonette', 4),
                'къща': ('house', None),
                'вила': ('villa', None),
                'ателие': ('studio', 1),
                'таван': ('studio', 1),
                'етаж от къща': ('house', None),
                'парцел': ('plot', None),
                'земеделска': ('plot', None),
                'офис': ('office', None),
                'магазин': ('commercial', None),
                'гараж': ('garage', None),
            }
            
            prop_type = 'apartment'
            rooms = None
            for key, (ptype, prooms) in type_map.items():
                if key in pubtypetxt:
                    prop_type = ptype
                    rooms = prooms
                    break
            
            # Floor from floor field or summary
            floor_text = item.get('floor', '') or ''
            floor = None
            total_floors = None
            
            if 'партер' in floor_text.lower():
                floor = 0
            else:
                floor_match = re.search(r'(\d+)', floor_text)
                if floor_match:
                    floor = int(floor_match.group(1))
            
            # Total floors from summary if present
            total_floors_match = re.search(r'от\s*(\d+)', floor_text)
            if total_floors_match:
                total_floors = int(total_floors_match.group(1))
            
            # Construction type from tbuild
            tbuild = (item.get('tbuild') or '').lower()
            construction = None
            if 'тухла' in tbuild:
                construction = 'brick'
            elif 'панел' in tbuild:
                construction = 'panel'
            elif 'епк' in tbuild:
                construction = 'epk'
            
            # Year built
            year_built = item.get('ybuild')
            if year_built and isinstance(year_built, (int, float)):
                year_built = int(year_built)
                if not (1900 <= year_built <= 2030):
                    year_built = None
            else:
                year_built = None
            
            # Furnishing from info text
            info = (item.get('info') or '').lower()
            furnishing = None
            if 'необзаведен' in info:
                furnishing = 'unfurnished'
            elif 'полуобзаведен' in info:
                furnishing = 'partial'
            elif 'обзаведен' in info:
                furnishing = 'furnished'
            
            # Heating from info
            heating = None
            if 'тец' in info:
                heating = 'central'
            elif 'локално' in info or 'лок.' in info:
                heating = 'local'
            elif 'газ' in info:
                heating = 'gas'
            elif 'електричество' in info or 'електр' in info:
                heating = 'electric'
            
            return {
                'source': 'imotiinfo',
                'source_id': source_id,
                'url': full_url,
                'title': f'{neighborhood}, София',
                'price_eur': round(price_eur, 2),
                'price_bgn': round(price_bgn, 2),
                'area_sqm': area_sqm,
                'price_per_sqm_eur': round(price_per_sqm, 2),
                'neighborhood': neighborhood,
                'property_type': prop_type,
                'rooms': rooms,
                'floor': floor,
                'total_floors': total_floors,
                'construction_type': construction,
                'heating': heating,
                'furnishing': furnishing,
                'year_built': year_built,
            }
            
        except Exception as e:
            logger.error(f"Error parsing imoti.info listing {item.get('id')}: {e}")
            return None
    
    def _scrape_url(self, base_url: str, seen_ids: set, prop_type_override: str = None, rooms_override: int = None) -> List[Dict[str, Any]]:
        """Scrape a URL with pagination."""
        listings = []
        
        for page in range(1, self.max_pages_per_type + 1):
            url = base_url if page == 1 else f"{base_url}/page-{page}"
            
            logger.info(f"Scraping imoti.info: {url}")
            
            soup = self.fetch_page(url)
            if not soup:
                logger.warning(f"Failed to fetch {url}, stopping")
                break
            
            data = self._extract_json_data(soup)
            if not data or 'results' not in data:
                logger.warning(f"No JSON data found on page {page}, stopping")
                break
            
            results = data['results']
            if not results:
                logger.info(f"No results on page {page}, stopping")
                break
            
            page_count = 0
            for item in results:
                listing = self._parse_listing(item)
                if listing and listing['source_id'] not in seen_ids:
                    if prop_type_override:
                        listing['property_type'] = prop_type_override
                    if rooms_override:
                        listing['rooms'] = rooms_override
                    seen_ids.add(listing['source_id'])
                    listings.append(listing)
                    page_count += 1
            
            logger.info(f"  → {page_count} new listings (total: {len(listings)})")
            
            # Check if there are more pages
            total = int(data.get('count', 0))
            if page * self.RESULTS_PER_PAGE >= total:
                logger.info(f"Reached end of results ({total} total)")
                break
        
        return listings
    
    def scrape(self) -> List[Dict[str, Any]]:
        """Scrape Sofia listings from imoti.info by property type."""
        all_listings = []
        seen_ids = set()
        
        # Scrape by property type for better coverage
        for slug, prop_type, rooms in self.PROPERTY_TYPE_URLS:
            type_url = f"{self.SOFIA_URL}/{slug}"
            logger.info(f"=== Scraping imoti.info: {slug} ({prop_type}) ===")
            
            type_listings = self._scrape_url(type_url, seen_ids, prop_type, rooms)
            all_listings.extend(type_listings)
            
            logger.info(f"{slug}: {len(type_listings)} listings (cumulative: {len(all_listings)})")
        
        logger.info(f"Total: scraped {len(all_listings)} unique Sofia listings from imoti.info")
        return all_listings
