"""Daily email digest generator for Sofia Real Estate Intelligence.

Generates HTML email + plain-text fallback from the SQLite database.
Can also be invoked with sample data for testing / dashboard previews.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from jinja2 import Environment, FileSystemLoader

from src.config import ANOMALY_ZSCORE_THRESHOLD, DASHBOARD_DATA_DIR, PRICE_DROP_PCT_THRESHOLD
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

def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _query_new_deals(conn: sqlite3.Connection, hours: int = 24) -> List[Dict[str, Any]]:
    """Listings with configured underpriced Z-score first seen in the last N hours."""
    cutoff = (utc_now() - timedelta(hours=hours)).isoformat()
    rows = conn.execute("""
        SELECT l.*, 
               (SELECT AVG(l2.price_per_sqm_eur) 
                FROM listings l2 
                WHERE l2.neighborhood = l.neighborhood 
                  AND l2.property_type = l.property_type
                  AND l2.is_active = 1) as group_avg
        FROM listings l
        LEFT JOIN alerts a ON a.listing_id = l.id AND a.alert_type = 'underpriced'
        WHERE l.is_active = 1
          AND l.first_seen >= ?
          AND (a.zscore IS NOT NULL AND a.zscore < ?)
        ORDER BY a.zscore ASC
        LIMIT 10
    """, (cutoff, ANOMALY_ZSCORE_THRESHOLD)).fetchall()

    # If no alert-based results, fall back to z-score column in data.json export
    if not rows:
        rows = conn.execute("""
            SELECT *,
                   (SELECT AVG(l2.price_per_sqm_eur) 
                    FROM listings l2 
                    WHERE l2.neighborhood = l.neighborhood 
                      AND l2.property_type = l.property_type
                      AND l2.is_active = 1) as group_avg
            FROM listings l
            WHERE l.is_active = 1
              AND l.first_seen >= ?
              AND l.price_per_sqm_eur > 0
            ORDER BY l.price_per_sqm_eur / NULLIF(
                (SELECT AVG(l2.price_per_sqm_eur) 
                 FROM listings l2 
                 WHERE l2.neighborhood = l.neighborhood 
                   AND l2.property_type = l.property_type
                   AND l2.is_active = 1), 0) ASC
            LIMIT 10
        """, (cutoff,)).fetchall()

    deals = []
    for r in rows:
        r = dict(r)
        group_avg = r.get("group_avg") or 0
        savings_eur = (group_avg - (r.get("price_per_sqm_eur") or 0)) * (r.get("area_sqm") or 1) if group_avg else 0
        savings_pct = ((group_avg - (r.get("price_per_sqm_eur") or 0)) / group_avg * 100) if group_avg else 0

        deals.append({
            "id": r["id"],
            "neighborhood": r.get("neighborhood", "Unknown"),
            "rooms_text": _room_text(r.get("rooms")),
            "area_sqm": f"{r.get('area_sqm', 0):.0f}",
            "construction_type": _construction_text(r.get("construction_type")),
            "floor": r.get("floor"),
            "total_floors": r.get("total_floors"),
            "price_eur": _fmt_price(r.get("price_eur")),
            "price_per_sqm": _fmt_price(r.get("price_per_sqm_eur")),
            "zscore": (
                f"{r.get('zscore', 0) or 0:.2f}"
                if r.get('zscore')
                else f"< {ANOMALY_ZSCORE_THRESHOLD:g}"
            ),
            "savings_eur": _fmt_price(max(savings_eur, 0)),
            "savings_pct": f"{max(savings_pct, 0):.1f}",
            "url": r.get("url", "#"),
        })

    return deals


def _query_price_drops(
    conn: sqlite3.Connection,
    min_drop_pct: float = PRICE_DROP_PCT_THRESHOLD,
) -> List[Dict[str, Any]]:
    """Listings whose current price is ≥ min_drop_pct below first recorded price."""
    rows = conn.execute("""
        SELECT *,
               first_price_eur,
               ROUND((first_price_eur - price_eur) / first_price_eur * 100, 1) as drop_pct
        FROM listings
        WHERE is_active = 1
          AND first_price_eur IS NOT NULL
          AND price_changes > 0
          AND first_price_eur > price_eur
          AND (first_price_eur - price_eur) / first_price_eur * 100 >= ?
        ORDER BY drop_pct DESC
        LIMIT 8
    """, (min_drop_pct,)).fetchall()

    drops = []
    for r in rows:
        r = dict(r)
        drops.append({
            "id": r["id"],
            "neighborhood": r.get("neighborhood", "Unknown"),
            "rooms_text": _room_text(r.get("rooms")),
            "area_sqm": f"{r.get('area_sqm', 0):.0f}",
            "old_price": _fmt_price(r.get("first_price_eur")),
            "new_price": _fmt_price(r.get("price_eur")),
            "drop_pct": f"{r.get('drop_pct', 0):.1f}",
            "url": r.get("url", "#"),
        })

    return drops


def _query_district_velocity(conn: sqlite3.Connection, days: int = 7) -> List[Dict[str, Any]]:
    """Per-district: new listings added vs. listings that went inactive (proxy for sold)."""
    cutoff = (utc_now() - timedelta(days=days)).isoformat()

    rows = conn.execute("""
        SELECT 
            neighborhood,
            SUM(CASE WHEN first_seen >= ? THEN 1 ELSE 0 END) as added,
            SUM(CASE WHEN is_active = 0 AND last_seen >= ? THEN 1 ELSE 0 END) as sold,
            ROUND(AVG(CASE WHEN is_active = 1 THEN price_per_sqm_eur END), 0) as avg_price,
            COUNT(*) as total
        FROM listings
        WHERE neighborhood IS NOT NULL
        GROUP BY neighborhood
        HAVING total >= 5
        ORDER BY added DESC
        LIMIT 8
    """, (cutoff, cutoff)).fetchall()

    districts = []
    for r in rows:
        r = dict(r)
        added = r.get("added", 0) or 0
        sold = r.get("sold", 0) or 0
        # velocity: positive = more supply coming in, negative = market absorbing fast
        velocity = added - sold
        if velocity > 2:
            label = "🔥 Hot supply"
        elif velocity < -2:
            label = "⚡ Fast selling"
        elif sold > added:
            label = "📈 Absorbing"
        else:
            label = "➡️ Stable"

        districts.append({
            "name": r.get("neighborhood", "Unknown"),
            "added": added,
            "sold": sold,
            "avg_price": _fmt_price(r.get("avg_price")),
            "velocity_score": velocity,
            "velocity_label": label,
        })

    return districts


def _query_top_pick(conn: sqlite3.Connection) -> Optional[Dict[str, Any]]:
    """Best single opportunity: highest savings %, active, apartment."""
    # Try alerts table first
    row = conn.execute("""
        SELECT l.*, a.zscore, a.savings_eur, a.savings_pct,
               (SELECT AVG(l2.price_per_sqm_eur) 
                FROM listings l2 
                WHERE l2.neighborhood = l.neighborhood 
                  AND l2.property_type = l.property_type
                  AND l2.is_active = 1) as group_avg
        FROM alerts a
        JOIN listings l ON l.id = a.listing_id
        WHERE l.is_active = 1
          AND a.zscore < ?
          AND l.property_type = 'apartment'
          AND a.savings_pct > 0
        ORDER BY a.savings_pct DESC
        LIMIT 1
    """, (ANOMALY_ZSCORE_THRESHOLD,)).fetchone()

    if not row:
        # Fallback: cheapest per-sqm active apartment relative to neighborhood
        row = conn.execute("""
            SELECT l.*,
                   (SELECT AVG(l2.price_per_sqm_eur) 
                    FROM listings l2 
                    WHERE l2.neighborhood = l.neighborhood 
                      AND l2.property_type = 'apartment'
                      AND l2.is_active = 1) as group_avg
            FROM listings l
            WHERE l.is_active = 1
              AND l.property_type = 'apartment'
              AND l.price_per_sqm_eur > 0
              AND l.area_sqm > 30
            ORDER BY l.price_per_sqm_eur / NULLIF(
                (SELECT AVG(l2.price_per_sqm_eur) 
                 FROM listings l2 
                 WHERE l2.neighborhood = l.neighborhood 
                   AND l2.property_type = 'apartment'
                   AND l2.is_active = 1), 0) ASC
            LIMIT 1
        """).fetchone()

    if not row:
        return None

    r = dict(row)
    group_avg = r.get("group_avg") or 0
    price_sqm = r.get("price_per_sqm_eur") or 0
    savings_pct = r.get("savings_pct") or ((group_avg - price_sqm) / group_avg * 100 if group_avg else 0)
    savings_eur = r.get("savings_eur") or ((group_avg - price_sqm) * (r.get("area_sqm") or 1) if group_avg else 0)

    # Build reasoning
    reasons = []
    if savings_pct > 20:
        reasons.append(f"Priced {savings_pct:.0f}% below neighborhood average")
    elif savings_pct > 10:
        reasons.append(f"Solid {savings_pct:.0f}% discount vs. market")
    else:
        reasons.append(f"Below average pricing at €{_fmt_price(price_sqm)}/m²")

    if r.get("construction_type") == "brick":
        reasons.append("Brick construction (premium quality)")
    if r.get("floor") and r.get("total_floors"):
        if r["floor"] > 1 and r["floor"] < r["total_floors"]:
            reasons.append(f"Mid-floor ({r['floor']}/{r['total_floors']}) — optimal")
    if r.get("days_on_market") and r["days_on_market"] < 7:
        reasons.append("Fresh listing — just appeared")
    elif r.get("price_changes") and r["price_changes"] > 0:
        reasons.append("Seller reduced price — motivated")

    return {
        "neighborhood": r.get("neighborhood", "Unknown"),
        "rooms_text": _room_text(r.get("rooms")),
        "area_sqm": f"{r.get('area_sqm', 0):.0f}",
        "construction_type": _construction_text(r.get("construction_type")),
        "price_eur": _fmt_price(r.get("price_eur")),
        "price_per_sqm": _fmt_price(price_sqm),
        "savings_pct": f"{max(savings_pct, 0):.0f}",
        "savings_eur": _fmt_price(max(savings_eur, 0)),
        "reasoning": ". ".join(reasons) + ".",
        "url": r.get("url", "#"),
    }


def _get_total_active(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) as cnt FROM listings WHERE is_active = 1").fetchone()
    return dict(row).get("cnt", 0)


def _get_new_today_count(conn: sqlite3.Connection, hours: int = 24) -> int:
    cutoff = (utc_now() - timedelta(hours=hours)).isoformat()
    row = conn.execute("SELECT COUNT(*) as cnt FROM listings WHERE first_seen >= ?", (cutoff,)).fetchone()
    return dict(row).get("cnt", 0)


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
) -> Tuple[str, str, Dict[str, Any]]:
    """Generate the daily email digest.

    Args:
        db_path: Path to SQLite database. If None, uses default location.
        dashboard_url: URL to the live dashboard.
        use_sample: If True, use sample data (for testing / preview).

    Returns:
        Tuple of (html_content, plain_text, context_dict).
        context_dict is the raw data used — useful for the dashboard API.
    """
    if use_sample or db_path is None:
        if db_path is None:
            default_db = DATA_DIR / "listings.db"
            if not default_db.exists():
                use_sample = True

    if use_sample:
        context = _sample_data()
    else:
        db_path = db_path or str(DATA_DIR / "listings.db")
        conn = _connect(db_path)

        new_deals = _query_new_deals(conn)
        price_drops = _query_price_drops(conn)
        hot_districts = _query_district_velocity(conn)
        top_pick = _query_top_pick(conn)
        total_active = _get_total_active(conn)
        new_today = _get_new_today_count(conn)

        conn.close()

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
