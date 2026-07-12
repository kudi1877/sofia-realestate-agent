"""ALO.bg Sofia-city apartment scraper over server-rendered result cards.

Recon on 2026-07-12 found that ``region_id=22`` alone includes the broader
Sofia region. The site's location-filter XHR identifies ``location_ids=4342``
as град София, so both parameters are required. Cards include all required
price/area fields; ten pages are bounded and need no detail-page requests.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Dict, List, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag
from loguru import logger

from src.config import ALO_MAX_PAGES, EUR_BGN_RATE
from src.scrapers.base import BaseScraper


class AloScraper(BaseScraper):
    BASE_URL = "https://www.alo.bg"
    SEARCH_URL = (
        f"{BASE_URL}/obiavi/imoti-prodajbi/apartamenti-stai/"
        "?region_id=22&location_ids=4342"
    )

    def __init__(self, max_pages: int = ALO_MAX_PAGES, max_listings: int | None = None):
        super().__init__("alo", self.BASE_URL)
        self.max_pages = max_pages
        self.max_listings = max_listings
        self.skip_counts: Counter[str] = Counter()

    @staticmethod
    def _parameters(card: Tag) -> Dict[str, str]:
        params = {}
        for row in card.select(".ads-params-row"):
            title = row.select_one(".ads-param-title")
            value = row.select_one(".ads-params-cell")
            if title and value:
                params[title.get_text(" ", strip=True).rstrip(":").lower()] = value.get_text(" ", strip=True)
        return params

    def parse_card(self, card: Tag) -> Optional[Dict[str, object]]:
        source_match = re.search(r"(\d+)$", str(card.get("id") or ""))
        link = card.select_one('a[href*="-"]')
        title_node = card.select_one("h3")
        if not source_match or not link or not title_node:
            return None
        params = self._parameters(card)
        price_text = params.get("цена") or card.get_text(" ", strip=True)
        price_match = re.search(r"([\d\s.,]+)\s*€", price_text)
        price_eur = _localized_number(price_match.group(1)) if price_match else None
        if not price_eur or price_eur <= 0:
            self.skip_counts["missing_price"] += 1
            return None

        area_text = next((value for key, value in params.items() if "квадратура" in key), "")
        area_match = re.search(r"([\d.,]+)", area_text)
        area = _localized_number(area_match.group(1)) if area_match else None
        if not area or area <= 0:
            self.skip_counts["missing_area"] += 1
            return None

        title = title_node.get_text(" ", strip=True)
        rooms = _rooms(title)
        address = card.select_one('[class*="item-address"]')
        location = address.get_text(" ", strip=True) if address else ""
        neighborhood = re.sub(r",?\s*София\s*$", "", location, flags=re.IGNORECASE).strip() or "Unknown"
        image = card.select_one('img[class*="image-img"]') or card.select_one('img[loading="lazy"]')
        image_src = image.get("src") if image else None

        floor_text = next((value for key, value in params.items() if key == "етаж"), "")
        floor = 0 if "партер" in floor_text.lower() else _first_int(floor_text)
        total_text = next((value for key, value in params.items() if "етажност" in key), "")
        total_floors = _first_int(total_text)
        year_text = next((value for key, value in params.items() if "година" in key), "")
        year_built = _first_int(year_text)
        construction_text = next((value for key, value in params.items() if "строителство" in key), "").lower()
        construction = (
            "brick" if "тух" in construction_text
            else "panel" if "панел" in construction_text
            else "epk" if "епк" in construction_text
            else None
        )
        publisher = card.select_one('[class*="publisher"]')
        seller_type = None
        if publisher:
            publisher_text = publisher.get_text(" ", strip=True).lower()
            if "частно лице" in publisher_text or "собственик" in publisher_text:
                seller_type = "private"
            elif publisher.select_one('img[class*="logo"]'):
                seller_type = "agency"
        description = card.select_one('[class*="desc"]')
        direct_text = f"{title} {description.get_text(' ', strip=True) if description else ''}".lower()
        if seller_type is None and "собственик" in direct_text:
            seller_type = "private"
        return {
            "source": "alo",
            "source_id": source_match.group(1),
            "listing_kind": "sale",
            "url": urljoin(self.BASE_URL, str(link.get("href") or "")),
            "image_url": urljoin(self.BASE_URL + "/", image_src) if image_src else None,
            "title": title,
            "price_eur": round(price_eur, 2),
            "price_bgn": round(price_eur * EUR_BGN_RATE, 2),
            "area_sqm": area,
            "price_per_sqm_eur": round(price_eur / area, 2),
            "neighborhood": neighborhood,
            "property_type": "apartment",
            "rooms": rooms,
            "floor": floor,
            "total_floors": total_floors,
            "construction_type": construction,
            "year_built": year_built if year_built and 1900 <= year_built <= 2035 else None,
            "description": description.get_text(" ", strip=True)[:1000] if description else None,
            "seller_type": seller_type,
        }

    def scrape(self) -> List[Dict[str, object]]:
        url = self.SEARCH_URL
        listings = []
        seen_ids = set()
        raw_count = 0
        for page in range(1, self.max_pages + 1):
            soup = self.fetch_page(url)
            if not soup:
                break
            cards = soup.select('[id^="adrows_"]')
            raw_count += len(cards)
            for card in cards:
                listing = self.parse_card(card)
                if not listing or listing["source_id"] in seen_ids:
                    continue
                seen_ids.add(listing["source_id"])
                listings.append(listing)
                if self.max_listings and len(listings) >= self.max_listings:
                    self._log_skip_rate(raw_count, len(listings))
                    return listings
            next_link = soup.select_one('link[rel="next"]') or soup.select_one('a[rel="next"]')
            if not next_link or not next_link.get("href"):
                break
            url = urljoin(self.BASE_URL, str(next_link.get("href")))
            logger.info(f"ALO page {page}: {len(listings)} parsed from {raw_count} cards")
        self._log_skip_rate(raw_count, len(listings))
        return listings

    def _log_skip_rate(self, raw_count: int, parsed_count: int) -> None:
        skipped = self.skip_counts["missing_price"] + self.skip_counts["missing_area"]
        logger.info(
            f"ALO skip rate {(skipped / raw_count * 100 if raw_count else 0):.1f}%: "
            f"{self.skip_counts['missing_price']} missing price, "
            f"{self.skip_counts['missing_area']} missing area; {parsed_count} parsed"
        )


def _localized_number(value: str) -> Optional[float]:
    cleaned = value.replace("\xa0", "").replace(" ", "").strip()
    if "," in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _first_int(value: str) -> Optional[int]:
    match = re.search(r"\d+", value)
    return int(match.group()) if match else None


def _rooms(title: str) -> Optional[int]:
    lower = title.lower()
    match = re.search(r"([1-5])\s*-?\s*стаен", lower)
    if match:
        return int(match.group(1))
    for word, rooms in {
        "едностаен": 1,
        "двустаен": 2,
        "тристаен": 3,
        "четиристаен": 4,
        "многостаен": 5,
    }.items():
        if word in lower:
            return rooms
    return None
