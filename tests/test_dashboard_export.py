from datetime import datetime, timedelta

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from src.database.models import (
    Alert,
    Base,
    Listing,
    Neighborhood,
    NeighborhoodRentStats,
    NeighborhoodStatsHistory,
    PriceHistory,
)
from src.exporters.dashboard import (
    _build_digest_payload,
    _build_listings_payload,
    _build_market_payload,
    _write_json,
)
from src.utils.time import utc_now


def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return engine, Session()


def listing(source_id):
    return Listing(
        source="test",
        source_id=source_id,
        url=f"https://example.test/{source_id}",
        image_url=f"https://images.example.test/{source_id}.jpg",
        title="Export listing",
        neighborhood="Люлин",
        property_type="apartment",
        rooms=2,
        floor=3,
        total_floors=8,
        area_sqm=50,
        price_eur=100000,
        price_per_sqm_eur=2000,
        price_changes=1,
        is_active=True,
        is_duplicate=False,
        first_seen=datetime(2026, 1, 1),
        last_seen=datetime(2026, 1, 2),
        availability_checked_at=datetime(2026, 1, 2, 12, 30),
        motivated_score=67,
        exposure='["south", "east"]',
        renovation_state="renovated",
        act16=True,
        has_elevator=False,
        parking="parking_space",
    )


def test_build_listings_payload_eager_loads_alerts_and_preserves_latest_values():
    engine, db = session()
    first = listing("export-1")
    second = listing("export-2")
    db.add_all([first, second])
    db.flush()
    db.add_all(
        [
            Alert(
                listing_id=first.id,
                alert_type="underpriced",
                zscore=-2.2222,
                savings_pct=15.44,
            ),
            Alert(
                listing_id=first.id,
                alert_type="underpriced",
                zscore=-3.0004,
                savings_pct=None,
            ),
            Alert(
                listing_id=second.id,
                alert_type="underpriced",
                zscore=-1.75,
                savings_pct=8.25,
            ),
        ]
    )
    db.commit()

    alert_selects = 0

    def count_alert_selects(conn, cursor, statement, parameters, context, executemany):
        nonlocal alert_selects
        if "FROM alerts" in statement:
            alert_selects += 1

    event.listen(engine, "before_cursor_execute", count_alert_selects)

    payload = _build_listings_payload(db)

    by_source = {item["url"].rsplit("/", 1)[-1]: item for item in payload["listings"]}
    assert by_source["export-1"]["zscore"] == -3.0
    assert by_source["export-1"]["savings_pct"] == 15.4
    assert by_source["export-2"]["zscore"] == -1.75
    assert by_source["export-2"]["savings_pct"] == 8.2
    assert by_source["export-1"]["image_url"] == "https://images.example.test/export-1.jpg"
    assert by_source["export-1"]["floor"] == 3
    assert by_source["export-1"]["total_floors"] == 8
    assert by_source["export-1"]["price_changes"] == 1
    assert by_source["export-1"]["site_count"] == 1
    assert by_source["export-1"]["last_seen"] == "2026-01-02T00:00:00"
    assert by_source["export-1"]["availability_checked_at"] == "2026-01-02T12:30:00"
    assert by_source["export-1"]["motivated_score"] == 67
    assert by_source["export-1"]["exposure"] == ["south", "east"]
    assert by_source["export-1"]["renovation_state"] == "renovated"
    assert by_source["export-1"]["act16"] is True
    assert by_source["export-1"]["has_elevator"] is False
    assert by_source["export-1"]["parking"] == "parking_space"
    assert alert_selects == 1


def test_build_listings_payload_omits_none_listing_values():
    engine, db = session()
    row = listing("export-no-alert")
    row.rooms = None
    row.construction_type = None
    row.image_url = None
    db.add(row)
    db.commit()

    payload = _build_listings_payload(db)

    item = payload["listings"][0]
    assert "rooms" not in item
    assert "construction_type" not in item
    assert "zscore" not in item
    assert "savings_pct" not in item
    assert "image_url" not in item


def test_listings_payload_excludes_rentals_and_exports_sale_gross_yield():
    _engine, db = session()
    sale = listing("sale-yield")
    rental = listing("rental-hidden")
    rental.listing_kind = "rent"
    rental.price_eur = 500
    rental.price_per_sqm_eur = 10
    auction = listing("auction-visible")
    auction.source = "bcpea"
    auction.listing_kind = "auction"
    auction.auction_start = datetime(2026, 7, 1)
    auction.auction_end = datetime(2026, 8, 1, 23, 59, 59)
    auction.bailiff_name = "Test Bailiff"
    auction.case_number = "20260001"
    db.add_all(
        [
            sale,
            rental,
            auction,
            NeighborhoodRentStats(
                neighborhood="Люлин",
                rooms_bucket="2",
                median_rent_per_sqm=10,
                listing_count=10,
            ),
        ]
    )
    db.commit()

    payload = _build_listings_payload(db)

    by_id = {row["source_id"]: row for row in payload["listings"]}
    assert set(by_id) == {"sale-yield", "auction-visible"}
    assert payload["stats"]["totalListings"] == 1
    assert by_id["sale-yield"]["gross_yield_pct"] == 6.0
    assert by_id["auction-visible"]["listing_kind"] == "auction"
    assert by_id["auction-visible"]["auction_end"] == "2026-08-01T23:59:59"
    assert by_id["auction-visible"]["bailiff_name"] == "Test Bailiff"
    assert by_id["auction-visible"]["is_deal"] is False
    assert "zscore" not in by_id["auction-visible"]


def test_dashboard_export_never_leaks_contact_fields_or_values():
    _engine, db = session()
    row = listing("private-contact")
    row.contact_phone = "+359888123456"
    row.contact_email = "private@example.test"
    db.add(row)
    db.commit()

    payload = _build_listings_payload(db)
    serialized = str(payload).lower()

    assert "contact_phone" not in serialized
    assert "contact_email" not in serialized
    assert "+359888123456" not in serialized
    assert "private@example.test" not in serialized


def test_build_listings_payload_counts_canonical_sibling_sources_and_exports_median():
    _engine, db = session()
    primary = listing("primary")
    primary.canonical_id = "canonical-group"
    duplicate = listing("duplicate")
    duplicate.source = "other"
    duplicate.canonical_id = "canonical-group"
    duplicate.is_duplicate = True
    db.add_all(
        [
            primary,
            duplicate,
            Neighborhood(
                name="Люлин",
                avg_price_per_sqm=2100,
                median_price_per_sqm=2050,
                listing_count=2,
            ),
        ]
    )
    db.commit()

    payload = _build_listings_payload(db)

    assert len(payload["listings"]) == 1
    assert payload["listings"][0]["site_count"] == 2
    assert payload["listings"][0]["cross_source_links"] == [
        {"source": "other", "url": "https://example.test/duplicate"},
        {"source": "test", "url": "https://example.test/primary"},
    ]
    assert payload["neighborhoods"][0]["medianPricePerSqm"] == 2050.0


def test_build_listings_payload_embeds_changed_price_history_and_percentile():
    _engine, db = session()
    low = listing("low")
    low.price_eur = 90000
    low.price_per_sqm_eur = 1800
    low.last_seen = datetime(2026, 1, 3)
    middle = listing("middle")
    middle.price_per_sqm_eur = 2000
    high = listing("high")
    high.price_per_sqm_eur = 2200
    db.add_all([low, middle, high])
    db.flush()
    db.add(
        PriceHistory(
            listing_id=low.id,
            price_eur=100000,
            price_per_sqm_eur=2000,
            recorded_at=datetime(2026, 1, 1),
        )
    )
    db.commit()

    payload = _build_listings_payload(db)
    exported = next(item for item in payload["listings"] if item["id"] == low.id)

    assert exported["price_percentile"] == 33.3
    assert exported["last_seen"] == "2026-01-03T00:00:00"
    assert exported["price_history"] == [
        {
            "date": "2026-01-01T00:00:00",
            "price_eur": 100000.0,
            "price_per_sqm_eur": 2000.0,
        },
        {
            "date": "2026-01-03T00:00:00",
            "price_eur": 90000.0,
            "price_per_sqm_eur": 1800.0,
        },
    ]


def test_build_market_payload_is_deduplicated_median_first_and_snapshot_ready(monkeypatch):
    _engine, db = session()
    now = datetime(2026, 7, 11)
    monkeypatch.setattr("src.exporters.dashboard.utc_now", lambda: now)

    rows = [listing("market-low"), listing("market-mid"), listing("market-high")]
    for index, row in enumerate(rows):
        row.canonical_id = f"market-{index}"
        row.price_per_sqm_eur = 1800 + index * 200
        row.first_seen = now - timedelta(days=10 + index * 10)
    duplicate = listing("market-duplicate")
    duplicate.canonical_id = "market-0"
    duplicate.is_duplicate = True
    duplicate.price_per_sqm_eur = 9000
    sold = listing("market-sold")
    sold.is_active = False
    sold.is_sold = True
    db.add_all(rows + [duplicate, sold])
    db.add(
        Neighborhood(
            name="Люлин",
            avg_price_per_sqm=2100,
            median_price_per_sqm=2000,
            listing_count=3,
        )
    )
    db.add(
        NeighborhoodRentStats(
            neighborhood="Люлин",
            rooms_bucket="all",
            median_rent_per_sqm=10,
            listing_count=20,
        )
    )
    for index in range(8):
        db.add(
            NeighborhoodStatsHistory(
                neighborhood="Люлин",
                snapshot_date=datetime(2026, 3, 1) + timedelta(days=index * 10),
                median_price_per_sqm=1800 + index * 25,
                mean_price_per_sqm=1850 + index * 25,
                listing_count=3,
            )
        )
    db.commit()

    payload = _build_market_payload(db)

    assert payload["city"]["median_price_per_sqm"] == 2000.0
    assert payload["city"]["active"] == 3
    assert payload["city"]["off_market"] == 1
    assert "avg_price_per_sqm" not in payload["city"]
    hood = payload["neighborhoods"][0]
    assert hood["median_days_on_market"] == 20.0
    assert hood["trend_30d_pct"] == 3.95
    assert hood["trend_direction"] == "up"
    assert len(hood["history"]) == 8
    assert hood["median_rent_per_sqm"] == 10.0
    assert hood["gross_yield_pct"] == 6.0


def test_write_json_uses_compact_separators(tmp_path):
    path = tmp_path / "data.json"

    _write_json(path, {"listings": [{"id": 1, "source": "test"}]})

    assert path.read_text(encoding="utf-8") == '{"listings":[{"id":1,"source":"test"}]}'


def test_digest_exports_recent_price_drop_with_old_and_new_prices(monkeypatch):
    _engine, db = session()
    row = listing("price-drop")
    row.price_eur = 90000
    row.price_per_sqm_eur = 1800
    row.first_price_eur = 100000
    row.price_changes = 1
    db.add(row)
    auction = listing("new-auction")
    auction.source = "bcpea"
    auction.listing_kind = "auction"
    auction.first_seen = utc_now()
    auction.auction_start = utc_now()
    auction.auction_end = utc_now() + timedelta(days=20)
    auction.bailiff_name = "Test Bailiff"
    db.add(auction)
    db.flush()
    db.add(
        PriceHistory(
            listing_id=row.id,
            price_eur=100000,
            price_per_sqm_eur=2000,
            recorded_at=utc_now(),
        )
    )
    db.commit()

    from src.alerts import daily_email

    monkeypatch.setattr(
        daily_email,
        "generate_daily_email",
        lambda: ("", "", {"new_deals": [], "hot_districts": []}),
    )

    payload = _build_digest_payload(db)

    assert payload["summary"]["price_drops"] == 1
    assert len(payload["auction_watch"]) == 1
    assert payload["auction_watch"][0]["source"] == "bcpea"
    assert payload["price_drops"] == [
        {
            "id": row.id,
            "source": "test",
            "url": "https://example.test/price-drop",
            "title": "Export listing",
            "image_url": None,
            "price_eur": 90000.0,
            "area_sqm": 50.0,
            "price_per_sqm_eur": 1800.0,
            "neighborhood": "Люлин",
            "property_type": "apartment",
            "rooms": 2.0,
            "zscore": None,
            "savings_pct": None,
            # A price drop is not automatically an underpriced deal.
            "is_deal": False,
            "old_price": 100000.0,
            "new_price": 90000.0,
            "price_drop_pct": 10.0,
        }
    ]
