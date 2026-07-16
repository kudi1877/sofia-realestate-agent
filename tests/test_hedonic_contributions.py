"""Regression tests for vectorized hedonic contributions (2026-07-16 incident).

The per-row implementation predicted one listing at a time (1 + n_features
single-row sklearn calls each) — nightly scoring ground for hours at pegged
CPU. The batch form must produce identical numbers in a bounded time.
"""

import time
from types import SimpleNamespace

import numpy as np

from src.analysis.hedonic import _contribution_effects, _format_top_contributions


def _toy_sklearn_artifact(n_features: int = 4):
    from sklearn.ensemble import HistGradientBoostingRegressor

    rng = np.random.default_rng(42)
    x = rng.normal(size=(200, n_features))
    y = x @ rng.normal(size=n_features) + rng.normal(scale=0.1, size=200)
    model = HistGradientBoostingRegressor(max_iter=30, random_state=0).fit(x, y)
    return SimpleNamespace(
        backend="sklearn_hist_gradient_boosting",
        model=model,
        encoder=SimpleNamespace(feature_names=[f"f{i}" for i in range(n_features)]),
    )


def test_batch_effects_match_per_row_counterfactuals():
    artifact = _toy_sklearn_artifact()
    rng = np.random.default_rng(7)
    transformed = rng.normal(size=(25, 4))
    log_predictions = np.asarray(artifact.model.predict(transformed), dtype=float)

    effects = _contribution_effects(artifact, transformed, log_predictions)

    # Reference: the old per-row semantics, computed independently.
    for row_index in (0, 12, 24):
        full = float(artifact.model.predict(transformed[row_index].reshape(1, -1))[0])
        for feature_index in range(4):
            counterfactual = transformed[row_index].copy()
            counterfactual[feature_index] = 0
            expected = full - float(
                artifact.model.predict(counterfactual.reshape(1, -1))[0]
            )
            assert abs(effects[row_index, feature_index] - expected) < 1e-9


def test_ridge_backend_effects_are_coef_times_row():
    coef = np.array([0.5, 1.0, -2.0, 3.0])  # [intercept, f0, f1, f2]
    artifact = SimpleNamespace(
        backend="numpy_ridge_fallback",
        model=SimpleNamespace(coef_=coef),
        encoder=SimpleNamespace(feature_names=["f0", "f1", "f2"]),
    )
    transformed = np.array([[1.0, 2.0, 3.0], [0.0, 1.0, 0.5]])

    effects = _contribution_effects(artifact, transformed, np.zeros(2))

    assert np.allclose(effects, transformed * coef[1:])


def test_batch_scoring_scale_is_bounded():
    # 6,500 rows × 30 features must complete in seconds, not hours.
    artifact = _toy_sklearn_artifact(n_features=30)
    rng = np.random.default_rng(1)
    transformed = rng.normal(size=(6500, 30))
    log_predictions = np.asarray(artifact.model.predict(transformed), dtype=float)

    start = time.time()
    effects = _contribution_effects(artifact, transformed, log_predictions)
    elapsed = time.time() - start

    assert effects.shape == (6500, 30)
    assert elapsed < 30, f"contribution matrix took {elapsed:.1f}s — vectorization regressed"


def test_format_top_contributions_ranks_by_magnitude():
    artifact = SimpleNamespace(
        backend="numpy_ridge_fallback",
        model=None,
        encoder=SimpleNamespace(feature_names=["small", "big_negative", "medium"]),
    )
    top = _format_top_contributions(artifact, np.array([0.01, -0.4, 0.2]))

    assert [item["feature"] for item in top] == ["big_negative", "medium", "small"]
    assert top[0]["impact_pct"] < 0
