from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.analysis.seller_signals import (
    calculate_market_signals,
    market_pulse_line,
    motivated_score,
    update_motivated_scores,
)
from src.database.models import Base, Listing, PriceHistory


NOW = datetime(2026, 7, 12, 12, 0)


def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def listing(source_id: str, **overrides) -> Listing:
    values = {
        "source": "test",
        "source_id": source_id,
        "url": f"https://example.test/{source_id}",
        "neighborhood": "Люлин",
        "property_type": "apartment",
        "area_sqm": 50,
        "price_eur": 90000,
        "first_price_eur": 100000,
        "price_per_sqm_eur": 1800,
        "first_seen": NOW - timedelta(days=30),
        "last_seen": NOW,
        "days_on_market": 30,
        "price_changes": 0,
        "is_active": True,
        "is_sold": False,
        "is_duplicate": False,
    }
    values.update(overrides)
    return Listing(**values)


def test_motivated_score_caps_four_reproducible_components():
    assert motivated_score(
        price_changes=4,
        first_price_eur=100000,
        current_price_eur=75000,
        days_since_last_cut=3,
        days_on_market=60,
        neighborhood_median_dom=20,
    ) == 100
    assert motivated_score(
        price_changes=0,
        first_price_eur=100000,
        current_price_eur=100000,
        days_since_last_cut=None,
        days_on_market=5,
        neighborhood_median_dom=20,
    ) == 0


def test_update_motivated_scores_persists_active_listing_scores():
    db = session()
    target = listing("target", price_changes=2, days_on_market=30, price_eur=85000)
    baseline = listing("baseline", days_on_market=10, price_eur=100000)
    db.add_all([target, baseline])
    db.flush()
    db.add(
        PriceHistory(
            listing_id=target.id,
            price_eur=90000,
            price_per_sqm_eur=1800,
            recorded_at=NOW - timedelta(days=2),
        )
    )
    db.commit()

    summary = update_motivated_scores(db, now=NOW)

    db.refresh(target)
    db.refresh(baseline)
    assert summary == {"scored": 2, "motivated": 1}
    assert target.motivated_score == 77
    assert baseline.motivated_score == 0


def test_exit_and_absorption_metrics_require_ten_exits():
    db = session()
    for index in range(10):
        db.add(
            listing(
                f"sold-{index}",
                is_active=False,
                is_sold=True,
                sold_date=NOW - timedelta(days=2 if index < 4 else 20),
                price_per_sqm_eur=1000 + index * 10,
                days_on_market=20 + index,
            )
        )
    for index in range(2):
        db.add(listing(f"new-{index}", first_seen=NOW - timedelta(days=1)))
    for index in range(9):
        db.add(
            listing(
                f"thin-{index}",
                neighborhood="Thin",
                is_active=False,
                is_sold=True,
                sold_date=NOW - timedelta(days=3),
            )
        )
    db.commit()

    signals = calculate_market_signals(db, now=NOW)
    hood = signals["neighborhoods"]["Люлин"]

    assert hood["exit_count"] == 10
    assert hood["median_exit_price_per_sqm"] == 1045.0
    assert hood["median_exit_discount_pct"] == 10.0
    assert hood["median_dom_to_exit"] == 24.5
    assert hood["weekly_new_listings"] == 2
    assert hood["weekly_exits"] == 4
    assert hood["weekly_absorption_ratio"] == 2.0
    assert signals["neighborhoods"]["Thin"]["median_exit_price_per_sqm"] is None
    assert market_pulse_line(signals["city"]) == (
        "Market pulse: 2 new listings vs 13 exits this week (6.50x absorption)."
    )
