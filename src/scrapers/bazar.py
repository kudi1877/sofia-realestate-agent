"""Bazar.bg Sofia sale scraper over server-rendered HTML.

Recon on 2026-07-12 found price/location/image in list cards but area only on
the server-rendered detail page. The default is therefore three search pages:
enough to sample this lower-priority portal without turning per-ad detail
requests into an unbounded nightly crawl.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag
from loguru import logger

from src.config import BAZAR_MAX_PAGES, EUR_BGN_RATE
from src.scrapers.base import BaseScraper


class BazarScraper(BaseScraper):
    BASE_URL = "https://bazar.bg"
    SEARCH_URL = f"{BASE_URL}/obiavi/prodazhba-imoti/sofia"

    def __init__(self, max_pages: int = BAZAR_MAX_PAGES, max_listings: int | None = None):
        super().__init__("bazar", self.BASE_URL)
        self.max_pages = max_pages
        self.max_listings = max_listings
        self.skip_counts: Counter[str] = Counter()

    @staticmethod
    def _price(card: Tag) -> Optional[float]:
        for node in card.select(".price"):
            match = re.search(r"([\d\s.,]+)\s*€", node.get_text(" ", strip=True))
            if match:
                return _localized_number(match.group(1))
        return None

    @staticmethod
    def _property(title: str) -> tuple[str, Optional[int]]:
        lower = title.lower()
        room_match = re.search(r"([1-5])\s*-?\s*стаен", lower)
        word_rooms = {"едностаен": 1, "двустаен": 2, "тристаен": 3, "четиристаен": 4}
        for word, rooms in word_rooms.items():
            if word in lower:
                return "apartment", rooms
        if room_match:
            return "apartment", int(room_match.group(1))
        if "мезонет" in lower:
            return "maisonette", 4
        if any(token in lower for token in ("къща", "вила")):
            return "house", None
        if any(token in lower for token in ("парцел", "земя", "упи")):
            return "plot", None
        if "гараж" in lower:
            return "garage", None
        if "офис" in lower:
            return "office", None
        return "commercial", None

    @staticmethod
    def _detail_value(soup: BeautifulSoup, label: str) -> Optional[str]:
        target = soup.find(
            lambda node: isinstance(node, Tag)
            and node.name in ("div", "span", "td")
            and node.get_text(" ", strip=True).rstrip(":") == label,
        )
        if not target:
            return None
        sibling = target.find_next_sibling()
        if sibling:
            return sibling.get_text(" ", strip=True)
        parent_text = target.parent.get_text(" ", strip=True) if target.parent else ""
        return parent_text.removeprefix(target.get_text(" ", strip=True)).strip() or None

    @staticmethod
    def _product_data(soup: BeautifulSoup) -> Dict[str, Any]:
        for script in soup.select('script[type="application/ld+json"]'):
            try:
                payload = json.loads(script.get_text())
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(payload, dict) and payload.get("@type") == "Product":
                return payload
        return {}

    def parse_card(self, card: Tag, detail: BeautifulSoup) -> Optional[Dict[str, Any]]:
        link = card.select_one("a.listItemLink[data-id]")
        if not link:
            return None
        price_eur = self._price(card)
        if not price_eur or price_eur <= 0:
            self.skip_counts["missing_price"] += 1
            return None
        area_text = self._detail_value(detail, "Квадратура") or ""
        area_match = re.search(r"([\d.,]+)", area_text)
        area = _localized_number(area_match.group(1)) if area_match else None
        if not area or area <= 0:
            self.skip_counts["missing_area"] += 1
            return None

        title = (link.get("title") or link.get_text(" ", strip=True)).strip()
        property_type, rooms = self._property(title)
        location = card.select_one(".location")
        neighborhood = re.sub(
            r"^гр\.\s*София\s*,?\s*",
            "",
            location.get_text(" ", strip=True) if location else "",
            flags=re.IGNORECASE,
        ) or "Unknown"
        image = card.select_one("img.cover")
        image_src = image.get("data-src") or image.get("src") if image else None
        product = self._product_data(detail)

        floor = _first_int(self._detail_value(detail, "Етаж") or "")
        total_floors = _first_int(self._detail_value(detail, "Етажност") or "")
        year_built = _first_int(self._detail_value(detail, "Година на строителство") or "")
        construction_text = (self._detail_value(detail, "Вид строителство") or "").lower()
        construction = (
            "brick" if "тух" in construction_text
            else "panel" if "панел" in construction_text
            else "epk" if "епк" in construction_text
            else None
        )
        return {
            "source": "bazar",
            "source_id": str(link.get("data-id")),
            "listing_kind": "sale",
            "url": urljoin(self.BASE_URL, str(link.get("href") or "")),
            "image_url": urljoin(self.BASE_URL, image_src) if image_src else None,
            "title": title,
            "price_eur": round(price_eur, 2),
            "price_bgn": round(price_eur * EUR_BGN_RATE, 2),
            "area_sqm": area,
            "price_per_sqm_eur": round(price_eur / area, 2),
            "neighborhood": neighborhood,
            "property_type": property_type,
            "rooms": rooms,
            "floor": floor,
            "total_floors": total_floors,
            "construction_type": construction,
            "year_built": year_built if year_built and 1900 <= year_built <= 2035 else None,
            "description": str(product.get("description") or "")[:1000] or None,
        }

    def scrape(self) -> List[Dict[str, Any]]:
        url = self.SEARCH_URL
        listings = []
        seen_ids = set()
        raw_count = 0
        for page in range(1, self.max_pages + 1):
            soup = self.fetch_page(url)
            if not soup:
                break
            cards = soup.select(".listItemContainer")
            raw_count += len(cards)
            for card in cards:
                link = card.select_one("a.listItemLink[data-id]")
                source_id = str(link.get("data-id")) if link else ""
                if not link or source_id in seen_ids:
                    continue
                if self._price(card) is None:
                    self.skip_counts["missing_price"] += 1
                    continue
                detail = self.fetch_page(urljoin(self.BASE_URL, str(link.get("href"))))
                if not detail:
                    continue
                listing = self.parse_card(card, detail)
                if not listing:
                    continue
                seen_ids.add(source_id)
                listings.append(listing)
                if self.max_listings and len(listings) >= self.max_listings:
                    self._log_skip_rate(raw_count, len(listings))
                    return listings
            next_url = _next_link(soup, self.BASE_URL)
            if not next_url:
                break
            url = next_url
            logger.info(f"Bazar page {page}: {len(listings)} parsed from {raw_count} cards")
        self._log_skip_rate(raw_count, len(listings))
        return listings

    def _log_skip_rate(self, raw_count: int, parsed_count: int) -> None:
        skipped = self.skip_counts["missing_price"] + self.skip_counts["missing_area"]
        logger.info(
            f"Bazar skip rate {(skipped / raw_count * 100 if raw_count else 0):.1f}%: "
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


def _next_link(soup: BeautifulSoup, base_url: str) -> Optional[str]:
    link = soup.select_one('link[rel="next"]') or soup.select_one('a[rel="next"]')
    if not link:
        link = soup.find("a", string=re.compile(r"Следващ", re.IGNORECASE))
    return urljoin(base_url, str(link.get("href"))) if link and link.get("href") else None
