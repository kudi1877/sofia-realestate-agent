"""Rental medians and gross-yield estimates kept separate from sale analytics."""

from __future__ import annotations

from collections import defaultdict
from statistics import median
from typing import Any, Dict, Iterable

from sqlalchemy.orm import Session

from src.config import MIN_LISTINGS_PER_GROUP
from src.database.models import (
    Listing,
    NeighborhoodRentStats,
    NeighborhoodRentStatsHistory,
)
from src.utils.time import utc_now


def rooms_bucket(rooms: int | None) -> str:
    if rooms is None:
        return "all"
    return "4+" if rooms >= 4 else str(max(1, rooms))


def calculate_rent_stats(
    listings: Iterable[Listing],
    min_group_size: int = MIN_LISTINGS_PER_GROUP,
) -> Dict[tuple[str, str], Dict[str, float | int]]:
    """Calculate active, unique rental medians with a neighborhood fallback."""
    grouped: Dict[tuple[str, str], list[float]] = defaultdict(list)
    for listing in listings:
        if (
            listing.listing_kind != "rent"
            or not listing.is_active
            or bool(listing.is_duplicate)
            or not listing.neighborhood
            or not listing.price_per_sqm_eur
            or listing.price_per_sqm_eur <= 0
        ):
            continue
        value = float(listing.price_per_sqm_eur)
        grouped[(listing.neighborhood, "all")].append(value)
        if listing.rooms is not None:
            grouped[(listing.neighborhood, rooms_bucket(listing.rooms))].append(value)

    return {
        key: {
            "median": round(float(median(values)), 2),
            "count": len(values),
        }
        for key, values in grouped.items()
        if len(values) >= min_group_size
    }


def update_neighborhood_rent_stats(db: Session, snapshot_date=None) -> int:
    """Replace current medians and append one snapshot set for this pipeline run."""
    rows = db.query(Listing).filter(Listing.listing_kind == "rent").all()
    stats = calculate_rent_stats(rows)
    recorded_at = snapshot_date or utc_now()

    db.query(NeighborhoodRentStats).delete(synchronize_session=False)
    for (neighborhood, bucket), values in stats.items():
        common = {
            "neighborhood": neighborhood,
            "rooms_bucket": bucket,
            "median_rent_per_sqm": values["median"],
            "listing_count": values["count"],
        }
        db.add(NeighborhoodRentStats(**common, updated_at=recorded_at))
        db.add(NeighborhoodRentStatsHistory(**common, snapshot_date=recorded_at))
    db.commit()
    return len(stats)


def rent_stats_lookup(db: Session) -> Dict[tuple[str, str], NeighborhoodRentStats]:
    return {
        (row.neighborhood, row.rooms_bucket): row
        for row in db.query(NeighborhoodRentStats).all()
    }


def gross_yield_pct(
    listing: Listing,
    stats: Dict[tuple[str, str], Any],
) -> float | None:
    """Estimate annual gross yield from same-room rent, then neighborhood rent."""
    if (
        listing.listing_kind != "sale"
        # Rent stats come from apartment rentals — applying them to plots or
        # houses produced 300%+ "yields" on the dashboard (TIN-523).
        or listing.property_type != "apartment"
        or not listing.neighborhood
        or not listing.area_sqm
        or not listing.price_eur
        or listing.area_sqm <= 0
        or listing.price_eur <= 0
    ):
        return None

    exact = stats.get((listing.neighborhood, rooms_bucket(listing.rooms)))
    fallback = stats.get((listing.neighborhood, "all"))
    rent_stat = exact or fallback
    if rent_stat is None:
        return None
    rent_per_sqm = (
        rent_stat.median_rent_per_sqm
        if hasattr(rent_stat, "median_rent_per_sqm")
        else rent_stat["median"]
    )
    yield_pct = round(float(rent_per_sqm) * 12 * float(listing.area_sqm) / float(listing.price_eur) * 100, 2)
    # Sofia gross yields run 4-8%; anything past 25% means the listing price
    # is not a whole-property price (part payment, trap, data error) — hide
    # rather than rank garbage at the top of the yield sort.
    return yield_pct if yield_pct <= 25 else None
