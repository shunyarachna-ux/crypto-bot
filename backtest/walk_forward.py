"""Phase 1 gate: walk-forward test of a strategy on unseen data.

    python -m backtest.walk_forward [--strategy trend_ema] [--synthetic]

Tune on the first 70% of history, judge once on the last 30%. Every variant run
is appended to reports/trials.csv (gate item 7). Writes reports/phase1_<strategy>.md.
"""
from __future__ import annotations

import argparse
import itertools
import json
import time
from dataclasses import asdict, replace
from pathlib import Path

import pandas as pd

from bot.config import ROOT, load_config
from bot.strategies import get_strategy

from .engine import Costs, ExitCfg, run_backtest
from .metrics import buy_and_hold_return, monte_carlo_drawdown, summarize
from .run_backtest import fmt, load_frames, setup_from_config

TRAIN_FRAC = 0.70
MIN_PF = 1.3
MIN_TEST_TRADES = 30
MAX_TRIALS = 100
MC_RUNS = 1000

GRIDS = {
    "trend_ema": {"ema_fast": [10, 20, 30], "ema_slow": [40, 50, 60], "trend_ema": [50]},
    "mean_reversion": {"bb_n": [15, 20, 25], "bb_k": [2.0, 2.5], "trend_ema": [50]},
    "regime": {"trend_adx": [25.0], "range_adx": [20.0]},
}


class TrialLog:
    """Append-only record of every variant tested (so over-fitting is visible)."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else ROOT / "reports" / "trials.csv"
        self.run_id = time.strftime("%Y%m%dT%H%M%S")
        self.count = 0

    def add(self, symbol, strategy, params, exit_cfg, period, summary, synthetic=False):
        row = {"run_id": self.run_id, "symbol": symbol, "strategy": strategy,
               "params": json.dumps(params, sort_keys=True), "exit_mode": exit_cfg.mode,
               "exit": json.dumps(asdict(exit_cfg), sort_keys=True), "period": period,
               "synthetic": synthetic,
               **{k: summary[k] for k in ("trades", "net_pnl", "profit_factor", "pf_pretax",
                                          "win_rate", "max_drawdown")}}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame([row]).to_csv(self.path, mode="a", header=not self.path.exists(), index=False)
        self.count += 1


def split_frame(frame: pd.DataFrame, frac: float = TRAIN_FRAC):
    k = int(len(frame) * frac)
    return frame.iloc[:k], frame.iloc[k:]


def slice_funding(fund, frame):
    if fund is None:
        return None
    lo, hi = int(frame.index[0]), int(frame.index[-1]) + 3_600_000
    return fund[(fund["ts"] >= lo) & (fund["ts"] < hi)]


def evaluate(frame, fund, costs, limits, exit_cfg):
    res = run_backtest(frame, slice_funding(fund, frame), costs=costs, limits=limits, exit_cfg=exit_cfg)
    return res, summarize(res)


def neighbours(params: dict, grid: dict) -> list[dict]:
    """Each numeric parameter nudged ±15% (rounded), one at a time."""
    out = []
    for k, v in params.items():
        if k not in grid or len(grid[k]) < 1 or not isinstance(v, (int, float)):
            continue
        for m in (0.85, 1.15):
            nv = round(v * m) if isinstance(v, int) else round(v * m, 2)
            if nv != v and nv > 0:
                out.append({**params, k: nv})
    return out


def score(s: dict) -> float:
    if s["trades"] < 20:
        return -1e9
    return s["profit_factor"] if s["profit_factor"] != float("inf") else 10.0


def run_phase1(strategy: str, cfg: dict, frames_by_symbol: dict, log: TrialLog,
               synthetic: bool = False, mc_runs: int = MC_RUNS, grid: dict | None = None) -> dict:
    costs, limits, exit_cfg = setup_from_config(cfg)
    if strategy == "mean_reversion":
        exit_cfg = replace(exit_cfg, mode="fixed")      # engine uses the strategy's own target
    grid = grid or GRIDS[strategy]
    syms = list(frames_by_symbol)
    primary, others = syms[0], syms[1:]

    def build(sym, params):
        h1, h4, _ = frames_by_symbol[sym]
        return get_strategy(strategy, **params).build(h1, h4)

    fund = {s: frames_by_symbol[s][2] for s in syms}

    # ---- 1. tune on the first 70% of the primary symbol ----
    keys = list(grid)
    best, best_s = None, -1e18
    for combo in itertools.product(*[grid[k] for k in keys]):
        params = dict(zip(keys, combo))
        if strategy == "trend_ema" and params["ema_fast"] >= params["ema_slow"]:
            continue
        train, _ = split_frame(build(primary, params))
        _, s = evaluate(train, fund[primary], costs, limits, exit_cfg)
        log.add(primary, strategy, params, exit_cfg, "train", s, synthetic)
        if score(s) > best_s:
            best, best_s = params, score(s)
    if best is None:
        best = {}

    # ---- 2. judge ONCE on the last 30% ----
    frame = build(primary, best)
    train_f, test_f = split_frame(frame)
    res, test = evaluate(test_f, fund[primary], costs, limits, exit_cfg)
    log.add(primary, strategy, best, exit_cfg, "test", test, synthetic)
    bh = buy_and_hold_return(test_f)
    mc_dd = monte_carlo_drawdown(res.trades, res.start_equity, mc_runs) if len(res.trades) else 0.0

    # ---- 3. stability: nearby settings should also make money on the test period ----
    nb = []
    for p in neighbours(best, grid):
        _, s = evaluate(split_frame(build(primary, p))[1], fund[primary], costs, limits, exit_cfg)
        log.add(primary, strategy, p, exit_cfg, "test_neighbour", s, synthetic)
        nb.append({"params": p, "net_pnl": s["net_pnl"], "profit_factor": s["profit_factor"], "trades": s["trades"]})

    # ---- 4. other symbols, same settings, no retuning ----
    other = {}
    for sym in others:
        _, t = split_frame(build(sym, best))
        _, s = evaluate(t, fund[sym], costs, limits, exit_cfg)
        log.add(sym, strategy, best, exit_cfg, "test_other_symbol", s, synthetic)
        other[sym] = {**s, "buy_hold": buy_and_hold_return(t)}

    checks = {
        "1_unseen_data": True,  # tuned only on train, judged on test (by construction)
        "2_profit_factor": test["profit_factor"] >= MIN_PF and test["trades"] >= MIN_TEST_TRADES,
        "3_stable_settings": bool(nb) and all(x["net_pnl"] > 0 for x in nb),
        "4_other_symbol": bool(other) and all(o["profit_factor"] >= MIN_PF for o in other.values()),
        "5_beats_buy_hold": test["return_pct"] / 100 > bh,
        "6_realistic_fills": True,  # engine: limits fill only if traded through; stop-first on ties
        "7_few_attempts": log.count <= MAX_TRIALS,
        "8_monte_carlo": len(res.trades) >= 2 and mc_dd < limits.max_drawdown,
    }
    return {"strategy": strategy, "synthetic": synthetic, "primary": primary, "best_params": best,
            "train_score_pf": best_s, "test": test, "buy_hold": bh, "mc_dd_95": mc_dd,
            "neighbours": nb, "other": other, "checks": checks, "passed": all(checks.values()),
            "trials_logged": log.count, "exit_mode": exit_cfg.mode}


def compare_exits(strategy: str, params: dict, cfg: dict, frame, fund, log: TrialLog,
                  symbol: str, synthetic=False) -> list[dict]:
    """Four exits (+ breakeven variants) on the unseen 30%, per the plan."""
    costs, limits, base = setup_from_config(cfg)
    _, test_f = split_frame(frame)
    variants = [("fixed 3x ATR target", replace(base, mode="fixed")),
                ("simple 2x ATR trail", replace(base, mode="trail2")),
                ("smart trail", replace(base, mode="smart")),
                ("smart trail, no half exit", replace(base, mode="smart_no_half")),
                ("smart, breakeven +1R", replace(base, mode="smart", be_at_r=1.0)),
                ("smart, breakeven never", replace(base, mode="smart", be_at_r=None))]
    rows = []
    for name, ex in variants:
        _, s = evaluate(test_f, fund, costs, limits, ex)
        log.add(symbol, strategy, params, ex, "test_exit_compare", s, synthetic)
        rows.append({"exit": name, **s})
    return rows


def render_report(r: dict, exits: list[dict] | None) -> str:
    t = r["test"]
    L = ["# Phase 1 gate: " + r["strategy"], ""]
    if r["synthetic"]:
        L += ["> **SYNTHETIC DATA. Plumbing check only. These numbers say nothing about real markets.**", ""]
    L += [f"Symbol: {r['primary']} · tuned on first 70%, judged on last 30% · exit mode: {r['exit_mode']}",
          f"Chosen settings: `{json.dumps(r['best_params'])}` · variants logged: {r['trials_logged']}", "",
          "## Result: " + ("PASS" if r["passed"] else "FAIL"), "",
          "| Check | Result |", "| --- | --- |"]
    for k, v in r["checks"].items():
        L.append(f"| {k} | {'pass' if v else 'FAIL'} |")
    L += ["", "## Unseen-period numbers (after fees, GST, slippage, funding, 30% tax)", "",
          f"- {fmt(t)}", f"- Buy & hold, no leverage: {100 * r['buy_hold']:+.1f}%",
          f"- Monte Carlo 95th-percentile max drawdown: {r['mc_dd_95']:.1%} (kill switch 15%)", ""]
    if r["neighbours"]:
        L += ["## Nearby settings (should also make money)", ""]
        L += [f"- `{json.dumps(n['params'])}`: net {n['net_pnl']:+.0f}, PF {n['profit_factor']:.2f}, {n['trades']} trades"
              for n in r["neighbours"]]
        L.append("")
    if r["other"]:
        L += ["## Other symbols, same settings", ""]
        L += [f"- {s}: {fmt(o)}; buy & hold {100 * o['buy_hold']:+.1f}%" for s, o in r["other"].items()]
        L.append("")
    if exits:
        L += ["## Exit comparison on unseen data", "",
              "| Exit | Trades | Net | PF | +4R reached | kept (≥ +2R) | Capture |", "| --- | --- | --- | --- | --- | --- | --- |"]
        for e in exits:
            L.append(f"| {e['exit']} | {e['trades']} | {e['net_pnl']:+.0f} | {e['profit_factor']:.2f} | "
                     f"{e.get('big_winners', 0)} | {e.get('big_winners_kept', 0)} | {e['capture']:.0%} |")
        L.append("")
    if not r["passed"]:
        L += ["If this strategy fails the gate, that is a useful answer. Do not keep tweaking settings "
              "until the backtest looks good; try a genuinely different idea or stop.", ""]
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", default=None, choices=list(GRIDS))
    ap.add_argument("--synthetic", action="store_true")
    ap.add_argument("--no-exit-compare", action="store_true")
    a = ap.parse_args(argv)
    cfg = load_config()
    strategy = a.strategy or cfg["strategy"]["name"]
    frames = {s: load_frames(cfg, s, a.synthetic, seed=i) for i, s in enumerate(cfg["symbols"])}
    log = TrialLog()
    r = run_phase1(strategy, cfg, frames, log, a.synthetic)
    exits = None
    if not a.no_exit_compare and strategy == "trend_ema":
        h1, h4, fund = frames[r["primary"]]
        frame = get_strategy(strategy, **r["best_params"]).build(h1, h4)
        exits = compare_exits(strategy, r["best_params"], cfg, frame, fund, log, r["primary"], a.synthetic)
        r["trials_logged"] = log.count
    md = render_report(r, exits)
    out = ROOT / "reports" / f"phase1_{strategy}{'_synthetic' if a.synthetic else ''}.md"
    out.write_text(md)
    print(md)
    print(f"\nwritten: {out}")


if __name__ == "__main__":
    main()
