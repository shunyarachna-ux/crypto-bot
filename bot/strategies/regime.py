from __future__ import annotations

import numpy as np

from .base import Strategy
from .mean_reversion import MeanReversion
from .trend_ema import TrendEMA


class Regime(Strategy):
    """ADX switch: ADX > trend_adx → trend, ADX < range_adx → mean reversion, else flat."""
    name = "regime"
    defaults = {"trend_adx": 25.0, "range_adx": 20.0, "trend": {}, "mr": {}}

    def build(self, df1h, df4h):
        p = self.params
        t = TrendEMA(**p["trend"]).build(df1h, df4h)
        m = MeanReversion(**p["mr"]).build(df1h, df4h)
        adx = t["adx"].values
        use_t = adx > p["trend_adx"]
        use_m = adx < p["range_adx"]
        f = t.copy()
        f["state"] = np.where(use_t, t["state"], np.where(use_m, m["state"], 0))
        f["signal"] = np.where(use_t, t["signal"], np.where(use_m, m["signal"], 0)).astype(int)
        f["kind"] = np.where(use_m, 1, 0)
        f["target"] = np.where(use_m & (m["signal"] != 0), m["target"], np.nan)
        return f


REGISTRY = {"trend_ema": TrendEMA, "mean_reversion": MeanReversion, "regime": Regime}


def get_strategy(name: str, **params) -> Strategy:
    return REGISTRY[name](**params)
