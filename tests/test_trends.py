from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.analysis.trends import calculate_neighborhood_trends, generate_market_summary
from src.config import MIN_LISTINGS_PER_GROUP
from src.database.models import Base, Listing, NeighborhoodStatsHistory
from src.main import update_neighborhood_stats
from src.utils.time import utc_now


def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def listing(source_id: str, price_per_sqm: float, *, duplicate=False, neighborhood="Люлин"):
    return Listing(
        source="test",
        source_id=source_id,
        url=f"https://example.test/{source_id}",
        neighborhood=neighborhood,
        property_type="apartment",
        construction_type="brick",
        area_sqm=50,
        price_eur=price_per_sqm * 50,
        price_per_sqm_eur=price_per_sqm,
        is_active=True,
        is_duplicate=duplicate,
    )


def test_trends_and_market_summary_exclude_duplicates_and_publish_median():
    db = session()
    off_market = listing("off-market", 1700)
    off_market.is_active = False
    off_market.is_sold = True
    rental = listing("rental", 5)
    rental.listing_kind = "rent"
    rental.is_sold = True
    auction = listing("auction", 50)
    auction.listing_kind = "auction"
    auction.is_sold = True
    db.add_all(
        [
            listing("low", 1000),
            listing("high", 3000),
            listing("duplicate", 9000, duplicate=True),
            off_market,
            rental,
            auction,
            NeighborhoodStatsHistory(
                neighborhood="Люлин",
                snapshot_date=utc_now() - timedelta(days=31),
                median_price_per_sqm=1800,
                mean_price_per_sqm=1800,
                listing_count=2,
            ),
        ]
    )
    db.commit()

    trends = calculate_neighborhood_trends(db)
    summary = generate_market_summary(db)

    assert trends["Люлин"]["listing_count"] == 2
    assert trends["Люлин"]["current_median"] == 2000
    assert trends["Люлин"]["current_mean"] == 2000
    assert trends["Люлин"]["pct_change_30d"] == 11.11
    assert summary["total_listings"] == 2
    assert summary["price_per_sqm"] == 2000
    assert summary["median_price_per_sqm"] == 2000
    assert summary["avg_price_per_sqm"] == 2000
    assert summary["off_market"] == 1


def test_update_neighborhood_stats_writes_one_snapshot_per_neighborhood():
    db = session()
    db.add_all(
        [
            listing(f"lyulin-{index}", 1000 + index * 100)
            for index in range(MIN_LISTINGS_PER_GROUP)
        ]
        + [
            listing(f"boyana-{index}", 2000 + index * 100, neighborhood="Бояна")
            for index in range(MIN_LISTINGS_PER_GROUP)
        ]
    )
    db.commit()

    update_neighborhood_stats(db)

    snapshots = db.query(NeighborhoodStatsHistory).order_by(
        NeighborhoodStatsHistory.neighborhood
    ).all()
    assert [row.neighborhood for row in snapshots] == ["Бояна", "Люлин"]
    assert {row.listing_count for row in snapshots} == {MIN_LISTINGS_PER_GROUP}
    assert len({row.snapshot_date for row in snapshots}) == 1
    assert snapshots[0].median_price_per_sqm == 2450
    assert snapshots[1].median_price_per_sqm == 1450


def test_trend_reports_insufficient_history_before_snapshots_accrue():
    db = session()
    db.add(listing("only", 1500))
    db.commit()

    trend = calculate_neighborhood_trends(db)["Люлин"]

    assert trend["trend"] == "insufficient history"
    assert "pct_change_30d" not in trend
