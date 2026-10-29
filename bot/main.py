"""Bot entry point.

    python -m bot.main                 run in the mode set in config.yaml
    python -m bot.main --once          process any new candles once and exit
    python -m bot.main --reset-kill    clear the drawdown kill switch / manual halt (by hand)

Modes: paper (live prices, simulated fills). `backtest` mode is run via backtest/.
`live` is refused until LiveExecutor exists and the owner unlocks it by hand.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from backtest.engine import Costs, ExitCfg, funding_per_bar
from .alerts import Alerts
from .config import load_config
from .exchange import TF_SECONDS, Exchange, closed_only
from .executor import LiveExecutor, PaperExecutor
from .risk import RiskLimits, RiskState, check_candle_sanity
from .strategies import get_strategy

log = logging.getLogger("bot")
H1 = 3_600_000


class Bot:
    def __init__(self, cfg: dict, exchange, alerts: Alerts, equity: float = 10_000.0, clock=time.time):
        self.cfg, self.ex, self.alerts, self.clock = cfg, exchange, alerts, clock
        self.symbols = cfg["symbols"]
        self.tf, self.tf_trend = cfg["timeframe"], cfg["trend_timeframe"]
        self.limits = RiskLimits.from_config(cfg)
        self.costs = Costs.from_config(cfg)
        s = cfg["strategy"]
        self.exit_cfg = ExitCfg(mode=s["exit_mode"], stop_atr=s["stop_atr"], target_atr=s["target_atr"],
                                entry_order=s["entry_order"])
        if cfg["mode"] == "live":
            LiveExecutor()                                # raises: not unlocked / not implemented
        if cfg["mode"] != "paper":
            raise SystemExit(f"bot.main runs paper mode only (config mode={cfg['mode']})")
        self.state_dir = Path(cfg["state_dir"])
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.paper = PaperExecutor(self.symbols, equity, self.costs, self.limits, self.exit_cfg, self.state_dir)
        self.meta = self._load_meta()
        self.account = RiskState(self.limits, self.paper.equity())
        if self.meta.get("peak"):
            self.account.peak = max(self.account.peak, self.meta["peak"])
        self.strategy = get_strategy(s["name"], **(s.get("params") or {}))
        self.start_equity = equity

    # ---- persisted bot-level state ----
    @property
    def _meta_path(self):
        return self.state_dir / "bot.json"

    def _load_meta(self) -> dict:
        m = {"last_ts": {}, "halted": False, "last_heartbeat": 0, "last_summary_day": None, "peak": None}
        if self._meta_path.exists():
            m.update(json.loads(self._meta_path.read_text()))
        return m

    def _save_meta(self):
        self._meta_path.write_text(json.dumps(self.meta))

    # ---- one pass ----
    def step(self) -> None:
        now = self.clock()
        self._commands()
        block = self.meta["halted"] or self.account.day_halted
        for sim in self.paper.sims.values():          # halted: no new entries, but keep processing
            sim.block_entries = block                 # candles so flatten requests actually execute
        for sym in self.symbols:
            try:
                self._process_symbol(sym, now)
            except Exception as e:  # noqa: BLE001 - one bad symbol must not kill the loop
                log.exception("error on %s", sym)
                self.alerts.send(f"⚠️ error on {sym}: {type(e).__name__}: {e}")
        self._account_guard(now)
        self._heartbeat(now)
        self.meta["peak"] = self.account.peak
        self.paper.save()
        self._save_meta()

    def _process_symbol(self, sym: str, now: float) -> None:
        sim = self.paper.sims[sym]
        h1 = closed_only(self.ex.candles(sym, self.tf, 600), self.tf, now)
        h4 = closed_only(self.ex.candles(sym, self.tf_trend, 200), self.tf_trend, now)
        if len(h1) < 100:
            return
        last = self.meta["last_ts"].get(sym)
        if last is None:                       # first run: start from the latest closed candle, no backfill
            self.meta["last_ts"][sym] = int(h1["ts"].iloc[-1])
            return
        new = h1[h1["ts"] > last]
        if new.empty:
            return
        frame = self.strategy.build(h1, h4)
        # funding: only the default 8-hourly rate is simulated in paper mode
        fund = funding_per_bar(frame.index.values.astype("int64"), None, self.costs.default_funding_8h)
        newest = int(h1["ts"].iloc[-1])
        for ts in new["ts"]:
            i = frame.index.get_loc(int(ts))
            r = frame.iloc[i]
            prev_close = frame["close"].iloc[i - 1]
            close_s = ts / 1000 + TF_SECONDS[self.tf]
            # staleness only matters for the newest candle; older ones are catch-up after downtime
            ok, why = check_candle_sanity(prev_close, r.open, r.close, close_s,
                                          now if int(ts) == newest else close_s,
                                          TF_SECONDS[self.tf], self.limits)
            self.meta["last_ts"][sym] = int(ts)
            if not ok:
                self.alerts.send(f"⚠️ {sym}: skipped candle ({why})")
                continue
            before_trades, had_pos = len(sim.trades), sim.pos is not None
            sim.on_candle(int(ts), r.open, r.high, r.low, r.close, r.atr, r.adx, r.ema20, int(r.trend4h),
                          r.swing_low4h, r.swing_high4h, int(r.signal), int(r.state), int(r.kind),
                          r.target, fund[i])
            self._announce(sym, sim, before_trades, had_pos)

    def _announce(self, sym, sim, before_trades, had_pos):
        if sim.pos is not None and not had_pos:
            p = sim.pos
            self.alerts.send(f"🟢 {sym} {'LONG' if p['side'] == 1 else 'SHORT'} opened @ {p['entry']:.2f} "
                             f"qty {p['qty']:.4f} stop {p['stop0']:.2f} (paper)")
        for t in sim.trades[before_trades:]:
            self.alerts.send(f"🔴 {sym} closed ({t['reason']}) @ {t['exit']:.2f} "
                             f"P&L {t['net']:+.2f} ({t['r_multiple']:+.2f}R) (paper)")
        if sim.stopped:
            self.alerts.send(f"🛑 {sym}: drawdown kill switch triggered. Bot halted until restarted by hand.")

    def _account_guard(self, now: float) -> None:
        eq = self.paper.equity()
        self.account.update_equity(eq, datetime.fromtimestamp(now, tz=timezone.utc).date())
        if self.account.killed and not self.meta["halted"]:
            self.paper.flatten_all("kill_switch")
            self.meta["halted"] = True
            self.alerts.send(f"🛑 account drawdown {self.account.drawdown:.1%}: closing everything and halting.")
        elif self.account.day_halted and not self.meta.get("day_alerted") == str(self.account.day):
            self.meta["day_alerted"] = str(self.account.day)
            self.paper.flatten_all("risk_halt")
            self.alerts.send("⛔ daily loss limit hit: flattening, no new entries today.")
        if any(s.stopped for s in self.paper.sims.values()) and not self.meta["halted"]:
            self.meta["halted"] = True

    def _commands(self) -> None:
        for cmd in self.alerts.poll_commands():
            if cmd == "/stop":
                self.paper.flatten_all("manual_stop")
                self.meta["halted"] = True
                self.alerts.send("🛑 /stop received: closing all positions and halting. Use --reset-kill to restart.")
            elif cmd == "/status":
                self.alerts.send(self.status())

    def status(self) -> str:
        pos = [f"{s}: {'LONG' if x.pos['side'] == 1 else 'SHORT'}@{x.pos['entry']:.2f}"
               for s, x in self.paper.sims.items() if x.pos]
        return (f"paper equity {self.paper.equity():.2f} dd {self.account.drawdown:.1%} "
                f"halted={self.meta['halted']} open: {', '.join(pos) or 'none'}")

    def _heartbeat(self, now: float) -> None:
        hours = self.cfg.get("alerts", {}).get("heartbeat_hours", 4)
        if now - self.meta["last_heartbeat"] >= hours * 3600:
            self.meta["last_heartbeat"] = now
            self.alerts.send("💓 " + self.status())
        day = datetime.fromtimestamp(now, tz=timezone.utc).date().isoformat()
        if self.meta["last_summary_day"] not in (None, day):
            self.alerts.send(self.daily_summary())
        self.meta["last_summary_day"] = day

    def daily_summary(self) -> str:
        t = [x for sim in self.paper.sims.values() for x in sim.trades]
        n = len(t)
        wins = sum(1 for x in t if x["net"] > 0)
        return (f"📊 summary (all-time, paper): {n} trades, win rate {wins / n:.0%}, " if n else
                "📊 summary: no trades yet, ") + f"net {sum(x['net'] for x in t):+.2f}, dd {self.account.drawdown:.1%}"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--reset-kill", action="store_true")
    ap.add_argument("--poll-seconds", type=int, default=30)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    cfg = load_config()
    if a.reset_kill:
        sd = Path(cfg["state_dir"])
        for f in sd.glob("paper_*.json"):
            d = json.loads(f.read_text())
            d["killed_at"] = None
            d["block_entries"] = False
            d["rs"].update(killed=False, day_halted=False, peak=d["rs"]["equity"])
            f.write_text(json.dumps(d))
        m = sd / "bot.json"
        if m.exists():
            d = json.loads(m.read_text())
            d["halted"] = False
            d["peak"] = None
            m.write_text(json.dumps(d))
        print("halt and kill switch cleared")
        return
    ex = Exchange(cfg["exchange"])
    bot = Bot(cfg, ex, Alerts(), cfg.get("paper_equity", 10_000.0))
    bot.alerts.send(f"▶️ bot started ({cfg['mode']}, {', '.join(cfg['symbols'])})")
    while True:
        bot.step()
        if a.once:
            break
        time.sleep(a.poll_seconds)


if __name__ == "__main__":
    main()
