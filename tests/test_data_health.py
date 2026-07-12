from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.analysis.data_health import evaluate_data_health
from src.database.models import Base, Listing, NeighborhoodStatsHistory
from src.exporters import dashboard as dashboard_exporter
from src.observability import RunRecorder


NOW = datetime(2026, 7, 12, 12, 0)


def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def listing(source_id: str, **overrides) -> Listing:
    values = {
        "source": "imotbg",
        "source_id": source_id,
        "url": f"https://example.test/{source_id}",
        "image_url": "https://images.example.test/photo.jpg",
        "neighborhood": "Люлин",
        "property_type": "apartment",
        "area_sqm": 50,
        "price_eur": 100000,
        "price_per_sqm_eur": 2000,
        "is_active": True,
        "is_duplicate": False,
    }
    values.update(overrides)
    return Listing(**values)


def healthy_daily_artifacts(db, data_dir):
    digest_dir = data_dir / "digests"
    digest_dir.mkdir(parents=True)
    (digest_dir / "2026-07-12.json").write_text("{}", encoding="utf-8")
    db.add(
        NeighborhoodStatsHistory(
            neighborhood="Люлин",
            snapshot_date=NOW,
            median_price_per_sqm=2000,
            mean_price_per_sqm=2050,
            listing_count=20,
        )
    )


def test_corrupt_benchmark_is_red_but_allowlisted_german_is_amber(tmp_path):
    db = session()
    db.add(listing("one"))
    healthy_daily_artifacts(db, tmp_path)
    db.commit()
    market = {
        "neighborhoods": [
            {"neighborhood": "Люлин", "listing_count": 25, "delta_vs_imotbg_pct": 55.0},
            {"neighborhood": "Герман", "listing_count": 30, "delta_vs_imotbg_pct": -62.0},
        ]
    }

    health = evaluate_data_health(
        db,
        market,
        current_sources=[],
        previous_runs=[],
        data_dir=tmp_path,
        now=NOW,
    )
    checks = {check["key"]: check for check in health["checks"]}

    assert health["status"] == "red"
    assert checks["benchmark:Люлин"]["status"] == "red"
    assert checks["benchmark:Герман"]["status"] == "amber"
    assert "house-tier village" in checks["benchmark:Герман"]["detail"]


def test_source_count_and_median_drift_against_trailing_runs(tmp_path):
    db = session()
    db.add_all([listing("one", price_per_sqm_eur=2000), listing("two", price_per_sqm_eur=2200)])
    healthy_daily_artifacts(db, tmp_path)
    db.commit()
    previous_runs = [
        {
            "data_health": {
                "source_metrics": {
                    "imotbg": {"scraped": 100, "median_price_per_sqm": 1000}
                }
            }
        }
        for _ in range(7)
    ]

    health = evaluate_data_health(
        db,
        {"neighborhoods": []},
        current_sources=[{"name": "imot.bg", "scraped": 200}],
        previous_runs=previous_runs,
        data_dir=tmp_path,
        now=NOW,
    )
    checks = {check["key"]: check for check in health["checks"]}

    assert checks["source:imotbg:scraped"]["status"] == "red"
    assert "+100.0%" in checks["source:imotbg:scraped"]["detail"]
    assert checks["source:imotbg:median_price_per_sqm"]["status"] == "red"
    assert health["source_metrics"]["imotbg"]["median_price_per_sqm"] == 2100.0


def test_health_failure_never_blocks_export(monkeypatch, tmp_path):
    db = session()
    data_dir = tmp_path / "data" / "dashboard"
    data_dir.parent.mkdir(parents=True)
    monkeypatch.setattr(dashboard_exporter, "DASHBOARD_REPO_PATH", tmp_path)
    monkeypatch.setattr(dashboard_exporter, "DASHBOARD_DATA_DIR", data_dir)
    monkeypatch.setattr(
        dashboard_exporter,
        "_build_listings_payload",
        lambda _db: {"listings": [], "neighborhoods": [], "stats": {"totalListings": 0, "totalDeals": 0}},
    )
    monkeypatch.setattr(dashboard_exporter, "calculate_market_signals", lambda _db: {"city": {}, "neighborhoods": {}})
    monkeypatch.setattr(dashboard_exporter, "_build_digest_payload", lambda *_args: {"summary": {}})
    monkeypatch.setattr(dashboard_exporter, "_build_market_payload", lambda *_args: {"city": {}, "neighborhoods": []})
    monkeypatch.setattr(dashboard_exporter, "_attach_imotbg_benchmark", lambda _payload: None)
    monkeypatch.setattr(
        dashboard_exporter,
        "evaluate_data_health",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("synthetic health failure")),
    )

    summary = dashboard_exporter.export_dashboard(db, push=False)

    assert summary["ok"] is True
    assert summary["data_health"]["status"] == "amber"
    assert "synthetic health failure" in summary["data_health"]["checks"][0]["detail"]


def test_digest_archive_failure_is_warning_only(monkeypatch, tmp_path):
    db = session()
    data_dir = tmp_path / "data" / "dashboard"
    data_dir.parent.mkdir(parents=True)
    monkeypatch.setattr(dashboard_exporter, "DASHBOARD_REPO_PATH", tmp_path)
    monkeypatch.setattr(dashboard_exporter, "DASHBOARD_DATA_DIR", data_dir)
    monkeypatch.setattr(
        dashboard_exporter,
        "_build_listings_payload",
        lambda _db: {"listings": [], "neighborhoods": [], "stats": {"totalListings": 0, "totalDeals": 0}},
    )
    monkeypatch.setattr(dashboard_exporter, "calculate_market_signals", lambda _db: {"city": {}, "neighborhoods": {}})
    monkeypatch.setattr(dashboard_exporter, "_build_digest_payload", lambda *_args: {"summary": {}})
    monkeypatch.setattr(dashboard_exporter, "_build_market_payload", lambda *_args: {"city": {}, "neighborhoods": []})
    monkeypatch.setattr(dashboard_exporter, "_attach_imotbg_benchmark", lambda _payload: None)
    monkeypatch.setattr(
        dashboard_exporter,
        "_archive_digest",
        lambda *_args: (_ for _ in ()).throw(OSError("archive unavailable")),
    )

    summary = dashboard_exporter.export_dashboard(db, push=False)
    checks = {check["key"]: check for check in summary["data_health"]["checks"]}

    assert summary["ok"] is True
    assert checks["digest_archive"]["status"] == "red"


def test_run_recorder_serializes_data_health_without_changing_run_status():
    recorder = RunRecorder()
    recorder.set_data_health({"status": "red", "checks": [{"key": "test"}]})

    assert recorder.status == "running"
    assert recorder.to_dict()["data_health"]["status"] == "red"


def test_data_health_publishes_hedonic_holdout_and_benchmark_gap(monkeypatch, tmp_path):
    db = session()
    db.add_all(
        [
            listing(
                f"model-{index}",
                neighborhood="Люлин",
                predicted_price_per_sqm=3000,
            )
            for index in range(20)
        ]
    )
    healthy_daily_artifacts(db, tmp_path)
    db.commit()
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    (model_dir / "hedonic-20260712.metrics.json").write_text(
        '{"model_mae_pct": 18.5, "baseline_mae_pct": 24.0, "ship_gate_passed": true}',
        encoding="utf-8",
    )
    monkeypatch.setattr("src.analysis.data_health.HEDONIC_MODEL_DIR", model_dir)

    health = evaluate_data_health(
        db,
        {"neighborhoods": [{"neighborhood": "Люлин", "imotbg_avg_price_per_sqm": 1800}]},
        current_sources=[],
        previous_runs=[],
        data_dir=tmp_path,
        now=NOW,
    )
    checks = {check["key"]: check for check in health["checks"]}

    assert checks["hedonic_holdout"]["value"] == 18.5
    assert checks["hedonic_holdout"]["status"] == "green"
    assert checks["hedonic_benchmark"]["status"] == "red"
    assert checks["hedonic_benchmark"]["value"] == 1
