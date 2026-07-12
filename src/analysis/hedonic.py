"""Weekly hedonic fair-value training and nightly per-listing scoring.

The preferred backend is sklearn's HistGradientBoostingRegressor. The minimal
project venv does not currently include sklearn and the task's dependency
allowlist does not permit installing it, so a persisted NumPy ridge backend is
the deterministic fallback. Both backends share the same feature encoder,
holdout split, metrics, and deal-engine contract.
"""

from __future__ import annotations

import json
import math
import pickle
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List

import numpy as np
import pandas as pd
from loguru import logger
from sqlalchemy.orm import Session

from src.config import (
    DEAL_ENGINE,
    HEDONIC_DEAL_RESIDUAL_PCT,
    HEDONIC_MAX_TRAIN_PRICE_PER_SQM,
    HEDONIC_MODEL_DIR,
    HEDONIC_TRAIN_WEEKDAY,
    MAX_APARTMENT_AREA_SQM,
    MIN_APARTMENT_AREA_SQM,
    MIN_APARTMENT_PRICE_PER_SQM_EUR,
    MIN_PRICE_EUR,
)
from src.database.models import Listing
from src.utils.time import utc_now


APARTMENT_TYPES = ("apartment", "studio", "maisonette")
TARGET_SMOOTHING = 20.0
RIDGE_ALPHA = 0.1
CATEGORICAL_COLUMNS = (
    "property_type",
    "construction_type",
    "year_bucket",
    "view",
    "renovation_state",
    "parking",
    "seller_type",
)


class NumpyRidgeRegressor:
    """Small serializable log-price regressor used when sklearn is unavailable."""

    def __init__(self, alpha: float = RIDGE_ALPHA):
        self.alpha = alpha
        self.coef_: np.ndarray | None = None

    def fit(self, x: np.ndarray, y: np.ndarray) -> "NumpyRidgeRegressor":
        design = np.column_stack([np.ones(len(x)), x])
        penalty = self.alpha * np.eye(design.shape[1])
        penalty[0, 0] = 0
        self.coef_ = np.linalg.solve(design.T @ design + penalty, design.T @ y)
        return self

    def predict(self, x: np.ndarray) -> np.ndarray:
        if self.coef_ is None:
            raise RuntimeError("Model is not fitted")
        return np.column_stack([np.ones(len(x)), x]) @ self.coef_


@dataclass
class FeatureEncoder:
    neighborhood_encoding: Dict[str, float]
    global_log_target: float
    category_values: Dict[str, List[str]]
    feature_names: List[str]
    means: np.ndarray
    scales: np.ndarray
    use_gps: bool

    @classmethod
    def fit(cls, frame: pd.DataFrame, y_log: np.ndarray, *, use_gps: bool) -> "FeatureEncoder":
        global_target = float(np.median(y_log))
        grouped = frame.assign(_target=y_log).groupby("neighborhood")["_target"].agg(["mean", "count"])
        neighborhood_encoding = (
            (grouped["mean"] * grouped["count"] + global_target * TARGET_SMOOTHING)
            / (grouped["count"] + TARGET_SMOOTHING)
        ).to_dict()
        category_values = {
            column: sorted(frame[column].fillna("unknown").astype(str).unique().tolist())
            for column in CATEGORICAL_COLUMNS
        }
        placeholder = cls(
            neighborhood_encoding=neighborhood_encoding,
            global_log_target=global_target,
            category_values=category_values,
            feature_names=[],
            means=np.array([]),
            scales=np.array([]),
            use_gps=use_gps,
        )
        raw, names = placeholder._raw_matrix(frame)
        means = raw.mean(axis=0)
        scales = raw.std(axis=0)
        scales[scales < 1e-8] = 1.0
        placeholder.feature_names = names
        placeholder.means = means
        placeholder.scales = scales
        return placeholder

    def transform(self, frame: pd.DataFrame) -> np.ndarray:
        raw, _names = self._raw_matrix(frame)
        return (raw - self.means) / self.scales

    def _raw_matrix(self, frame: pd.DataFrame) -> tuple[np.ndarray, List[str]]:
        area = frame["area_sqm"].astype(float).to_numpy()
        log_area = np.log(np.maximum(area, 1))
        rooms = frame["rooms"].fillna(0).astype(float).to_numpy()
        floor = frame["floor"].fillna(-1).astype(float).to_numpy()
        total_floors = frame["total_floors"].fillna(-1).astype(float).to_numpy()
        floor_ratio = np.where(total_floors > 0, np.maximum(floor, 0) / total_floors, 0)
        hood_encoded = frame["neighborhood"].map(self.neighborhood_encoding).fillna(self.global_log_target).to_numpy(float)

        columns = [
            hood_encoded,
            log_area,
            area / 100,
            rooms,
            floor,
            floor_ratio,
            (floor == 0).astype(float),
            ((total_floors > 0) & (floor == total_floors)).astype(float),
            frame["exposure_south"].astype(float).to_numpy(),
            frame["exposure_east"].astype(float).to_numpy(),
            frame["act16"].fillna(False).astype(float).to_numpy(),
            frame["has_elevator"].fillna(False).astype(float).to_numpy(),
            log_area**2,
            rooms**2,
            floor_ratio**2,
            hood_encoded * log_area,
            hood_encoded * rooms,
        ]
        names = [
            "neighborhood",
            "log_area",
            "area",
            "rooms",
            "floor",
            "floor_ratio",
            "ground_floor",
            "top_floor",
            "south_exposure",
            "east_exposure",
            "act16",
            "elevator",
            "log_area_squared",
            "rooms_squared",
            "floor_ratio_squared",
            "neighborhood_x_area",
            "neighborhood_x_rooms",
        ]
        if self.use_gps:
            columns.extend(
                [
                    frame["latitude"].fillna(42.6977).astype(float).to_numpy(),
                    frame["longitude"].fillna(23.3219).astype(float).to_numpy(),
                ]
            )
            names.extend(["latitude", "longitude"])

        for column in CATEGORICAL_COLUMNS:
            values = frame[column].fillna("unknown").astype(str)
            for category in self.category_values[column][1:]:
                columns.append((values == category).astype(float).to_numpy())
                names.append(f"{column}={category}")
        return np.column_stack(columns), names


@dataclass
class HedonicArtifact:
    backend: str
    encoder: FeatureEncoder
    model: Any
    metrics: Dict[str, Any]

    def predict_log(self, frame: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
        transformed = self.encoder.transform(frame)
        return np.asarray(self.model.predict(transformed), dtype=float), transformed


def _year_bucket(value: Any) -> str:
    try:
        year = int(value)
    except (TypeError, ValueError):
        return "unknown"
    if year < 1960:
        return "pre-1960"
    if year >= 2020:
        return "2020+"
    return f"{year // 10 * 10}s"


def _llm_view(raw: str | None) -> str:
    if not raw:
        return "unknown"
    try:
        value = json.loads(raw).get("view")
    except (ValueError, TypeError, AttributeError):
        return "unknown"
    return str(value).strip().lower()[:80] if value else "unknown"


def _listing_frame(rows: Iterable[Listing]) -> pd.DataFrame:
    records = []
    for row in rows:
        try:
            exposure = set(json.loads(row.exposure or "[]"))
        except (ValueError, TypeError):
            exposure = set()
        records.append(
            {
                "id": row.id,
                "neighborhood": row.neighborhood or "Unknown",
                "property_type": row.property_type or "unknown",
                "area_sqm": float(row.area_sqm),
                "rooms": row.rooms,
                "floor": row.floor,
                "total_floors": row.total_floors,
                "construction_type": row.construction_type or "unknown",
                "year_bucket": _year_bucket(row.year_built),
                "exposure_south": "south" in exposure,
                "exposure_east": "east" in exposure,
                "view": _llm_view(row.llm_extract),
                "renovation_state": row.renovation_state or "unknown",
                "act16": row.act16,
                "has_elevator": row.has_elevator,
                "parking": row.parking or "unknown",
                "seller_type": row.seller_type or "unknown",
                "latitude": row.latitude,
                "longitude": row.longitude,
                "price_per_sqm_eur": float(row.price_per_sqm_eur),
                "observed_at": row.first_seen,
            }
        )
    return pd.DataFrame.from_records(records)


def _training_rows(db: Session) -> List[Listing]:
    return db.query(Listing).filter(
        Listing.listing_kind == "sale",
        Listing.property_type.in_(APARTMENT_TYPES),
        (Listing.is_duplicate.is_(False)) | (Listing.is_duplicate.is_(None)),
        Listing.first_seen.isnot(None),
        Listing.area_sqm.between(MIN_APARTMENT_AREA_SQM, MAX_APARTMENT_AREA_SQM),
        Listing.price_per_sqm_eur.between(
            MIN_APARTMENT_PRICE_PER_SQM_EUR,
            HEDONIC_MAX_TRAIN_PRICE_PER_SQM,
        ),
    ).all()


def _baseline_predictions(train: pd.DataFrame, validation: pd.DataFrame) -> np.ndarray:
    tier_one = train.groupby(["neighborhood", "property_type", "construction_type"], dropna=False)["price_per_sqm_eur"].median().to_dict()
    tier_two = train.groupby(["neighborhood", "property_type"], dropna=False)["price_per_sqm_eur"].median().to_dict()
    tier_three = train.groupby("neighborhood")["price_per_sqm_eur"].median().to_dict()
    global_median = float(train["price_per_sqm_eur"].median())
    return np.asarray(
        [
            tier_one.get(
                (row.neighborhood, row.property_type, row.construction_type),
                tier_two.get(
                    (row.neighborhood, row.property_type),
                    tier_three.get(row.neighborhood, global_median),
                ),
            )
            for row in validation.itertuples()
        ],
        dtype=float,
    )


def _mae_pct(actual: np.ndarray, predicted: np.ndarray) -> float:
    return float(np.mean(np.abs(predicted - actual) / actual) * 100)


def _r2(actual: np.ndarray, predicted: np.ndarray) -> float:
    denominator = float(np.sum((actual - actual.mean()) ** 2))
    return 1 - float(np.sum((actual - predicted) ** 2)) / denominator if denominator else 0.0


def _fit_model(x: np.ndarray, y_log: np.ndarray) -> tuple[str, Any]:
    try:
        from sklearn.ensemble import HistGradientBoostingRegressor

        model = HistGradientBoostingRegressor(
            learning_rate=0.05,
            max_iter=250,
            max_leaf_nodes=31,
            l2_regularization=1.0,
            random_state=42,
        ).fit(x, y_log)
        return "sklearn_hist_gradient_boosting", model
    except ImportError:
        return "numpy_ridge_fallback", NumpyRidgeRegressor().fit(x, y_log)


def train(
    db: Session,
    *,
    model_dir: Path = HEDONIC_MODEL_DIR,
    now: datetime | None = None,
) -> Dict[str, Any]:
    """Train on pre-cutoff inventory and persist the model plus holdout metrics."""
    now = now or utc_now()
    frame = _listing_frame(_training_rows(db))
    if len(frame) < 100:
        raise ValueError(f"Need at least 100 valid listings, got {len(frame)}")
    latest = pd.Timestamp(frame["observed_at"].max())
    cutoff = latest - pd.Timedelta(days=30)
    training = frame[frame["observed_at"] <= cutoff].copy()
    validation = frame[frame["observed_at"] > cutoff].copy()
    if len(training) < 80 or len(validation) < 20:
        raise ValueError(f"Insufficient time split: {len(training)} train / {len(validation)} validate")

    y_train_log = np.log(training["price_per_sqm_eur"].to_numpy(float))
    y_validation = validation["price_per_sqm_eur"].to_numpy(float)
    gps_coverage = float(training[["latitude", "longitude"]].notna().all(axis=1).mean())
    encoder = FeatureEncoder.fit(training, y_train_log, use_gps=gps_coverage >= 0.30)
    x_train = encoder.transform(training)
    x_validation = encoder.transform(validation)
    backend, model = _fit_model(x_train, y_train_log)
    predictions = np.exp(model.predict(x_validation))
    baseline = _baseline_predictions(training, validation)
    model_mae = _mae_pct(y_validation, predictions)
    baseline_mae = _mae_pct(y_validation, baseline)
    eligible = model_mae < baseline_mae
    metrics = {
        "trained_at": now.isoformat(),
        "cutoff": cutoff.isoformat(),
        "backend": backend,
        "train_count": int(len(training)),
        "validation_count": int(len(validation)),
        "gps_coverage_pct": round(gps_coverage * 100, 2),
        "gps_features_used": encoder.use_gps,
        "model_mae_pct": round(model_mae, 4),
        "baseline_mae_pct": round(baseline_mae, 4),
        "improvement_pct": round((baseline_mae - model_mae) / baseline_mae * 100, 2),
        "r2": round(_r2(y_validation, predictions), 4),
        "ship_gate_passed": eligible,
        "deal_engine": "hedonic" if eligible else "hedonic-experimental",
    }
    artifact = HedonicArtifact(backend=backend, encoder=encoder, model=model, metrics=metrics)
    model_dir.mkdir(parents=True, exist_ok=True)
    stem = f"hedonic-{now.strftime('%Y%m%d')}"
    with (model_dir / f"{stem}.pkl").open("wb") as handle:
        pickle.dump(artifact, handle)
    (model_dir / f"{stem}.metrics.json").write_text(
        json.dumps(metrics, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    logger.info(
        f"Hedonic {backend}: {model_mae:.2f}% holdout MAE vs {baseline_mae:.2f}% baseline "
        f"({'PASS' if eligible else 'EXPERIMENTAL'})"
    )
    return metrics


def latest_model_path(model_dir: Path = HEDONIC_MODEL_DIR) -> Path | None:
    paths = sorted(model_dir.glob("hedonic-*.pkl")) if model_dir.exists() else []
    return paths[-1] if paths else None


def latest_metrics(model_dir: Path = HEDONIC_MODEL_DIR) -> Dict[str, Any] | None:
    paths = sorted(model_dir.glob("hedonic-*.metrics.json")) if model_dir.exists() else []
    if not paths:
        return None
    try:
        return json.loads(paths[-1].read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None


def load_artifact(path: Path | None = None, *, model_dir: Path = HEDONIC_MODEL_DIR) -> HedonicArtifact:
    path = path or latest_model_path(model_dir)
    if path is None:
        raise FileNotFoundError("No hedonic model is available")
    with path.open("rb") as handle:
        artifact = pickle.load(handle)
    if not isinstance(artifact, HedonicArtifact):
        raise TypeError("Unsupported hedonic model artifact")
    return artifact


def _top_contributions(
    artifact: HedonicArtifact,
    transformed_row: np.ndarray,
) -> List[Dict[str, Any]]:
    if artifact.backend == "numpy_ridge_fallback":
        effects = artifact.model.coef_[1:] * transformed_row
    else:
        base = transformed_row.copy()
        effects = np.zeros_like(transformed_row)
        full = float(artifact.model.predict(transformed_row.reshape(1, -1))[0])
        for index in range(len(transformed_row)):
            counterfactual = base.copy()
            counterfactual[index] = 0
            effects[index] = full - float(artifact.model.predict(counterfactual.reshape(1, -1))[0])
    ranked = sorted(range(len(effects)), key=lambda index: abs(effects[index]), reverse=True)[:5]
    return [
        {
            "feature": artifact.encoder.feature_names[index],
            "impact_pct": round((math.exp(float(effects[index])) - 1) * 100, 2),
        }
        for index in ranked
    ]


def score(
    db: Session,
    *,
    model_path: Path | None = None,
    model_dir: Path = HEDONIC_MODEL_DIR,
) -> Dict[str, Any]:
    """Fill nightly fair-value, residual, and top-five explanation columns."""
    artifact = load_artifact(model_path, model_dir=model_dir)
    rows = db.query(Listing).filter(
        Listing.listing_kind == "sale",
        Listing.property_type.in_(APARTMENT_TYPES),
        Listing.is_active.is_(True),
        (Listing.is_duplicate.is_(False)) | (Listing.is_duplicate.is_(None)),
        Listing.area_sqm.between(MIN_APARTMENT_AREA_SQM, MAX_APARTMENT_AREA_SQM),
        Listing.price_per_sqm_eur > 0,
    ).all()
    if not rows:
        return {"scored": 0, "backend": artifact.backend}
    frame = _listing_frame(rows)
    log_predictions, transformed = artifact.predict_log(frame)
    predictions = np.exp(log_predictions)
    for index, row in enumerate(rows):
        predicted = float(predictions[index])
        row.predicted_price_per_sqm = round(predicted, 2)
        row.residual_pct = round((float(row.price_per_sqm_eur) - predicted) / predicted * 100, 2)
        row.hedonic_contributions = json.dumps(
            _top_contributions(artifact, transformed[index]),
            ensure_ascii=False,
        )
    db.commit()
    return {
        "scored": len(rows),
        "backend": artifact.backend,
        "ship_gate_passed": bool(artifact.metrics.get("ship_gate_passed")),
    }


def effective_deal_engine(
    requested: str = DEAL_ENGINE,
    *,
    model_dir: Path = HEDONIC_MODEL_DIR,
) -> str:
    """Keep z-score active unless a validated model or explicit experiment exists."""
    requested = (requested or "zscore").lower()
    if requested == "hedonic-experimental":
        return requested
    if requested != "hedonic":
        return "zscore"
    metrics = latest_metrics(model_dir)
    return "hedonic" if metrics and metrics.get("ship_gate_passed") else "zscore"


def is_hedonic_deal(listing: Listing, *, threshold: float = HEDONIC_DEAL_RESIDUAL_PCT) -> bool:
    return bool(
        listing.listing_kind == "sale"
        and listing.is_active
        and listing.property_type in APARTMENT_TYPES
        and float(listing.price_eur or 0) >= MIN_PRICE_EUR
        and MIN_APARTMENT_AREA_SQM <= float(listing.area_sqm or 0) <= MAX_APARTMENT_AREA_SQM
        and listing.predicted_price_per_sqm
        and listing.residual_pct is not None
        and listing.residual_pct <= threshold
    )


def weekly_train_and_score(
    db: Session,
    *,
    now: datetime | None = None,
    model_dir: Path = HEDONIC_MODEL_DIR,
) -> Dict[str, Any]:
    now = now or utc_now()
    should_train = now.weekday() == HEDONIC_TRAIN_WEEKDAY or latest_model_path(model_dir) is None
    metrics = train(db, model_dir=model_dir, now=now) if should_train else latest_metrics(model_dir)
    scoring = score(db, model_dir=model_dir)
    return {"trained": should_train, "metrics": metrics, **scoring}
