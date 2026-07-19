"""Pure-Python time-series forecasting engine for balance evolution.

Zero external dependencies (no numpy/scipy/statsmodels).
Uses only Python's built-in math library.

Pipeline:
  - <3 data points  → Naive (last value, ±10% CI)
  - 3-4 data points → Linear OLS Regression, 95% CI
  - ≥5 data points  → Harmonic Seasonal Regression (sin/cos period 12),
                       solved via Gauss-Jordan elimination with partial
                       pivoting, 95% CI

Returns a ForecastResult with aligned arrays and method attribution.
Gracefully falls back on any numerical error.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import List, Optional

log = logging.getLogger(__name__)

Z95 = 1.96  # Z-score for 95% confidence interval


# ── Result container ────────────────────────────────────────────────────────

@dataclass
class ForecastResult:
    forecast_values: List[float]
    lower_bound: List[float]
    upper_bound: List[float]
    method: str = "unknown"


# ── Public API ──────────────────────────────────────────────────────────────

def forecast_balance(
    history: List[dict],
    periods: int = 6,
) -> Optional[ForecastResult]:
    """Forecast future balance values.

    Args:
        history: Chronologically ordered list of dicts with "balance" key.
        periods: Number of future months to project (default 6).

    Returns:
        ForecastResult or None if history is empty.
    """
    if not history:
        return None

    n = len(history)
    try:
        if n < 3:
            return _forecast_naive(history, periods)
        elif n < 5:
            return _forecast_linear(history, periods)
        else:
            return _forecast_harmonic(history, periods)
    except Exception as exc:
        log.warning("Forecast failed (%s), falling back to naive: %s", type(exc).__name__, exc)
        return _forecast_naive(history, periods)


# ── Layer 1 — Naive (cold start, <3 points) ────────────────────────────────

def _forecast_naive(history: List[dict], periods: int) -> ForecastResult:
    last = history[-1]["balance"] if history else 0.0
    margin = abs(last) * 0.10 if last != 0 else 100.0

    return ForecastResult(
        forecast_values=[round(last, 2)] * periods,
        lower_bound=[round(last - margin, 2)] * periods,
        upper_bound=[round(last + margin, 2)] * periods,
        method="naive",
    )


# ── Layer 2 — Linear OLS Regression (3-4 points) ───────────────────────────

def _forecast_linear(history: List[dict], periods: int) -> ForecastResult:
    n = len(history)
    y = [h["balance"] for h in history]
    x = list(range(n))

    sum_x  = sum(x)
    sum_y  = sum(y)
    sum_xx = sum(xi * xi for xi in x)
    sum_xy = sum(xi * yi for xi, yi in zip(x, y))

    denom = n * sum_xx - sum_x * sum_x
    if denom == 0:
        slope, intercept = 0.0, y[-1]
    else:
        slope = (n * sum_xy - sum_x * sum_y) / denom
        intercept = (sum_y - slope * sum_x) / n

    # Residuals → standard error
    y_hat = [slope * xi + intercept for xi in x]
    residuals = [yi - h for yi, h in zip(y, y_hat)]
    variance = sum(r * r for r in residuals) / max(n - 2, 1)
    std_err = math.sqrt(variance) if variance > 1e-10 else abs(y[-1]) * 0.05 or 1.0

    # Projections
    fv, lo, hi = [], [], []
    for step in range(1, periods + 1):
        future_x = n - 1 + step
        pred = slope * future_x + intercept
        margin = Z95 * std_err * math.sqrt(step)
        fv.append(round(pred, 2))
        lo.append(round(pred - margin, 2))
        hi.append(round(pred + margin, 2))

    return ForecastResult(forecast_values=fv, lower_bound=lo, upper_bound=hi, method="linear")


# ── Layer 3 — Harmonic Seasonal Regression (≥5 points) ─────────────────────

# Model: y = c + a*t + b1*sin(2πt/12) + b2*cos(2πt/12)
# Unknowns: [c, a, b1, b2]  →  4 columns in the design matrix.

def _forecast_harmonic(history: List[dict], periods: int) -> ForecastResult:
    n = len(history)
    y = [h["balance"] for h in history]

    # Dynamic period: 12 if enough data, else half the training length (min 2)
    # Matches notebook: model_period = 12 if len(y_train) >= 12 else max(2, len(y_train) // 2)
    period = 12 if n >= 12 else max(2, n // 2)

    # Build design matrix [1, t, sin(2πt/period), cos(2πt/period)] for each observation
    A = []
    for i in range(n):
        t = float(i)
        A.append([1.0, t, math.sin(2 * math.pi * t / period), math.cos(2 * math.pi * t / period)])

    # Solve the normal equations: (A^T A) β = A^T y   via Gauss-Jordan
    ATA, ATy = _normal_equations(A, y)
    beta = _gauss_jordan(ATA, ATy)

    # Predictions over the training set
    y_hat = [_predict(beta, float(i), period) for i in range(n)]
    residuals = [yi - h for yi, h in zip(y, y_hat)]
    variance = sum(r * r for r in residuals) / max(n - 4, 1)
    std_err = math.sqrt(variance) if variance > 1e-10 else abs(y[-1]) * 0.05 or 1.0

    # Future projections
    fv, lo, hi = [], [], []
    for step in range(1, periods + 1):
        future_t = float(n - 1 + step)
        pred = _predict(beta, future_t, period)
        margin = Z95 * std_err * math.sqrt(step)
        fv.append(round(pred, 2))
        lo.append(round(pred - margin, 2))
        hi.append(round(pred + margin, 2))

    return ForecastResult(forecast_values=fv, lower_bound=lo, upper_bound=hi, method="harmonic_seasonal")


# ── Linear algebra helpers (pure Python) ────────────────────────────────────

def _normal_equations(A: List[List[float]], y: List[float]):
    """Compute A^T A and A^T y."""
    m = len(A)
    k = len(A[0])
    ATA = [[0.0] * k for _ in range(k)]
    ATy = [0.0] * k

    for i in range(m):
        for j in range(k):
            ATy[j] += A[i][j] * y[i]
            for jj in range(k):
                ATA[j][jj] += A[i][j] * A[i][jj]

    return ATA, ATy


def _gauss_jordan(A: List[List[float]], b: List[float]) -> List[float]:
    """Solve Ax = b via Gauss-Jordan elimination with partial pivoting.

    Returns the solution vector x.
    Raises ValueError if the matrix is singular.
    """
    n = len(A)
    # Augmented matrix
    aug = [row[:] + [b[i]] for i, row in enumerate(A)]

    for col in range(n):
        # Partial pivoting — find row with largest absolute value in this column
        max_row = col
        max_val = abs(aug[col][col])
        for row in range(col + 1, n):
            if abs(aug[row][col]) > max_val:
                max_row = row
                max_val = abs(aug[row][col])

        if max_val < 1e-12:
            raise ValueError("Singular or near-singular matrix")

        # Swap rows
        if max_row != col:
            aug[col], aug[max_row] = aug[max_row], aug[col]

        # Scale pivot row
        pivot = aug[col][col]
        for j in range(col, n + 1):
            aug[col][j] /= pivot

        # Eliminate all other rows
        for row in range(n):
            if row == col:
                continue
            factor = aug[row][col]
            for j in range(col, n + 1):
                aug[row][j] -= factor * aug[col][j]

    return [aug[i][n] for i in range(n)]


def _predict(beta: List[float], t: float, period: int = 12) -> float:
    """Evaluate the harmonic model: c + a*t + b1*sin(2πt/period) + b2*cos(2πt/period)."""
    return (
        beta[0]
        + beta[1] * t
        + beta[2] * math.sin(2 * math.pi * t / period)
        + beta[3] * math.cos(2 * math.pi * t / period)
    )
