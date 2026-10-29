import copy
import json

import pandas as pd
import pytest

from backtest.engine import Costs, ExitCfg, Simulator, run_backtest
from backtest.synthetic import synthetic_frames
from bot.alerts import Alerts
from bot.config import load_config
from bot.executor import LiveExecutor
from bot.main import Bot
from bot.risk import RiskLimits
from bot.strategies import get_strategy

H = 3_600_000
SYM = ["BTC/USDT:USDT"]


class FakeExchange:
    def __init__(self, h1, h4):
        self.d = {"1h": h1, "4h": h4}
        self.now = 0.0

    def candles(self, symbol, tf, limit=500):
        df = self.d[tf]
        return df[df["ts"] < self.now * 1000].copy()      # includes the still-forming candle, like a real API


def make_cfg(tmp_path, **over):
    cfg = load_config()
    cfg["symbols"] = SYM
    cfg["state_dir"] = str(tmp_path / "state")
    cfg["mode"] = "paper"
    cfg["strategy"]["params"] = {"ema_fast": 10, "ema_slow": 30, "trend_ema": 50}
    cfg.update(over)
    return cfg


def quiet_alerts():
    a = Alerts(token="", chat_id="")
    a.poll_commands = lambda: []
    return a


H1, H4 = synthetic_frames(n_days=45, seed=7)
START = 400


def drive(bot, ex, upto, start=START):
    for k in range(start, upto):
        ex.now = (H1.ts.iloc[k] + H) / 1000 + 5          # shortly after candle k closed
        bot.clock = lambda t=ex.now: t
        bot.step()


def test_first_step_only_records_position_in_time(tmp_path):
    ex = FakeExchange(H1, H4)
    bot = Bot(make_cfg(tmp_path), ex, quiet_alerts(), clock=lambda: 0)
    drive(bot, ex, START + 1)
    assert bot.paper.sims[SYM[0]].trades == [] and bot.meta["last_ts"][SYM[0]] == int(H1.ts.iloc[START])


def test_paper_matches_backtest_exactly(tmp_path):
    cfg = make_cfg(tmp_path)
    ex = FakeExchange(H1, H4)
    bot = Bot(cfg, ex, quiet_alerts())
    end = len(H1) - 5
    drive(bot, ex, end)
    sim = bot.paper.sims[SYM[0]]
    assert len(sim.trades) >= 2
    frame = get_strategy("trend_ema", **cfg["strategy"]["params"]).build(H1.iloc[:end], H4)
    frame = frame[frame.index > H1.ts.iloc[START]]            # paper starts after the first recorded candle
    res = run_backtest(frame, None, costs=bot.costs, limits=bot.limits, exit_cfg=bot.exit_cfg,
                       equity=10_000.0)
    got = pd.DataFrame(sim.trades)
    sim.finish(frame.close.iloc[-1], int(frame.index[-1]))
    want = res.trades
    assert len(got) >= len(want) - 1
    n = min(len(got) - 1, len(want) - 1)       # last trade is closed differently at end of data
    pd.testing.assert_frame_equal(got.iloc[:n].reset_index(drop=True), want.iloc[:n].reset_index(drop=True))


def test_restart_resumes_identically(tmp_path):
    cfg = make_cfg(tmp_path)
    ex = FakeExchange(H1, H4)
    a = Bot(cfg, ex, quiet_alerts())
    drive(a, ex, 700)
    full = copy.deepcopy(a.paper.sims[SYM[0]].trades)

    cfg2 = make_cfg(tmp_path / "b")
    ex2 = FakeExchange(H1, H4)
    b = Bot(cfg2, ex2, quiet_alerts())
    drive(b, ex2, 550)
    b2 = Bot(cfg2, ex2, quiet_alerts())               # "crash" and restart from disk
    drive(b2, ex2, 700, start=550)
    assert b2.paper.sims[SYM[0]].trades == full
    assert len(full) >= 1


def test_downtime_catch_up_processes_missed_candles(tmp_path):
    cfg = make_cfg(tmp_path)
    ex = FakeExchange(H1, H4)
    bot = Bot(cfg, ex, quiet_alerts())
    drive(bot, ex, START + 1)
    ex.now = (H1.ts.iloc[START + 40] + H) / 1000 + 5   # 40 candles of downtime
    bot.clock = lambda: ex.now
    bot.step()
    assert bot.meta["last_ts"][SYM[0]] == int(H1.ts.iloc[START + 40])
    assert not any("skipped candle" in m for m in bot.alerts.sent)


def test_stop_command_flattens_and_blocks_entries(tmp_path):
    cfg = make_cfg(tmp_path)
    ex = FakeExchange(H1, H4)
    alerts = quiet_alerts()
    bot = Bot(cfg, ex, alerts)
    sim = bot.paper.sims[SYM[0]]
    k = START
    while sim.pos is None and k < len(H1) - 60:        # run until a position opens
        drive(bot, ex, k + 1, start=k)
        k += 1
    assert sim.pos is not None
    alerts.poll_commands = lambda: ["/stop"]
    drive(bot, ex, k + 1, start=k)
    alerts.poll_commands = lambda: []
    assert bot.meta["halted"]
    drive(bot, ex, k + 40, start=k + 1)
    assert sim.pos is None and sim.trades[-1]["reason"] == "manual_stop"
    assert sim.pending is None
    n = len(sim.trades)
    drive(bot, ex, k + 200, start=k + 40)
    assert len(sim.trades) == n                          # nothing new while halted


def test_bad_candle_is_skipped_with_alert(tmp_path):
    h1 = H1.copy()
    k = START + 3
    h1.loc[h1.index[k], ["close", "high"]] = h1.close.iloc[k - 1] * 1.5
    ex = FakeExchange(h1, H4)
    bot = Bot(make_cfg(tmp_path), ex, quiet_alerts())
    drive(bot, ex, START + 6)
    assert any("abnormal_jump" in m for m in bot.alerts.sent)


def test_live_mode_is_refused(tmp_path, monkeypatch):
    monkeypatch.delenv("I_UNDERSTAND_LIVE_TRADING", raising=False)
    with pytest.raises(RuntimeError):
        LiveExecutor()
    monkeypatch.setenv("I_UNDERSTAND_LIVE_TRADING", "yes")
    with pytest.raises(NotImplementedError):
        LiveExecutor()
    cfg = make_cfg(tmp_path, mode="live")
    with pytest.raises((RuntimeError, NotImplementedError)):
        Bot(cfg, FakeExchange(H1, H4), quiet_alerts())


def test_simulator_snapshot_is_json_and_roundtrips():
    cfg = load_config()
    costs, limits = Costs.from_config(cfg), RiskLimits.from_config(cfg)
    f = get_strategy("trend_ema").build(H1, H4)
    ex = ExitCfg(mode="smart")
    a = Simulator(10_000, costs, limits, ex)
    b = Simulator(10_000, costs, limits, ex)
    rows = list(f.iloc[100:900].itertuples())
    def feed(sim, rows):
        for r in rows:
            sim.on_candle(r.Index, r.open, r.high, r.low, r.close, r.atr, r.adx, r.ema20, r.trend4h,
                          r.swing_low4h, r.swing_high4h, r.signal, r.state, r.kind, r.target, 0.0)
    feed(a, rows)
    feed(b, rows[:400])
    b2 = Simulator(10_000, costs, limits, ex)
    b2.restore(json.loads(json.dumps(b.snapshot(), default=float)))
    feed(b2, rows[400:])
    assert b2.trades == a.trades and b2.E == pytest.approx(a.E)
