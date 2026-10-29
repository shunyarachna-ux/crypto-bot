from __future__ import annotations

import numpy as np
import pandas as pd


def max_drawdown(equity: pd.Series | np.ndarray) -> float:
    e = np.asarray(equity, dtype=float)
    if len(e) == 0:
        return 0.0
    peak = np.maximum.accumulate(e)
    return float(np.max(1 - e / peak))


def profit_factor(net: pd.Series) -> float:
    wins, losses = net[net > 0].sum(), -net[net < 0].sum()
    if losses == 0:
        return float("inf") if wins > 0 else 0.0
    return float(wins / losses)


def summarize(res, big_r: float = 4.0) -> dict:
    t = res.trades
    out = {"trades": len(t), "start_equity": res.start_equity,
           "end_equity": float(res.equity.iloc[-1]) if len(res.equity) else res.start_equity,
           "max_drawdown": max_drawdown(res.equity), "killed": res.killed_at is not None}
    out["net_pnl"] = out["end_equity"] - res.start_equity
    out["return_pct"] = 100 * out["net_pnl"] / res.start_equity
    if len(t) == 0:
        out.update(win_rate=0.0, profit_factor=0.0, pf_pretax=0.0, avg_r=0.0,
                   big_winners=0, capture=0.0, fees=0.0, tax=0.0, funding=0.0)
        return out
    out["win_rate"] = float((t["net"] > 0).mean())
    out["profit_factor"] = profit_factor(t["net"])           # after fees, GST, slippage, funding AND tax
    out["pf_pretax"] = profit_factor(t["pretax"])
    out["avg_r"] = float(t["r_multiple"].mean())
    out["big_winners"] = int((t["mfe_r"] >= big_r).sum())    # trades whose best excursion reached +4R
    out["big_winners_kept"] = int(((t["mfe_r"] >= big_r) & (t["r_multiple"] >= 2.0)).sum())
    mfe = t.loc[t["mfe_dollars"] > 0, ["gross", "mfe_dollars"]]
    out["capture"] = float(mfe["gross"].sum() / mfe["mfe_dollars"].sum()) if len(mfe) else 0.0
    out["fees"], out["tax"], out["funding"] = (float(t[k].sum()) for k in ("fees", "tax", "funding"))
    return out


def buy_and_hold_return(frame: pd.DataFrame) -> float:
    """Unleveraged hold over the same period, as a fraction."""
    c = frame["close"].dropna()
    return float(c.iloc[-1] / c.iloc[0] - 1) if len(c) > 1 else 0.0


def monte_carlo_drawdown(trades: pd.DataFrame, start_equity: float, runs: int = 1000,
                         seed: int = 0, pct: float = 95.0) -> float:
    """Shuffle trade order; return the `pct`-th percentile max drawdown."""
    if len(trades) < 2:
        return 0.0
    net = trades["net"].values
    before = start_equity + np.concatenate([[0.0], np.cumsum(net)[:-1]])
    rets = net / before                       # per-trade return on account
    rng = np.random.default_rng(seed)
    dds = np.empty(runs)
    for k in range(runs):
        path = start_equity * np.cumprod(1 + rng.permutation(rets))
        dds[k] = max_drawdown(np.concatenate([[start_equity], path]))
    return float(np.percentile(dds, pct))
