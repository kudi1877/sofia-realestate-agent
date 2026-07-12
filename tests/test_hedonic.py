import json
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.analysis import hedonic
from src.analysis.hedonic import effective_deal_engine, is_hedonic_deal, score, train
from src.database.models import Base, Listing


def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def model_listing(index: int, *, first_seen: datetime) -> Listing:
    neighborhood = "Люлин" if index % 2 else "Младост"
    area = 35 + (index % 90)
    floor = index % 9
    rooms = 1 + (index % 4)
    neighborhood_premium = 350 if neighborhood == "Младост" else 0
    price_per_sqm = 1050 + neighborhood_premium + area * 11 + floor * 24 + rooms * 35
    return Listing(
        source="test",
        source_id=f"model-{index}",
        url=f"https://example.test/model-{index}",
        listing_kind="sale",
        neighborhood=neighborhood,
        property_type="apartment",
        construction_type="brick" if index % 3 else "panel",
        year_built=1970 + index % 55,
        rooms=rooms,
        floor=floor,
        total_floors=8,
        area_sqm=area,
        price_eur=price_per_sqm * area,
        price_per_sqm_eur=price_per_sqm,
        first_seen=first_seen,
        last_seen=first_seen,
        is_active=True,
        is_duplicate=False,
        seller_type="private" if index % 5 else "agency",
        exposure='["south", "east"]' if index % 2 else '["north"]',
        renovation_state="renovated" if index % 4 else "for_renovation",
        act16=index % 3 != 0,
        has_elevator=index % 2 == 0,
        parking="garage" if index % 7 == 0 else "none",
        llm_extract='{"view": "mountain"}' if index % 6 == 0 else None,
    )


def test_train_beats_group_median_ship_gate_and_score_writes_explanations(tmp_path):
    db = session()
    start = datetime(2025, 1, 1)
    db.add_all([model_listing(index, first_seen=start + timedelta(days=index)) for index in range(220)])
    db.commit()

    metrics = train(db, model_dir=tmp_path, now=datetime(2026, 7, 12))

    assert metrics["model_mae_pct"] < metrics["baseline_mae_pct"]
    assert metrics["ship_gate_passed"] is True
    assert metrics["validation_count"] >= 20
    assert (tmp_path / "hedonic-20260712.pkl").exists()
    assert (tmp_path / "hedonic-20260712.metrics.json").exists()

    result = score(db, model_dir=tmp_path)
    scored = db.query(Listing).filter(Listing.predicted_price_per_sqm.isnot(None)).all()

    assert result["scored"] == 220
    assert all(row.residual_pct is not None for row in scored)
    assert all(len(json.loads(row.hedonic_contributions)) == 5 for row in scored)
    assert effective_deal_engine("hedonic", model_dir=tmp_path) == "hedonic"


def test_hedonic_deal_keeps_existing_price_and_area_sanity_filters():
    row = model_listing(1, first_seen=datetime(2026, 1, 1))
    row.predicted_price_per_sqm = 2500
    row.residual_pct = -20

    assert is_hedonic_deal(row) is True

    row.price_eur = 1000
    assert is_hedonic_deal(row) is False

    row.price_eur = 100000
    row.area_sqm = 700
    assert is_hedonic_deal(row) is False


def test_failed_ship_gate_keeps_default_engine_on_zscore(tmp_path):
    (tmp_path / "hedonic-20260712.metrics.json").write_text(
        json.dumps({"ship_gate_passed": False}),
        encoding="utf-8",
    )

    assert effective_deal_engine("hedonic", model_dir=tmp_path) == "zscore"
    assert effective_deal_engine("hedonic-experimental", model_dir=tmp_path) == "hedonic-experimental"


def test_weekly_gate_scores_nightly_without_retraining(monkeypatch, tmp_path):
    model_path = tmp_path / "hedonic-existing.pkl"
    model_path.touch()
    monkeypatch.setattr(hedonic, "latest_model_path", lambda _model_dir: model_path)
    monkeypatch.setattr(hedonic, "latest_metrics", lambda _model_dir: {"ship_gate_passed": True})
    monkeypatch.setattr(hedonic, "score", lambda _db, model_dir: {"scored": 12, "backend": "test"})
    monkeypatch.setattr(
        hedonic,
        "train",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unexpected training")),
    )

    result = hedonic.weekly_train_and_score(
        object(),
        now=datetime(2026, 7, 14),
        model_dir=tmp_path,
    )

    assert result == {
        "trained": False,
        "metrics": {"ship_gate_passed": True},
        "scored": 12,
        "backend": "test",
    }
