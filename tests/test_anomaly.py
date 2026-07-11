import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.analysis.anomaly import analyze_database, calculate_neighborhood_stats, detect_anomalies
from src.config import MIN_LISTINGS_PER_GROUP
from src.database.models import Base, Listing


def listing(
    price_per_sqm,
    *,
    neighborhood="Люлин",
    property_type="apartment",
    construction_type="brick",
    area_sqm=100,
    source_id=None,
    is_duplicate=False,
):
    return Listing(
        source="test",
        source_id=source_id
        or f"{neighborhood}-{property_type}-{construction_type}-{price_per_sqm}",
        url="https://example.test/listing",
        neighborhood=neighborhood,
        property_type=property_type,
        construction_type=construction_type,
        area_sqm=area_sqm,
        price_eur=price_per_sqm * area_sqm,
        price_per_sqm_eur=price_per_sqm,
        is_active=True,
        is_duplicate=is_duplicate,
    )


def test_group_below_minimum_size_produces_no_stats():
    rows = [listing(price) for price in (900, 950, 1000)]

    stats = calculate_neighborhood_stats(rows, min_group_size=5)

    assert stats == {}


def test_underpriced_listing_is_detected_with_expected_scores():
    rows = [listing(price) for price in (1000, 1000, 1000, 1000, 500)]
    stats = calculate_neighborhood_stats(rows, min_group_size=5)

    anomalies = detect_anomalies(rows, stats)

    assert len(anomalies) == 1
    anomaly = anomalies[0]
    assert anomaly.listing.price_per_sqm_eur == 500
    assert anomaly.zscore == pytest.approx(-2.0)
    assert anomaly.savings_pct == pytest.approx(44.4444444444)
    assert anomaly.group_count == 5


def test_zero_standard_deviation_group_is_skipped():
    rows = [listing(1000) for _ in range(5)]
    stats = calculate_neighborhood_stats(rows, min_group_size=5)

    assert stats[("Люлин", "apartment", "brick")]["std"] == 0
    assert detect_anomalies(rows, stats) == []


def test_detection_falls_back_to_property_type_group():
    underpriced = listing(500, construction_type="panel")
    stats = {
        ("Люлин", "apartment", "all"): {
            "mean": 1000,
            "median": 1000,
            "std": 200,
            "p20": 850,
            "count": 8,
            "min": 500,
            "max": 1200,
        }
    }

    anomalies = detect_anomalies([underpriced], stats)

    assert len(anomalies) == 1
    assert anomalies[0].group_count == 8


def test_detection_never_compares_across_property_types():
    # TIN-468: a house/plot with no same-type peers must NOT be scored
    # against the mixed neighborhood-wide pool (that flagged €71/m² plots
    # as −100% "deals" vs apartments). No same-type group → no anomaly.
    underpriced = listing(500, property_type="house", construction_type=None)
    stats = {
        ("Люлин", "all", "all"): {
            "mean": 1000,
            "median": 1000,
            "std": 200,
            "p20": 850,
            "count": 12,
            "min": 500,
            "max": 1200,
        }
    }

    anomalies = detect_anomalies([underpriced], stats)

    assert anomalies == []


def test_detection_scores_plots_against_plot_peers():
    # Plots ARE eligible for deals — against other plots in the same hood.
    underpriced = listing(50, property_type="plot", construction_type=None)
    stats = {
        ("Люлин", "plot", "all"): {
            "mean": 120,
            "median": 115,
            "std": 25,
            "p20": 95,
            "count": 6,
            "min": 50,
            "max": 160,
        }
    }

    anomalies = detect_anomalies([underpriced], stats)

    assert len(anomalies) == 1
    assert anomalies[0].group_count == 6


def test_analyze_database_excludes_active_duplicate_listings():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    for index in range(MIN_LISTINGS_PER_GROUP * 3):
        db.add(listing(1000, source_id=f"unique-{index}", is_duplicate=False))
    duplicate = listing(500, source_id="duplicate-underpriced", is_duplicate=True)
    db.add(duplicate)
    db.commit()

    anomalies = analyze_database(db)

    assert anomalies == []
