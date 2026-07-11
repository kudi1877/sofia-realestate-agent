"""Dashboard data exporter.

Regenerates the two JSON files the Next.js dashboard reads from disk:
  - data/dashboard/data.json          (all listings + neighborhood stats + aggregate)
  - data/dashboard/daily-digest.json  (today's new deals, price drops, hot districts)

Optionally git-commits and pushes the dashboard repo so Vercel auto-deploys.

Path discovery:
  DASHBOARD_REPO_PATH env var (config.py) — default ../sofia-realestate-dashboard.

Auto-push:
  DASHBOARD_AUTO_PUSH env var (config.py) — default on. Set to "0" to skip git ops.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List

from loguru import logger
from sqlalchemy.orm import Session, selectinload

from src.config import DASHBOARD_REPO_PATH, DASHBOARD_DATA_DIR, DASHBOARD_AUTO_PUSH
from src.database.models import Listing, Neighborhood
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
        .options(selectinload(Listing.alerts))
        .filter((Listing.is_duplicate.is_(False)) | (Listing.is_duplicate.is_(None)))
        .filter(
            (Listing.is_active.is_(True))
            | (Listing.last_seen >= cutoff)
        )
        .all()
    )

    listings: List[Dict[str, Any]] = []
    for l in rows:
        zscore, savings_pct = _latest_alert_values_for_listing(l)
        listings.append(
            _omit_none_values(
                {
                    "id": l.id,
                    "source": l.source,
                    "url": l.url,
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
                    # zscore + savings_pct are computed during analysis but stored on
                    # the Alert, not the Listing. The dashboard's anomaly highlighting
                    # uses the latest underpriced alert per listing.
                    "zscore": zscore,
                    "savings_pct": savings_pct,
                    # When we first scraped this ad → effectively the "added on" date
                    # for the user.
                    "first_seen": l.first_seen.isoformat() if l.first_seen else None,
                    # Soft-depreciation flag — true when the listing is still on the
                    # source site (we re-confirmed it in the latest scrape). False
                    # means we haven't seen it for ≥1 scrape but it's within 30d.
                    "is_active": bool(l.is_active),
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
        }
        for h in hoods
    ]

    # Aggregate stats.
    total_listings = len(listings)
    total_deals = sum(1 for l in listings if (l.get("zscore") or 0) <= -1.5)
    prices_per_sqm = [
        l["price_per_sqm_eur"] for l in listings if l["price_per_sqm_eur"]
    ]
    avg_price_per_sqm = (
        round(sum(prices_per_sqm) / len(prices_per_sqm), 2) if prices_per_sqm else 0
    )

    return {
        "listings": listings,
        "neighborhoods": neighborhoods,
        "stats": {
            "totalListings": total_listings,
            "totalDeals": total_deals,
            "avgPricePerSqm": avg_price_per_sqm,
        },
        "updatedAt": utc_now().isoformat(),
    }


def _omit_none_values(item: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in item.items() if value is not None}


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


# ── Daily digest JSON ─────────────────────────────────────────────────────────


def _build_digest_payload(db: Session) -> Dict[str, Any]:
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
            _html, _plain, ctx = generate_daily_email()
        except Exception as e:
            logger.warning(f"Daily digest render failed, using database fallbacks: {e}")

    from src.analysis.trends import detect_price_drops, generate_market_summary

    market_summary = generate_market_summary(db)
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
        "hot_districts": [_dashboard_district(h) for h in (ctx.get("hot_districts") or [])],
        "top_pick":      _dashboard_deal(ctx.get("top_pick")) if ctx.get("top_pick") else None,
    }


def _empty_digest() -> Dict[str, Any]:
    return {
        "generated_at": utc_now().isoformat(),
        "summary": {"total_active": 0, "off_market": 0, "new_today": 0, "price_drops": 0, "hot_deals": 0},
        "new_deals": [], "price_drops": [], "hot_districts": [], "top_pick": None,
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


def _dashboard_deal(d: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id":                d.get("id"),
        "source":            d.get("source"),
        "url":               d.get("url"),
        "title":             d.get("title") or d.get("neighborhood"),
        "price_eur":         _to_num(d.get("price_eur")),
        "area_sqm":          _to_num(d.get("area_sqm")),
        "price_per_sqm_eur": _to_num(d.get("price_per_sqm") or d.get("price_per_sqm_eur")),
        "neighborhood":      d.get("neighborhood"),
        "property_type":     d.get("property_type"),
        "rooms":             _to_num(d.get("rooms")),
        "zscore":            _to_num(d.get("zscore")),
        "savings_pct":       _to_num(d.get("savings_pct")),
    }


def _dashboard_drop(d: Dict[str, Any]) -> Dict[str, Any]:
    base = _dashboard_deal(d)
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


def _dashboard_district(h: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "neighborhood":       h.get("neighborhood"),
        "new_listings":       int(_to_num(h.get("new_listings")) or 0),
        "avg_price_per_sqm":  _to_num(h.get("avg_price_per_sqm") or h.get("avg_price")) or 0,
        "deal_count":         int(_to_num(h.get("deal_count")) or 0),
    }


# ── File writing ──────────────────────────────────────────────────────────────


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str),
        encoding="utf-8",
    )


def _commit_dashboard_data(repo: Path, message: str) -> bool:
    """Stage dashboard data JSON, commit, and push. No-op if no changes.

    Returns True if a commit was created and pushed, False if no changes (or fail).
    """
    changed = changed_files(repo)
    relevant = [
        f for f in changed
        if f in ("data/dashboard/data.json", "data/dashboard/daily-digest.json")
    ]
    if not relevant:
        logger.info(f"No dashboard JSON changes in {repo} — skipping commit/push")
        return False

    pushed = commit_and_push(repo, files=relevant, message=message)
    if pushed:
        logger.info(f"Pushed dashboard data update to origin/main ({len(relevant)} file(s))")
    return pushed


# ── Public entry point ────────────────────────────────────────────────────────


def export_dashboard(db: Session, push: bool | None = None) -> Dict[str, Any]:
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
    digest_payload = _build_digest_payload(db)

    _write_json(data_dir / "data.json", listings_payload)
    _write_json(data_dir / "daily-digest.json", digest_payload)

    summary = {
        "ok": True,
        "listings": listings_payload["stats"]["totalListings"],
        "deals": listings_payload["stats"]["totalDeals"],
        "neighborhoods": len(listings_payload["neighborhoods"]),
        "wrote": ["data/dashboard/data.json", "data/dashboard/daily-digest.json"],
        "pushed": False,
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
