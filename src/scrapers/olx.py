"""OLX Sofia sale scraper using the web application's observed JSON API.

Recon on 2026-07-12 found the search page hydration pointing to
``/api/v1/offers`` with category 381, Sofia-grad region 306, and Sofia city
8771. The endpoint returned structured prices, area, seller account type,
photos, property attributes, and a canonical ``links.next.href``. Ten windows
provide a useful FSBO sample while keeping this lower-priority source bounded.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List, Optional

import httpx
from bs4 import BeautifulSoup
from loguru import logger

from src.config import EUR_BGN_RATE, OLX_MAX_PAGES
from src.scrapers.base import BaseScraper


class OlxScraper(BaseScraper):
    BASE_URL = "https://www.olx.bg"
    API_URL = f"{BASE_URL}/api/v1/offers"
    SEARCH_PARAMS = {
        "offset": 0,
        "limit": 40,
        "category_id": 381,
        "region_id": 306,
        "city_id": 8771,
        "suggest_filters": "true",
        "return_filters": "true",
    }

    CATEGORY_TYPES = {
        524: "apartment",
        529: "plot",
    }

    def __init__(self, max_pages: int = OLX_MAX_PAGES, max_listings: int | None = None):
        super().__init__("olx", self.BASE_URL)
        self.max_pages = max_pages
        self.max_listings = max_listings
        self.skip_counts: Counter[str] = Counter()

    @staticmethod
    def _params(item: Dict[str, Any]) -> Dict[str, Any]:
        return {
            str(param.get("key")): param.get("value") or {}
            for param in item.get("params") or []
        }

    @staticmethod
    def _number(value: Any) -> Optional[float]:
        if isinstance(value, dict):
            value = value.get("key", value.get("value"))
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _property_type(cls, item: Dict[str, Any]) -> str:
        category_id = (item.get("category") or {}).get("id")
        if category_id in cls.CATEGORY_TYPES:
            return cls.CATEGORY_TYPES[category_id]
        title = str(item.get("title") or "").lower()
        if any(token in title for token in ("стаен", "апартамент", "мезонет", "ателие")):
            return "apartment"
        if any(token in title for token in ("къща", "вила")):
            return "house"
        if any(token in title for token in ("парцел", "земя", "упи")):
            return "plot"
        if "гараж" in title or "паркомясто" in title:
            return "garage"
        if "офис" in title:
            return "office"
        return "commercial"

    def parse_offer(self, item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        params = self._params(item)
        price_data = params.get("price") or {}
        price = self._number(price_data.get("value"))
        if not price or price <= 0:
            self.skip_counts["missing_price"] += 1
            return None
        currency = str(price_data.get("currency") or "EUR").upper()
        price_eur = price / EUR_BGN_RATE if currency == "BGN" else price
        price_bgn = price if currency == "BGN" else price * EUR_BGN_RATE

        area = self._number(params.get("space"))
        if not area or area <= 0:
            self.skip_counts["missing_area"] += 1
            return None

        location = item.get("location") or {}
        property_type = self._property_type(item)
        neighborhood = ((location.get("district") or {}).get("name") or "Unknown").strip()
        title_neighborhood = _title_neighborhood(str(item.get("title") or ""))
        if property_type in ("plot", "house") and title_neighborhood:
            neighborhood = title_neighborhood
        room_value = self._number(params.get("atype"))
        rooms = int(room_value) if room_value and property_type == "apartment" else None

        floor_data = params.get("floor") or {}
        floor_label = str(floor_data.get("label") or "").lower()
        floor = 0 if "партер" in floor_label else _first_int(floor_label)
        total_floors = _first_int(str((params.get("floors") or {}).get("label") or ""))

        construction_key = str((params.get("ctype") or {}).get("key") or "").lower()
        construction = {
            "tuhla": "brick",
            "panel": "panel",
            "epk": "epk",
        }.get(construction_key)
        year_value = self._number(params.get("cyear"))
        year_built = int(year_value) if year_value and 1900 <= year_value <= 2035 else None

        furnishing_key = str((params.get("furn") or {}).get("key") or "").lower()
        furnishing = {
            "obzaveden": "furnished",
            "poluobzaveden": "partial",
            "neobzaveden": "unfurnished",
        }.get(furnishing_key)
        heating_key = str((params.get("heat") or {}).get("key") or "").lower()
        heating = {
            "tec": "central",
            "gaz": "gas",
            "electricity": "electric",
            "local": "local",
        }.get(heating_key)

        photos = item.get("photos") or []
        image_url = None
        if photos and isinstance(photos[0], dict):
            template = str(photos[0].get("link") or "")
            image_url = template.replace("{width}", "640").replace("{height}", "480") or None

        description = BeautifulSoup(str(item.get("description") or ""), "html.parser").get_text(" ", strip=True)
        return {
            "source": "olx",
            "source_id": str(item.get("id") or ""),
            "listing_kind": "sale",
            "url": str(item.get("url") or ""),
            "image_url": image_url,
            "title": str(item.get("title") or neighborhood),
            "price_eur": round(price_eur, 2),
            "price_bgn": round(price_bgn, 2),
            "area_sqm": area,
            "price_per_sqm_eur": round(price_eur / area, 2),
            "neighborhood": neighborhood,
            "property_type": property_type,
            "rooms": rooms,
            "floor": floor,
            "total_floors": total_floors,
            "construction_type": construction,
            "year_built": year_built,
            "furnishing": furnishing,
            "heating": heating,
            "description": description[:1000] or None,
            "seller_type": "agency" if item.get("business") else "private",
        }

    def scrape(self) -> List[Dict[str, Any]]:
        if not self.session:
            self._init_session()
        url = self.API_URL
        params: Dict[str, Any] | None = dict(self.SEARCH_PARAMS)
        listings = []
        seen_ids = set()
        raw_count = 0
        for page in range(1, self.max_pages + 1):
            self._rate_limit()
            response = self.session.get(
                url,
                params=params,
                headers={**self._get_headers(), "Accept": "application/json"},
            )
            if response.status_code in (403, 429):
                logger.warning(f"OLX API blocked with HTTP {response.status_code}; stopping source")
                break
            response.raise_for_status()
            payload = response.json()
            offers = payload.get("data") or []
            raw_count += len(offers)
            for item in offers:
                listing = self.parse_offer(item)
                if not listing or listing["source_id"] in seen_ids:
                    continue
                seen_ids.add(listing["source_id"])
                listings.append(listing)
                if self.max_listings and len(listings) >= self.max_listings:
                    self._log_skip_rate(raw_count, len(listings))
                    return listings
            next_link = ((payload.get("links") or {}).get("next") or {}).get("href")
            if not next_link:
                break
            url = str(next_link)
            params = None
            logger.info(f"OLX page {page}: {len(listings)} parsed from {raw_count} raw offers")
        self._log_skip_rate(raw_count, len(listings))
        return listings

    def _log_skip_rate(self, raw_count: int, parsed_count: int) -> None:
        skipped = self.skip_counts["missing_price"] + self.skip_counts["missing_area"]
        rate = skipped / raw_count * 100 if raw_count else 0
        logger.info(
            f"OLX skip rate {rate:.1f}%: {self.skip_counts['missing_price']} missing price, "
            f"{self.skip_counts['missing_area']} missing area; {parsed_count} parsed"
        )


def _first_int(value: str) -> Optional[int]:
    import re

    match = re.search(r"\d+", value)
    return int(match.group()) if match else None


def _title_neighborhood(title: str) -> Optional[str]:
    import re

    matches = re.findall(
        r"(?:\bв\s+(?:с\.\s*)?|\bс\.\s*)([А-Я][а-я]+(?:\s+[А-Я][а-я]+|\s+\d+)?)",
        title,
    )
    return matches[-1].strip() if matches else None
