from __future__ import annotations

import numpy as np

from ..indicators import bollinger
from .base import Strategy, base_frame, signals_from_state


class MeanReversion(Strategy):
    """Fade closes outside the Bollinger band, only in the 4h trend direction.

    Exit: fixed target at the band midpoint (set at signal time), stop and time
    stop handled by the engine. No trailing.
    """
    name = "mean_reversion"
    defaults = {"bb_n": 20, "bb_k": 2.0, "trend_ema": 50, "atr_period": 14}

    def build(self, df1h, df4h):
        p = self.params
        f = base_frame(df1h, df4h, p["atr_period"], p["trend_ema"])
        mid, up, lo = bollinger(df1h["close"], p["bb_n"], p["bb_k"])
        c = df1h["close"].values
        raw = np.where(c < lo.values, 1, np.where(c > up.values, -1, 0))
        f["state"] = np.where(raw == f["trend4h"].values, raw, 0)
        f["signal"] = signals_from_state(f["state"]).astype(int)
        f["kind"] = 1
        f["target"] = np.where(f["signal"] != 0, mid.values, np.nan)
        return f
