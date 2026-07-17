"""Daily email digest generator for Sofia Real Estate Intelligence.

Generates HTML email + plain-text fallback from the SQLite database.
Can also be invoked with sample data for testing / dashboard previews.
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Any, Dict, List, Optional, Tuple

from jinja2 import Environment, FileSystemLoader
from sqlalchemy import and_, create_engine, or_
from sqlalchemy.orm import Session, sessionmaker

from src.config import (
    ANOMALY_ZSCORE_THRESHOLD,
    AUTHENTICITY_DEAL_MIN_SCORE,
    DASHBOARD_DATA_DIR,
    DEAL_ENGINE,
    HEDONIC_DEAL_RESIDUAL_PCT,
    MAX_APARTMENT_AREA_SQM,
    MIN_APARTMENT_AREA_SQM,
    MIN_APARTMENT_PRICE_PER_SQM_EUR,
    MIN_PRICE_EUR,
    PRICE_DROP_PCT_THRESHOLD,
)
from src.analysis.hedonic import effective_deal_engine
from src.database.models import Alert, Listing, get_db
from src.enrichment.llm_extract import hard_traps
from src.database.repository import ListingRepository
from src.utils.time import utc_now

# ─── Paths ────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent.parent.parent          # project root
TEMPLATES_DIR = BASE_DIR / "templates"
DATA_DIR = BASE_DIR / "data"

# ─── Helpers ──────────────────────────────────────────────────────────────────

def _fmt_price(price: float | None) -> str:
    """Format price with thousands separator."""
    if price is None:
        return "N/A"
    return f"{price:,.0f}".replace(",", " ")


def _room_text(rooms: int | None) -> str:
    """Bulgarian-style room description."""
    if rooms is None or rooms == 0:
        return "студио"
    mapping = {1: "1-стаен", 2: "2-стаен", 3: "3-стаен", 4: "4-стаен", 5: "5-стаен"}
    return mapping.get(rooms, "многостаен")


def _construction_text(ct: str | None) -> str:
    mapping = {"brick": "Тухла", "panel": "Панел", "epk": "ЕПК", "steel": "Сглобяема"}
    return mapping.get(ct or "", ct or "")


# ─── Database queries ─────────────────────────────────────────────────────────


def _unique_listing_clause():
    return (Listing.is_duplicate.is_(False)) | (Listing.is_duplicate.is_(None))


def _sale_listing_clause():
    return Listing.listing_kind == "sale"


def _sane_price_clause():
    """Defense in depth vs price-parse artifacts (TIN-472: a €6 'Top Pick')."""
    return Listing.price_eur >= MIN_PRICE_EUR


def _sane_apartment_area_clause():
    """Reject implausible apartment size and €/m² source misclassifications."""
    return and_(
        Listing.area_sqm.between(MIN_APARTMENT_AREA_SQM, MAX_APARTMENT_AREA_SQM),
        Listing.price_per_sqm_eur >= MIN_APARTMENT_PRICE_PER_SQM_EUR,
    )


def _authenticity_clause():
    return or_(
        Listing.authenticity_score.is_(None),
        Listing.authenticity_score >= AUTHENTICITY_DEAL_MIN_SCORE,
    )


def _group_medians(db: Session) -> Dict[tuple[str, str], float]:
    grouped = defaultdict(list)
    rows = db.query(
        Listing.neighborhood,
        Listing.property_type,
        Listing.price_per_sqm_eur,
    ).filter(
        Listing.is_active.is_(True),
        _sale_listing_clause(),
        _unique_listing_clause(),
        _sane_price_clause(),
        Listing.price_per_sqm_eur > 0,
    ).all()
    for neighborhood, property_type, price_per_sqm in rows:
        grouped[(neighborhood, property_type)].append(float(price_per_sqm))
    return {key: float(median(prices)) for key, prices in grouped.items()}


def _deal_payload(listing: Listing, group_price: float, *, hedonic: bool = False) -> Dict[str, Any]:
    price_per_sqm = float(listing.price_per_sqm_eur or 0)
    savings_eur = (
        (group_price - price_per_sqm) * float(listing.area_sqm or 1)
        if group_price
        else 0
    )
    savings_pct = (
        (group_price - price_per_sqm) / group_price * 100
        if group_price
        else 0
    )
    return {
        "id": listing.id,
        "neighborhood": listing.neighborhood or "Unknown",
        "rooms_text": _room_text(listing.rooms),
        "area_sqm": f"{listing.area_sqm or 0:.0f}",
        "construction_type": _construction_text(listing.construction_type),
        "floor": listing.floor,
        "total_floors": listing.total_floors,
        "price_eur": _fmt_price(listing.price_eur),
        "price_per_sqm": _fmt_price(listing.price_per_sqm_eur),
        "zscore": "N/A" if hedonic else f"< {ANOMALY_ZSCORE_THRESHOLD:g}",
        "savings_eur": _fmt_price(max(savings_eur, 0)),
        "savings_pct": f"{max(savings_pct, 0):.1f}",
        "url": listing.url or "#",
        "image_url": listing.image_url or None,
    }


def _query_new_deals(db: Session, hours: int = 24) -> List[Dict[str, Any]]:
    """Recent underpriced listings using deduplicated median baselines."""
    cutoff = utc_now() - timedelta(hours=hours)
    engine = effective_deal_engine(DEAL_ENGINE)
    if engine.startswith("hedonic"):
        rows = db.query(Listing).filter(
            Listing.is_active.is_(True),
            _sale_listing_clause(),
            _unique_listing_clause(),
            _sane_price_clause(),
            _sane_apartment_area_clause(),
            _authenticity_clause(),
            Listing.property_type == "apartment",
            Listing.first_seen >= cutoff,
            Listing.predicted_price_per_sqm.isnot(None),
            Listing.residual_pct <= HEDONIC_DEAL_RESIDUAL_PCT,
        ).order_by(Listing.residual_pct.asc()).limit(10).all()
        return [
            _deal_payload(listing, float(listing.predicted_price_per_sqm), hedonic=True)
            for listing in rows
        ]

    group_prices = _group_medians(db)
    rows = db.query(Listing).join(Alert).filter(
        Listing.is_active.is_(True),
        _sale_listing_clause(),
        _unique_listing_clause(),
        _sane_price_clause(),
        _authenticity_clause(),
        Listing.first_seen >= cutoff,
        Alert.alert_type == "underpriced",
        Alert.zscore.isnot(None),
        Alert.zscore < ANOMALY_ZSCORE_THRESHOLD,
    ).order_by(Alert.zscore.asc()).limit(10).all()

    if not rows:
        candidates = db.query(Listing).filter(
            Listing.is_active.is_(True),
            _sale_listing_clause(),
            _unique_listing_clause(),
            _sane_price_clause(),
            _authenticity_clause(),
            Listing.first_seen >= cutoff,
            Listing.price_per_sqm_eur > 0,
        ).all()
        rows = sorted(
            candidates,
            key=lambda listing: (
                float(listing.price_per_sqm_eur)
                / group_prices.get((listing.neighborhood, listing.property_type), 1)
            ),
        )[:10]

    return [
        _deal_payload(
            listing,
            group_prices.get((listing.neighborhood, listing.property_type), 0),
        )
        for listing in rows
    ]


def _query_price_drops(
    db: Session,
    min_drop_pct: float = PRICE_DROP_PCT_THRESHOLD,
) -> List[Dict[str, Any]]:
    """Active unique listings below their first recorded price."""
    rows = [
        listing
        for listing in ListingRepository(db).get_price_drops(min_drop_pct=min_drop_pct)
        if listing.authenticity_score is None
        or listing.authenticity_score >= AUTHENTICITY_DEAL_MIN_SCORE
    ][:8]
    return [
        {
            "id": listing.id,
            "neighborhood": listing.neighborhood or "Unknown",
            "rooms_text": _room_text(listing.rooms),
            "area_sqm": f"{listing.area_sqm or 0:.0f}",
            "old_price": _fmt_price(listing.first_price_eur),
            "new_price": _fmt_price(listing.price_eur),
            "drop_pct": f"{((listing.first_price_eur - listing.price_eur) / listing.first_price_eur * 100):.1f}",
            "url": listing.url or "#",
            "image_url": listing.image_url or None,
        }
        for listing in rows
    ]


def _query_district_velocity(db: Session, days: int = 7) -> List[Dict[str, Any]]:
    """Per-district unique supply and off-market velocity with median pricing."""
    cutoff = utc_now() - timedelta(days=days)
    rows = db.query(Listing).filter(
        Listing.neighborhood.isnot(None),
        _sale_listing_clause(),
        _unique_listing_clause(),
    ).order_by(Listing.id).all()
    grouped = defaultdict(list)
    for listing in rows:
        grouped[listing.neighborhood].append(listing)

    districts = []
    for neighborhood, listings in grouped.items():
        if len(listings) < 5:
            continue
        added = sum(
            1 for listing in listings
            if listing.first_seen and listing.first_seen >= cutoff
        )
        sold = sum(
            1 for listing in listings
            if not listing.is_active
            and listing.last_seen
            and listing.last_seen >= cutoff
        )
        active_prices = [
            float(listing.price_per_sqm_eur)
            for listing in listings
            if listing.is_active and listing.price_per_sqm_eur is not None
        ]
        district_price = round(float(median(active_prices))) if active_prices else None
        velocity = added - sold
        if velocity > 2:
            label = "🔥 Hot supply"
        elif velocity < -2:
            label = "⚡ Fast selling"
        elif sold > added:
            label = "📈 Absorbing"
        else:
            label = "➡️ Stable"
        districts.append(
            {
                "name": neighborhood or "Unknown",
                "added": added,
                "sold": sold,
                "avg_price": _fmt_price(district_price),
                "velocity_score": velocity,
                "velocity_label": label,
            }
        )

    districts.sort(key=lambda district: district["added"], reverse=True)
    return districts[:8]


def _top_pick_zscore(db: Session, listing: Listing) -> Optional[float]:
    """Gate a Top Pick candidate (TIN-521): needs a photo, no hard traps, and
    a numeric underpriced z-score — otherwise the digest card renders a
    placeholder image and 'Z-score: N/A'. Returns the z-score or None."""
    if not listing.image_url:
        return None
    if hard_traps(listing.llm_extract):
        return None
    alert = (
        db.query(Alert)
        .filter(Alert.listing_id == listing.id, Alert.alert_type == "underpriced", Alert.zscore.isnot(None))
        .order_by(Alert.id.desc())
        .first()
    )
    return float(alert.zscore) if alert else None


def _query_top_pick(db: Session) -> Optional[Dict[str, Any]]:
    """Best active unique apartment opportunity against its median baseline."""
    group_prices = _group_medians(db)
    engine = effective_deal_engine(DEAL_ENGINE)
    top_zscore: Optional[float] = None
    if engine.startswith("hedonic"):
        candidates = db.query(Listing).filter(
            Listing.is_active.is_(True),
            _sale_listing_clause(),
            _unique_listing_clause(),
            _sane_price_clause(),
            _sane_apartment_area_clause(),
            _authenticity_clause(),
            Listing.property_type == "apartment",
            Listing.image_url.isnot(None),
            Listing.predicted_price_per_sqm.isnot(None),
            Listing.residual_pct <= HEDONIC_DEAL_RESIDUAL_PCT,
        ).order_by(Listing.residual_pct.asc()).limit(25).all()
        listing = None
        for candidate in candidates:
            top_zscore = _top_pick_zscore(db, candidate)
            if top_zscore is not None:
                listing = candidate
                break
        if listing is None:
            return None
        stored_savings_pct = max(0.0, -float(listing.residual_pct or 0))
        stored_savings_eur = max(
            0.0,
            (float(listing.predicted_price_per_sqm) - float(listing.price_per_sqm_eur))
            * float(listing.area_sqm),
        )
        group_price = float(listing.predicted_price_per_sqm)
    else:
        alert_rows = db.query(Listing, Alert).join(Alert).filter(
            Listing.is_active.is_(True),
            _sale_listing_clause(),
            _unique_listing_clause(),
            _sane_price_clause(),
            _sane_apartment_area_clause(),
            _authenticity_clause(),
            Listing.property_type == "apartment",
            Listing.image_url.isnot(None),
            Alert.zscore < ANOMALY_ZSCORE_THRESHOLD,
            Alert.savings_pct > 0,
        ).order_by(Alert.savings_pct.desc()).limit(25).all()

        listing = None
        for candidate, alert in alert_rows:
            top_zscore = _top_pick_zscore(db, candidate)
            if top_zscore is not None:
                listing = candidate
                stored_savings_pct = alert.savings_pct
                stored_savings_eur = alert.savings_eur
                break
        if listing is None:
            candidates = db.query(Listing).filter(
                Listing.is_active.is_(True),
                _sale_listing_clause(),
                _unique_listing_clause(),
                _sane_price_clause(),
                _sane_apartment_area_clause(),
                _authenticity_clause(),
                Listing.property_type == "apartment",
                Listing.image_url.isnot(None),
                Listing.price_per_sqm_eur > 0,
            ).all()
            candidates = [
                row for row in candidates
                if group_prices.get((row.neighborhood, "apartment"), 0) > 0
            ]
            candidates.sort(
                key=lambda candidate: (
                    float(candidate.price_per_sqm_eur)
                    / group_prices[(candidate.neighborhood, "apartment")]
                ),
            )
            for candidate in candidates[:25]:
                top_zscore = _top_pick_zscore(db, candidate)
                if top_zscore is not None:
                    listing = candidate
                    break
            if listing is None:
                return None
            stored_savings_pct = None
            stored_savings_eur = None

        group_price = group_prices.get((listing.neighborhood, listing.property_type), 0)
    price_sqm = float(listing.price_per_sqm_eur or 0)
    savings_pct = stored_savings_pct or (
        (group_price - price_sqm) / group_price * 100 if group_price else 0
    )
    savings_eur = stored_savings_eur or (
        (group_price - price_sqm) * float(listing.area_sqm or 1)
        if group_price
        else 0
    )

    reasons = []
    if savings_pct > 20:
        reasons.append(
            f"Priced {savings_pct:.0f}% below "
            f"{'model expectation' if engine.startswith('hedonic') else 'neighborhood average'}"
        )
    elif savings_pct > 10:
        reasons.append(
            f"Solid {savings_pct:.0f}% discount vs. "
            f"{'model expectation' if engine.startswith('hedonic') else 'market'}"
        )
    else:
        reasons.append(f"Below average pricing at €{_fmt_price(price_sqm)}/m²")
    if listing.construction_type == "brick":
        reasons.append("Brick construction (premium quality)")
    if listing.floor and listing.total_floors:
        if listing.floor > 1 and listing.floor < listing.total_floors:
            reasons.append(f"Mid-floor ({listing.floor}/{listing.total_floors}) — optimal")
    if listing.days_on_market and listing.days_on_market < 7:
        reasons.append("Fresh listing — just appeared")
    elif listing.price_changes and listing.price_changes > 0:
        reasons.append("Seller reduced price — motivated")

    return {
        "id": listing.id,
        "neighborhood": listing.neighborhood or "Unknown",
        "rooms_text": _room_text(listing.rooms),
        "area_sqm": f"{listing.area_sqm or 0:.0f}",
        "construction_type": _construction_text(listing.construction_type),
        "price_eur": _fmt_price(listing.price_eur),
        "price_per_sqm": _fmt_price(price_sqm),
        "savings_pct": f"{max(savings_pct, 0):.0f}",
        "savings_eur": _fmt_price(max(savings_eur, 0)),
        "reasoning": ". ".join(reasons) + ".",
        "url": listing.url or "#",
        "image_url": listing.image_url,
        "zscore": top_zscore,
    }


def _get_total_active(db: Session) -> int:
    return db.query(Listing).filter(
        Listing.is_active.is_(True),
        _sale_listing_clause(),
        _unique_listing_clause(),
    ).count()


def _get_new_today_count(db: Session, hours: int = 24) -> int:
    cutoff = utc_now() - timedelta(hours=hours)
    return db.query(Listing).filter(
        Listing.first_seen >= cutoff,
        _sale_listing_clause(),
        _unique_listing_clause(),
    ).count()


def _session_for_path(db_path: str) -> Session:
    engine = create_engine(f"sqlite:///{Path(db_path).expanduser().resolve()}")
    return sessionmaker(bind=engine)()


# ─── Sample data for testing ─────────────────────────────────────────────────

def _sample_data() -> Dict[str, Any]:
    """Sample context for template rendering when no database is available."""
    return {
        "date": datetime.now().strftime("%B %d, %Y"),
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "preheader_text": "3 new deals • 2 price drops • Лозенец is hot today",
        "total_active": 1247,
        "new_today": 23,
        "price_drops_count": 5,
        "dashboard_url": "https://sofia-realestate.vercel.app",
        "unsubscribe_url": "#unsubscribe",
        "new_deals": [
            {
                "neighborhood": "Лозенец",
                "rooms_text": "2-стаен",
                "area_sqm": "72",
                "construction_type": "Тухла",
                "floor": 4,
                "total_floors": 8,
                "price_eur": "95 000",
                "price_per_sqm": "1 319",
                "zscore": "-2.14",
                "savings_eur": "18 500",
                "savings_pct": "16.3",
                "url": "https://www.imot.bg/example1",
            },
            {
                "neighborhood": "Младост 1",
                "rooms_text": "3-стаен",
                "area_sqm": "95",
                "construction_type": "Панел",
                "floor": 6,
                "total_floors": 8,
                "price_eur": "110 000",
                "price_per_sqm": "1 158",
                "zscore": "-1.87",
                "savings_eur": "14 200",
                "savings_pct": "11.4",
                "url": "https://www.imot.bg/example2",
            },
            {
                "neighborhood": "Център",
                "rooms_text": "1-стаен",
                "area_sqm": "48",
                "construction_type": "Тухла",
                "floor": 3,
                "total_floors": 6,
                "price_eur": "68 000",
                "price_per_sqm": "1 417",
                "zscore": "-1.62",
                "savings_eur": "9 800",
                "savings_pct": "12.6",
                "url": "https://www.imot.bg/example3",
            },
        ],
        "price_drops": [
            {
                "neighborhood": "Витоша",
                "rooms_text": "3-стаен",
                "area_sqm": "102",
                "old_price": "185 000",
                "new_price": "165 000",
                "drop_pct": "10.8",
                "url": "https://www.imot.bg/example4",
            },
            {
                "neighborhood": "Студентски град",
                "rooms_text": "2-стаен",
                "area_sqm": "65",
                "old_price": "92 000",
                "new_price": "85 000",
                "drop_pct": "7.6",
                "url": "https://www.imot.bg/example5",
            },
        ],
        "hot_districts": [
            {"name": "Лозенец", "added": 12, "sold": 4, "avg_price": "1 890", "velocity_score": 8, "velocity_label": "🔥 Hot supply"},
            {"name": "Младост 1", "added": 8, "sold": 11, "avg_price": "1 350", "velocity_score": -3, "velocity_label": "⚡ Fast selling"},
            {"name": "Център", "added": 6, "sold": 5, "avg_price": "2 150", "velocity_score": 1, "velocity_label": "➡️ Stable"},
            {"name": "Витоша", "added": 5, "sold": 7, "avg_price": "1 620", "velocity_score": -2, "velocity_label": "📈 Absorbing"},
            {"name": "Люлин", "added": 9, "sold": 3, "avg_price": "980", "velocity_score": 6, "velocity_label": "🔥 Hot supply"},
        ],
        "top_pick": {
            "neighborhood": "Лозенец",
            "rooms_text": "2-стаен",
            "area_sqm": "72",
            "construction_type": "Тухла",
            "price_eur": "95 000",
            "price_per_sqm": "1 319",
            "savings_pct": "16",
            "savings_eur": "18 500",
            "reasoning": "Priced 16% below Лозенец average. Brick construction (premium quality). Mid-floor (4/8) — optimal. Fresh listing — just appeared.",
            "url": "https://www.imot.bg/example1",
        },
    }


# ─── Main generator ──────────────────────────────────────────────────────────

def generate_daily_email(
    db_path: str | None = None,
    dashboard_url: str = "https://sofia-realestate.vercel.app",
    use_sample: bool = False,
    db: Session | None = None,
) -> Tuple[str, str, Dict[str, Any]]:
    """Generate the daily email digest.

    Args:
        db_path: Optional SQLite path for CLI/backward compatibility.
        dashboard_url: URL to the live dashboard.
        use_sample: If True, use sample data (for testing / preview).
        db: Existing shared SQLAlchemy session. Takes precedence over db_path.

    Returns:
        Tuple of (html_content, plain_text, context_dict).
        context_dict is the raw data used — useful for the dashboard API.
    """
    if use_sample or (db is None and db_path is None):
        if db_path is None:
            default_db = DATA_DIR / "listings.db"
            if not default_db.exists():
                use_sample = True

    if use_sample:
        context = _sample_data()
    else:
        owns_session = db is None
        if db is None:
            db = _session_for_path(db_path) if db_path else get_db()
        try:
            new_deals = _query_new_deals(db)
            price_drops = _query_price_drops(db)
            hot_districts = _query_district_velocity(db)
            top_pick = _query_top_pick(db)
            total_active = _get_total_active(db)
            new_today = _get_new_today_count(db)
        finally:
            if owns_session:
                db.close()

        context = {
            "date": datetime.now().strftime("%B %d, %Y"),
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "preheader_text": f"{len(new_deals)} new deal{'s' if len(new_deals) != 1 else ''} • {len(price_drops)} price drop{'s' if len(price_drops) != 1 else ''}",
            "total_active": total_active,
            "new_today": new_today,
            "price_drops_count": len(price_drops),
            "dashboard_url": dashboard_url,
            "unsubscribe_url": "#unsubscribe",
            "new_deals": new_deals,
            "price_drops": price_drops,
            "hot_districts": hot_districts,
            "top_pick": top_pick,
        }

    # ── Render HTML ───────────────────────────────────────────────────────
    env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)))
    template = env.get_template("daily-email.html")
    html = template.render(**context)

    # ── Plain-text fallback ───────────────────────────────────────────────
    plain = _render_plain_text(context)

    # ── Save latest digest for dashboard consumption ──────────────────────
    digest_path = DATA_DIR / "latest_digest.json"
    digest_payload = {
        **context,
        "generated_at_iso": utc_now().isoformat(),
    }
    try:
        digest_path.write_text(json.dumps(digest_payload, ensure_ascii=False, indent=2, default=str))
    except Exception:
        pass  # non-critical

    # Also copy to the dashboard data dir if that repo exists locally.
    if DASHBOARD_DATA_DIR.parent.exists():
        try:
            DASHBOARD_DATA_DIR.mkdir(parents=True, exist_ok=True)
            (DASHBOARD_DATA_DIR / "daily-digest.json").write_text(
                json.dumps(digest_payload, ensure_ascii=False, indent=2, default=str)
            )
        except Exception:
            pass

    return html, plain, context


def _render_plain_text(ctx: Dict[str, Any]) -> str:
    """Build a plain-text version of the digest."""
    lines = [
        f"🏠 Sofia Real Estate Intelligence — Daily Digest",
        f"📅 {ctx['date']}",
        f"",
        f"═══ Summary ═══",
        f"Total Active: {ctx['total_active']}",
        f"New Today: {ctx['new_today']}",
        f"Price Drops: {ctx['price_drops_count']}",
        f"",
    ]

    if ctx.get("new_deals"):
        lines.append(f"═══ 🔥 New Deals (Z-score < {ANOMALY_ZSCORE_THRESHOLD:g}) ═══")
        for d in ctx["new_deals"]:
            lines.append(f"  📍 {d['neighborhood']} — {d['rooms_text']}, {d['area_sqm']}m²")
            lines.append(f"     €{d['price_eur']} (€{d['price_per_sqm']}/m²) | Z: {d['zscore']} | Save {d['savings_pct']}%")
            lines.append(f"     {d['url']}")
            lines.append("")

    if ctx.get("price_drops"):
        lines.append(f"═══ 📉 Price Drops ({PRICE_DROP_PCT_THRESHOLD:g}%+) ═══")
        for d in ctx["price_drops"]:
            lines.append(f"  📍 {d['neighborhood']} — {d['rooms_text']}, {d['area_sqm']}m²")
            lines.append(f"     €{d['old_price']} → €{d['new_price']} (-{d['drop_pct']}%)")
            lines.append("")

    if ctx.get("hot_districts"):
        lines.append("═══ 🏘️ Hot Districts ═══")
        for d in ctx["hot_districts"]:
            lines.append(f"  {d['name']}: +{d['added']} added, -{d['sold']} sold | €{d['avg_price']}/m² | {d['velocity_label']}")
        lines.append("")

    if ctx.get("top_pick"):
        tp = ctx["top_pick"]
        lines.append("═══ ⭐ Top Pick ═══")
        lines.append(f"  📍 {tp['neighborhood']} — {tp['rooms_text']}, {tp['area_sqm']}m²")
        lines.append(f"  €{tp['price_eur']} (€{tp['price_per_sqm']}/m²) | Save {tp['savings_pct']}%")
        lines.append(f"  💡 {tp['reasoning']}")
        lines.append(f"  {tp['url']}")
        lines.append("")

    lines.append(f"📊 Full dashboard: {ctx.get('dashboard_url', '')}")
    lines.append(f"Generated: {ctx['generated_at']}")

    return "\n".join(lines)


# ─── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Generate daily email digest")
    parser.add_argument("--db", type=str, help="Path to listings.db")
    parser.add_argument("--sample", action="store_true", help="Use sample data")
    parser.add_argument("--output", type=str, help="Write HTML to file")
    args = parser.parse_args()

    html, plain, ctx = generate_daily_email(
        db_path=args.db,
        use_sample=args.sample,
    )

    if args.output:
        Path(args.output).write_text(html)
        print(f"✅ Written to {args.output}")
    else:
        print(plain)
        print(f"\n─── HTML length: {len(html)} chars ───")
        # Write preview
        preview_path = DATA_DIR / "email_preview.html"
        preview_path.write_text(html)
        print(f"📧 HTML preview saved to: {preview_path}")
