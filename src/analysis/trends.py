"""Trend analysis for neighborhood prices."""

from collections import defaultdict
from datetime import timedelta
from statistics import fmean, median
from typing import Any, Dict, List

from sqlalchemy.orm import Session

from src.database.models import (
    Listing,
    Neighborhood,
    NeighborhoodStatsHistory,
    PriceHistory,
)
from src.utils.time import utc_now


def _is_unique_listing_clause():
    return (Listing.is_duplicate.is_(False)) | (Listing.is_duplicate.is_(None))


def _active_unique_prices(db: Session):
    return db.query(
        Listing.neighborhood,
        Listing.property_type,
        Listing.price_per_sqm_eur,
    ).filter(
        Listing.is_active.is_(True),
        _is_unique_listing_clause(),
        Listing.price_per_sqm_eur > 0,
    ).all()


def calculate_neighborhood_trends(db: Session, days: int = 30) -> Dict[str, Any]:
    """Compare today's active-listing median with a same-metric snapshot."""
    prices_by_neighborhood = defaultdict(list)
    for neighborhood, _property_type, price_per_sqm in _active_unique_prices(db):
        prices_by_neighborhood[neighborhood].append(float(price_per_sqm))

    trends = {}
    for neighborhood, prices in prices_by_neighborhood.items():
        current_median = float(median(prices))
        current_mean = float(fmean(prices))
        trends[neighborhood] = {
            "current_price_per_sqm": current_median,
            "current_median": current_median,
            "current_mean": current_mean,
            "current_avg": current_mean,
            "listing_count": len(prices),
            "trend": "insufficient history",
        }

    cutoff_date = utc_now() - timedelta(days=days)
    historical_rows = db.query(NeighborhoodStatsHistory).filter(
        NeighborhoodStatsHistory.snapshot_date <= cutoff_date
    ).order_by(
        NeighborhoodStatsHistory.neighborhood,
        NeighborhoodStatsHistory.snapshot_date.desc(),
    ).all()

    previous_by_neighborhood = {}
    for snapshot in historical_rows:
        previous_by_neighborhood.setdefault(snapshot.neighborhood, snapshot)

    for neighborhood, current_data in trends.items():
        snapshot = previous_by_neighborhood.get(neighborhood)
        if snapshot is None or snapshot.median_price_per_sqm <= 0:
            continue

        previous_median = float(snapshot.median_price_per_sqm)
        pct_change = (
            (current_data["current_median"] - previous_median)
            / previous_median
            * 100
        )
        if pct_change > 5:
            direction = "up"
        elif pct_change < -5:
            direction = "down"
        else:
            direction = "stable"

        current_data.update(
            {
                "trend": direction,
                "pct_change_30d": round(pct_change, 2),
                "previous_median": round(previous_median, 2),
                "previous_snapshot_date": snapshot.snapshot_date.isoformat(),
            }
        )

    return trends


def get_price_history(db: Session, listing_id: int) -> List[Dict[str, Any]]:
    """Get price history for a specific listing."""
    history = db.query(PriceHistory).filter(
        PriceHistory.listing_id == listing_id
    ).order_by(PriceHistory.recorded_at).all()

    return [
        {
            "price_eur": row.price_eur,
            "price_per_sqm_eur": row.price_per_sqm_eur,
            "recorded_at": row.recorded_at.isoformat(),
        }
        for row in history
    ]


def detect_price_drops(db: Session, days: int = 7) -> List[Dict[str, Any]]:
    """Detect listings with recent price drops."""
    cutoff_date = utc_now() - timedelta(days=days)
    history = db.query(PriceHistory, Listing).join(Listing).filter(
        PriceHistory.recorded_at >= cutoff_date
    ).all()

    by_listing = defaultdict(list)
    for price_history, listing in history:
        by_listing[listing.id].append((price_history, listing))

    drops = []
    for entries in by_listing.values():
        if len(entries) < 2:
            continue

        entries.sort(key=lambda item: item[0].recorded_at)
        first_price = entries[0][0].price_eur
        last_price = entries[-1][0].price_eur

        if last_price < first_price:
            drop_pct = ((first_price - last_price) / first_price) * 100
            if drop_pct >= 5:
                drops.append(
                    {
                        "listing": entries[-1][1],
                        "original_price": first_price,
                        "current_price": last_price,
                        "drop_eur": first_price - last_price,
                        "drop_pct": round(drop_pct, 2),
                    }
                )

    drops.sort(key=lambda item: item["drop_pct"], reverse=True)
    return drops


def _summarize_prices(prices: List[float]) -> Dict[str, float]:
    if not prices:
        return {"median": 0, "mean": 0}
    return {
        "median": round(float(median(prices)), 2),
        "mean": round(float(fmean(prices)), 2),
    }


def generate_market_summary(db: Session) -> Dict[str, Any]:
    """Generate deduplicated, median-first market summary statistics."""
    rows = _active_unique_prices(db)
    all_prices = [float(row.price_per_sqm_eur) for row in rows]
    overall = _summarize_prices(all_prices)

    prices_by_type = defaultdict(list)
    for _neighborhood, property_type, price_per_sqm in rows:
        prices_by_type[property_type].append(float(price_per_sqm))

    by_property_type = {}
    for property_type, prices in prices_by_type.items():
        summary = _summarize_prices(prices)
        by_property_type[property_type] = {
            "price_per_sqm": summary["median"],
            "median_price": summary["median"],
            "avg_price": summary["mean"],
            "count": len(prices),
        }

    zone_by_neighborhood = {
        row.name: row.zone
        for row in db.query(Neighborhood).filter(Neighborhood.zone.isnot(None)).all()
    }
    prices_by_zone = defaultdict(list)
    for neighborhood, _property_type, price_per_sqm in rows:
        zone = zone_by_neighborhood.get(neighborhood)
        if zone:
            prices_by_zone[zone].append(float(price_per_sqm))

    return {
        "total_listings": len(rows),
        "price_per_sqm": overall["median"],
        "median_price_per_sqm": overall["median"],
        "avg_price_per_sqm": overall["mean"],
        "by_property_type": by_property_type,
        "by_zone": {
            zone: _summarize_prices(prices)["median"]
            for zone, prices in prices_by_zone.items()
        },
        "generated_at": utc_now().isoformat(),
    }
