import numpy as np
import pandas as pd
import pytest

from backtest.engine import Costs, ExitCfg, avoid_levels, run_backtest
from backtest.metrics import (buy_and_hold_return, max_drawdown,
                              monte_carlo_drawdown, profit_factor, summarize)
from bot.risk import RiskLimits

H = 3_600_000
T0 = 1_700_000_000_000 // H * H
ZERO = Costs(maker_fee=0, taker_fee=0, gst_on_fees=0, slippage=0, tax_on_wins=0, default_funding_8h=0)


def frame(bars, signal_at=None, atr=2.0, trend=1, state_from=None, kind=0, target=None,
          adx=30.0, ema20=None, swl=None):
    """Hand-made 1h frame. bars = [(o,h,l,c), ...]. First 3 bars are warm-up filler."""
    n = len(bars)
    f = pd.DataFrame(bars, columns=["open", "high", "low", "close"], index=T0 + np.arange(n) * H)
    f["atr"], f["adx"] = atr, adx
    f["ema20"] = f["close"] if ema20 is None else ema20
    f["trend4h"] = trend
    f["swing_low4h"] = f["low"].min() - 50 if swl is None else swl
    f["swing_high4h"] = f["high"].max() + 50
    f["signal"] = 0
    f["state"] = 0
    f["kind"] = kind
    f["target"] = np.nan
    for i, side in (signal_at or {}).items():
        f.iloc[i, f.columns.get_loc("signal")] = side
        f.iloc[i:, f.columns.get_loc("state")] = side if state_from is None else 0
        if target is not None:
            f.iloc[i, f.columns.get_loc("target")] = target
    return f


def flat(n, p=100.0):
    return [(p, p, p, p)] * n


MKT = dict(entry_order="market", level_buffer_atr=0.0)


def run(f, exit_cfg=None, costs=ZERO, **kw):
    return run_backtest(f, None, costs=costs, exit_cfg=exit_cfg or ExitCfg(mode="fixed", **MKT), **kw)


def test_fixed_target_hit_exact_pnl():
    # signal at bar 3 → market entry bar 4 @100; ATR 2 → stop 97, target 106, qty = 100/3
    bars = flat(4) + [(100, 101, 99, 100), (100, 107, 100, 106)] + flat(3, 106)
    r = run(frame(bars, {3: 1}))
    t = r.trades.iloc[0]
    assert len(r.trades) == 1 and t.reason == "target"
    assert t.qty == pytest.approx(100 / 3)
    assert t.net == pytest.approx(6 * 100 / 3)          # +2R = +$200, no costs
    assert t.r_multiple == pytest.approx(2.0)
    assert r.equity.iloc[-1] == pytest.approx(10_200)


def test_stop_hit_loses_one_percent():
    bars = flat(4) + [(100, 101, 99, 100), (100, 100, 96, 97)] + flat(3, 97)
    r = run(frame(bars, {3: 1}))
    t = r.trades.iloc[0]
    assert t.reason == "stop" and t.net == pytest.approx(-100.0)
    assert t.r_multiple == pytest.approx(-1.0)


def test_stop_wins_when_both_hit_same_candle():
    bars = flat(4) + [(100, 101, 99, 100), (100, 110, 90, 100)] + flat(3)
    r = run(frame(bars, {3: 1}))
    assert r.trades.iloc[0].reason == "stop"


def test_gap_through_stop_fills_at_open():
    bars = flat(4) + [(100, 101, 99, 100), (90, 91, 89, 90)] + flat(3, 90)
    t = run(frame(bars, {3: 1})).trades.iloc[0]
    assert t.reason == "stop_gap" and t.exit == 90 and t.net < -100


def test_short_side_mirror():
    bars = flat(4) + [(100, 101, 99, 100), (100, 100, 93, 94)] + flat(3, 94)
    t = run(frame(bars, {3: -1}, trend=-1)).trades.iloc[0]
    assert t.reason == "target" and t.net == pytest.approx(200.0)


def test_costs_fees_gst_slippage_and_tax():
    c = Costs(maker_fee=0.0002, taker_fee=0.0005, gst_on_fees=0.18, slippage=0.001, tax_on_wins=0.30)
    bars = flat(4) + [(100, 101, 99, 100), (100, 107, 100, 106)] + flat(3, 106)
    t = run(frame(bars, {3: 1}), costs=c).trades.iloc[0]
    entry = 100 * 1.001                                 # market buy pays slippage
    stop = entry - 3                                    # stop uses the actual fill price
    qty = 100 / (entry - stop)
    exp_fee = (qty * entry * 0.0005 + qty * (entry + 6) * 0.0002) * 1.18
    gross = (entry + 6 - entry) * qty
    assert t.entry == pytest.approx(entry) and t.fees == pytest.approx(exp_fee)
    assert t.pretax == pytest.approx(gross - exp_fee)
    assert t.tax == pytest.approx(0.30 * (gross - exp_fee))
    assert t.net == pytest.approx(0.70 * (gross - exp_fee))


def test_no_tax_relief_on_losses():
    c = Costs(maker_fee=0, taker_fee=0, gst_on_fees=0, slippage=0, tax_on_wins=0.30)
    win = [(100, 101, 99, 100), (100, 107, 100, 106)] + flat(2, 106)
    lose = [(106, 106, 106, 106), (106, 106, 102, 103)] + flat(2, 103)
    bars = flat(4) + win + lose
    f = frame(bars, {3: 1, 8: 1})
    r = run(f, costs=c, enable_halts=False)
    t = r.trades
    assert len(t) == 2 and t.tax.iloc[1] == 0 and t.tax.iloc[0] > 0
    assert t.net.sum() == pytest.approx(t.pretax.sum() - t.tax.sum())


def test_funding_is_paid_by_longs_and_received_by_shorts():
    f = frame(flat(4) + [(100, 101, 99, 100)] * 30, {3: 1})
    fund = pd.DataFrame({"ts": [int(f.index[10]), int(f.index[20])], "rate": [0.001, 0.001]})
    cfg = ExitCfg(mode="fixed", target_atr=50, **MKT)
    long_t = run_backtest(f, fund, costs=ZERO, exit_cfg=cfg, enable_halts=False).trades.iloc[0]
    assert long_t.funding == pytest.approx(2 * 0.001 * long_t.qty * 100)
    assert long_t.pretax < 0
    fs = frame(flat(4) + [(100, 101, 99, 100)] * 30, {3: -1}, trend=-1)
    short_t = run_backtest(fs, fund, costs=ZERO, exit_cfg=cfg, enable_halts=False).trades.iloc[0]
    assert short_t.funding == pytest.approx(-long_t.funding)


def test_limit_entry_needs_price_to_trade_through():
    lim = ExitCfg(mode="fixed", entry_order="limit_fallback", level_buffer_atr=0.0, target_atr=50)
    base = flat(4)
    touch = base + [(101, 102, 100, 101)] * 2 + [(101, 102, 101, 101)] * 4   # low == limit: no fill
    # state is "on" during the wait → after 2 bars the fallback market entry happens at open of bar 6
    t = run(frame(touch, {3: 1}), lim, enable_halts=False).trades.iloc[0]
    assert t.entry == pytest.approx(101 * 1.0) and t.entry_ts == T0 + 6 * H
    through = base + [(101, 102, 99.5, 101)] + flat(4, 101)
    t2 = run(frame(through, {3: 1}), lim, enable_halts=False).trades.iloc[0]
    assert t2.entry == 100 and t2.entry_ts == T0 + 4 * H


def test_limit_fallback_skipped_when_signal_no_longer_valid():
    lim = ExitCfg(mode="fixed", entry_order="limit_fallback", level_buffer_atr=0.0)
    bars = flat(4) + [(101, 102, 100.5, 101)] * 6
    f = frame(bars, {3: 1})
    f.iloc[5:, f.columns.get_loc("state")] = 0           # signal invalidated before fallback
    r = run(f, lim, enable_halts=False)
    assert len(r.trades) == 0 and r.skipped.get("entry_not_filled") == 1


def test_leverage_cap_shrinks_position():
    bars = flat(4) + [(100, 100.4, 99.9, 100)] * 3 + flat(3)
    r = run(frame(bars, {3: 1}, atr=0.2), ExitCfg(mode="fixed", stop_atr=1.0, target_atr=50, **{k: v for k, v in MKT.items()}))
    t = r.trades.iloc[0]
    assert t.qty * t.entry <= 2 * 10_000 + 1e-6


def test_daily_loss_limit_blocks_further_entries_same_day():
    # three losing trades in one UTC day; only 3% may be lost, entries after that are skipped
    day0 = (T0 // (24 * H) + 1) * 24 * H
    n = 24
    f = pd.DataFrame(index=day0 + np.arange(n) * H, columns=["open", "high", "low", "close"], dtype=float)
    f[:] = 100.0
    for k in range(4, n, 4):
        f.iloc[k, f.columns.get_loc("low")] = 96.0
        f.iloc[k, f.columns.get_loc("close")] = 97.0
    f["atr"], f["adx"], f["ema20"], f["trend4h"] = 2.0, 30.0, 100.0, 1
    f["swing_low4h"], f["swing_high4h"] = 0.0, 1e9
    f["signal"], f["state"], f["kind"], f["target"] = 0, 1, 0, np.nan
    for k in range(1, n - 2, 3):
        f.iloc[k, f.columns.get_loc("signal")] = 1
    r = run_backtest(f, None, costs=ZERO, exit_cfg=ExitCfg(mode="fixed", target_atr=50, **MKT))
    # the halt triggers once 3% is lost, so the last trade may overshoot by at most its 1% risk
    assert r.trades.net.sum() >= -0.04 * 10_000
    assert len(r.trades) < 7
    assert r.skipped.get("daily_loss_limit", 0) >= 1


def test_kill_switch_stops_trading():
    lim = RiskLimits(daily_loss_limit=0.9, max_drawdown=0.015)
    bars = flat(4) + [(100, 100, 96, 97)] + flat(4, 97) + [(97, 97, 93, 94)] + flat(10, 94)
    f = frame(bars, {3: 1, 8: 1, 12: 1})
    r = run_backtest(f, None, costs=ZERO, limits=lim, exit_cfg=ExitCfg(mode="fixed", **MKT))
    assert r.killed_at is not None and len(r.trades) == 2   # third signal is never traded


def _smart(**kw):
    return ExitCfg(mode="smart", entry_order="market", level_buffer_atr=0.0, **kw)


def test_smart_banks_half_at_2r_then_trails_out():
    # entry 100, stop 97 (R=3). Rise to 107 (>+2R=106) then climb and reverse
    up = [(100, 101, 99.5, 101), (101, 107, 101, 107), (107, 112, 107, 112), (112, 118, 112, 118),
          (118, 118.5, 100, 101), (101, 101, 101, 101)] + flat(3, 101)
    t = run(frame(flat(4) + up, {3: 1}, ema20=100.0, swl=95.0), _smart()).trades.iloc[0]
    assert t.banked and t.mfe_r >= 4 and t.net > 100
    assert t.reason in ("trail", "stop")


def test_smart_time_stop_closes_dead_trade():
    bars = flat(4) + [(100, 100.5, 99.5, 100)] * 40
    t = run(frame(bars, {3: 1}), _smart(time_stop_bars=24)).trades.iloc[0]
    assert t.reason == "time_stop" and t.bars == 25


def test_smart_does_not_time_stop_if_trade_reached_1r():
    bars = flat(4) + [(100, 103.5, 100, 103)] + [(103, 103.5, 102.5, 103)] * 40
    t = run(frame(bars, {3: 1}), _smart(time_stop_bars=24, trail_start_r=99, be_at_r=None, half_at_r=99)).trades.iloc[0]
    assert t.reason != "time_stop"


def test_mean_reversion_fixed_target_and_time_stop():
    bars = flat(4) + [(95, 96, 94.5, 95), (95, 100.5, 95, 100)] + flat(3)
    f = frame(bars, {3: 1}, kind=1, target=100.0, atr=2.0)
    t = run(f, ExitCfg(mode="smart", entry_order="market", level_buffer_atr=0.0)).trades.iloc[0]
    assert t.reason == "target" and t.net > 0


def test_avoid_levels():
    assert avoid_levels(60_010, 1, 500, 0.2) == pytest.approx(60_000 - 100)
    assert avoid_levels(59_990, 1, 500, 0.2) == pytest.approx(60_000 - 100)
    assert avoid_levels(60_010, -1, 500, 0.2) == pytest.approx(60_000 + 100)
    assert avoid_levels(58_700, 1, 500, 0.2) == 58_700


def test_metrics_helpers():
    assert max_drawdown([100, 120, 90, 130]) == pytest.approx(0.25)
    assert profit_factor(pd.Series([3.0, -1.0, -1.0])) == pytest.approx(1.5)
    assert buy_and_hold_return(pd.DataFrame({"close": [100.0, 150.0]})) == pytest.approx(0.5)
    tr = pd.DataFrame({"net": [100.0, -50.0] * 20})
    dd = monte_carlo_drawdown(tr, 10_000, runs=200)
    assert 0 < dd < 0.2
    assert monte_carlo_drawdown(tr, 10_000, runs=200) == dd     # seeded → repeatable


def test_summarize_keys_and_empty():
    r = run(frame(flat(10)))
    s = summarize(r)
    assert s["trades"] == 0 and s["net_pnl"] == 0
    bars = flat(4) + [(100, 101, 99, 100), (100, 107, 100, 106)] + flat(3, 106)
    s = summarize(run(frame(bars, {3: 1})))
    assert s["win_rate"] == 1 and s["net_pnl"] == pytest.approx(200)
