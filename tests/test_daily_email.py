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
    rental = listing("rental", 1)
    rental.listing_kind = "rent"
    rental.first_price_eur = 1000
    rental.price_eur = 100
    rental.price_changes = 1
    auction = listing("auction", 2)
    auction.listing_kind = "auction"
    db.add_all(rows + [duplicate, rental, auction])
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
    db.add(
        Alert(
            listing_id=rental.id,
            alert_type="underpriced",
            zscore=-10,
            savings_eur=100000,
            savings_pct=99,
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


def test_top_pick_rejects_implausible_apartment_price_per_sqm():
    db = session()
    implausible = listing("implausible", 38)
    implausible.area_sqm = 260
    implausible.price_eur = 10000
    sane = listing("sane", 1000)
    db.add_all([implausible, sane])
    db.flush()
    db.add_all(
        [
            Alert(
                listing_id=implausible.id,
                alert_type="underpriced",
                zscore=-3,
                savings_eur=250000,
                savings_pct=99,
            ),
            Alert(
                listing_id=sane.id,
                alert_type="underpriced",
                zscore=-2,
                savings_eur=25000,
                savings_pct=50,
            ),
        ]
    )
    db.commit()

    top_pick = daily_email._query_top_pick(db)

    assert top_pick["price_per_sqm"] == "1 000"
    assert top_pick["url"].endswith("/sane")


def test_daily_email_uses_hedonic_expectation_when_enabled(monkeypatch):
    db = session()
    row = listing("hedonic", 1600)
    row.predicted_price_per_sqm = 2000
    row.residual_pct = -20
    db.add(row)
    db.commit()

    monkeypatch.setattr(daily_email, "DEAL_ENGINE", "hedonic")
    monkeypatch.setattr(daily_email, "effective_deal_engine", lambda _requested: "hedonic")

    deals = daily_email._query_new_deals(db)
    top_pick = daily_email._query_top_pick(db)

    assert deals[0]["zscore"] == "N/A"
    assert deals[0]["savings_pct"] == "20.0"
    assert top_pick["savings_pct"] == "20"
    assert "model expectation" in top_pick["reasoning"]


def test_daily_digest_excludes_low_authenticity_deals_and_price_drops():
    db = session()
    bait = listing("bait", 800)
    bait.authenticity_score = 35
    bait.first_price_eur = 50000
    bait.price_eur = 40000
    bait.price_changes = 1
    safe = listing("safe", 1000)
    safe.authenticity_score = 85
    db.add_all([bait, safe])
    db.flush()
    db.add_all(
        [
            Alert(listing_id=bait.id, alert_type="underpriced", zscore=-4, savings_pct=60),
            Alert(listing_id=safe.id, alert_type="underpriced", zscore=-2, savings_pct=20),
        ]
    )
    db.commit()

    deals = daily_email._query_new_deals(db)
    top_pick = daily_email._query_top_pick(db)
    price_drops = daily_email._query_price_drops(db)

    assert [deal["id"] for deal in deals] == [safe.id]
    assert top_pick["url"].endswith("/safe")
    assert price_drops == []
