"""Run one backtest:  python -m backtest.run_backtest [--symbol BTC/USDT:USDT] [--synthetic]

--synthetic uses random data to check the plumbing. Its results mean nothing.
"""
from __future__ import annotations

import argparse
from dataclasses import replace

import pandas as pd

from bot.config import load_config
from bot.data import DataStore
from bot.risk import RiskLimits
from bot.strategies import get_strategy

from .engine import Costs, ExitCfg, run_backtest
from .metrics import buy_and_hold_return, summarize
from .synthetic import synthetic_frames


def setup_from_config(cfg: dict):
    s = cfg["strategy"]
    exit_cfg = ExitCfg(mode=s["exit_mode"], stop_atr=s["stop_atr"], target_atr=s["target_atr"],
                       entry_order=s["entry_order"])
    return Costs.from_config(cfg), RiskLimits.from_config(cfg), exit_cfg


def load_frames(cfg: dict, symbol: str, synthetic: bool = False, seed: int = 0):
    """Return (1h candles, 4h candles, funding or None)."""
    if synthetic:
        h1, h4 = synthetic_frames(n_days=3 * 365, seed=seed)
        return h1, h4, None
    store = DataStore(cfg["data_dir"])
    return store.load_tf(symbol, "1h"), store.load_tf(symbol, "4h"), store.load_funding(symbol)


def fmt(s: dict) -> str:
    return (f"trades={s['trades']} net={s['net_pnl']:+.0f} ({s['return_pct']:+.1f}%) "
            f"PF={s['profit_factor']:.2f} (pre-tax {s['pf_pretax']:.2f}) win={s['win_rate']:.0%} "
            f"maxDD={s['max_drawdown']:.1%} avgR={s['avg_r']:+.2f} fees={s['fees']:.0f} "
            f"tax={s['tax']:.0f} funding={s['funding']:+.0f}")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default=None)
    ap.add_argument("--strategy", default=None)
    ap.add_argument("--exit-mode", default=None)
    ap.add_argument("--synthetic", action="store_true")
    a = ap.parse_args(argv)
    cfg = load_config()
    symbol = a.symbol or cfg["symbols"][0]
    if a.synthetic:
        print("!! SYNTHETIC DATA — plumbing check only, results are meaningless !!")
    costs, limits, exit_cfg = setup_from_config(cfg)
    if a.exit_mode:
        exit_cfg = replace(exit_cfg, mode=a.exit_mode)
    h1, h4, fund = load_frames(cfg, symbol, a.synthetic)
    name = a.strategy or cfg["strategy"]["name"]
    frame = get_strategy(name, **(cfg["strategy"].get("params") or {}) if name == cfg["strategy"]["name"] else {}).build(h1, h4)
    res = run_backtest(frame, fund, costs=costs, limits=limits, exit_cfg=exit_cfg)
    s = summarize(res)
    print(f"{symbol} {name} exit={exit_cfg.mode}\n  {fmt(s)}")
    print(f"  buy&hold (no leverage): {100 * buy_and_hold_return(frame):+.1f}%  skipped={res.skipped}")


if __name__ == "__main__":
    main()
