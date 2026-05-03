"""Dashboard data exporter.

Regenerates the two JSON files the Next.js dashboard reads from disk:
  - public/data.json          (all listings + neighborhood stats + aggregate)
  - public/daily-digest.json  (today's new deals, price drops, hot districts)

Optionally git-commits and pushes the dashboard repo so Vercel auto-deploys.

Path discovery:
  DASHBOARD_REPO_PATH env var (config.py) — default ../sofia-realestate-dashboard.

Auto-push:
  DASHBOARD_AUTO_PUSH env var (config.py) — default on. Set to "0" to skip git ops.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from loguru import logger
from sqlalchemy.orm import Session

from src.config import DASHBOARD_REPO_PATH, DASHBOARD_AUTO_PUSH
from src.database.models import Listing, Neighborhood


# ── Listings JSON ─────────────────────────────────────────────────────────────


def _build_listings_payload(db: Session) -> Dict[str, Any]:
    """Build the data.json structure consumed by the dashboard.

    Schema (must match src/lib/types.ts in the dashboard repo):
      { listings: Listing[], neighborhoods: Neighborhood[], stats: {...}, updatedAt }
    """
    # Active, non-duplicate listings only — that's what the dashboard should show.
    active = (
        db.query(Listing)
        .filter(Listing.is_active.is_(True))
        .filter((Listing.is_duplicate.is_(False)) | (Listing.is_duplicate.is_(None)))
        .all()
    )

    listings: List[Dict[str, Any]] = []
    for l in active:
        listings.append(
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
                "floor": l.floor,
                "total_floors": l.total_floors,
                # zscore + savings_pct are computed during analysis but stored on
                # the Alert, not the Listing. The dashboard's anomaly highlighting
                # uses the latest underpriced alert per listing.
                "zscore": _latest_zscore_for_listing(l),
                "savings_pct": _latest_savings_pct_for_listing(l),
                # When we first scraped this ad → effectively the "added on" date
                # for the user. last_seen tells you it's still being re-scraped
                # (i.e. still active on the source site as of that timestamp).
                "first_seen": l.first_seen.isoformat() if l.first_seen else None,
                "last_seen": l.last_seen.isoformat() if l.last_seen else None,
            }
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
    total_deals = sum(1 for l in listings if (l["zscore"] or 0) <= -1.5)
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
        "updatedAt": datetime.utcnow().isoformat(),
    }


def _latest_zscore_for_listing(l: Listing) -> float | None:
    """Pull the zscore from the most recent 'underpriced' alert, if any."""
    for alert in sorted(l.alerts or [], key=lambda a: a.id, reverse=True):
        if alert.alert_type == "underpriced" and alert.zscore is not None:
            return round(float(alert.zscore), 3)
    return None


def _latest_savings_pct_for_listing(l: Listing) -> float | None:
    for alert in sorted(l.alerts or [], key=lambda a: a.id, reverse=True):
        if alert.alert_type == "underpriced" and alert.savings_pct is not None:
            return round(float(alert.savings_pct), 1)
    return None


# ── Daily digest JSON ─────────────────────────────────────────────────────────


def _build_digest_payload(db: Session) -> Dict[str, Any]:
    """Build daily-digest.json — summary card data for the dashboard.

    Reuses daily_email's render path so the schema matches what the dashboard
    already expects (we don't want two diverging digest formats).
    """
    try:
        from src.alerts.daily_email import generate_daily_email  # heavy import
    except Exception as e:
        logger.warning(f"Could not import daily_email for digest: {e}")
        return {"generated_at": datetime.utcnow().isoformat(), "summary": {}}

    try:
        # generate_daily_email returns (html, plain, context); we only want context.
        _html, _plain, context = generate_daily_email()
        return {**context, "generated_at_iso": datetime.utcnow().isoformat()}
    except Exception as e:
        logger.warning(f"Daily digest render failed, falling back to minimal payload: {e}")
        return {
            "generated_at": datetime.utcnow().isoformat(),
            "summary": {},
            "new_deals": [],
            "price_drops": [],
            "hot_districts": [],
        }


# ── File writing ──────────────────────────────────────────────────────────────


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


def _files_changed_in_git(repo: Path) -> List[str]:
    """Return list of paths that are dirty in `repo` (relative to repo root)."""
    result = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return []
    return [line[3:] for line in result.stdout.splitlines() if line.strip()]


# ── Git commit + push ─────────────────────────────────────────────────────────


def _commit_and_push(repo: Path, message: str) -> bool:
    """Stage data.json + daily-digest.json, commit, and push. No-op if no changes.

    Returns True if a commit was created and pushed, False if no changes (or fail).
    """
    changed = _files_changed_in_git(repo)
    relevant = [f for f in changed if f in ("public/data.json", "public/daily-digest.json")]
    if not relevant:
        logger.info(f"No dashboard JSON changes in {repo} — skipping commit/push")
        return False

    try:
        subprocess.run(
            ["git", "-C", str(repo), "add", *relevant],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-m", message],
            check=True,
            capture_output=True,
            text=True,
        )
        push = subprocess.run(
            ["git", "-C", str(repo), "push", "origin", "main"],
            capture_output=True,
            text=True,
            check=False,
        )
        if push.returncode != 0:
            logger.error(f"Dashboard push failed: {push.stderr.strip()}")
            return False
        logger.info(f"Pushed dashboard data update to origin/main ({len(relevant)} file(s))")
        return True
    except subprocess.CalledProcessError as e:
        logger.error(
            f"Git op failed (rc={e.returncode}): "
            f"stderr={e.stderr.strip() if e.stderr else ''}"
        )
        return False


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
    public = repo / "public"
    if not public.exists():
        logger.error(
            f"Dashboard public/ dir not found at {public}. "
            f"Set DASHBOARD_REPO_PATH env var to the dashboard repo root."
        )
        return {"ok": False, "reason": "dashboard_path_missing", "path": str(public)}

    logger.info(f"Exporting dashboard data → {public}")

    listings_payload = _build_listings_payload(db)
    digest_payload = _build_digest_payload(db)

    _write_json(public / "data.json", listings_payload)
    _write_json(public / "daily-digest.json", digest_payload)

    summary = {
        "ok": True,
        "listings": listings_payload["stats"]["totalListings"],
        "deals": listings_payload["stats"]["totalDeals"],
        "neighborhoods": len(listings_payload["neighborhoods"]),
        "wrote": ["public/data.json", "public/daily-digest.json"],
        "pushed": False,
    }
    logger.info(
        f"Wrote {summary['listings']} listings, {summary['deals']} deals, "
        f"{summary['neighborhoods']} neighborhoods"
    )

    if push:
        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        msg = (
            f"data: refresh dashboard ({timestamp})\n\n"
            f"{summary['listings']} listings, {summary['deals']} deals, "
            f"{summary['neighborhoods']} neighborhoods.\n"
            f"Auto-generated by sofia-realestate-agent."
        )
        summary["pushed"] = _commit_and_push(repo, msg)

    return summary
