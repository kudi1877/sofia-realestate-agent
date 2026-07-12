"""Dashboard data exporter.

Regenerates the three JSON files the Next.js dashboard reads from disk:
  - data/dashboard/data.json          (all listings + neighborhood stats + aggregate)
  - data/dashboard/daily-digest.json  (today's new deals, price drops, hot districts)
  - data/dashboard/market.json        (pre-aggregated city/neighborhood analytics)

Optionally git-commits and pushes the dashboard repo so Vercel auto-deploys.

Path discovery:
  DASHBOARD_REPO_PATH env var (config.py) — default ../sofia-realestate-dashboard.

Auto-push:
  DASHBOARD_AUTO_PUSH env var (config.py) — default on. Set to "0" to skip git ops.
"""

from __future__ import annotations

import json
from bisect import bisect_right
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Any, Dict, List

from loguru import logger
from sqlalchemy.orm import Session, selectinload

from src.config import (
    ANOMALY_ZSCORE_THRESHOLD,
    DASHBOARD_REPO_PATH,
    DASHBOARD_DATA_DIR,
    DASHBOARD_AUTO_PUSH,
    MIN_LISTINGS_PER_GROUP,
)
from src.analysis.anomaly import calculate_neighborhood_stats
from src.analysis.imotbg_benchmark import fetch_benchmark
from src.analysis.seller_signals import calculate_market_signals, market_pulse_line
from src.analysis.rental_market import gross_yield_pct, rent_stats_lookup
from src.analysis.data_health import evaluate_data_health, load_previous_runs
from src.database.models import (
    Listing,
    Neighborhood,
    NeighborhoodRentStats,
    NeighborhoodStatsHistory,
)
from src.utils.git import changed_files, commit_and_push
from src.utils.time import utc_now


# ── Listings JSON ─────────────────────────────────────────────────────────────


def _build_listings_payload(db: Session) -> Dict[str, Any]:
    """Build the data.json structure consumed by the dashboard.

    Schema (must match src/lib/types.ts in the dashboard repo):
      { listings: Listing[], neighborhoods: Neighborhood[], stats: {...}, updatedAt }
    """
    # SOFT DEPRECIATION (Phase 2.3A):
    # Show active listings + recently-inactive ones (last_seen ≤ 30 days).
    # The frontend renders inactive ones dimmed and hides them by default
    # behind a "Show inactive" toggle. This way a partial scrape that fails
    # to confirm some listings doesn't make them vanish from the dashboard.
    cutoff = utc_now() - timedelta(days=30)

    rows = (
        db.query(Listing)
        .options(selectinload(Listing.alerts), selectinload(Listing.price_history))
        # The browser remains a sale product; rentals feed pre-aggregated rent
        # and yield metrics below, avoiding incomparable monthly/card prices.
        .filter(Listing.listing_kind == "sale")
        .filter((Listing.is_duplicate.is_(False)) | (Listing.is_duplicate.is_(None)))
        .filter(
            (Listing.is_active.is_(True))
            | (Listing.last_seen >= cutoff)
        )
        .all()
    )
    sibling_rows = (
        db.query(Listing.canonical_id, Listing.source, Listing.url)
        .filter(Listing.listing_kind == "sale")
        .filter(Listing.canonical_id.isnot(None))
        .filter(
            (Listing.is_active.is_(True))
            | (Listing.last_seen >= cutoff)
        )
        .all()
    )
    links_by_canonical: Dict[str, Dict[str, str]] = defaultdict(dict)
    for canonical_id, source, url in sibling_rows:
        links_by_canonical[canonical_id].setdefault(source, url)

    neighborhood_prices: Dict[str, List[float]] = defaultdict(list)
    for row in rows:
        if row.neighborhood and row.price_per_sqm_eur is not None:
            neighborhood_prices[row.neighborhood].append(float(row.price_per_sqm_eur))
    for prices in neighborhood_prices.values():
        prices.sort()

    # TIN-471: give EVERY listing a z-score, not just alert-flagged outliers.
    # Same-type groups only (tier 1→2, no cross-type fallback per TIN-468).
    group_stats = calculate_neighborhood_stats([l for l in rows if l.is_active])

    def _group_zscore(l: Listing) -> float | None:
        if not l.price_per_sqm_eur or l.price_per_sqm_eur <= 0:
            return None
        construction = l.construction_type or "unknown"
        for key in (
            (l.neighborhood, l.property_type, construction),
            (l.neighborhood, l.property_type, "all"),
        ):
            g = group_stats.get(key)
            if g and g["std"]:
                return round((float(l.price_per_sqm_eur) - g["mean"]) / g["std"], 3)
        return None

    listings: List[Dict[str, Any]] = []
    rental_stats = rent_stats_lookup(db)
    for l in rows:
        alert_zscore, savings_pct = _latest_alert_values_for_listing(l)
        # is_deal is the single source of truth for deal badges/feeds: only
        # alert-qualified outliers. zscore alone no longer implies "deal".
        is_deal = alert_zscore is not None and alert_zscore <= ANOMALY_ZSCORE_THRESHOLD
        zscore = alert_zscore if alert_zscore is not None else _group_zscore(l)
        source_links = links_by_canonical.get(l.canonical_id, {})
        percentile = _price_percentile(
            neighborhood_prices.get(l.neighborhood, []),
            l.price_per_sqm_eur,
        )
        listings.append(
            _omit_none_values(
                {
                    "id": l.id,
                    "source": l.source,
                    "listing_kind": l.listing_kind,
                    # Stable keys for the dashboard's favorites/watchlist —
                    # canonical_id survives cross-source dedup switches.
                    "source_id": l.source_id,
                    "canonical_id": l.canonical_id,
                    "url": l.url,
                    "image_url": l.image_url,
                    "neighborhood": l.neighborhood,
                    "property_type": l.property_type,
                    "rooms": l.rooms,
                    "area_sqm": float(l.area_sqm) if l.area_sqm is not None else None,
                    "price_eur": float(l.price_eur) if l.price_eur is not None else None,
                    "price_per_sqm_eur": (
                        round(float(l.price_per_sqm_eur), 2)
                        if l.price_per_sqm_eur is not None
                        else None
                    ),
                    "construction_type": l.construction_type,
                    "floor": l.floor,
                    "total_floors": l.total_floors,
                    "price_changes": l.price_changes or 0,
                    "site_count": max(1, len(source_links)),
                    "cross_source_links": (
                        [
                            {"source": source, "url": url}
                            for source, url in sorted(source_links.items())
                        ]
                        if len(source_links) > 1
                        else None
                    ),
                    "price_history": _listing_price_history(l),
                    "price_percentile": percentile,
                    "days_on_market": l.days_on_market,
                    # zscore is present for every listing with a same-type group
                    # (alert value preferred when one exists); is_deal marks only
                    # alert-qualified outliers and drives all deal UI.
                    "zscore": zscore,
                    "savings_pct": savings_pct,
                    "is_deal": is_deal,
                    # When we first scraped this ad → effectively the "added on" date
                    # for the user.
                    "first_seen": l.first_seen.isoformat() if l.first_seen else None,
                    "last_seen": l.last_seen.isoformat() if l.last_seen else None,
                    "availability_checked_at": (
                        l.availability_checked_at.isoformat()
                        if l.availability_checked_at
                        else None
                    ),
                    # Soft-depreciation flag — true when the listing is still on the
                    # source site (we re-confirmed it in the latest scrape). False
                    # means we haven't seen it for ≥1 scrape but it's within 30d.
                    "is_active": bool(l.is_active),
                    "motivated_score": int(l.motivated_score or 0),
                    "gross_yield_pct": gross_yield_pct(l, rental_stats),
                    "exposure": json.loads(l.exposure) if l.exposure else None,
                    "renovation_state": l.renovation_state,
                    "act16": l.act16,
                    "has_elevator": l.has_elevator,
                    "parking": l.parking,
                }
            )
        )

    # Neighborhood stats — for the price heatmap.
    hoods = (
        db.query(Neighborhood)
        .filter(Neighborhood.avg_price_per_sqm.isnot(None))
        .filter(Neighborhood.avg_price_per_sqm > 0)
        .all()
    )
    neighborhoods = [
        {
            "neighborhood": h.name,
            "listingCount": h.listing_count or 0,
            "avgPricePerSqm": round(float(h.avg_price_per_sqm), 2),
            "medianPricePerSqm": (
                round(float(h.median_price_per_sqm), 2)
                if h.median_price_per_sqm is not None
                else None
            ),
        }
        for h in hoods
    ]

    # Aggregate stats.
    total_listings = len(listings)
    total_deals = sum(1 for l in listings if l.get("is_deal"))
    prices_per_sqm = [
        l["price_per_sqm_eur"] for l in listings if l["price_per_sqm_eur"]
    ]
    avg_price_per_sqm = (
        round(sum(prices_per_sqm) / len(prices_per_sqm), 2) if prices_per_sqm else 0
    )
    median_price_per_sqm = (
        round(float(median(prices_per_sqm)), 2) if prices_per_sqm else 0
    )

    return {
        "listings": listings,
        "neighborhoods": neighborhoods,
        "stats": {
            "totalListings": total_listings,
            "totalDeals": total_deals,
            "avgPricePerSqm": avg_price_per_sqm,
            "medianPricePerSqm": median_price_per_sqm,
        },
        "updatedAt": utc_now().isoformat(),
    }


def _omit_none_values(item: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in item.items() if value is not None}


def _listing_price_history(l: Listing) -> List[Dict[str, Any]] | None:
    """Return de-duplicated price changes, including the listing's current price."""
    points = [
        {
            "date": item.recorded_at.isoformat(),
            "price_eur": round(float(item.price_eur), 2),
            "price_per_sqm_eur": round(float(item.price_per_sqm_eur), 2),
        }
        for item in sorted(l.price_history or [], key=lambda item: item.recorded_at)
        if item.recorded_at and item.price_eur is not None and item.price_per_sqm_eur is not None
    ]
    if l.last_seen and l.price_eur is not None and l.price_per_sqm_eur is not None:
        points.append(
            {
                "date": l.last_seen.isoformat(),
                "price_eur": round(float(l.price_eur), 2),
                "price_per_sqm_eur": round(float(l.price_per_sqm_eur), 2),
            }
        )

    changes: List[Dict[str, Any]] = []
    for point in points:
        if changes and changes[-1]["price_eur"] == point["price_eur"]:
            changes[-1] = point
        else:
            changes.append(point)
    return changes if len(changes) > 1 else None


def _price_percentile(prices: List[float], price: float | None) -> float | None:
    if not prices or price is None:
        return None
    return round(100 * bisect_right(prices, float(price)) / len(prices), 1)


def _latest_alert_values_for_listing(l: Listing) -> tuple[float | None, float | None]:
    """Pull latest zscore/savings values from underpriced alerts in one pass."""
    zscore = None
    savings_pct = None
    for alert in sorted(l.alerts or [], key=lambda a: a.id, reverse=True):
        if alert.alert_type != "underpriced":
            continue
        if zscore is None and alert.zscore is not None:
            zscore = round(float(alert.zscore), 3)
        if savings_pct is None and alert.savings_pct is not None:
            savings_pct = round(float(alert.savings_pct), 1)
        if zscore is not None and savings_pct is not None:
            break
    return zscore, savings_pct


# ── Market analytics JSON ────────────────────────────────────────────────────


def _build_market_payload(
    db: Session,
    seller_signals: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Build median-first, pre-aggregated market analytics for the dashboard."""
    now = utc_now()
    seller_signals = seller_signals or calculate_market_signals(db, now=now)
    unique_clause = (Listing.is_duplicate.is_(False)) | (Listing.is_duplicate.is_(None))
    active_rows = db.query(Listing).filter(
        Listing.is_active.is_(True),
        Listing.listing_kind == "sale",
        unique_clause,
    ).all()

    all_prices = [
        float(row.price_per_sqm_eur)
        for row in active_rows
        if row.price_per_sqm_eur and row.price_per_sqm_eur > 0
    ]
    apartment_prices = [
        float(row.price_per_sqm_eur)
        for row in active_rows
        if row.property_type == "apartment"
        and row.price_per_sqm_eur
        and row.price_per_sqm_eur > 0
    ]
    city_prices = apartment_prices or all_prices

    dom_all: Dict[str, List[int]] = defaultdict(list)
    dom_apartments: Dict[str, List[int]] = defaultdict(list)
    for row in active_rows:
        if not row.neighborhood or not row.first_seen:
            continue
        days = max(0, (now - row.first_seen).days)
        dom_all[row.neighborhood].append(days)
        if row.property_type == "apartment":
            dom_apartments[row.neighborhood].append(days)

    history_by_neighborhood: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    history_rows = db.query(NeighborhoodStatsHistory).order_by(
        NeighborhoodStatsHistory.neighborhood,
        NeighborhoodStatsHistory.snapshot_date,
    ).all()
    for row in history_rows:
        history_by_neighborhood[row.neighborhood].append(
            {
                "date": row.snapshot_date.isoformat(),
                "median_price_per_sqm": round(float(row.median_price_per_sqm), 2),
                "listing_count": int(row.listing_count),
            }
        )

    neighborhoods = []
    rent_by_neighborhood = {
        row.neighborhood: row
        for row in db.query(NeighborhoodRentStats).filter(
            NeighborhoodRentStats.rooms_bucket == "all"
        ).all()
    }
    hood_rows = db.query(Neighborhood).filter(
        Neighborhood.median_price_per_sqm.isnot(None),
        Neighborhood.median_price_per_sqm > 0,
    ).all()
    for hood in hood_rows:
        history = history_by_neighborhood.get(hood.name, [])
        trend_pct = _snapshot_trend_pct(history, days=30)
        apartment_dom = dom_apartments.get(hood.name) or []
        dom_values = (
            apartment_dom
            if len(apartment_dom) >= MIN_LISTINGS_PER_GROUP
            else dom_all.get(hood.name) or []
        )
        rent_stat = rent_by_neighborhood.get(hood.name)
        median_rent = float(rent_stat.median_rent_per_sqm) if rent_stat else None
        representative_yield = (
            round(median_rent * 12 / float(hood.median_price_per_sqm) * 100, 2)
            if median_rent is not None and hood.median_price_per_sqm
            else None
        )
        neighborhoods.append(
            {
                "neighborhood": hood.name,
                "median_price_per_sqm": round(float(hood.median_price_per_sqm), 2),
                "listing_count": int(hood.listing_count or 0),
                "median_days_on_market": (
                    round(float(median(dom_values)), 1) if dom_values else None
                ),
                "trend_30d_pct": trend_pct,
                "trend_direction": (
                    "up"
                    if trend_pct is not None and trend_pct > 0.5
                    else "down"
                    if trend_pct is not None and trend_pct < -0.5
                    else "stable"
                    if trend_pct is not None
                    else "insufficient history"
                ),
                "history": history,
                "median_rent_per_sqm": round(median_rent, 2) if median_rent is not None else None,
                "gross_yield_pct": representative_yield,
                **seller_signals["neighborhoods"].get(hood.name, {}),
            }
        )

    off_market = db.query(Listing).filter(
        Listing.is_sold.is_(True),
        Listing.listing_kind == "sale",
        unique_clause,
    ).count()
    new_this_week = sum(
        1
        for row in active_rows
        if row.first_seen and row.first_seen >= now - timedelta(days=7)
    )
    price_drops = sum(
        1
        for row in active_rows
        if row.first_price_eur is not None and row.price_eur < row.first_price_eur
    )

    return {
        "city": {
            "median_price_per_sqm": (
                round(float(median(city_prices)), 2) if city_prices else 0
            ),
            "active": len(active_rows),
            "new_this_week": new_this_week,
            "price_drops": price_drops,
            "off_market": off_market,
            **seller_signals["city"],
        },
        "neighborhoods": sorted(
            neighborhoods,
            key=lambda item: item["listing_count"],
            reverse=True,
        ),
        "updated_at": now.isoformat(),
    }


def _snapshot_trend_pct(history: List[Dict[str, Any]], days: int) -> float | None:
    if len(history) < 2:
        return None

    latest = history[-1]
    latest_date = datetime.fromisoformat(latest["date"])
    cutoff = latest_date - timedelta(days=days)
    previous = next(
        (
            point
            for point in reversed(history[:-1])
            if datetime.fromisoformat(point["date"]) <= cutoff
        ),
        None,
    )
    if previous is None or previous["median_price_per_sqm"] <= 0:
        return None

    return round(
        (latest["median_price_per_sqm"] - previous["median_price_per_sqm"])
        / previous["median_price_per_sqm"]
        * 100,
        2,
    )


# ── Daily digest JSON ─────────────────────────────────────────────────────────


def _build_digest_payload(
    db: Session,
    deals_by_hood: Dict[str, int] | None = None,
    seller_signals: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Build daily-digest.json in the schema the dashboard's DailyDigest tab expects.

    The agent's `daily_email.py` produces an EMAIL-formatted context where
    prices are pre-formatted strings ("15 000") — fine for HTML rendering but
    breaks the dashboard, which wants raw numbers nested under a `summary`
    block. So we transform the email context into a dashboard-friendly shape.

    Schema (matches `DigestData` interface in components/DailyDigest.tsx):
      {
        generated_at: ISO,
        summary: { total_active, new_today, price_drops, hot_deals },
        new_deals: [{ id, source, url, title, price_eur, area_sqm, ... }],
        price_drops: [...],
        hot_districts: [...],
        top_pick: {...} | null,
      }
    """
    try:
        from src.alerts.daily_email import generate_daily_email  # heavy import
    except Exception as e:
        logger.warning(f"Could not import daily_email for digest: {e}")
        generate_daily_email = None

    ctx = {}
    if generate_daily_email is not None:
        try:
            _html, _plain, ctx = generate_daily_email(db=db)
        except Exception as e:
            logger.warning(f"Daily digest render failed, using database fallbacks: {e}")

    from src.analysis.trends import detect_price_drops, generate_market_summary

    market_summary = generate_market_summary(db)
    seller_signals = seller_signals or calculate_market_signals(db)
    price_drops = [
        _dashboard_price_drop(drop)
        for drop in detect_price_drops(db)[:8]
    ]

    return {
        "generated_at": utc_now().isoformat(),
        "summary": {
            "total_active":  int(market_summary["total_listings"]),
            "off_market":    int(market_summary["off_market"]),
            "new_today":     int(ctx.get("new_today", 0) or 0),
            "price_drops":   len(price_drops),
            "hot_deals":     len(ctx.get("new_deals", []) or []),
        },
        "new_deals":     [_dashboard_deal(d) for d in (ctx.get("new_deals") or [])],
        "price_drops":   price_drops,
        "hot_districts": [
            _dashboard_district(h, deals_by_hood)
            for h in (ctx.get("hot_districts") or [])
        ],
        "top_pick":      _dashboard_deal(ctx.get("top_pick")) if ctx.get("top_pick") else None,
        "market_pulse":  market_pulse_line(seller_signals["city"]),
    }


def _empty_digest() -> Dict[str, Any]:
    return {
        "generated_at": utc_now().isoformat(),
        "summary": {"total_active": 0, "off_market": 0, "new_today": 0, "price_drops": 0, "hot_deals": 0},
        "new_deals": [], "price_drops": [], "hot_districts": [], "top_pick": None,
        "market_pulse": "Market pulse: insufficient weekly history.",
    }


def _to_num(v: Any) -> float | None:
    """Parse a number that might come in as a formatted string ('15 000', '1,200', '< -1.5')."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        # Strip thousand-separators (space, comma, NBSP) and stray symbols
        cleaned = v.replace(" ", "").replace("\xa0", "").replace(",", "").replace("€", "").strip()
        # Bail on sentinels like "< -1.5"
        if cleaned.startswith("<") or cleaned.startswith(">"):
            return None
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def _dashboard_deal(d: Dict[str, Any], is_deal: bool = True) -> Dict[str, Any]:
    return {
        "id":                d.get("id"),
        "source":            d.get("source"),
        "url":               d.get("url"),
        "title":             d.get("title") or d.get("neighborhood"),
        "image_url":         d.get("image_url"),
        "price_eur":         _to_num(d.get("price_eur")),
        "area_sqm":          _to_num(d.get("area_sqm")),
        "price_per_sqm_eur": _to_num(d.get("price_per_sqm") or d.get("price_per_sqm_eur")),
        "neighborhood":      d.get("neighborhood"),
        "property_type":     d.get("property_type"),
        "rooms":             _to_num(d.get("rooms")),
        "zscore":            _to_num(d.get("zscore")),
        "savings_pct":       _to_num(d.get("savings_pct")),
        "is_deal":           is_deal,
    }


def _dashboard_drop(d: Dict[str, Any]) -> Dict[str, Any]:
    # A price drop is not necessarily an underpriced deal.
    base = _dashboard_deal(d, is_deal=False)
    base.update({
        "old_price":      _to_num(d.get("old_price") or d.get("first_price_eur")),
        "new_price":      _to_num(d.get("new_price") or d.get("price_eur")),
        "price_drop_pct": _to_num(d.get("price_drop_pct") or d.get("drop_pct")),
    })
    return base


def _dashboard_price_drop(drop: Dict[str, Any]) -> Dict[str, Any]:
    listing = drop["listing"]
    return _dashboard_drop(
        {
            "id": listing.id,
            "source": listing.source,
            "url": listing.url,
            "title": listing.title or listing.neighborhood,
            "neighborhood": listing.neighborhood,
            "property_type": listing.property_type,
            "rooms": listing.rooms,
            "area_sqm": listing.area_sqm,
            "price_eur": drop["current_price"],
            "price_per_sqm_eur": listing.price_per_sqm_eur,
            "old_price": drop["original_price"],
            "new_price": drop["current_price"],
            "drop_pct": drop["drop_pct"],
        }
    )


def _dashboard_district(
    h: Dict[str, Any],
    deals_by_hood: Dict[str, int] | None = None,
) -> Dict[str, Any]:
    # District velocity rows key the name as 'name' and new supply as 'added'
    # (TIN-472: the old mapping read keys that never existed → blank names).
    name = h.get("neighborhood") or h.get("name")
    return {
        "neighborhood":       name,
        "new_listings":       int(_to_num(h.get("new_listings") or h.get("added")) or 0),
        "avg_price_per_sqm":  _to_num(h.get("avg_price_per_sqm") or h.get("avg_price")) or 0,
        "deal_count":         int((deals_by_hood or {}).get(name, 0)),
        "off_market":         int(_to_num(h.get("sold")) or 0),
    }


# ── File writing ──────────────────────────────────────────────────────────────


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str),
        encoding="utf-8",
    )


DIGEST_HISTORY_DAYS = 30


def _archive_digest(data_dir: Path, digest_payload: Dict[str, Any]) -> None:
    """Keep one browsable digest per calendar day (TIN-473).

    Writes digests/<YYYY-MM-DD>.json (same-day re-runs overwrite) plus
    digests/index.json listing available dates newest-first; prunes files
    older than DIGEST_HISTORY_DAYS entries.
    """
    digests_dir = data_dir / "digests"
    digests_dir.mkdir(parents=True, exist_ok=True)

    today = utc_now().strftime("%Y-%m-%d")
    _write_json(digests_dir / f"{today}.json", digest_payload)

    dates = sorted(
        (p.stem for p in digests_dir.glob("????-??-??.json")),
        reverse=True,
    )
    for stale in dates[DIGEST_HISTORY_DAYS:]:
        (digests_dir / f"{stale}.json").unlink(missing_ok=True)
    dates = dates[:DIGEST_HISTORY_DAYS]

    _write_json(digests_dir / "index.json", {"dates": dates, "updated_at": utc_now().isoformat()})


def _attach_imotbg_benchmark(market_payload: Dict[str, Any]) -> None:
    """Join imot.bg's published averages onto our market stats (TIN-476).

    Ours = cross-portal deduplicated median; theirs = single-portal average.
    Fetch failure leaves the fields null — the export never breaks on this.
    """
    benchmark = fetch_benchmark()
    matched = 0
    for row in market_payload.get("neighborhoods", []):
        theirs = (benchmark or {}).get(row.get("neighborhood"))
        ours = row.get("median_price_per_sqm")
        row["imotbg_avg_price_per_sqm"] = theirs
        row["delta_vs_imotbg_pct"] = (
            round((float(ours) - theirs) / theirs * 100, 1)
            if theirs and ours
            else None
        )
        matched += 1 if theirs else 0

    city = market_payload.get("city")
    if isinstance(city, dict):
        # imot.bg has no single city row; use the median of their per-hood
        # averages as the city-level reference.
        city_theirs = (
            round(float(median(list(benchmark.values()))), 0) if benchmark else None
        )
        city_ours = city.get("median_price_per_sqm")
        city["imotbg_avg_price_per_sqm"] = city_theirs
        city["delta_vs_imotbg_pct"] = (
            round((float(city_ours) - city_theirs) / city_theirs * 100, 1)
            if city_theirs and city_ours
            else None
        )

    if benchmark:
        logger.info(f"imot.bg benchmark joined: {matched} of {len(market_payload.get('neighborhoods', []))} hoods matched")


def _commit_dashboard_data(repo: Path, message: str) -> bool:
    """Stage dashboard data JSON, commit, and push. No-op if no changes.

    Returns True if a commit was created and pushed, False if no changes (or fail).
    """
    changed = changed_files(repo)
    relevant = [
        f for f in changed
        if f in (
            "data/dashboard/data.json",
            "data/dashboard/daily-digest.json",
            "data/dashboard/market.json",
        )
        or f.startswith("data/dashboard/digests/")
    ]
    if not relevant:
        logger.info(f"No dashboard JSON changes in {repo} — skipping commit/push")
        return False

    pushed = commit_and_push(repo, files=relevant, message=message)
    if pushed:
        logger.info(f"Pushed dashboard data update to origin/main ({len(relevant)} file(s))")
    return pushed


# ── Public entry point ────────────────────────────────────────────────────────


def export_dashboard(
    db: Session,
    push: bool | None = None,
    current_sources: List[Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    """Regenerate dashboard JSON files from DB; optionally commit + push.

    Args:
        db: SQLAlchemy session.
        push: Override DASHBOARD_AUTO_PUSH. None = use config default.

    Returns:
        Summary dict with counts + push status.
    """
    if push is None:
        push = DASHBOARD_AUTO_PUSH

    repo = DASHBOARD_REPO_PATH
    data_dir = DASHBOARD_DATA_DIR
    if not data_dir.parent.exists():
        logger.error(
            f"Dashboard data parent dir not found at {data_dir.parent}. "
            f"Set DASHBOARD_REPO_PATH env var to the dashboard repo root."
        )
        return {"ok": False, "reason": "dashboard_path_missing", "path": str(data_dir)}

    logger.info(f"Exporting dashboard data → {data_dir}")

    listings_payload = _build_listings_payload(db)

    # Real per-neighborhood deal counts for the digest's Hot Districts table.
    deals_by_hood: Dict[str, int] = defaultdict(int)
    for item in listings_payload["listings"]:
        if item.get("is_deal") and item.get("neighborhood"):
            deals_by_hood[item["neighborhood"]] += 1

    seller_signals = calculate_market_signals(db)
    digest_payload = _build_digest_payload(db, dict(deals_by_hood), seller_signals)
    market_payload = _build_market_payload(db, seller_signals)
    _attach_imotbg_benchmark(market_payload)

    _write_json(data_dir / "data.json", listings_payload)
    _write_json(data_dir / "daily-digest.json", digest_payload)
    _write_json(data_dir / "market.json", market_payload)
    try:
        _archive_digest(data_dir, digest_payload)
    except Exception as exc:
        logger.warning(f"Digest archive write failed without blocking export: {exc}")

    # Warning-only by contract: health telemetry must never block publishing.
    try:
        data_health = evaluate_data_health(
            db,
            market_payload,
            current_sources=current_sources,
            previous_runs=load_previous_runs(data_dir / "runs.json"),
            data_dir=data_dir,
        )
        for check in data_health["checks"]:
            if check["status"] in ("amber", "red"):
                logger.warning(f"Data health {check['status']}: {check['label']} - {check['detail']}")
    except Exception as exc:
        logger.warning(f"Data health evaluation failed without blocking export: {exc}")
        data_health = {
            "status": "amber",
            "checks": [
                {
                    "key": "evaluation_error",
                    "label": "Data health evaluation",
                    "status": "amber",
                    "value": 0,
                    "unit": "checks",
                    "detail": str(exc)[:200],
                }
            ],
            "source_metrics": {},
            "generated_at": utc_now().isoformat(),
        }

    summary = {
        "ok": True,
        "listings": listings_payload["stats"]["totalListings"],
        "deals": listings_payload["stats"]["totalDeals"],
        "neighborhoods": len(listings_payload["neighborhoods"]),
        "wrote": [
            "data/dashboard/data.json",
            "data/dashboard/daily-digest.json",
            "data/dashboard/market.json",
        ],
        "pushed": False,
        "data_health": data_health,
    }
    logger.info(
        f"Wrote {summary['listings']} listings, {summary['deals']} deals, "
        f"{summary['neighborhoods']} neighborhoods"
    )

    if push:
        timestamp = utc_now().strftime("%Y-%m-%d %H:%M UTC")
        msg = (
            f"data: refresh dashboard ({timestamp})\n\n"
            f"{summary['listings']} listings, {summary['deals']} deals, "
            f"{summary['neighborhoods']} neighborhoods.\n"
            f"Auto-generated by sofia-realestate-agent."
        )
        summary["pushed"] = _commit_dashboard_data(repo, msg)

    return summary
