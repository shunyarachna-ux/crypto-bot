from __future__ import annotations

import numpy as np

from .base import Strategy, base_frame, signals_from_state


class TrendEMA(Strategy):
    """EMA-cross trend following, only in the direction of the 4h trend."""
    name = "trend_ema"
    defaults = {"ema_fast": 20, "ema_slow": 50, "trend_ema": 50, "atr_period": 14}

    def build(self, df1h, df4h):
        p = self.params
        f = base_frame(df1h, df4h, p["atr_period"], p["trend_ema"])
        from ..indicators import ema
        fast = ema(df1h["close"], p["ema_fast"]).values
        slow = ema(df1h["close"], p["ema_slow"]).values
        ok = ~(np.isnan(fast) | np.isnan(slow))
        cross = np.where(ok, np.sign(fast - slow), 0).astype(int)
        f["state"] = np.where(cross == f["trend4h"].values, cross, 0)
        f["signal"] = signals_from_state(f["state"]).astype(int)
        return f
