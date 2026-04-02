"""Scraper for imot.bg - Bulgaria's largest real estate portal.

URL pattern: https://www.imot.bg/obiavi/prodazhbi/grad-sofiya (33,775 Sofia listings)
Pagination: /p-2, /p-3, etc. (40 listings per page)
Encoding: windows-1251
Listing items: div.item.TOP or div.item.BEST
"""

import re
from typing import List, Dict, Any, Optional

from bs4 import BeautifulSoup
from loguru import logger

from src.scrapers.base import BaseScraper
from src.config import EUR_BGN_RATE


class ImotBgScraper(BaseScraper):
    """Scraper for imot.bg — Sofia listings only."""
    
    BASE_URL = "https://www.imot.bg"
    SOFIA_URL = "https://www.imot.bg/obiavi/prodazhbi/grad-sofiya"
    LISTINGS_PER_PAGE = 40
    
    # Scrape each property type separately to bypass 25-page limit
    PROPERTY_TYPE_SLUGS = [
        ('ednostaen', 'apartment', 1),
        ('dvustaen', 'apartment', 2),
        ('tristaen', 'apartment', 3),
        ('chetiristaen', 'apartment', 4),
        ('mnogostaen', 'apartment', 5),
        ('mezonet', 'maisonette', 4),
        ('kashta', 'house', None),
        ('vila', 'villa', None),
        ('partsel', 'plot', None),
        ('atelie-tavan', 'studio', 1),
        ('etazh-ot-kashta', 'house', None),
    ]
    
    def __init__(self, max_pages_per_type: int = 50):
        super().__init__("imotbg", self.BASE_URL)
        self.max_pages_per_type = max_pages_per_type
    
    def _parse_listing_item(self, item_div) -> Optional[Dict[str, Any]]:
        """Parse a single listing item div from the search results."""
        try:
            text = item_div.get_text(' ', strip=True)
            
            # Must be Sofia
            if 'София' not in text and 'софия' not in text.lower():
                return None
            
            # Extract URL from link (imot.bg uses class='saveSlink' or 'title saveSlink')
            link = item_div.find('a', class_='saveSlink')
            if not link:
                link = item_div.find('a', href=lambda h: h and 'obiava' in str(h))
            if not link:
                return None
            href = link.get('href', '')
            if href.startswith('//'):
                href = 'https:' + href
            
            # Extract source_id from URL
            id_match = re.search(r'obiava-(\w+)', href)
            if not id_match:
                # Try the saveSlink pattern
                id_match = re.search(r'/(\d+)(?:\?|$)', href)
            source_id = id_match.group(1) if id_match else href[-20:]
            
            # --- Price ---
            price_div = item_div.find('div', class_='price')
            price_text = price_div.get_text(strip=True) if price_div else ''
            # Clean spaces/nbsp
            price_clean = price_text.replace('\xa0', ' ').replace(',', '')
            
            eur_match = re.search(r'([\d\s]+)\s*€', price_clean)
            if eur_match:
                price_eur = float(eur_match.group(1).replace(' ', ''))
                price_bgn = price_eur * EUR_BGN_RATE
            else:
                bgn_match = re.search(r'([\d\s]+)\s*лв', price_clean)
                if bgn_match:
                    price_bgn = float(bgn_match.group(1).replace(' ', ''))
                    price_eur = price_bgn / EUR_BGN_RATE
                else:
                    return None  # No price = skip
            
            if price_eur <= 0:
                return None
            
            # --- Area ---
            sqm_match = re.search(r'(\d+(?:\.\d+)?)\s*кв\.?\s*м', text)
            area_sqm = float(sqm_match.group(1)) if sqm_match else 0.0
            if area_sqm <= 0:
                return None  # No area = skip
            
            price_per_sqm = price_eur / area_sqm
            
            # --- Neighborhood ---
            # Pattern: "град София, Кръстова вада 56 000 €"
            # Neighborhood is between "София," and the first digit sequence that's a price
            hood_match = re.search(r'град\s+София\s*,?\s*(.+?)(?:\s+\d{2,}[\s\d]*€|\s+\d{2,}[\s\d]*лв)', text)
            if hood_match:
                neighborhood = hood_match.group(1).strip()
                # Remove trailing numbers (phone fragments, prices)
                neighborhood = re.sub(r'\s+\d+$', '', neighborhood).strip()
                # Remove leading prefixes but keep the name
                neighborhood = re.sub(r'^(?:жк|кв|ж\.к|м-т)\.?\s*', '', neighborhood, flags=re.IGNORECASE).strip()
                # Remove trailing comma/dots
                neighborhood = neighborhood.rstrip('.,;: ')
            else:
                neighborhood = 'Unknown'
            
            # --- Property Type ---
            text_lower = text.lower()
            if 'къща' in text_lower:
                prop_type = 'house'
            elif 'парцел' in text_lower or 'земя' in text_lower:
                prop_type = 'plot'
            elif 'ателие' in text_lower:
                prop_type = 'studio'
            elif 'мезонет' in text_lower:
                prop_type = 'maisonette'
            else:
                prop_type = 'apartment'
            
            # --- Rooms ---
            rooms = None
            room_patterns = [
                (r'1-СТАЕН|едностаен', 1),
                (r'2-СТАЕН|двустаен', 2),
                (r'3-СТАЕН|тристаен', 3),
                (r'4-СТАЕН|четиристаен', 4),
                (r'МНОГОСТАЕН|многостаен', 5),
            ]
            for pattern, count in room_patterns:
                if re.search(pattern, text, re.IGNORECASE):
                    rooms = count
                    break
            
            # --- Floor ---
            floor = None
            total_floors = None
            if 'партер' in text_lower:
                floor = 0
                floor_total_match = re.search(r'от\s*(\d+)', text)
                total_floors = int(floor_total_match.group(1)) if floor_total_match else None
            else:
                floor_match = re.search(r'(\d+)(?:-ти|-ви|-ри)?\s*ет\.?\s*от\s*(\d+)', text)
                if floor_match:
                    floor = int(floor_match.group(1))
                    total_floors = int(floor_match.group(2))
            
            # --- Construction ---
            construction = None
            if 'тухла' in text_lower:
                construction = 'brick'
            elif 'панел' in text_lower:
                construction = 'panel'
            elif 'епк' in text_lower or 'ЕПК' in text:
                construction = 'epk'
            
            # --- Heating ---
            heating = None
            if 'тец' in text_lower or 'ТЕЦ' in text:
                heating = 'central'
            elif 'лок' in text_lower and 'отопл' in text_lower:
                heating = 'local'
            elif 'газ' in text_lower:
                heating = 'gas'
            elif 'електр' in text_lower:
                heating = 'electric'
            
            # --- Year ---
            year = None
            year_match = re.search(r'(?:експлоатация|Въведен в)\s*(\d{4})\s*г', text)
            if year_match:
                y = int(year_match.group(1))
                if 1950 <= y <= 2030:
                    year = y
            
            # --- Furnishing ---
            furnishing = None
            if 'обзаведен' in text_lower:
                if 'необзаведен' in text_lower:
                    furnishing = 'unfurnished'
                elif 'полу' in text_lower:
                    furnishing = 'partial'
                else:
                    furnishing = 'furnished'
            
            return {
                'source': 'imotbg',
                'source_id': source_id,
                'url': href,
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
                'year_built': year,
            }
            
        except Exception as e:
            logger.error(f"Error parsing imot.bg listing: {e}")
            return None
    
    def _scrape_url(self, base_url: str, seen_ids: set, prop_type_override: str = None, rooms_override: int = None) -> List[Dict[str, Any]]:
        """Scrape a single URL with pagination."""
        listings = []
        
        for page in range(1, self.max_pages_per_type + 1):
            url = base_url if page == 1 else f"{base_url}/p-{page}"
            
            logger.info(f"Scraping imot.bg: {url}")
            
            soup = self.fetch_page(url, encoding='windows-1251')
            if not soup:
                logger.warning(f"Failed to fetch {url}, stopping")
                break
            
            items = soup.find_all('div', class_=lambda c: c and 'item' in c and ('TOP' in c or 'BEST' in c))
            
            if not items:
                logger.info(f"No listings found, stopping")
                break
            
            page_count = 0
            for item in items:
                listing = self._parse_listing_item(item)
                if listing and listing['source_id'] not in seen_ids:
                    # Override type/rooms if scraping by type
                    if prop_type_override:
                        listing['property_type'] = prop_type_override
                    if rooms_override:
                        listing['rooms'] = rooms_override
                    seen_ids.add(listing['source_id'])
                    listings.append(listing)
                    page_count += 1
            
            logger.info(f"  → {page_count} new listings (total: {len(listings)})")
            
            next_link = soup.find('a', class_=lambda c: c and 'next' in c)
            if not next_link:
                break
        
        return listings
    
    def scrape(self) -> List[Dict[str, Any]]:
        """Scrape Sofia listings from imot.bg — by type to maximize coverage."""
        all_listings = []
        seen_ids = set()
        
        # Strategy: scrape each property type separately
        # Each type has its own 25-page limit, so we get more total
        for slug, prop_type, rooms in self.PROPERTY_TYPE_SLUGS:
            type_url = f"{self.SOFIA_URL}/{slug}"
            logger.info(f"=== Scraping {slug} ({prop_type}) ===")
            
            type_listings = self._scrape_url(type_url, seen_ids, prop_type, rooms)
            all_listings.extend(type_listings)
            
            logger.info(f"{slug}: {len(type_listings)} listings (cumulative: {len(all_listings)})")
        
        # Also scrape the main page for any we missed (mixed types)
        logger.info("=== Scraping main Sofia page for any missed ===")
        main_listings = self._scrape_url(self.SOFIA_URL, seen_ids)
        all_listings.extend(main_listings)
        
        logger.info(f"Total: scraped {len(all_listings)} unique Sofia listings from imot.bg")
        return all_listings
