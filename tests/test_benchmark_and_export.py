"""Tests for TIN-471 (per-listing z-scores), TIN-472 (sanity guards),
TIN-473 (digest archive), TIN-476 (imot.bg benchmark)."""

from pathlib import Path

from src.analysis.imotbg_benchmark import parse_benchmark_table
from src.exporters.dashboard import _archive_digest, _attach_imotbg_benchmark, _dashboard_district
from src.utils.neighborhoods import canonicalize_neighborhood

FIXTURE = Path(__file__).parent / "fixtures" / "sredni_ceni_table.html"


# ── TIN-476: benchmark parser ────────────────────────────────────────────────


def test_benchmark_parser_on_fixture():
    table = parse_benchmark_table(FIXTURE.read_text(encoding="utf-8"))
    assert len(table) > 100
    # Canonical names, sane values
    assert 800 < table["Банишора"] < 6000
    assert "Банкя" in table  # their "Банкя (гр.)"-style label canonicalizes
    for value in table.values():
        assert 100 <= value <= 20000


def test_benchmark_parser_empty_html():
    assert parse_benchmark_table("<html><body>nope</body></html>") == {}


def test_attach_benchmark_computes_delta(monkeypatch):
    monkeypatch.setattr(
        "src.exporters.dashboard.fetch_benchmark",
        lambda: {"Банкя": 2000.0},
    )
    payload = {
        "city": {"median_price_per_sqm": 2900.0},
        "neighborhoods": [
            {"neighborhood": "Банкя", "median_price_per_sqm": 2200.0},
            {"neighborhood": "Никъде", "median_price_per_sqm": 1000.0},
        ],
    }
    _attach_imotbg_benchmark(payload)
    bankja = payload["neighborhoods"][0]
    assert bankja["imotbg_avg_price_per_sqm"] == 2000.0
    assert bankja["delta_vs_imotbg_pct"] == 10.0  # (2200-2000)/2000
    missing = payload["neighborhoods"][1]
    assert missing["imotbg_avg_price_per_sqm"] is None
    assert missing["delta_vs_imotbg_pct"] is None
    assert payload["city"]["imotbg_avg_price_per_sqm"] == 2000.0


def test_attach_benchmark_survives_fetch_failure(monkeypatch):
    monkeypatch.setattr("src.exporters.dashboard.fetch_benchmark", lambda: None)
    payload = {
        "city": {"median_price_per_sqm": 2900.0},
        "neighborhoods": [{"neighborhood": "Банкя", "median_price_per_sqm": 2200.0}],
    }
    _attach_imotbg_benchmark(payload)  # must not raise
    assert payload["neighborhoods"][0]["imotbg_avg_price_per_sqm"] is None
    assert payload["city"]["delta_vs_imotbg_pct"] is None


# ── TIN-472: junk neighborhood guard ─────────────────────────────────────────


def test_junk_ad_text_becomes_unknown():
    junk = "Снимки 8 продава Парцел, 4500 м 2 София, Световрачене"
    assert canonicalize_neighborhood(junk) == "Unknown"
    assert canonicalize_neighborhood("х" * 50) == "Unknown"
    # Real names survive
    assert canonicalize_neighborhood("Манастирски ливади") == "Манастирски ливади"


# ── TIN-472: district row mapping ────────────────────────────────────────────


def test_dashboard_district_maps_velocity_row_keys():
    row = {"name": "Люлин 5", "added": 7, "sold": 3, "avg_price": "1 841"}
    out = _dashboard_district(row, {"Люлин 5": 2})
    assert out["neighborhood"] == "Люлин 5"
    assert out["new_listings"] == 7
    assert out["deal_count"] == 2
    assert out["off_market"] == 3
    assert out["avg_price_per_sqm"] == 1841.0


# ── TIN-473: digest archive ──────────────────────────────────────────────────


def test_archive_digest_writes_dated_file_and_index(tmp_path):
    payload = {"summary": {"total_active": 1}, "new_deals": []}
    _archive_digest(tmp_path, payload)
    import json

    index = json.loads((tmp_path / "digests" / "index.json").read_text())
    assert len(index["dates"]) == 1
    date = index["dates"][0]
    daily = json.loads((tmp_path / "digests" / f"{date}.json").read_text())
    assert daily["summary"]["total_active"] == 1

    # Same-day re-run overwrites, doesn't duplicate
    _archive_digest(tmp_path, payload)
    index = json.loads((tmp_path / "digests" / "index.json").read_text())
    assert len(index["dates"]) == 1
