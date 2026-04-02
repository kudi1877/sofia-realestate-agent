"""Scraper for homes.bg via their JSON API.

API: https://www.homes.bg/api/offers?page=N (20 results/page, 11K+ total)
No HTML parsing needed — clean structured JSON.

Pagination Fix: The API uses 'from' parameter for offset-based pagination
instead of 'page'. We calculate offset as (page-1) * 20.
"""

import re
import time
import random
from typing import List, Dict, Any, Optional

import httpx
from loguru import logger

from src.config import EUR_BGN_RATE, SCRAPE_DELAY_MIN, SCRAPE_DELAY_MAX


class HomesBgScraper:
    """Scraper for homes.bg using their JSON API with fixed pagination."""
    
    API_URL = "https://www.homes.bg/api/offers"
    BASE_URL = "https://www.homes.bg"
    RESULTS_PER_PAGE = 20
    
    def __init__(self, max_pages: int = 50):
        self.max_pages = max_pages  # 50 pages = 1000 listings
        self.source_name = "homesbg"
    
    def _parse_listing(self, item: dict) -> Optional[Dict[str, Any]]:
        """Parse a single listing from the API response."""
        try:
            location = (item.get('location') or '').strip()
            
            # Filter for Sofia only (handles "София", "София - град", etc.)
            if 'София' not in location and 'sofia' not in location.lower():
                return None
            
            # Extract neighborhood from location ("Център, София" → "Център")
            neighborhood = re.sub(r',?\s*София(?:\s*-\s*\w+)?$', '', location).strip()
            neighborhood = re.sub(r'^(?:жк|кв|ж\.к|м-т)\.?\s*', '', neighborhood, flags=re.IGNORECASE).strip()
            if not neighborhood:
                neighborhood = 'Unknown'
            
            # Parse price
            price_data = item.get('price', {})
            price_str = (price_data.get('value') or '0').replace(',', '')
            currency = price_data.get('currency', 'EUR')
            
            try:
                price = float(price_str)
            except ValueError:
                return None
            
            if currency == 'EUR':
                price_eur = price
                price_bgn = price * EUR_BGN_RATE
            elif currency == 'BGN':
                price_bgn = price
                price_eur = price / EUR_BGN_RATE
            else:
                price_eur = price
                price_bgn = price * EUR_BGN_RATE
            
            if price_eur <= 0:
                return None
            
            # Parse price per sqm
            psqm_str = price_data.get('price_per_square_meter', '')
            psqm_match = re.search(r'([\d,]+)', psqm_str)
            price_per_sqm = float(psqm_match.group(1).replace(',', '')) if psqm_match else 0.0
            
            # Parse title for room count and area
            title = item.get('title', '')
            
            # Area from title: "Тристаен, 109m²"
            area_match = re.search(r'(\d+(?:\.\d+)?)\s*m[²2]', title, re.IGNORECASE)
            area_sqm = float(area_match.group(1)) if area_match else 0.0
            
            if area_sqm <= 0:
                return None
            
            # Recalculate price_per_sqm if we have area
            if price_per_sqm == 0 and area_sqm > 0:
                price_per_sqm = price_eur / area_sqm
            
            # Room count from title
            rooms = None
            room_map = {
                'едностаен': 1, 'двустаен': 2, 'тристаен': 3,
                'четиристаен': 4, 'многостаен': 5,
                'студио': 1, 'мезонет': 4, 'ателие': 1,
            }
            title_lower = title.lower()
            for word, count in room_map.items():
                if word in title_lower:
                    rooms = count
                    break
            
            # Property type from URL type code
            type_code = item.get('type', 'as')
            type_map = {
                'as': 'apartment',  # апартамент за продажба
                'hs': 'house',     # къща за продажба
                'ps': 'plot',      # парцел за продажба
            }
            prop_type = type_map.get(type_code, 'apartment')
            
            # Description parsing
            desc = item.get('description', '')
            
            # Construction type
            construction = None
            desc_lower = desc.lower()
            if 'тухла' in desc_lower:
                construction = 'brick'
            elif 'панел' in desc_lower:
                construction = 'panel'
            elif 'епк' in desc_lower:
                construction = 'epk'
            
            # Furnishing
            furnishing = None
            if 'полуобзаведен' in desc_lower:
                furnishing = 'partial'
            elif 'необзаведен' in desc_lower:
                furnishing = 'unfurnished'
            elif 'обзаведен' in desc_lower:
                furnishing = 'furnished'
            
            # Heating
            heating = None
            if 'тец' in desc_lower or 'ТЕЦ' in desc:
                heating = 'central'
            elif 'локално' in desc_lower:
                heating = 'local'
            elif 'газ' in desc_lower:
                heating = 'gas'
            elif 'електричество' in desc_lower:
                heating = 'electric'
            
            return {
                'source': 'homesbg',
                'source_id': str(item.get('id', '')),
                'url': f"{self.BASE_URL}{item.get('viewHref', '')}",
                'title': f"{neighborhood}, София",
                'price_eur': round(price_eur, 2),
                'price_bgn': round(price_bgn, 2),
                'area_sqm': area_sqm,
                'price_per_sqm_eur': round(price_per_sqm, 2),
                'neighborhood': neighborhood,
                'property_type': prop_type,
                'rooms': rooms,
                'floor': None,
                'total_floors': None,
                'construction_type': construction,
                'furnishing': furnishing,
                'heating': heating,
                'year_built': None,
            }
            
        except Exception as e:
            logger.error(f"Error parsing homes.bg listing {item.get('id')}: {e}")
            return None
    
    def scrape(self) -> List[Dict[str, Any]]:
        """Scrape Sofia listings from homes.bg API with fixed offset-based pagination."""
        all_listings = []
        seen_ids = set()
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
            'Accept': 'application/json',
            'Referer': 'https://www.homes.bg/',
        }
        
        page = 1
        consecutive_empty = 0
        max_consecutive_empty = 3
        
        while page <= self.max_pages and consecutive_empty < max_consecutive_empty:
            # Use offset-based pagination instead of page number
            # API uses 'from' parameter for offset
            offset = (page - 1) * self.RESULTS_PER_PAGE
            url = f"{self.API_URL}?from={offset}"
            
            logger.info(f"Scraping homes.bg API page {page} (offset={offset}): {url}")
            
            # Rate limit
            time.sleep(random.uniform(SCRAPE_DELAY_MIN, SCRAPE_DELAY_MAX))
            
            try:
                resp = httpx.get(url, headers=headers, follow_redirects=True, timeout=30)
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                logger.error(f"Failed to fetch homes.bg page {page}: {e}")
                break
            
            results = data.get('result', [])
            if not results:
                logger.info(f"No results on page {page}, consecutive_empty={consecutive_empty+1}")
                consecutive_empty += 1
                page += 1
                continue
            
            page_count = 0
            for item in results:
                listing = self._parse_listing(item)
                if listing and listing['source_id'] not in seen_ids:
                    seen_ids.add(listing['source_id'])
                    all_listings.append(listing)
                    page_count += 1
            
            logger.info(f"Page {page}: {page_count} Sofia listings (total: {len(all_listings)})")
            
            if page_count == 0:
                consecutive_empty += 1
            else:
                consecutive_empty = 0
            
            # Check if more pages available - use total count if available
            total_count = data.get('totalCount') or data.get('count', 0)
            if total_count and offset + len(results) >= total_count:
                logger.info(f"Reached end of results ({total_count} total)")
                break
            
            # Alternative: check hasMoreItems flag
            if not data.get('hasMoreItems', True) and page_count == 0:
                logger.info("No more pages available (hasMoreItems=false)")
                break
            
            page += 1
        
        logger.info(f"Total: scraped {len(all_listings)} Sofia listings from homes.bg")
        return all_listings
