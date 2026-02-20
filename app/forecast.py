from dataclasses import dataclass
from datetime import date

import numpy as np


@dataclass
class ForecastResult:
    growth_gb_per_day: float | None
    days_to_full: float | None


def forecast_linear(dates: list[date], used_gb: list[float | None], capacity_gb: float | None) -> ForecastResult:
    points = [(d, u) for d, u in zip(dates, used_gb) if u is not None]
    if len(points) < 2 or capacity_gb is None:
        return ForecastResult(None, None)

    d0 = points[0][0]
    x = np.array([(d - d0).days for d, _ in points], dtype=float)
    y = np.array([u for _, u in points], dtype=float)

    if len(np.unique(x)) < 2:
        return ForecastResult(None, None)

    a, b = np.polyfit(x, y, 1)
    growth = float(a)
    current_used = float(points[-1][1])

    if growth <= 0:
        return ForecastResult(growth, None)

    remaining = capacity_gb - current_used
    if remaining <= 0:
        return ForecastResult(growth, 0.0)

    return ForecastResult(growth, float(remaining / growth))
