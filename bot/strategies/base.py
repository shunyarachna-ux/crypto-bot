"""Strategy interface shared by backtest, paper and live modes.

A strategy turns 1h candles (plus 4h candles for the trend filter) into a
`Frame`: one row per 1h candle, using only information available at that
candle's close. The engine/bot acts on `signal` at the next candle.

Columns produced (besides open/high/low/close):
  atr, adx, ema20     helpers used by sizing and the exit rules
  trend4h             +1/-1/0 from the last COMPLETED 4h candle (no lookahead)
  swing_low4h/high4h  lowest low / highest high of the last 6 completed 4h candles
  state               +1/-1/0 desired direction while conditions hold
  signal              +1/-1 on the candle where `state` turns on, else 0
  kind                0 = trend trade (engine exit rules), 1 = mean-reversion
                      (fixed target + time stop)
  target              fixed take-profit price for kind == 1, else NaN
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..indicators import adx, atr, ema

H1_MS = 3_600_000
H4_MS = 4 * H1_MS
SWING_BARS = 6


class Strategy:
    name = "base"
    defaults: dict = {}

    def __init__(self, **params):
        unknown = set(params) - set(self.defaults)
        if unknown:
            raise ValueError(f"unknown params for {self.name}: {sorted(unknown)}")
        self.params = {**self.defaults, **params}

    def build(self, df1h: pd.DataFrame, df4h: pd.DataFrame) -> pd.DataFrame:
        raise NotImplementedError


def base_frame(df1h: pd.DataFrame, df4h: pd.DataFrame, atr_period: int = 14,
               trend_ema: int = 50) -> pd.DataFrame:
    """Common columns. 4h features are shifted to when the 4h candle completes."""
    f = df1h.set_index("ts")[["open", "high", "low", "close"]].copy()
    f["atr"] = atr(df1h, atr_period).values
    f["adx"] = adx(df1h, 14).values
    f["ema20"] = ema(df1h["close"], 20).values

    h4 = df4h.set_index("ts")
    t4 = pd.DataFrame(index=h4.index)
    e = ema(h4["close"], trend_ema)
    t4["trend4h"] = np.where(h4["close"] > e, 1, np.where(h4["close"] < e, -1, 0))
    t4.loc[e.isna(), "trend4h"] = 0
    t4["swing_low4h"] = h4["low"].rolling(SWING_BARS).min()
    t4["swing_high4h"] = h4["high"].rolling(SWING_BARS).max()
    # a 4h candle opened at T is complete at the close of the 1h candle opened at T+3h
    t4.index = t4.index + (H4_MS - H1_MS)
    t4 = t4.reindex(f.index.union(t4.index)).ffill().reindex(f.index)
    f["trend4h"] = t4["trend4h"].fillna(0).astype(int)
    f["swing_low4h"] = t4["swing_low4h"]
    f["swing_high4h"] = t4["swing_high4h"]
    f["state"] = 0
    f["signal"] = 0
    f["kind"] = 0
    f["target"] = np.nan
    return f


def signals_from_state(state: pd.Series) -> pd.Series:
    prev = state.shift(1).fillna(0)
    return pd.Series(np.where((state != 0) & (state != prev), state, 0), index=state.index)
