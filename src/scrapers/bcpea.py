"""Scraper for Sofia-city residential ЧСИ auctions on sales.bcpea.org.

Live recon on 2026-07-12 found a server-rendered HTML search, not JSON/XHR:
``GET /properties?court=28&perpage=12&p=N`` returns complete auction cards and
ordinary pagination. Cards expose the opening price, address, bailiff, area,
and bidding window. Case numbers and precise quarters are not structured;
this scraper best-effort enriches the newest bounded set from each HTML detail
page, while scanned-only values remain nullable.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from loguru import logger

from src.config import BCPEA_MAX_DETAIL_FETCHES, EUR_BGN_RATE, SOFIA_NEIGHBORHOODS
from src.scrapers.base import BaseScraper
from src.utils.neighborhoods import canonicalize_neighborhood
from src.utils.time import utc_now


RESIDENTIAL_CATEGORIES = {
    "Едностаен апартамент": ("apartment", 1),
    "Двустаен апартамент": ("apartment", 2),
    "Тристаен апартамент": ("apartment", 3),
    "Многостаен апартамент": ("apartment", 4),
    "Мезонет": ("maisonette", 4),
    "Ателие, Таван": ("studio", 1),
    "Вила": ("villa", None),
    "Етаж от къща": ("house", None),
    "Къща": ("house", None),
    "Стая": ("apartment", 1),
    "Жилищна сграда": ("house", None),
    "Парцел с къща": ("house", None),
    "Къща с парцел": ("house", None),
}

_KNOWN_HOODS = sorted(SOFIA_NEIGHBORHOODS, key=len, reverse=True)


def _parse_date(value: str, *, end_of_day: bool = False) -> Optional[datetime]:
    try:
        parsed = datetime.strptime(value.strip(), "%d.%m.%Y")
    except (TypeError, ValueError):
        return None
    return parsed.replace(hour=23, minute=59, second=59) if end_of_day else parsed


def _auction_window(text: str) -> tuple[Optional[datetime], Optional[datetime]]:
    match = re.search(r"от\s*(\d{2}\.\d{2}\.\d{4})\s*до\s*(\d{2}\.\d{2}\.\d{4})", text)
    if not match:
        return None, None
    return _parse_date(match.group(1)), _parse_date(match.group(2), end_of_day=True)


def _best_effort_neighborhood(*values: str | None) -> str:
    text = " ".join(value or "" for value in values)
    normalized = re.sub(r"[^\wа-яА-Я]+", " ", text.casefold())
    for hood in _KNOWN_HOODS:
        needle = re.sub(r"[^\wа-яА-Я]+", " ", hood.casefold()).strip()
        if re.search(rf"(?:^|\s){re.escape(needle)}(?:\s|$)", normalized):
            return canonicalize_neighborhood(hood)
    return "Unknown"


def _case_number(text: str) -> Optional[str]:
    match = re.search(
        r"(?:изпълнително|изп\.?)\s*дело\s*(?:№|номер)?\s*([\d\s./-]{5,})",
        text,
        re.IGNORECASE,
    )
    return re.sub(r"\s+", "", match.group(1)).rstrip(".,") if match else None


class BCPEAScraper(BaseScraper):
    BASE_URL = "https://sales.bcpea.org"
    SEARCH_URL = f"{BASE_URL}/properties"

    def __init__(self, max_pages: int = 10, max_detail_fetches: int = BCPEA_MAX_DETAIL_FETCHES):
        super().__init__("bcpea", self.BASE_URL)
        self.max_pages = max_pages
        self.max_detail_fetches = max_detail_fetches

    @staticmethod
    def _labels(container) -> Dict[str, str]:
        labels: Dict[str, str] = {}
        for group in container.select(".label__group"):
            label = group.select_one(".label")
            info = group.select_one(".info")
            if label and info:
                labels.setdefault(label.get_text(" ", strip=True).casefold(), info.get_text(" ", strip=True))
        return labels

    @classmethod
    def parse_card(cls, card) -> Optional[Dict[str, Any]]:
        try:
            title_node = card.select_one(".title")
            category = title_node.get_text(" ", strip=True) if title_node else ""
            if category not in RESIDENTIAL_CATEGORIES:
                return None
            property_type, rooms = RESIDENTIAL_CATEGORIES[category]

            link = card.select_one('a[href^="/properties/"]')
            if not link:
                return None
            source_id_match = re.search(r"/properties/(\d+)", link.get("href", ""))
            if not source_id_match:
                return None

            text = card.get_text(" ", strip=True).replace("\xa0", " ")
            area_match = re.search(r"([\d\s]+(?:[.,]\d+)?)\s*кв\.м", text, re.IGNORECASE)
            area = float(area_match.group(1).replace(" ", "").replace(",", ".")) if area_match else 0
            price_node = card.select_one(".content--price .price")
            price_text = price_node.get_text(" ", strip=True).replace("\xa0", " ") if price_node else ""
            price_match = re.search(r"([\d\s]+(?:[.,]\d+)?)\s*(EUR|€|BGN|лв\.?)", price_text, re.IGNORECASE)
            if not price_match or area <= 0:
                return None
            price = float(price_match.group(1).replace(" ", "").replace(",", "."))
            currency = price_match.group(2).casefold()
            price_eur = price / EUR_BGN_RATE if currency.startswith(("bgn", "лв")) else price
            price_bgn = price if currency.startswith(("bgn", "лв")) else price * EUR_BGN_RATE

            labels = cls._labels(card)
            address = labels.get("адрес") or labels.get("населено място")
            start, end = _auction_window(labels.get("срок", text))
            if end and end < utc_now():
                return None
            neighborhood = _best_effort_neighborhood(address)
            image = card.select_one("img[src]")
            title = category

            return {
                "source": "bcpea",
                "source_id": source_id_match.group(1),
                "listing_kind": "auction",
                "url": urljoin(cls.BASE_URL, link.get("href")),
                "image_url": urljoin(cls.BASE_URL, image.get("src")) if image else None,
                "title": title,
                "price_eur": round(price_eur, 2),
                "price_bgn": round(price_bgn, 2),
                "area_sqm": area,
                "price_per_sqm_eur": round(price_eur / area, 2),
                "neighborhood": neighborhood,
                "property_type": property_type,
                "rooms": rooms,
                "description": address,
                "auction_start": start,
                "auction_end": end,
                "bailiff_name": labels.get("частен съдебен изпълнител"),
                "case_number": _case_number(text),
                "is_active": True,
            }
        except Exception as exc:
            logger.warning(f"Could not parse bcpea auction card: {exc}")
            return None

    @classmethod
    def parse_detail(cls, soup: BeautifulSoup) -> Dict[str, Any]:
        labels = cls._labels(soup)
        description = labels.get("описание")
        quarter = labels.get("квартал")
        address = labels.get("адрес")
        images = [urljoin(cls.BASE_URL, image.get("src")) for image in soup.select("img[src]") if "/upload/" in image.get("src", "")]
        return {
            "description_full": description,
            "address": address,
            "neighborhood": _best_effort_neighborhood(quarter, address, description),
            "case_number": _case_number(description or soup.get_text(" ", strip=True)),
            "image_urls": images,
        }

    def scrape(self) -> List[Dict[str, Any]]:
        listings: List[Dict[str, Any]] = []
        seen = set()
        for page in range(1, self.max_pages + 1):
            url = f"{self.SEARCH_URL}?court=28&perpage=12&p={page}"
            logger.info(f"Scraping bcpea Sofia auctions page {page}")
            soup = self.fetch_page(url)
            if not soup:
                break
            cards = soup.select(".item__container .item__group")
            if not cards:
                break
            for card in cards:
                listing = self.parse_card(card)
                if listing and listing["source_id"] not in seen:
                    seen.add(listing["source_id"])
                    listings.append(listing)
            if not soup.select_one(f'.pagination a[href*="p={page + 1}"]'):
                break

        for listing in listings[: self.max_detail_fetches]:
            soup = self.fetch_page(listing["url"])
            if not soup:
                continue
            details = self.parse_detail(soup)
            for key, value in details.items():
                if value not in (None, "", [], "Unknown"):
                    listing[key] = value

        logger.info(f"bcpea: scraped {len(listings)} active Sofia residential auctions")
        return listings
