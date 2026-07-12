"""Direct listing availability checks with source-specific gone detection.

Bounded live reconnaissance on 2026-07-12 established the dead-ad behavior:
imot.bg and imoti.info redirect detail URLs to result pages; imoti.net returns
404; homes.bg returns 404 with ``InactivePageError`` in its preloaded JSON;
property.bg retains a 200 detail page marked ``Outdated listing`` and ``SOLD``.
Unexpected HTTP/client failures remain unknown and never deactivate a row.
"""

from __future__ import annotations

import time
import re
from collections import defaultdict, deque
from datetime import timedelta
from typing import Callable, Dict, Iterable, List, Literal, Protocol
from urllib.parse import urlparse

import httpx
from loguru import logger
from sqlalchemy import exists, or_
from sqlalchemy.orm import Session

from src.config import (
    ANOMALY_ZSCORE_THRESHOLD,
    PING_DELAY_SECONDS,
    PING_MAX_PER_RUN,
    PING_RECENT_DAYS,
    USER_AGENTS,
)
from src.database.models import Alert, Listing
from src.utils.time import utc_now

Availability = Literal["live", "gone", "unknown"]


class HttpClient(Protocol):
    def get(self, url: str, **kwargs) -> httpx.Response: ...

    def close(self) -> None: ...


def classify_response(source: str, requested_url: str, response: httpx.Response) -> Availability:
    """Classify one response using only observed, source-owned signals."""
    source = source.removesuffix("-rent")
    if response.status_code in (404, 410):
        return "gone"
    if response.status_code == 429 or response.status_code >= 500:
        return "unknown"
    if response.status_code >= 400:
        return "unknown"

    requested_path = urlparse(requested_url).path.rstrip("/")
    final_path = response.url.path.rstrip("/")
    text = response.text.lower()

    if source == "imotbg":
        if "/obiava-" in requested_path and "/obiava-" not in final_path:
            return "gone"
        if any(marker in text for marker in ("обявата е изтрита", "обявата е архивирана")):
            return "gone"
    elif source == "imotiinfo":
        if "/obiava/" in requested_path and "/obiava/" not in final_path:
            return "gone"
        if any(marker in text for marker in ("обявата е изтрита", "обявата не е активна")):
            return "gone"
    elif source == "imotinet":
        if any(marker in text for marker in ("обявата е архивирана", "archived listing")):
            return "gone"
    elif source == "homesbg":
        if "inactivepageerror" in text or "офертата, която се опитвате да отворите, е неактивна" in text:
            return "gone"
    elif source == "propertybg":
        sold_band = re.search(r'class=["\'][^"\']*band[^"\']*["\'][^>]*>\s*sold\s*<', text)
        if "outdated listing" in text and sold_band:
            return "gone"
    else:
        return "unknown"

    return "live"


def select_candidates(db: Session, *, max_per_run: int, recent_days: int) -> List[Listing]:
    """Select active unique deals or recently discovered ads, round-robin by source."""
    cutoff = utc_now() - timedelta(days=recent_days)
    is_deal = exists().where(
        Alert.listing_id == Listing.id,
        Alert.alert_type == "underpriced",
        Alert.zscore <= ANOMALY_ZSCORE_THRESHOLD,
    )
    rows = (
        db.query(Listing)
        .filter(
            Listing.is_active.is_(True),
            or_(Listing.is_duplicate.is_(False), Listing.is_duplicate.is_(None)),
            or_(Listing.first_seen >= cutoff, is_deal),
        )
        .order_by(Listing.availability_checked_at.asc(), Listing.first_seen.desc())
        .all()
    )
    return _round_robin_by_source(rows, max_per_run)


def _round_robin_by_source(rows: Iterable[Listing], limit: int) -> List[Listing]:
    grouped: Dict[str, deque[Listing]] = defaultdict(deque)
    for row in rows:
        grouped[row.source].append(row)

    selected: List[Listing] = []
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


def ping_availability(
    db: Session,
    *,
    max_per_run: int = PING_MAX_PER_RUN,
    recent_days: int = PING_RECENT_DAYS,
    delay_seconds: float = PING_DELAY_SECONDS,
    client: HttpClient | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Dict[str, int]:
    """Check selected ads, persisting only confirmed live/gone outcomes."""
    candidates = select_candidates(db, max_per_run=max_per_run, recent_days=recent_days)
    counts = {"pinged": 0, "live": 0, "gone": 0, "unknown": 0}
    owns_client = client is None
    if client is None:
        client = httpx.Client(follow_redirects=True, timeout=30)

    try:
        for index, listing in enumerate(candidates):
            if index:
                sleep(delay_seconds)
            try:
                response = client.get(
                    listing.url,
                    headers={"User-Agent": USER_AGENTS[index % len(USER_AGENTS)]},
                )
                outcome = classify_response(listing.source, listing.url, response)
            except (httpx.HTTPError, OSError) as exc:
                logger.warning(f"Availability check failed for {listing.source}:{listing.source_id}: {exc}")
                outcome = "unknown"

            counts["pinged"] += 1
            counts[outcome] += 1
            if outcome in ("live", "gone"):
                listing.availability_checked_at = utc_now()
            if outcome == "gone":
                listing.is_active = False

        db.commit()
    finally:
        if owns_client:
            client.close()

    logger.info(
        "Availability: {pinged} pinged, {live} live, {gone} gone, {unknown} unknown".format(**counts)
    )
    return counts
