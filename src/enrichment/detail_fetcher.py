"""Bounded, round-robin detail-page enrichment for active listings."""

from __future__ import annotations

import json
import re
import time
from collections import defaultdict, deque
from typing import Callable, Dict, Iterable, List, Protocol

import httpx
from bs4 import BeautifulSoup
from loguru import logger
from sqlalchemy import exists, func, or_
from sqlalchemy.orm import Session

from src.config import ENRICH_DELAY_SECONDS, ENRICH_MAX_PER_RUN, USER_AGENTS
from src.database.models import Listing, PriceHistory
from src.scrapers.homesbg import HomesBgScraper
from src.scrapers.imotbg import ImotBgScraper
from src.scrapers.imotiinfo import ImotiInfoScraper
from src.scrapers.imotinet import ImotiNetScraper
from src.scrapers.propertybg import PropertyBGScraper
from src.utils.phone import normalize_bulgarian_phone
from src.utils.time import utc_now

DETAIL_PARSERS = {
    "homesbg": HomesBgScraper.parse_detail,
    "homesbg-rent": HomesBgScraper.parse_detail,
    "imotbg": ImotBgScraper.parse_detail,
    "imotbg-rent": ImotBgScraper.parse_detail,
    "imotiinfo": ImotiInfoScraper.parse_detail,
    "imotinet": ImotiNetScraper.parse_detail,
    "propertybg": PropertyBGScraper.parse_detail,
}

SOURCE_ENCODINGS = {
    "imotbg": "windows-1251",
    "imotbg-rent": "windows-1251",
}


class HttpClient(Protocol):
    def get(self, url: str, **kwargs) -> httpx.Response: ...

    def close(self) -> None: ...


def select_enrichment_candidates(db: Session, *, max_per_run: int) -> List[Listing]:
    """Select oldest active backfill plus rows whose price changed since enrichment."""
    changed_after_enrichment = exists().where(
        PriceHistory.listing_id == Listing.id,
        PriceHistory.recorded_at > Listing.enriched_at,
    )
    rows = (
        db.query(Listing)
        .filter(
            Listing.is_active.is_(True),
            or_(Listing.is_duplicate.is_(False), Listing.is_duplicate.is_(None)),
            Listing.source.in_(DETAIL_PARSERS),
            or_(Listing.enriched_at.is_(None), changed_after_enrichment),
        )
        .order_by(Listing.enriched_at.asc(), Listing.first_seen.asc(), Listing.id.asc())
        .all()
    )
    return _round_robin(rows, max_per_run)


def enrich_listing_details(
    db: Session,
    *,
    max_per_run: int = ENRICH_MAX_PER_RUN,
    delay_seconds: float = ENRICH_DELAY_SECONDS,
    client: HttpClient | None = None,
    sleep: Callable[[float], None] = time.sleep,
    recorder=None,
) -> Dict[str, object]:
    """Fetch and persist detail fields; three 403/429s pause that source."""
    candidates = select_enrichment_candidates(db, max_per_run=max_per_run)
    owns_client = client is None
    if client is None:
        client = httpx.Client(follow_redirects=True, timeout=30)

    consecutive_blocked: Dict[str, int] = defaultdict(int)
    backed_off = set()
    requested = enriched = failed = 0
    last_request_at = None
    try:
        for index, listing in enumerate(candidates):
            if listing.source in backed_off:
                continue
            if last_request_at is not None:
                elapsed = time.monotonic() - last_request_at
                if elapsed < delay_seconds:
                    sleep(delay_seconds - elapsed)
            try:
                response = client.get(
                    listing.url,
                    headers={"User-Agent": USER_AGENTS[index % len(USER_AGENTS)]},
                )
                last_request_at = time.monotonic()
                requested += 1
            except (httpx.HTTPError, OSError) as exc:
                logger.warning(f"Detail fetch failed for {listing.source}:{listing.source_id}: {exc}")
                failed += 1
                continue

            if response.status_code in (403, 429):
                failed += 1
                consecutive_blocked[listing.source] += 1
                if consecutive_blocked[listing.source] >= 3:
                    backed_off.add(listing.source)
                    message = f"Detail enrichment backed off {listing.source} after 3 consecutive HTTP {response.status_code} responses"
                    logger.warning(message)
                    if recorder is not None:
                        recorder.add_error(message)
                continue
            consecutive_blocked[listing.source] = 0
            if response.status_code >= 400:
                failed += 1
                continue

            parser = DETAIL_PARSERS[listing.source]
            encoding = SOURCE_ENCODINGS.get(listing.source, response.encoding or "utf-8")
            text = response.content.decode(encoding, errors="replace")
            try:
                detail = parser(BeautifulSoup(text, "html.parser"))
            except Exception as exc:
                logger.warning(f"Detail parse failed for {listing.source}:{listing.source_id}: {exc}")
                failed += 1
                continue
            if not detail or not any(detail.get(key) for key in ("description_full", "image_urls", "address")):
                failed += 1
                continue

            _apply_detail(listing, detail)
            listing.enriched_at = utc_now()
            enriched += 1
            if enriched % 25 == 0:
                db.commit()

        db.commit()
    finally:
        if owns_client:
            client.close()

    _log_phone_merge_candidates(db)
    summary: Dict[str, object] = {
        "selected": len(candidates),
        "requested": requested,
        "enriched": enriched,
        "failed": failed,
        "backed_off_sources": sorted(backed_off),
    }
    logger.info(
        f"Detail enrichment: {enriched}/{requested} enriched, {failed} failed, "
        f"{len(backed_off)} sources backed off"
    )
    return summary


def _round_robin(rows: Iterable[Listing], limit: int) -> List[Listing]:
    grouped: Dict[str, deque[Listing]] = defaultdict(deque)
    for row in rows:
        grouped[row.source].append(row)
    selected = []
    source_order = sorted(grouped)
    while source_order and len(selected) < limit:
        next_order = []
        for source in source_order:
            selected.append(grouped[source].popleft())
            if grouped[source]:
                next_order.append(source)
            if len(selected) >= limit:
                break
        source_order = next_order
    return selected


def _apply_detail(listing: Listing, detail: Dict[str, object]) -> None:
    for field in (
        "description_full",
        "latitude",
        "longitude",
        "address",
        "seller_type",
        "seller_name",
    ):
        value = detail.get(field)
        if value not in (None, ""):
            setattr(listing, field, value)

    raw_phone = detail.get("contact_phone")
    phone = normalize_bulgarian_phone(str(raw_phone) if raw_phone else None)
    if phone:
        listing.contact_phone = phone
    email = str(detail.get("contact_email") or "").strip().lower()
    if re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        listing.contact_email = email

    images = list(dict.fromkeys(str(url) for url in detail.get("image_urls") or [] if url))
    if images:
        listing.image_urls = json.dumps(images, ensure_ascii=False)
        listing.image_count = len(images)


def _log_phone_merge_candidates(db: Session) -> None:
    candidates = (
        db.query(Listing.contact_phone)
        .filter(Listing.contact_phone.isnot(None))
        .group_by(Listing.contact_phone)
        .having(func.count(func.distinct(Listing.source)) > 1)
        .count()
    )
    if candidates:
        logger.info(f"Phone-match candidate merge groups across sources: {candidates} (logged only; no merge performed)")
