import pandas as pd
import pytest

from backtest.engine import Costs, ExitCfg, run_backtest
from backtest.run_backtest import setup_from_config
from backtest.synthetic import synthetic_frames
from backtest.walk_forward import (TrialLog, neighbours, render_report,
                                   run_phase1, split_frame)
from bot.config import load_config
from bot.strategies import get_strategy

CFG = load_config()
FRAMES = {s: (*synthetic_frames(n_days=250, seed=i), None) for i, s in enumerate(CFG["symbols"])}
SMALL = {"trend_ema": {"ema_fast": [10, 20], "ema_slow": [50], "trend_ema": [50]}}


def test_split_has_no_overlap():
    f = get_strategy("trend_ema").build(FRAMES[CFG["symbols"][0]][0], FRAMES[CFG["symbols"][0]][1])
    a, b = split_frame(f)
    assert len(a) + len(b) == len(f) and a.index[-1] < b.index[0]
    assert len(a) == int(len(f) * 0.7)


def test_neighbours_nudge_one_param_at_a_time():
    grid = {"a": [1], "b": [1]}
    nb = neighbours({"a": 20, "b": 2.0, "c": 5}, grid)
    assert {tuple(sorted(k for k in n if n[k] != {"a": 20, "b": 2.0, "c": 5}[k])) for n in nb} <= {("a",), ("b",)}
    assert {"a": 17, "b": 2.0, "c": 5} in nb and {"a": 23, "b": 2.0, "c": 5} in nb


@pytest.mark.parametrize("strategy", ["trend_ema", "mean_reversion", "regime"])
def test_phase1_runs_logs_and_rejects_noise(tmp_path, strategy):
    log = TrialLog(tmp_path / "trials.csv")
    grid = SMALL.get(strategy) or {"bb_n": [20], "bb_k": [2.0], "trend_ema": [50]} if strategy != "regime" else {"trend_adx": [25.0], "range_adx": [20.0]}
    r = run_phase1(strategy, CFG, FRAMES, log, synthetic=True, mc_runs=50, grid=grid)
    assert r["passed"] is False                      # random data must not pass the gate
    assert set(r["checks"]) == {"1_unseen_data", "2_profit_factor", "3_stable_settings",
                                "4_other_symbol", "5_beats_buy_hold", "6_realistic_fills",
                                "7_few_attempts", "8_monte_carlo"}
    df = pd.read_csv(tmp_path / "trials.csv")
    assert len(df) == log.count == r["trials_logged"] and df.synthetic.all()
    assert {"train", "test"} <= set(df.period)
    assert "SYNTHETIC" in render_report(r, None)


def test_pipeline_has_no_lookahead():
    """Trades that finished well before a cut-off are identical if later data is removed."""
    h1, h4, _ = FRAMES[CFG["symbols"][0]]
    costs, limits, ex = setup_from_config(CFG)
    ex = ExitCfg(**{**ex.__dict__, "mode": "smart"})
    full = get_strategy("trend_ema").build(h1, h4)
    k = int(len(full) * 0.6)
    cut_h1, cut_h4 = h1.iloc[:k], h4[h4.ts + 4 * 3_600_000 <= h1.ts.iloc[k - 1] + 3_600_000]
    part = get_strategy("trend_ema").build(cut_h1, cut_h4)
    a = run_backtest(full, None, costs=costs, limits=limits, exit_cfg=ex).trades
    b = run_backtest(part, None, costs=costs, limits=limits, exit_cfg=ex).trades
    horizon = int(full.index[k - 1]) - 200 * 3_600_000
    a, b = a[a.exit_ts < horizon].reset_index(drop=True), b[b.exit_ts < horizon].reset_index(drop=True)
    assert len(a) > 3
    pd.testing.assert_frame_equal(a, b)


def test_mean_reversion_end_to_end_trades():
    h1, h4, _ = FRAMES[CFG["symbols"][0]]
    costs, limits, ex = setup_from_config(CFG)
    f = get_strategy("mean_reversion").build(h1, h4)
    r = run_backtest(f, None, costs=costs, limits=limits, exit_cfg=ExitCfg(mode="fixed"))
    assert len(r.trades) > 5
    assert set(r.trades.reason) <= {"target", "stop", "stop_gap", "time_stop", "risk_halt", "end_of_data"}
