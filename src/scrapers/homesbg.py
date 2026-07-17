"""Scraper for homes.bg via the JSON API used by its Sofia search page."""

import json
import re
import time
import random
from typing import List, Dict, Any, Optional

import httpx
from loguru import logger

from src.config import EUR_BGN_RATE, SCRAPE_API_DELAY_MIN, SCRAPE_API_DELAY_MAX


class HomesBgScraper:
    """Scrape the site's Sofia sale or rental result set."""
    
    API_URL = "https://www.homes.bg/api/offers"
    BASE_URL = "https://www.homes.bg"
    RESULTS_PER_PAGE = 20
    SEARCH_PARAMS_BY_KIND = {
        "sale": {"typeId": "ApartmentSell", "locationId": "1"},
        "rent": {"typeId": "ApartmentRent", "locationId": "1"},
    }
    
    def __init__(self, max_pages: int = 50, deal_type: str = "sale"):
        if deal_type not in self.SEARCH_PARAMS_BY_KIND:
            raise ValueError("deal_type must be 'sale' or 'rent'")
        # Intentional sample: ~1,000 of ~11k listings keeps nightly runtime down.
        self.max_pages = max_pages
        self.listing_kind = deal_type
        self.source_name = "homesbg-rent" if deal_type == "rent" else "homesbg"

    @classmethod
    def parse_detail(cls, soup) -> Dict[str, Any]:
        """Parse the server-embedded offer JSON from a Homes.bg detail page.

        Live recon found no separate detail endpoint in the offer bundle; the
        SSR page's ``window.__PRELOADED_STATE__`` contains the complete offer.
        """
        script = next(
            (tag.get_text() for tag in soup.find_all("script") if "window.__PRELOADED_STATE__" in tag.get_text()),
            "",
        )
        match = re.search(r"window\.__PRELOADED_STATE__\s*=\s*(\{.*\})\s*;?", script, re.DOTALL)
        if not match:
            return {}
        try:
            offer = json.loads(match.group(1)).get("data", {}).get("offer", {})
        except (json.JSONDecodeError, TypeError):
            return {}
        if not offer:
            return {}

        attributes = {item.get("key"): item.get("value") for item in offer.get("attributes", [])}
        address_data = offer.get("address") or {}
        coordinates = address_data.get("coordinates") or []
        broker = (offer.get("contacts") or {}).get("broker") or {}
        agency = (offer.get("contacts") or {}).get("agency") or {}
        images = []
        for photo in offer.get("photos") or []:
            path = str(photo.get("path") or "").lstrip("/")
            name = str(photo.get("name") or "")
            if name:
                images.append(f"https://g1.homes.bg/{path}{name}o.jpg")

        # TIN-520: the offer JSON carries structured attributes we previously
        # discarded (floor='1-ви', total_floors, build_type='Тухла', ...).
        floor_match = re.search(r"\d+", str(attributes.get("floor") or ""))
        floor = int(floor_match.group()) if floor_match else (
            0 if "партер" in str(attributes.get("floor") or "").lower() else None
        )
        floors_match = re.search(r"\d+", str(attributes.get("total_floors") or ""))
        build_map = {"тухла": "brick", "панел": "panel", "епк": "epk"}
        heat_map = {"тец": "central", "електричество": "electric", "газ": "gas", "локално": "local"}
        # Order matters: "полуобзаведен"/"необзаведен" contain "обзаведен".
        furniture_raw = str(attributes.get("furniture") or "").lower()
        furnishing = None
        for marker, value in (("полуобзаведен", "partial"), ("необзаведен", "unfurnished"), ("обзаведен", "furnished")):
            if marker in furniture_raw:
                furnishing = value
                break
        extras = {str(extra.get("name") or "").lower() for extra in offer.get("extras") or []}
        parking = "garage" if "гараж" in extras else "parking_space" if "паркомясто" in extras else None

        return {
            "description_full": attributes.get("notes") or None,
            "floor": floor,
            "total_floors": int(floors_match.group()) if floors_match else None,
            "construction_type": build_map.get(str(attributes.get("build_type") or "").lower()),
            "heating": heat_map.get(str(attributes.get("heating") or "").lower()),
            "furnishing": furnishing,
            "has_elevator": True if "асансьор" in extras else None,
            "parking": parking,
            "address": ", ".join(
                str(value) for value in (address_data.get("city"),)
                if value not in (None, "", 0)
            ) or None,
            "latitude": coordinates[0] if len(coordinates) >= 2 else None,
            "longitude": coordinates[1] if len(coordinates) >= 2 else None,
            "seller_type": "agency" if agency.get("name") else "broker" if broker.get("name") else None,
            "seller_name": broker.get("name") or agency.get("name"),
            "contact_phone": broker.get("phone") or agency.get("phone"),
            "contact_email": None,
            "image_urls": images,
        }

    @classmethod
    def _request_params(cls, page: int, listing_kind: str = "sale") -> Dict[str, Any]:
        """Build the pagination query used by the Homes.bg infinite-scroll client."""
        start_index = (page - 1) * cls.RESULTS_PER_PAGE
        return {
            **cls.SEARCH_PARAMS_BY_KIND[listing_kind],
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
                'ar': 'apartment', # апартамент под наем
                'hr': 'house',     # къща под наем
                'pr': 'plot',      # парцел под наем
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
                'source': self.source_name,
                'source_id': str(item.get('id', '')),
                'listing_kind': self.listing_kind,
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

        # TIN-518: one client for the whole run (keep-alive instead of a new
        # TCP+TLS handshake per page) and the faster JSON-API cadence.
        client = httpx.Client(follow_redirects=True, timeout=30, headers=headers)
        page = 1
        while page <= self.max_pages:
            params = self._request_params(page, self.listing_kind)
            logger.info(
                f"Scraping homes.bg Sofia API page {page} "
                f"(results {params['startIndex']}-{params['stopIndex']})"
            )

            # Rate limit (JSON API tier)
            time.sleep(random.uniform(SCRAPE_API_DELAY_MIN, SCRAPE_API_DELAY_MAX))

            try:
                resp = client.get(self.API_URL, params=params)
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

        client.close()
        logger.info(
            f"Homes.bg API reported {api_total_count if api_total_count is not None else 'unknown'} "
            f"Sofia apartment listings; scraped {len(all_listings)}"
        )
        return all_listings
