"""Synthetic 15m candles for tests and plumbing checks. NOT market data:
results on this data say nothing about whether a strategy works."""
from __future__ import annotations

import numpy as np
import pandas as pd

from bot.data import TF_MS, resample


def synthetic_15m(n_days: int = 365, seed: int = 0, start_price: float = 30_000.0,
                  start_ts: int = 1_600_000_000_000 // 900_000 * 900_000,
                  drift_regimes: bool = True) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    n = n_days * 96
    vol = 0.0022                                  # ~15m volatility
    drift = np.zeros(n)
    if drift_regimes:                             # alternate trends and chop
        i = 0
        while i < n:
            ln = int(rng.integers(300, 2500))
            mu = rng.choice([-1, 0, 0, 1, 1]) * 0.00004 * rng.random()
            drift[i:i + ln] = mu
            i += ln
    r = drift + vol * rng.standard_normal(n)
    close = start_price * np.exp(np.cumsum(r))
    open_ = np.concatenate([[start_price], close[:-1]])
    wick = np.abs(rng.standard_normal((n, 2))) * vol * 0.6
    high = np.maximum(open_, close) * (1 + wick[:, 0])
    low = np.minimum(open_, close) * (1 - wick[:, 1])
    ts = start_ts + np.arange(n) * TF_MS["15m"]
    return pd.DataFrame({"ts": ts, "open": open_, "high": high, "low": low,
                         "close": close, "volume": rng.random(n) * 100})


def synthetic_frames(**kw):
    d15 = synthetic_15m(**kw)
    return resample(d15, "1h"), resample(d15, "4h")
