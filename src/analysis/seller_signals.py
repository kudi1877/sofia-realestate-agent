"""Seller-motivation, exit-price, and weekly absorption analytics."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from statistics import median
from typing import Any, Dict, Iterable

from sqlalchemy.orm import Session, selectinload

from src.database.models import Listing
from src.utils.time import utc_now

MIN_EXIT_SAMPLE = 10


def motivated_score(
    *,
    price_changes: int,
    first_price_eur: float | None,
    current_price_eur: float | None,
    days_since_last_cut: int | None,
    days_on_market: int,
    neighborhood_median_dom: float | None,
) -> int:
    """Calculate a reproducible 0-100 score from four capped components."""
    change_points = min(max(price_changes, 0) * 8, 25)

    cut_pct = 0.0
    if first_price_eur and current_price_eur is not None and first_price_eur > 0:
        cut_pct = max(0.0, (first_price_eur - current_price_eur) / first_price_eur * 100)
    cut_points = min(cut_pct * 1.5, 30)

    recency_points = 0
    if price_changes > 0 and days_since_last_cut is not None:
        if days_since_last_cut <= 7:
            recency_points = 20
        elif days_since_last_cut <= 30:
            recency_points = 14
        elif days_since_last_cut <= 60:
            recency_points = 7

    dom_points = 0
    if neighborhood_median_dom and neighborhood_median_dom > 0:
        dom_ratio = days_on_market / neighborhood_median_dom
        if dom_ratio >= 2:
            dom_points = 25
        elif dom_ratio >= 1.5:
            dom_points = 18
        elif dom_ratio >= 1:
            dom_points = 10

    total = change_points + cut_points + recency_points + dom_points
    return min(100, int(total + 0.5))


def update_motivated_scores(db: Session, *, now: datetime | None = None) -> Dict[str, int]:
    """Recompute and persist motivated scores for active unique listings."""
    now = now or utc_now()
    rows = (
        db.query(Listing)
        .options(selectinload(Listing.price_history))
        .filter(
            Listing.is_active.is_(True),
            Listing.listing_kind == "sale",
            (Listing.is_duplicate.is_(False)) | (Listing.is_duplicate.is_(None)),
        )
        .all()
    )

    dom_by_hood: Dict[str, list[int]] = defaultdict(list)
    all_dom = []
    for row in rows:
        dom = _days_on_market(row, now)
        dom_by_hood[row.neighborhood].append(dom)
        all_dom.append(dom)
    city_median_dom = float(median(all_dom)) if all_dom else None

    motivated = 0
    for row in rows:
        hood_values = dom_by_hood.get(row.neighborhood) or []
        hood_median_dom = float(median(hood_values)) if hood_values else city_median_dom
        latest_cut_at = _latest_cut_at(row)
        row.motivated_score = motivated_score(
            price_changes=int(row.price_changes or 0),
            first_price_eur=row.first_price_eur,
            current_price_eur=row.price_eur,
            days_since_last_cut=(max(0, (now - latest_cut_at).days) if latest_cut_at else None),
            days_on_market=_days_on_market(row, now),
            neighborhood_median_dom=hood_median_dom,
        )
        motivated += row.motivated_score >= 60

    db.commit()
    return {"scored": len(rows), "motivated": motivated}


def calculate_market_signals(
    db: Session,
    *,
    now: datetime | None = None,
    min_exit_sample: int = MIN_EXIT_SAMPLE,
) -> Dict[str, Any]:
    """Calculate median exit metrics and seven-day exits/new absorption."""
    now = now or utc_now()
    cutoff = now - timedelta(days=7)
    rows = db.query(Listing).filter(
        Listing.listing_kind == "sale",
        (Listing.is_duplicate.is_(False)) | (Listing.is_duplicate.is_(None)),
    ).all()

    sold_by_hood: Dict[str, list[Listing]] = defaultdict(list)
    weekly_new: Dict[str, int] = defaultdict(int)
    weekly_exits: Dict[str, int] = defaultdict(int)
    for row in rows:
        hood = row.neighborhood or "Unknown"
        if row.first_seen and row.first_seen >= cutoff:
            weekly_new[hood] += 1
        if row.is_sold:
            sold_by_hood[hood].append(row)
            if row.sold_date and row.sold_date >= cutoff:
                weekly_exits[hood] += 1

    neighborhoods: Dict[str, Dict[str, Any]] = {}
    all_hoods = set(sold_by_hood) | set(weekly_new) | set(weekly_exits)
    for hood in all_hoods:
        sold = sold_by_hood.get(hood, [])
        new_count = weekly_new.get(hood, 0)
        exit_count_7d = weekly_exits.get(hood, 0)
        values: Dict[str, Any] = {
            "exit_count": len(sold),
            "weekly_new_listings": new_count,
            "weekly_exits": exit_count_7d,
            "weekly_absorption_ratio": (
                round(exit_count_7d / new_count, 2) if new_count else None
            ),
            "median_exit_price_per_sqm": None,
            "median_exit_discount_pct": None,
            "median_dom_to_exit": None,
        }
        if len(sold) >= min_exit_sample:
            exit_prices = [
                float(row.price_per_sqm_eur)
                for row in sold
                if row.price_per_sqm_eur and row.price_per_sqm_eur > 0
            ]
            discounts = [
                max(0.0, (row.first_price_eur - row.price_eur) / row.first_price_eur * 100)
                for row in sold
                if row.first_price_eur and row.first_price_eur > 0 and row.price_eur is not None
            ]
            dom_values = [int(row.days_on_market) for row in sold if row.days_on_market is not None]
            values.update(
                {
                    "median_exit_price_per_sqm": _median_or_none(exit_prices),
                    "median_exit_discount_pct": _median_or_none(discounts),
                    "median_dom_to_exit": _median_or_none(dom_values),
                }
            )
        neighborhoods[hood] = values

    city_new = sum(weekly_new.values())
    city_exits = sum(weekly_exits.values())
    return {
        "neighborhoods": neighborhoods,
        "city": {
            "weekly_new_listings": city_new,
            "weekly_exits": city_exits,
            "weekly_absorption_ratio": round(city_exits / city_new, 2) if city_new else None,
        },
    }


def market_pulse_line(city: Dict[str, Any]) -> str:
    """Render the compact digest sentence from pre-aggregated city signals."""
    new_count = int(city.get("weekly_new_listings") or 0)
    exits = int(city.get("weekly_exits") or 0)
    ratio = city.get("weekly_absorption_ratio")
    suffix = f" ({ratio:.2f}x absorption)" if ratio is not None else ""
    return f"Market pulse: {new_count:,} new listings vs {exits:,} exits this week{suffix}."


def _latest_cut_at(row: Listing) -> datetime | None:
    if not row.price_changes:
        return None
    timestamps = [point.recorded_at for point in row.price_history or [] if point.recorded_at]
    return max(timestamps) if timestamps else None


def _days_on_market(row: Listing, now: datetime) -> int:
    if row.days_on_market is not None:
        return max(0, int(row.days_on_market))
    return max(0, (now - row.first_seen).days) if row.first_seen else 0


def _median_or_none(values: Iterable[float | int]) -> float | None:
    values = list(values)
    return round(float(median(values)), 2) if values else None
