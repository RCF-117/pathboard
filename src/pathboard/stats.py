"""Statistics used by all three experiments.

Runs of the same task are not independent, so uncertainty is estimated with a
cluster bootstrap that resamples tasks and keeps each task's runs together.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

import numpy as np


def cluster_bootstrap_ci(
    clusters: Mapping[str, Sequence[float]],
    statistic: Callable[[np.ndarray], float],
    n_boot: int,
    confidence: float,
    rng: np.random.Generator,
) -> tuple[float, float, float]:
    """Point estimate and percentile CI of ``statistic`` over cluster means.

    Each cluster (a task) contributes the mean of its runs.  Returns
    ``(estimate, low, high)``.
    """
    if not clusters:
        raise ValueError("no clusters given")
    if not 0.0 < confidence < 1.0:
        raise ValueError(f"confidence must be in (0, 1), got {confidence}")
    if n_boot < 1:
        raise ValueError("n_boot must be positive")
    means = []
    for name, values in clusters.items():
        arr = np.asarray(values, dtype=float)
        if arr.size == 0:
            raise ValueError(f"cluster {name!r} has no values")
        if not np.all(np.isfinite(arr)):
            raise ValueError(f"cluster {name!r} has non-finite values")
        means.append(arr.mean())
    means_arr = np.asarray(means, dtype=float)
    estimate = float(statistic(means_arr))
    n = means_arr.size
    idx = rng.integers(0, n, size=(n_boot, n))
    boots = np.array([statistic(means_arr[row]) for row in idx], dtype=float)
    alpha = 1.0 - confidence
    low, high = np.quantile(boots, [alpha / 2.0, 1.0 - alpha / 2.0])
    return estimate, float(low), float(high)


def geometric_mean(x: np.ndarray) -> float:
    """Geometric mean; the right average for speedup ratios."""
    arr = np.asarray(x, dtype=float)
    if arr.size == 0:
        raise ValueError("geometric mean of empty array")
    if np.any(arr <= 0.0):
        raise ValueError("geometric mean needs strictly positive values")
    return float(np.exp(np.mean(np.log(arr))))


def wilson_interval(successes: int, n: int, confidence: float) -> tuple[float, float, float]:
    """Proportion with a Wilson score interval. Returns ``(p, low, high)``."""
    if n <= 0:
        raise ValueError("n must be positive")
    if not 0 <= successes <= n:
        raise ValueError(f"successes={successes} outside [0, {n}]")
    from statistics import NormalDist

    z = NormalDist().inv_cdf(0.5 + confidence / 2.0)
    p = successes / n
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / denom
    return p, max(0.0, centre - half), min(1.0, centre + half)
