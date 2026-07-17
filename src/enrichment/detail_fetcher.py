"""Bounded, round-robin detail-page enrichment for active listings."""

from __future__ import annotations

import json
import re
import time
from collections import defaultdict, deque
from typing import Callable, Dict, Iterable, List, Protocol

import httpx
from bs4 import BeautifulSoup

from src.utils.soup import make_soup
from loguru import logger
from sqlalchemy import exists, func, or_
from sqlalchemy.orm import Session

from src.config import (
    ENRICH_DELAY_SECONDS,
    ENRICH_MAX_PER_RUN,
    SCRAPE_MAX_PARALLEL_SOURCES,
    USER_AGENTS,
)
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


def _fetch_lane(
    source: str,
    rows: List[Dict[str, object]],
    *,
    delay_seconds: float,
    client: HttpClient | None,
    sleep: Callable[[float], None],
) -> Dict[str, object]:
    """Network+parse for one source lane — NO database access (TIN-518).

    Runs serially with per-request delays against its own host; three
    consecutive 403/429 responses back the whole lane off. Returns plain
    data so the caller can apply results on the main thread/session.
    """
    owns_client = client is None
    lane_client = client or httpx.Client(follow_redirects=True, timeout=30)
    results: List[Dict[str, object]] = []
    consecutive_blocked = 0
    blocked_status = None
    backed_off = False
    requested = failed = 0
    last_request_at = None
    try:
        for index, row in enumerate(rows):
            if backed_off:
                break
            if last_request_at is not None:
                elapsed = time.monotonic() - last_request_at
                if elapsed < delay_seconds:
                    sleep(delay_seconds - elapsed)
            try:
                response = lane_client.get(
                    row["url"],
                    headers={"User-Agent": USER_AGENTS[index % len(USER_AGENTS)]},
                )
                last_request_at = time.monotonic()
                requested += 1
            except (httpx.HTTPError, OSError) as exc:
                logger.warning(f"Detail fetch failed for {source}:{row['source_id']}: {exc}")
                failed += 1
                continue

            if response.status_code in (403, 429):
                failed += 1
                consecutive_blocked += 1
                blocked_status = response.status_code
                if consecutive_blocked >= 3:
                    backed_off = True
                    logger.warning(
                        f"Detail enrichment backed off {source} after 3 consecutive "
                        f"HTTP {response.status_code} responses"
                    )
                continue
            consecutive_blocked = 0
            if response.status_code >= 400:
                failed += 1
                continue

            parser = DETAIL_PARSERS[source]
            encoding = SOURCE_ENCODINGS.get(source, response.encoding or "utf-8")
            text = response.content.decode(encoding, errors="replace")
            try:
                detail = parser(make_soup(text))
            except Exception as exc:
                logger.warning(f"Detail parse failed for {source}:{row['source_id']}: {exc}")
                failed += 1
                continue
            if not detail or not any(detail.get(key) for key in ("description_full", "image_urls", "address")):
                failed += 1
                continue
            results.append({"listing_id": row["id"], "detail": detail})
    finally:
        if owns_client:
            lane_client.close()
    return {
        "source": source,
        "results": results,
        "requested": requested,
        "failed": failed,
        "backed_off": backed_off,
        "blocked_status": blocked_status,
    }


def enrich_listing_details(
    db: Session,
    *,
    max_per_run: int = ENRICH_MAX_PER_RUN,
    delay_seconds: float = ENRICH_DELAY_SECONDS,
    client: HttpClient | None = None,
    sleep: Callable[[float], None] = time.sleep,
    recorder=None,
) -> Dict[str, object]:
    """Fetch and persist detail fields; three 403/429s pause that source.

    TIN-518: sources fetch in PARALLEL lanes (each lane serial + delayed
    against its own host — per-host politeness unchanged; wall time drops
    from Σ sources to the slowest lane). All ORM writes happen here on the
    caller's session after the network phase. An injected `client` (tests)
    forces the sequential path for determinism.
    """
    candidates = select_enrichment_candidates(db, max_per_run=max_per_run)
    by_source: Dict[str, List[Listing]] = defaultdict(list)
    for listing in candidates:
        by_source[listing.source].append(listing)
    listing_by_id = {listing.id: listing for listing in candidates}

    lane_inputs = [
        (
            source,
            [
                {"id": listing.id, "source_id": listing.source_id, "url": listing.url}
                for listing in rows
            ],
        )
        for source, rows in by_source.items()
    ]

    if client is not None or len(lane_inputs) <= 1:
        lane_summaries = [
            _fetch_lane(source, rows, delay_seconds=delay_seconds, client=client, sleep=sleep)
            for source, rows in lane_inputs
        ]
    else:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=min(SCRAPE_MAX_PARALLEL_SOURCES, len(lane_inputs))) as pool:
            lane_summaries = list(
                pool.map(
                    lambda item: _fetch_lane(
                        item[0], item[1], delay_seconds=delay_seconds, client=None, sleep=sleep
                    ),
                    lane_inputs,
                )
            )

    requested = enriched = failed = 0
    backed_off = set()
    for lane in lane_summaries:
        requested += lane["requested"]
        failed += lane["failed"]
        if lane["backed_off"]:
            backed_off.add(lane["source"])
            if recorder is not None:
                recorder.add_error(
                    f"Detail enrichment backed off {lane['source']} after 3 consecutive "
                    f"HTTP {lane['blocked_status']} responses"
                )
        for item in lane["results"]:
            listing = listing_by_id[item["listing_id"]]
            _apply_detail(listing, item["detail"])
            listing.enriched_at = utc_now()
            enriched += 1
            if enriched % 25 == 0:
                db.commit()
    db.commit()

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

    # TIN-520: structured attributes from the portal's own detail payload.
    # Fill-only-when-empty: a value the search-result parse already set (or a
    # human corrected) wins over the detail page; portal data wins over the
    # LLM only by arriving first, since the LLM extractor also fills NULLs.
    for field in (
        "floor",
        "total_floors",
        "construction_type",
        "year_built",
        "heating",
        "furnishing",
        "has_elevator",
        "parking",
    ):
        value = detail.get(field)
        if value not in (None, "") and getattr(listing, field, None) in (None, ""):
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
