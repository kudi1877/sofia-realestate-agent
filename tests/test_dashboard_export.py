from datetime import datetime

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from src.database.models import Alert, Base, Listing
from src.exporters.dashboard import _build_listings_payload, _write_json


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
        title="Export listing",
        neighborhood="Люлин",
        property_type="apartment",
        rooms=2,
        area_sqm=50,
        price_eur=100000,
        price_per_sqm_eur=2000,
        is_active=True,
        is_duplicate=False,
        first_seen=datetime(2026, 1, 1),
        last_seen=datetime(2026, 1, 2),
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
    assert "floor" not in by_source["export-1"]
    assert "total_floors" not in by_source["export-1"]
    assert "last_seen" not in by_source["export-1"]
    assert alert_selects == 1


def test_build_listings_payload_omits_none_listing_values():
    engine, db = session()
    row = listing("export-no-alert")
    row.rooms = None
    row.construction_type = None
    db.add(row)
    db.commit()

    payload = _build_listings_payload(db)

    item = payload["listings"][0]
    assert "rooms" not in item
    assert "construction_type" not in item
    assert "zscore" not in item
    assert "savings_pct" not in item


def test_write_json_uses_compact_separators(tmp_path):
    path = tmp_path / "data.json"

    _write_json(path, {"listings": [{"id": 1, "source": "test"}]})

    assert path.read_text(encoding="utf-8") == '{"listings":[{"id":1,"source":"test"}]}'
