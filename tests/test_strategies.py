import numpy as np

from backtest.synthetic import synthetic_frames
from bot.strategies import get_strategy

H1, H4 = synthetic_frames(n_days=200, seed=3)


def test_trend_signals_only_with_4h_trend():
    f = get_strategy("trend_ema").build(H1, H4)
    sig = f[f.signal != 0]
    assert len(sig) > 5
    assert (sig.signal == sig.trend4h).all()


def test_no_lookahead_on_4h_features():
    """Changing future 4h candles must not change earlier 1h features."""
    f1 = get_strategy("trend_ema").build(H1, H4)
    H4b = H4.copy()
    H4b.loc[H4b.index[-30:], ["open", "high", "low", "close"]] *= 3
    f2 = get_strategy("trend_ema").build(H1, H4b)
    cutoff = H4b.ts.iloc[-30] + 3_600_000 * 3     # first 1h candle that sees a changed 4h candle
    before = f1.index < cutoff
    for col in ("trend4h", "swing_low4h", "swing_high4h", "state", "signal"):
        a, b = f1.loc[before, col].values, f2.loc[before, col].values
        assert np.array_equal(a, b, equal_nan=True), col


def test_no_lookahead_on_1h_signals():
    f1 = get_strategy("trend_ema").build(H1, H4)
    H1b = H1.copy()
    k = len(H1) - 100
    H1b.loc[H1b.index[k:], ["open", "high", "low", "close"]] *= 2
    f2 = get_strategy("trend_ema").build(H1b, H4)
    assert np.array_equal(f1["signal"].values[:k], f2["signal"].values[:k])
    assert np.allclose(f1["atr"].values[:k], f2["atr"].values[:k], equal_nan=True)


def test_mean_reversion_has_targets_and_trend_direction():
    f = get_strategy("mean_reversion").build(H1, H4)
    sig = f[f.signal != 0]
    assert len(sig) > 5 and (sig.signal == sig.trend4h).all()
    assert sig.target.notna().all() and (f.kind == 1).all()
    longs = sig[sig.signal == 1]
    assert (longs.target > longs.close).all()


def test_regime_routes_by_adx():
    f = get_strategy("regime").build(H1, H4)
    sig = f[f.signal != 0]
    assert len(sig) > 0
    assert ((sig.kind == 0) & (sig.adx > 25) | (sig.kind == 1) & (sig.adx < 20)).all()
    assert (f[(f.adx >= 20) & (f.adx <= 25)].signal == 0).all()


def test_unknown_param_rejected():
    import pytest
    with pytest.raises(ValueError):
        get_strategy("trend_ema", nope=1)
