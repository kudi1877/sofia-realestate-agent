from bs4 import BeautifulSoup
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.analysis.rental_market import (
    calculate_rent_stats,
    gross_yield_pct,
    rent_stats_lookup,
    update_neighborhood_rent_stats,
)
from src.config import MIN_LISTINGS_PER_GROUP
from src.database.models import (
    Base,
    Listing,
    NeighborhoodRentStats,
    NeighborhoodRentStatsHistory,
)
from src.scrapers.imotbg import ImotBgScraper


def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def listing(source_id: str, *, kind: str, rooms: int = 2, price_per_sqm: float = 10) -> Listing:
    price = 100000 if kind == "sale" else price_per_sqm * 50
    return Listing(
        source="test",
        source_id=source_id,
        listing_kind=kind,
        url=f"https://example.test/{source_id}",
        neighborhood="Люлин",
        property_type="apartment",
        rooms=rooms,
        area_sqm=50,
        price_eur=price,
        price_per_sqm_eur=price_per_sqm if kind == "rent" else price / 50,
        is_active=True,
        is_duplicate=False,
    )


def test_rent_stats_require_minimum_sample_and_write_one_snapshot_set():
    db = session()
    rentals = [
        listing(f"rent-{index}", kind="rent", price_per_sqm=8 + index * 0.5)
        for index in range(MIN_LISTINGS_PER_GROUP)
    ]
    db.add_all(rentals)
    db.commit()

    assert set(calculate_rent_stats(rentals)) == {("Люлин", "2"), ("Люлин", "all")}
    assert update_neighborhood_rent_stats(db) == 2
    assert db.query(NeighborhoodRentStats).count() == 2
    assert db.query(NeighborhoodRentStatsHistory).count() == 2


def test_gross_yield_prefers_room_bucket_and_falls_back_to_neighborhood():
    db = session()
    db.add_all(
        [
            NeighborhoodRentStats(
                neighborhood="Люлин",
                rooms_bucket="2",
                median_rent_per_sqm=10,
                listing_count=10,
            ),
            NeighborhoodRentStats(
                neighborhood="Люлин",
                rooms_bucket="all",
                median_rent_per_sqm=8,
                listing_count=20,
            ),
        ]
    )
    db.commit()
    stats = rent_stats_lookup(db)

    assert gross_yield_pct(listing("sale-exact", kind="sale", rooms=2), stats) == 6.0
    assert gross_yield_pct(listing("sale-fallback", kind="sale", rooms=3), stats) == 4.8


def test_gross_yield_only_for_apartments_and_plausible_values():
    # TIN-523: apartment rent medians applied to plots produced 300%+ "yields"
    # that topped the dashboard's yield sort.
    db = session()
    db.add(
        NeighborhoodRentStats(
            neighborhood="Люлин", rooms_bucket="all", median_rent_per_sqm=8, listing_count=20
        )
    )
    db.commit()
    stats = rent_stats_lookup(db)

    plot = listing("plot", kind="sale")
    plot.property_type = "plot"
    assert gross_yield_pct(plot, stats) is None

    implausible = listing("cheap", kind="sale")
    implausible.price_eur = 10000  # 48% "yield" — not a whole-property price
    assert gross_yield_pct(implausible, stats) is None


def test_imot_rent_parser_emits_separate_source_and_kind():
    soup = BeautifulSoup(
        """
        <div class="item TOP">
          <a class="saveSlink" href="https://www.imot.bg/obiava-123"></a>
          <div class="price">500 €</div>
          Дава под наем 2-СТАЕН град София, Люлин 500 € 65 кв. м, 3-ти ет. от 8
        </div>
        """,
        "html.parser",
    )

    parsed = ImotBgScraper(deal_type="rent")._parse_listing_item(soup.div)

    assert parsed["source"] == "imotbg-rent"
    assert parsed["listing_kind"] == "rent"
    assert parsed["price_eur"] == 500
