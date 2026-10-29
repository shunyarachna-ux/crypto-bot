from datetime import date, timedelta

import pytest

from bot.config import ConfigError, load_config, validate_config
from bot.risk import (RiskLimits, RiskState, check_candle_sanity,
                      liquidation_price, position_size)

L = RiskLimits()


def test_size_from_stop_risks_one_percent():
    r = position_size(10_000, 100, 98, 1, L)   # wide stop → well under 2x
    assert r.ok and r.reason == "ok"
    assert r.qty * 2 == pytest.approx(100.0)       # loses exactly 1% = $100 at stop
    assert r.leverage == pytest.approx(r.notional / 10_000)


def test_short_sizing_symmetric():
    r = position_size(10_000, 100, 102, -1, L)
    assert r.ok and r.qty * 2 == pytest.approx(100.0)


def test_tight_stop_shrinks_instead_of_raising_leverage():
    r = position_size(10_000, 100, 99.9, 1, L)     # 0.1% stop → 10x needed
    assert r.reason == "capped_by_leverage"
    assert r.leverage == pytest.approx(L.max_leverage)
    assert r.qty * 100 <= 2 * 10_000 + 1e-9


def test_never_exceeds_max_leverage_across_stops():
    for stop in (99.99, 99.5, 99, 97, 95, 90):
        r = position_size(5_000, 100, stop, 1, L)
        if r.ok:
            assert r.leverage <= L.max_leverage + 1e-9


def test_rejects_wrong_side_stop_and_bad_inputs():
    assert not position_size(10_000, 100, 101, 1, L).ok
    assert not position_size(10_000, 100, 99, -1, L).ok
    assert not position_size(0, 100, 99, 1, L).ok
    assert not position_size(10_000, 100, 99, 0, L).ok


def test_min_order_size():
    r = position_size(100, 50_000, 49_000, 1, L, min_qty=0.01)
    assert not r.ok and r.reason == "below_min_order_size"


def test_qty_step_rounds_down():
    r = position_size(10_000, 100, 98, 1, L, qty_step=0.3)
    assert r.qty == pytest.approx(round(r.qty / 0.3) * 0.3)
    assert r.qty * 2 <= 100.0 + 1e-9


def test_liquidation_buffer_rejects_far_stop():
    # stop 40% away: liq (~50% at 2x) is less than 3x the stop distance
    r = position_size(10_000, 100, 60, 1, L)
    assert not r.ok and r.reason == "liquidation_too_close"


def test_liquidation_price_sides():
    assert liquidation_price(100, 1, 2, 0.005) == pytest.approx(50.5)
    assert liquidation_price(100, -1, 2, 0.005) == pytest.approx(149.5)


def test_limits_reject_unsafe_values():
    with pytest.raises(ValueError):
        RiskLimits(max_leverage=3.5)
    with pytest.raises(ValueError):
        RiskLimits(risk_per_trade=0.05)


def test_daily_loss_limit_halts_then_resets_next_day():
    s = RiskState(L, 10_000)
    d = date(2026, 1, 1)
    s.update_equity(10_000, d)
    s.update_equity(9_800, d)
    assert not s.day_halted and s.can_open()[0]
    s.update_equity(9_700, d)                   # -3%
    assert s.day_halted and s.must_flatten
    assert s.can_open() == (False, "daily_loss_limit")
    s.update_equity(9_700, d + timedelta(days=1))
    assert not s.day_halted and s.can_open()[0]


def test_drawdown_kill_switch_is_sticky_until_manual_reset():
    s = RiskState(L, 10_000)
    d = date(2026, 1, 1)
    s.update_equity(12_000, d)
    s.update_equity(10_200, d + timedelta(days=1))   # exactly -15% from peak
    assert s.killed
    s.update_equity(12_500, d + timedelta(days=2))
    assert s.killed and s.can_open() == (False, "kill_switch")
    s.reset_kill_switch()
    assert s.can_open()[0]


def test_max_open_positions():
    s = RiskState(L, 10_000)
    s.open_positions = 1
    assert s.can_open() == (False, "max_positions")


def test_data_sanity():
    tf = 3600
    assert check_candle_sanity(100, 100, 101, 1000, 1000 + 100, tf, L)[0]
    assert check_candle_sanity(100, 100, 101, 1000, 1000 + 3 * tf, tf, L) == (False, "stale_data")
    assert check_candle_sanity(100, 100, 130, 1000, 1000, tf, L) == (False, "abnormal_jump")
    assert check_candle_sanity(100, 100, float("nan"), 1000, 1000, tf, L) == (False, "bad_price")
    assert check_candle_sanity(0, 100, 100, 1000, 1000, tf, L) == (False, "bad_price")


def test_config_defaults_valid_and_live_blocked(monkeypatch):
    cfg = load_config()
    assert cfg["mode"] != "live"
    cfg["mode"] = "live"
    monkeypatch.delenv("I_UNDERSTAND_LIVE_TRADING", raising=False)
    with pytest.raises(ConfigError):
        validate_config(cfg)
    monkeypatch.setenv("I_UNDERSTAND_LIVE_TRADING", "yes")
    validate_config(cfg)
    cfg["risk"]["max_leverage"] = 5
    with pytest.raises(ConfigError):
        validate_config(cfg)
