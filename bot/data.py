"""Download, store and resample candles and funding rates.

Candles are stored as CSV (open time in ms, UTC) under data_dir:
  <SYMBOL>_15m.csv, <SYMBOL>_funding.csv
1h and 4h candles are built from 15m so every timeframe agrees.

Research data may come from a larger exchange's public API; backtests still
apply the trading exchange's fees (see config.yaml `costs`).
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Callable

import pandas as pd

TF_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000,
         "4h": 14_400_000, "1d": 86_400_000}
OHLCV_COLS = ["ts", "open", "high", "low", "close", "volume"]
FUNDING_COLS = ["ts", "rate"]

# fetch(since_ms, limit) -> list of rows, ascending by ts
Fetcher = Callable[[int, int], list]


def safe_name(symbol: str) -> str:
    return symbol.replace("/", "").replace(":", "_")


def paginate(fetch: Fetcher, since_ms: int, until_ms: int, step_ms: int,
             limit: int = 1000, pause: float = 0.0, max_retries: int = 4) -> list:
    """Page through an exchange endpoint, retrying with backoff, until `until_ms`."""
    rows: list = []
    cursor = since_ms
    while cursor < until_ms:
        for attempt in range(max_retries + 1):
            try:
                batch = fetch(cursor, limit)
                break
            except Exception:
                if attempt == max_retries:
                    raise
                time.sleep(min(2 ** (attempt + 1), 16) if pause or attempt else 0)
        if not batch:
            break
        rows.extend(r for r in batch if r[0] < until_ms)
        last = batch[-1][0]
        if last + step_ms <= cursor:        # no progress; avoid infinite loop
            break
        cursor = last + step_ms
        if pause:
            time.sleep(pause)
    return rows


def to_frame(rows: list, cols: list[str]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=cols)
    df = df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    return df.astype({c: float for c in cols if c != "ts"}).astype({"ts": "int64"})


def check_candles(df: pd.DataFrame, step_ms: int) -> dict:
    """Return data-quality facts: gaps, duplicates, bad OHLC."""
    gaps = df["ts"].diff().dropna()
    bad = ((df["high"] < df[["open", "close", "low"]].max(axis=1))
           | (df["low"] > df[["open", "close", "high"]].min(axis=1))
           | (df[["open", "high", "low", "close"]] <= 0).any(axis=1))
    return {"rows": len(df), "gaps": int((gaps > step_ms).sum()),
            "duplicates": int(df["ts"].duplicated().sum()), "bad_ohlc": int(bad.sum())}


def resample(df15: pd.DataFrame, tf: str) -> pd.DataFrame:
    """Build tf candles from 15m. Incomplete trailing buckets are dropped."""
    step = TF_MS[tf]
    base = 900_000
    d = df15.copy()
    d["bucket"] = d["ts"] // step * step
    g = d.groupby("bucket")
    out = g.agg(open=("open", "first"), high=("high", "max"), low=("low", "min"),
                close=("close", "last"), volume=("volume", "sum"), n=("ts", "count"))
    out = out[out["n"] == step // base].drop(columns="n")
    out.index.name = "ts"
    return out.reset_index()


def with_time_index(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    d.index = pd.to_datetime(d["ts"], unit="ms", utc=True)
    return d


class DataStore:
    def __init__(self, data_dir: str | Path = "data"):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, symbol: str, kind: str) -> Path:
        return self.dir / f"{safe_name(symbol)}_{kind}.csv"

    def save(self, symbol: str, kind: str, df: pd.DataFrame) -> Path:
        p = self._path(symbol, kind)
        df.to_csv(p, index=False)
        return p

    def load(self, symbol: str, kind: str) -> pd.DataFrame:
        p = self._path(symbol, kind)
        if not p.exists():
            raise FileNotFoundError(f"{p} missing; run `python -m bot.data download`")
        return pd.read_csv(p)

    def load_tf(self, symbol: str, tf: str) -> pd.DataFrame:
        base = self.load(symbol, "15m")
        return base if tf == "15m" else resample(base, tf)

    def load_funding(self, symbol: str) -> pd.DataFrame | None:
        try:
            return self.load(symbol, "funding")
        except FileNotFoundError:
            return None

    def update_candles(self, symbol: str, fetch: Fetcher, since_ms: int,
                       until_ms: int, **kw) -> pd.DataFrame:
        """Append to existing data (resumes from the last stored candle)."""
        have = None
        try:
            have = self.load(symbol, "15m")
            since_ms = max(since_ms, int(have["ts"].iloc[-1]) + TF_MS["15m"])
        except FileNotFoundError:
            pass
        rows = paginate(fetch, since_ms, until_ms, TF_MS["15m"], **kw)
        new = to_frame(rows, OHLCV_COLS) if rows else pd.DataFrame(columns=OHLCV_COLS)
        df = pd.concat([have, new]) if have is not None else new
        df = to_frame(df.values.tolist(), OHLCV_COLS)
        self.save(symbol, "15m", df)
        return df

    def update_funding(self, symbol: str, fetch: Fetcher, since_ms: int,
                       until_ms: int, **kw) -> pd.DataFrame:
        rows = paginate(fetch, since_ms, until_ms, 1, **kw)
        df = to_frame(rows, FUNDING_COLS) if rows else pd.DataFrame(columns=FUNDING_COLS)
        self.save(symbol, "funding", df)
        return df


def ccxt_fetchers(exchange_id: str, symbol: str):
    """Build candle and funding fetchers from ccxt (public endpoints, no keys)."""
    import ccxt  # imported lazily so backtests on local data don't need it

    ex = getattr(ccxt, exchange_id)({"enableRateLimit": True,
                                     "options": {"defaultType": "swap"}})

    def candles(since, limit):
        return ex.fetch_ohlcv(symbol, "15m", since=since, limit=limit)

    def funding(since, limit):
        rows = ex.fetch_funding_rate_history(symbol, since=since, limit=limit)
        return [[r["timestamp"], r["fundingRate"]] for r in rows]

    return candles, funding


def main(argv=None) -> None:
    import argparse
    import datetime as dt

    from .config import load_config

    p = argparse.ArgumentParser(description="Download candles + funding")
    p.add_argument("action", choices=["download", "check"])
    p.add_argument("--years", type=float, default=3.0)
    p.add_argument("--exchange", default=None)
    args = p.parse_args(argv)
    cfg = load_config()
    store = DataStore(cfg["data_dir"])
    now = int(time.time() * 1000)
    since = now - int(args.years * 365 * 86_400_000)
    for sym in cfg["symbols"]:
        if args.action == "download":
            c, f = ccxt_fetchers(args.exchange or cfg["exchange"], sym)
            store.update_candles(sym, c, since, now, pause=0.2)
            try:
                store.update_funding(sym, f, since, now, pause=0.2)
            except Exception as e:  # funding history is optional
                print(f"{sym}: funding download failed ({e}); default rate will be used")
        df = store.load(sym, "15m")
        q = check_candles(df, TF_MS["15m"])
        a = dt.datetime.utcfromtimestamp(df.ts.iloc[0] / 1000)
        b = dt.datetime.utcfromtimestamp(df.ts.iloc[-1] / 1000)
        print(f"{sym}: {a:%Y-%m-%d} → {b:%Y-%m-%d} {q}")


if __name__ == "__main__":
    main()
