from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.alerts import daily_email
from src.database.models import Alert, Base, Listing
from src.utils.time import utc_now


def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    return Session()


def listing(source_id: str, price_per_sqm: float, *, duplicate=False) -> Listing:
    return Listing(
        source="test",
        source_id=source_id,
        url=f"https://example.test/{source_id}",
        title="Test listing",
        neighborhood="Люлин",
        property_type="apartment",
        rooms=2,
        area_sqm=50,
        price_eur=price_per_sqm * 50,
        price_per_sqm_eur=price_per_sqm,
        first_price_eur=price_per_sqm * 50,
        first_seen=utc_now() - timedelta(hours=1),
        last_seen=utc_now(),
        is_active=True,
        is_duplicate=duplicate,
        price_changes=0,
    )


def test_daily_email_uses_shared_session_dedup_and_preserves_shapes(tmp_path, monkeypatch):
    db = session()
    rows = [listing(f"unique-{index}", 1000 + index * 100) for index in range(5)]
    rows[0].first_price_eur = 60000
    rows[0].price_eur = 50000
    rows[0].price_changes = 1
    duplicate = listing("duplicate", 100, duplicate=True)
    duplicate.first_price_eur = 100000
    duplicate.price_eur = 10000
    duplicate.price_changes = 1
    db.add_all(rows + [duplicate])
    db.flush()
    db.add(
        Alert(
            listing_id=rows[0].id,
            alert_type="underpriced",
            zscore=-2.0,
            savings_eur=10000,
            savings_pct=20,
        )
    )
    db.commit()

    monkeypatch.setattr(daily_email, "DATA_DIR", tmp_path)
    monkeypatch.setattr(daily_email, "DASHBOARD_DATA_DIR", tmp_path / "dashboard")

    _html, _plain, context = daily_email.generate_daily_email(db=db)

    assert set(context) == {
        "date", "generated_at", "preheader_text", "total_active", "new_today",
        "price_drops_count", "dashboard_url", "unsubscribe_url", "new_deals",
        "price_drops", "hot_districts", "top_pick",
    }
    assert context["total_active"] == 5
    assert context["new_today"] == 5
    assert context["price_drops_count"] == 1
    assert set(context["new_deals"][0]) == {
        "id", "neighborhood", "rooms_text", "area_sqm", "construction_type",
        "floor", "total_floors", "price_eur", "price_per_sqm", "zscore",
        "savings_eur", "savings_pct", "url", "image_url",
    }
    assert set(context["price_drops"][0]) == {
        "id", "neighborhood", "rooms_text", "area_sqm", "old_price",
        "new_price", "drop_pct", "url", "image_url",
    }
    assert set(context["hot_districts"][0]) == {
        "name", "added", "sold", "avg_price", "velocity_score", "velocity_label",
    }
    assert set(context["top_pick"]) == {
        "neighborhood", "rooms_text", "area_sqm", "construction_type",
        "price_eur", "price_per_sqm", "savings_pct", "savings_eur",
        "reasoning", "url",
    }
