"""Warning-only data quality checks recorded alongside each pipeline run."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any, Dict, Iterable, List

from sqlalchemy.orm import Session

from src.config import (
    DATA_HEALTH_BENCHMARK_DELTA_PCT,
    DATA_HEALTH_DRIFT_PCT,
    DATA_HEALTH_IMAGE_ERROR_PCT,
    DATA_HEALTH_IMAGE_WARN_PCT,
)
from src.database.models import Listing, NeighborhoodStatsHistory
from src.utils.time import utc_now

BENCHMARK_ALLOWLIST = {
    "Герман": "house-tier village",
}

SOURCE_KEYS = {
    "imot.bg": "imotbg",
    "homes.bg": "homesbg",
    "imoti.info": "imotiinfo",
    "imoti.net": "imotinet",
    "property.bg": "propertybg",
}


def evaluate_data_health(
    db: Session,
    market_payload: Dict[str, Any],
    *,
    current_sources: Iterable[Dict[str, Any]] | None,
    previous_runs: List[Dict[str, Any]],
    data_dir: Path,
    now: datetime | None = None,
) -> Dict[str, Any]:
    """Evaluate benchmark, drift, coverage, and daily-write tripwires."""
    now = now or utc_now()
    checks: List[Dict[str, Any]] = []

    _benchmark_checks(checks, market_payload)
    source_metrics = _source_metrics(db, current_sources)
    _source_drift_checks(checks, source_metrics, previous_runs)
    _image_coverage_check(checks, db)
    _daily_write_checks(checks, db, data_dir, now)

    statuses = {check["status"] for check in checks}
    overall = "red" if "red" in statuses else "amber" if "amber" in statuses else "green"
    return {
        "status": overall,
        "checks": checks,
        "source_metrics": source_metrics,
        "generated_at": now.isoformat(),
    }


def load_previous_runs(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    try:
        return list(json.loads(path.read_text(encoding="utf-8")).get("runs", []))[:7]
    except (OSError, ValueError, TypeError):
        return []


def _benchmark_checks(checks: List[Dict[str, Any]], market_payload: Dict[str, Any]) -> None:
    outliers = []
    for row in market_payload.get("neighborhoods", []):
        count = int(row.get("listing_count") or 0)
        delta = row.get("delta_vs_imotbg_pct")
        if count < 20 or delta is None or abs(float(delta)) <= DATA_HEALTH_BENCHMARK_DELTA_PCT:
            continue
        hood = str(row.get("neighborhood") or "Unknown")
        reason = BENCHMARK_ALLOWLIST.get(hood)
        outliers.append(hood)
        checks.append(
            _check(
                key=f"benchmark:{hood}",
                label=f"{hood} vs imot.bg",
                status="amber" if reason else "red",
                value=round(float(delta), 1),
                unit="%",
                detail=(
                    f"Allowed: {reason}; {count} listings"
                    if reason
                    else f"{count} listings; threshold +/-{DATA_HEALTH_BENCHMARK_DELTA_PCT:.0f}%"
                ),
            )
        )
    if not outliers:
        checks.append(
            _check(
                key="benchmark",
                label="Neighborhood benchmark divergence",
                status="green",
                value=0,
                unit="flags",
                detail=f"All 20+ listing neighborhoods within +/-{DATA_HEALTH_BENCHMARK_DELTA_PCT:.0f}%",
            )
        )


def _source_metrics(
    db: Session,
    current_sources: Iterable[Dict[str, Any]] | None,
) -> Dict[str, Dict[str, Any]]:
    scraped = {}
    labels = {}
    for source in current_sources or []:
        label = str(source.get("name") or "")
        key = SOURCE_KEYS.get(label, label.replace(".", "").replace(" ", "").lower())
        scraped[key] = int(source.get("scraped") or 0)
        labels[key] = label or key

    prices: Dict[str, List[float]] = defaultdict(list)
    rows = db.query(Listing.source, Listing.price_per_sqm_eur).filter(
        Listing.is_active.is_(True),
        (Listing.is_duplicate.is_(False)) | (Listing.is_duplicate.is_(None)),
        Listing.price_per_sqm_eur > 0,
    ).all()
    for source, price in rows:
        prices[source].append(float(price))

    metrics = {}
    for source in sorted(set(prices) | set(scraped)):
        metrics[source] = {
            "label": labels.get(source, source),
            "scraped": scraped.get(source),
            "median_price_per_sqm": (
                round(float(median(prices[source])), 2) if prices.get(source) else None
            ),
        }
    return metrics


def _source_drift_checks(
    checks: List[Dict[str, Any]],
    source_metrics: Dict[str, Dict[str, Any]],
    previous_runs: List[Dict[str, Any]],
) -> None:
    for source, current in source_metrics.items():
        history = [
            run.get("data_health", {}).get("source_metrics", {}).get(source, {})
            for run in previous_runs[:7]
        ]
        for field, label, unit in (
            ("scraped", "scraped count", "listings"),
            ("median_price_per_sqm", "median EUR/m2", "EUR/m2"),
        ):
            value = current.get(field)
            if value is None:
                continue
            baselines = [float(item[field]) for item in history if item.get(field) is not None]
            baseline = float(median(baselines)) if baselines else None
            delta = _delta_pct(float(value), baseline)
            drifted = delta is not None and abs(delta) > DATA_HEALTH_DRIFT_PCT
            checks.append(
                _check(
                    key=f"source:{source}:{field}",
                    label=f"{current['label']} {label}",
                    status="red" if drifted else "green",
                    value=round(float(value), 1),
                    unit=unit,
                    detail=(
                        f"{delta:+.1f}% vs trailing-{len(baselines)} median {baseline:,.1f}"
                        if baseline is not None and delta is not None
                        else "Collecting trailing-seven baseline"
                    ),
                )
            )


def _image_coverage_check(checks: List[Dict[str, Any]], db: Session) -> None:
    rows = db.query(Listing.image_url).filter(
        Listing.is_active.is_(True),
        (Listing.is_duplicate.is_(False)) | (Listing.is_duplicate.is_(None)),
    ).all()
    total = len(rows)
    with_images = sum(1 for (url,) in rows if url)
    pct = round(with_images / total * 100, 1) if total else 0.0
    status = (
        "red"
        if pct < DATA_HEALTH_IMAGE_ERROR_PCT
        else "amber"
        if pct < DATA_HEALTH_IMAGE_WARN_PCT
        else "green"
    )
    checks.append(
        _check(
            key="image_coverage",
            label="Active listings with images",
            status=status,
            value=pct,
            unit="%",
            detail=f"{with_images:,} of {total:,}",
        )
    )


def _daily_write_checks(
    checks: List[Dict[str, Any]],
    db: Session,
    data_dir: Path,
    now: datetime,
) -> None:
    date_key = now.strftime("%Y-%m-%d")
    digest_exists = (data_dir / "digests" / f"{date_key}.json").exists()
    checks.append(
        _check(
            key="digest_archive",
            label="Digest archive wrote today",
            status="green" if digest_exists else "red",
            value=1 if digest_exists else 0,
            unit="write",
            detail=date_key,
        )
    )

    start = datetime(now.year, now.month, now.day)
    snapshots = db.query(NeighborhoodStatsHistory).filter(
        NeighborhoodStatsHistory.snapshot_date >= start,
    ).count()
    checks.append(
        _check(
            key="snapshot_growth",
            label="Neighborhood snapshots today",
            status="green" if snapshots > 0 else "red",
            value=snapshots,
            unit="rows",
            detail="Snapshot table must grow on each scrape day",
        )
    )


def _check(
    *,
    key: str,
    label: str,
    status: str,
    value: float | int,
    unit: str,
    detail: str,
) -> Dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "status": status,
        "value": value,
        "unit": unit,
        "detail": detail,
    }


def _delta_pct(value: float, baseline: float | None) -> float | None:
    if baseline is None or baseline == 0:
        return None
    return (value - baseline) / baseline * 100
