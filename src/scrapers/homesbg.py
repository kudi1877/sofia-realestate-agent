"""Scraper for homes.bg via the JSON API used by its Sofia search page."""

import re
import time
import random
from typing import List, Dict, Any, Optional

import httpx
from loguru import logger

from src.config import EUR_BGN_RATE, SCRAPE_DELAY_MIN, SCRAPE_DELAY_MAX


class HomesBgScraper:
    """Scrape the site's Sofia apartment-for-sale result set."""
    
    API_URL = "https://www.homes.bg/api/offers"
    BASE_URL = "https://www.homes.bg"
    RESULTS_PER_PAGE = 20
    SEARCH_PARAMS = {
        "typeId": "ApartmentSell",
        "locationId": "1",  # Sofia in the site's search form.
    }
    
    def __init__(self, max_pages: int = 50):
        # Intentional sample: ~1,000 of ~11k listings keeps nightly runtime down.
        self.max_pages = max_pages
        self.source_name = "homesbg"

    @classmethod
    def _request_params(cls, page: int) -> Dict[str, Any]:
        """Build the pagination query used by the Homes.bg infinite-scroll client."""
        start_index = (page - 1) * cls.RESULTS_PER_PAGE
        return {
            **cls.SEARCH_PARAMS,
            "startIndex": start_index,
            "stopIndex": start_index + cls.RESULTS_PER_PAGE - 1,
        }

    @staticmethod
    def _api_total_count(data: Dict[str, Any]) -> Optional[int]:
        """Read the matching-offer count across known API response variants."""
        for key in ("offersCount", "totalCount", "count"):
            value = data.get(key)
            if isinstance(value, int):
                return value
        return None

    @classmethod
    def _api_is_exhausted(
        cls,
        data: Dict[str, Any],
        *,
        raw_result_count: int,
        stop_index: int,
    ) -> bool:
        """Decide exhaustion from API metadata, never from parsed Sofia matches."""
        if raw_result_count == 0 or data.get("hasMoreItems") is False:
            return True

        total_count = cls._api_total_count(data)
        return total_count is not None and stop_index + 1 >= total_count

    @staticmethod
    def _primary_image_url(item: Dict[str, Any]) -> Optional[str]:
        """Build the public thumbnail URL from Homes.bg API photo metadata."""
        photo = item.get('photo')
        if not isinstance(photo, dict):
            photos = item.get('photos') or []
            photo = photos[0] if photos and isinstance(photos[0], dict) else None
        if not photo:
            return None

        path = str(photo.get('path') or '').lstrip('/')
        name = str(photo.get('name') or '')
        return f"https://g1.homes.bg/{path}{name}b.jpg" if name else None
    
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
                'image_url': self._primary_image_url(item),
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
        """Scrape Sofia listings from the search API's indexed result window."""
        all_listings = []
        seen_ids = set()
        api_total_count = None
        
        headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
            'Accept': 'application/json',
            'Referer': 'https://www.homes.bg/',
        }
        
        page = 1
        while page <= self.max_pages:
            params = self._request_params(page)
            logger.info(
                f"Scraping homes.bg Sofia API page {page} "
                f"(results {params['startIndex']}-{params['stopIndex']})"
            )
            
            # Rate limit
            time.sleep(random.uniform(SCRAPE_DELAY_MIN, SCRAPE_DELAY_MAX))
            
            try:
                resp = httpx.get(
                    self.API_URL,
                    params=params,
                    headers=headers,
                    follow_redirects=True,
                    timeout=30,
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                logger.error(f"Failed to fetch homes.bg page {page}: {e}")
                break
            
            results = data.get('result') or []
            reported_total = self._api_total_count(data)
            if reported_total is not None:
                api_total_count = reported_total
            
            page_count = 0
            for item in results:
                listing = self._parse_listing(item)
                if listing and listing['source_id'] not in seen_ids:
                    seen_ids.add(listing['source_id'])
                    all_listings.append(listing)
                    page_count += 1
            
            logger.info(f"Page {page}: {page_count} Sofia listings (total: {len(all_listings)})")

            if results and page_count == 0:
                logger.warning(
                    f"Homes.bg page {page} returned {len(results)} API results but "
                    "none passed Sofia parsing; continuing according to API pagination"
                )

            if self._api_is_exhausted(
                data,
                raw_result_count=len(results),
                stop_index=params["stopIndex"],
            ):
                logger.info(
                    "Reached end of homes.bg API results "
                    f"(reported total: {api_total_count if api_total_count is not None else 'unknown'})"
                )
                break
            
            page += 1
        
        logger.info(
            f"Homes.bg API reported {api_total_count if api_total_count is not None else 'unknown'} "
            f"Sofia apartment listings; scraped {len(all_listings)}"
        )
        return all_listings
