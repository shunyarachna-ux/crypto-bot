"""Exchange access. Public market data works without keys (paper mode).

Order placement lives in executor.LiveExecutor and is intentionally NOT
implemented here: bracket-order endpoints differ per exchange and must be
written and tested on that exchange's testnet before any live use.
"""
from __future__ import annotations

import pandas as pd

from .data import OHLCV_COLS, TF_MS

TF_SECONDS = {k: v // 1000 for k, v in TF_MS.items()}


class Exchange:
    def __init__(self, exchange_id: str, api_key: str | None = None, secret: str | None = None):
        import ccxt
        params = {"enableRateLimit": True, "options": {"defaultType": "swap"}}
        if api_key and secret:
            params.update(apiKey=api_key, secret=secret)
        self.ex = getattr(ccxt, exchange_id)(params)

    def candles(self, symbol: str, tf: str, limit: int = 500) -> pd.DataFrame:
        rows = self.ex.fetch_ohlcv(symbol, tf, limit=limit)
        df = pd.DataFrame(rows, columns=OHLCV_COLS).drop_duplicates("ts").sort_values("ts")
        return df.reset_index(drop=True).astype({"ts": "int64"})

    def min_order(self, symbol: str) -> tuple[float, float | None]:
        """(minimum quantity, quantity step) from market metadata."""
        m = self.ex.market(symbol)
        return (m.get("limits", {}).get("amount", {}).get("min") or 0.0,
                m.get("precision", {}).get("amount"))


def closed_only(df: pd.DataFrame, tf: str, now_s: float) -> pd.DataFrame:
    """Drop the still-forming last candle."""
    return df[df["ts"] + TF_MS[tf] <= now_s * 1000].reset_index(drop=True)
