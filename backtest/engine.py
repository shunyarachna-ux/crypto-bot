"""Event-driven backtester on 1h candles.

Costs modelled on every trade: maker/taker fees, 18% GST on fees, slippage on
market fills, funding payments, and 30% tax on each winning trade with no
loss set-off. Risk rules come from bot/risk.py (same code as paper/live).

Fill rules (deliberately pessimistic):
  * stop and target both inside one candle → the stop is assumed to hit first
  * a resting limit order fills only if price trades THROUGH it (low < buy limit)
  * stops are market orders: slippage + taker fee; targets/limits: maker fee
  * close-based exits (trail, time stop) execute at the NEXT candle's open
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from bot.risk import RiskLimits, RiskState, position_size

H1_MS = 3_600_000


@dataclass(frozen=True)
class Costs:
    maker_fee: float = 0.0002
    taker_fee: float = 0.0005
    gst_on_fees: float = 0.18
    slippage: float = 0.0003
    tax_on_wins: float = 0.30
    default_funding_8h: float = 0.0001

    @classmethod
    def from_config(cls, cfg: dict) -> "Costs":
        c = cfg["costs"] if "costs" in cfg else cfg
        return cls(**{k: c[k] for k in cls.__dataclass_fields__ if k in c})


@dataclass(frozen=True)
class ExitCfg:
    mode: str = "smart"            # fixed | trail2 | smart | smart_no_half
    stop_atr: float = 1.5
    target_atr: float = 3.0        # fixed mode
    trail2_atr: float = 2.0        # trail2 mode
    be_at_r: float | None = 1.5    # smart: move safety stop to breakeven at +R (None = never)
    trail_start_r: float = 1.5
    half_at_r: float = 2.0
    trail_atr: float = 3.0
    tight_atr: float = 1.5
    stretch_atr: float = 3.0
    adx_floor: float = 20.0
    time_stop_bars: int = 24
    time_stop_r: float = 1.0
    level_buffer_atr: float = 0.2
    safety_buffer_atr: float = 1.0
    entry_order: str = "limit_fallback"   # market | limit_fallback
    entry_wait_bars: int = 2

    def __post_init__(self):
        if self.mode not in ("fixed", "trail2", "smart", "smart_no_half"):
            raise ValueError(f"bad exit mode {self.mode}")
        if self.entry_order not in ("market", "limit_fallback"):
            raise ValueError(f"bad entry_order {self.entry_order}")


def round_step(price: float) -> float:
    return 10 ** (math.floor(math.log10(price)) - 1)


def avoid_levels(stop: float, side: int, atr: float, buf: float) -> float:
    """Push a stop `buf` ATR beyond a nearby round number, where stops cluster."""
    if not (stop > 0 and atr > 0):
        return stop
    step = round_step(stop)
    r = round(stop / step) * step
    if abs(stop - r) <= buf * atr:
        return r - buf * atr if side == 1 else r + buf * atr
    return stop


@dataclass
class Result:
    trades: pd.DataFrame
    equity: pd.Series          # mark-to-market per candle (index = candle ts, ms)
    start_equity: float
    killed_at: int | None = None
    skipped: dict = field(default_factory=dict)


def funding_per_bar(ts: np.ndarray, funding: pd.DataFrame | None, default_8h: float) -> np.ndarray:
    """Sum of funding rates falling inside each 1h bar."""
    out = np.zeros(len(ts))
    if funding is not None and len(funding):
        idx = np.searchsorted(ts, funding["ts"].values, side="right") - 1
        ok = (idx >= 0) & (funding["ts"].values < ts[-1] + H1_MS)
        np.add.at(out, idx[ok], funding["rate"].values[ok])
    else:
        hour = (ts // H1_MS) % 24
        out[(hour % 8) == 0] = default_8h
    return out


class Simulator:
    """Candle-by-candle trading simulator. No lookahead: each call sees one closed candle.

    Used by `run_backtest` (loop over history) and by paper mode (one call per new
    live candle, state persisted with snapshot()/restore()).
    """

    def __init__(self, equity: float, costs: Costs, limits: RiskLimits, exit_cfg: ExitCfg,
                 enable_halts: bool = True):
        self.costs, self.limits, self.ec, self.halts = costs, limits, exit_cfg, enable_halts
        self.start_equity = equity
        self.E = equity
        self.rs = RiskState(limits, equity)
        self.pos: dict | None = None
        self.pending: dict | None = None
        self.trades: list[dict] = []
        self.skipped: dict[str, int] = {}
        self.killed_at: int | None = None
        self.last_mtm = equity
        self.block_entries = False     # set by the bot when halted (/stop, kill switch, daily limit)

    # -- helpers --
    def fee(self, notional, rate):
        return notional * rate * (1 + self.costs.gst_on_fees)

    def skip(self, why):
        self.skipped[why] = self.skipped.get(why, 0) + 1

    @property
    def stopped(self) -> bool:
        return self.killed_at is not None

    def request_flatten(self, reason="manual_stop"):
        """Close at the next candle's open (used by the /stop command)."""
        if self.pos is not None and not self.pos["pending_exit"]:
            self.pos["pending_exit"] = reason
        self.pending = None

    # -- main step --
    def on_candle(self, ts, o, h, l, c, atr, adx, ema20, trend, swl, swh, sig, state, kind, tgt, fund):
        """Process one CLOSED candle. Returns the mark-to-market equity at its close."""
        if self.stopped:
            return self.last_mtm
        ec, costs = self.ec, self.costs
        today = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).date()

        # ---- resolve a pending entry on this candle ----
        if self.pending is not None and self.pos is None:
            self._try_fill(ts, o, h, l, state)

        # ---- manage the open position ----
        if self.pos is not None:
            self._manage(ts, o, h, l, c, atr, adx, ema20, trend, swl, swh, fund)

        # ---- equity and account-level guards at candle close ----
        mtm = self.E
        if self.pos is not None:
            mtm += (c - self.pos["entry"]) * self.pos["side"] * self.pos["qty"]
        self.last_mtm = mtm
        if self.halts:
            self.rs.update_equity(mtm, today)
            if self.rs.must_flatten and self.pos is not None and not self.pos["pending_exit"]:
                self.pos["pending_exit"] = "risk_halt"
            if self.rs.killed and self.pos is None:
                self.killed_at = int(ts)
                self.pending = None
                return mtm

        if self.pending is not None:
            self.pending["state_ok"] = bool(state == self.pending["side"])

        # ---- new signal → create a pending entry (fills from the NEXT candle) ----
        if self.pos is None and self.pending is None and sig != 0 and not np.isnan(atr):
            ok, why = self.rs.can_open() if self.halts else (True, "ok")
            if self.block_entries:
                ok, why = False, "entries_blocked"
            if not ok:
                self.skip(why)
            elif kind == 1 and not np.isfinite(tgt):
                self.skip("no_target")
            else:
                self.pending = dict(side=int(sig), limit=float(c), atr=float(atr), kind=int(kind),
                                    target=float(tgt) if np.isfinite(tgt) else None,
                                    waited=0, state_ok=True)
        return mtm

    def _try_fill(self, ts, o, h, l, state):
        ec, pe = self.ec, self.pending
        pe["waited"] += 1
        side, fill, taker = pe["side"], None, False
        if ec.entry_order == "market":
            fill, taker = o * (1 + side * self.costs.slippage), True
        elif pe["waited"] <= ec.entry_wait_bars:
            if (side == 1 and l < pe["limit"]) or (side == -1 and h > pe["limit"]):
                fill = pe["limit"]          # filled only because price traded THROUGH the limit
        else:                               # waited out: market fallback if the signal still holds
            if pe["state_ok"]:
                fill, taker = o * (1 + side * self.costs.slippage), True
            else:
                self.skip("entry_not_filled")
                self.pending = None
                return
        if fill is None:
            return
        self.pending = None
        ok, why = self.rs.can_open() if self.halts else (True, "ok")
        if self.block_entries:
            ok, why = False, "entries_blocked"
        if not ok:
            self.skip(why)
            return
        a = pe["atr"]
        stop = fill - side * ec.stop_atr * a
        if ec.mode.startswith("smart") and pe["kind"] == 0:
            stop = avoid_levels(stop, side, a, ec.level_buffer_atr)
        sz = position_size(self.E, fill, stop, side, self.limits)
        if not sz.ok:
            self.skip(sz.reason)
            return
        R = abs(fill - stop)
        f0 = self.fee(sz.notional, self.costs.taker_fee if taker else self.costs.maker_fee)
        self.E -= f0
        pos = dict(side=side, entry=float(fill), qty=float(sz.qty), qty0=float(sz.qty), R=float(R),
                   stop0=float(stop), safety=float(stop), trail=None, kind=pe["kind"], bars=0,
                   banked=False, gross=0.0, fees=float(f0), funding=0.0, mfe=0.0, reached1=False,
                   pending_exit=None, best_close=float(fill), entry_ts=int(ts), max_r_close=0.0,
                   half_px=float(fill + side * ec.half_at_r * R), target=None)
        if pe["kind"] == 1:
            pos["target"] = pe["target"]
        elif ec.mode == "fixed":
            pos["target"] = float(fill + side * ec.target_atr * a)
        self.pos = pos
        self.rs.open_positions = 1

    def _manage(self, ts, o, h, l, c, atr, adx, ema20, trend, swl, swh, fund):
        ec, costs, p = self.ec, self.costs, self.pos
        s = p["side"]
        if p["bars"] > 0:                       # funding accrues from the first full candle
            fc = p["qty"] * o * fund * s
            p["funding"] += fc
            self.E -= fc
        p["bars"] += 1
        exit_px = reason = None
        taker = True
        if p["pending_exit"]:
            exit_px, reason = o * (1 - s * costs.slippage), p["pending_exit"]
        elif s * (o - p["safety"]) <= 0:         # gapped through the stop
            exit_px, reason = o * (1 - s * costs.slippage), "stop_gap"
        elif (s == 1 and l <= p["safety"]) or (s == -1 and h >= p["safety"]):
            exit_px, reason = p["safety"] * (1 - s * costs.slippage), "stop"
        elif p["target"] is not None and ((s == 1 and h > p["target"]) or (s == -1 and l < p["target"])):
            exit_px, reason, taker = p["target"], "target", False
        elif (ec.mode == "smart" and p["kind"] == 0 and not p["banked"]
              and ((s == 1 and h > p["half_px"]) or (s == -1 and l < p["half_px"]))):
            q = p["qty"] / 2
            g = (p["half_px"] - p["entry"]) * s * q
            fb = self.fee(q * p["half_px"], costs.maker_fee)
            p["gross"] += g
            p["fees"] += fb
            self.E += g - fb
            p["qty"] -= q
            p["banked"] = True
        if exit_px is not None:
            self.E = _finish(p, exit_px, ts, reason, taker, self.E, costs, self.fee, self.trades)
            self.pos = None
            self.rs.open_positions = 0
            return
        fav = (h - p["entry"]) if s == 1 else (p["entry"] - l)
        p["mfe"] = max(p["mfe"], fav)
        if p["mfe"] >= ec.time_stop_r * p["R"]:
            p["reached1"] = True
        _on_close(p, c, atr, adx, ema20, trend, swl, swh, ec)

    def finish(self, c, ts):
        """Close any open position at `c` (end of data)."""
        if self.pos is not None:
            self.E = _finish(self.pos, c * (1 - self.pos["side"] * self.costs.slippage), ts,
                             "end_of_data", True, self.E, self.costs, self.fee, self.trades)
            self.pos = None
            self.last_mtm = self.E

    # -- persistence (paper mode) --
    def snapshot(self) -> dict:
        r = self.rs
        return {"E": self.E, "pos": self.pos, "pending": self.pending, "trades": self.trades,
                "skipped": self.skipped, "killed_at": self.killed_at, "last_mtm": self.last_mtm,
                "block_entries": self.block_entries,
                "start_equity": self.start_equity,
                "rs": {"equity": r.equity, "peak": r.peak, "day": r.day.isoformat() if r.day else None,
                       "day_start_equity": r.day_start_equity, "killed": r.killed,
                       "day_halted": r.day_halted, "open_positions": r.open_positions}}

    def restore(self, d: dict) -> None:
        from datetime import date
        self.E, self.pos, self.pending = d["E"], d["pos"], d["pending"]
        self.trades, self.skipped = d["trades"], d["skipped"]
        self.killed_at, self.last_mtm, self.start_equity = d["killed_at"], d["last_mtm"], d["start_equity"]
        self.block_entries = d.get("block_entries", False)
        r, q = self.rs, d["rs"]
        r.equity, r.peak, r.day_start_equity = q["equity"], q["peak"], q["day_start_equity"]
        r.day = date.fromisoformat(q["day"]) if q["day"] else None
        r.killed, r.day_halted, r.open_positions = q["killed"], q["day_halted"], q["open_positions"]


def run_backtest(frame: pd.DataFrame, funding: pd.DataFrame | None = None, *,
                 equity: float = 10_000.0, costs: Costs = Costs(),
                 limits: RiskLimits = RiskLimits(), exit_cfg: ExitCfg = ExitCfg(),
                 enable_halts: bool = True) -> Result:
    ts = frame.index.values.astype("int64")
    n = len(frame)
    cols = [frame[k].values.astype(float) for k in
            ("open", "high", "low", "close", "atr", "adx", "ema20")]
    trend = frame["trend4h"].values.astype(int)
    swl, swh = frame["swing_low4h"].values.astype(float), frame["swing_high4h"].values.astype(float)
    sig, state = frame["signal"].values.astype(int), frame["state"].values.astype(int)
    kind, tgt = frame["kind"].values.astype(int), frame["target"].values.astype(float)
    fund = funding_per_bar(ts, funding, costs.default_funding_8h)
    valid = ~np.isnan(cols[4]) & ~np.isnan(cols[5]) & ~np.isnan(cols[6])
    first = int(np.argmax(valid)) if valid.any() else n

    sim = Simulator(equity, costs, limits, exit_cfg, enable_halts)
    eq = np.full(n, np.nan)
    o, h, l, c, atr_, adx_, ema20 = cols
    for i in range(first, n):
        eq[i] = sim.on_candle(ts[i], o[i], h[i], l[i], c[i], atr_[i], adx_[i], ema20[i], trend[i],
                              swl[i], swh[i], sig[i], state[i], kind[i], tgt[i], fund[i])
        if sim.stopped:
            break
    if not sim.stopped and n:
        sim.finish(c[n - 1], ts[n - 1])
        eq[n - 1] = sim.E
    curve = pd.Series(eq, index=frame.index).ffill().fillna(equity)
    return Result(pd.DataFrame(sim.trades), curve, equity, sim.killed_at, sim.skipped)


def _finish(p, px, t, reason, taker, E, costs, fee, trades):
    """Close the remaining position, book costs and tax, record the trade. Returns new equity."""
    s = p["side"]
    leg = (px - p["entry"]) * s * p["qty"]
    f_ = fee(px * p["qty"], costs.taker_fee if taker else costs.maker_fee)
    p["gross"] += leg
    p["fees"] += f_
    E += leg - f_
    pre_tax = p["gross"] - p["fees"] - p["funding"]
    tax = costs.tax_on_wins * pre_tax if pre_tax > 0 else 0.0   # no loss set-off
    E -= tax
    risk_dollars = p["qty0"] * p["R"]
    trades.append({
        "entry_ts": int(p["entry_ts"]), "exit_ts": int(t), "side": s,
        "entry": p["entry"], "exit": px, "qty": p["qty0"], "bars": p["bars"],
        "gross": p["gross"], "fees": p["fees"], "funding": p["funding"],
        "pretax": pre_tax, "tax": tax, "net": pre_tax - tax,
        "r_multiple": pre_tax / risk_dollars if risk_dollars else 0.0,
        "mfe_r": p["mfe"] / p["R"] if p["R"] else 0.0,
        "mfe_dollars": p["mfe"] * p["qty0"], "banked": p["banked"],
        "kind": p["kind"], "reason": reason,
    })
    return E


def _on_close(p, c, a, adx, ema20, trend, swl, swh, ec: ExitCfg) -> None:
    """Close-of-candle logic: update stops/trails; flag close-based exits for next open."""
    s = p["side"]
    mode = ec.mode
    p["best_close"] = max(p["best_close"], c) if s == 1 else min(p["best_close"], c)
    p["max_r_close"] = (p["best_close"] - p["entry"]) * s / p["R"]

    if p["kind"] == 1 or mode in ("smart", "smart_no_half"):
        if not p["reached1"] and p["bars"] >= ec.time_stop_bars:
            p["pending_exit"] = "time_stop"
            return
    if p["kind"] == 1 or mode == "fixed":
        return

    if mode == "trail2":
        new = p["best_close"] - s * ec.trail2_atr * a
        p["safety"] = s * max(s * p["safety"], s * new)
        return

    # ---- smart trail ----
    if p["trail"] is not None and s * (c - p["trail"]) < 0:
        p["pending_exit"] = "trail"
        return
    if ec.be_at_r is not None and p["max_r_close"] >= ec.be_at_r:
        p["safety"] = s * max(s * p["safety"], s * p["entry"])
    if p["max_r_close"] < ec.trail_start_r:
        return
    stretched = (c - ema20) * s > ec.stretch_atr * a
    flipped = trend != s
    weak = adx < ec.adx_floor
    if stretched or flipped or weak:
        cand = p["best_close"] - s * ec.tight_atr * a
    else:
        t1 = p["best_close"] - s * ec.trail_atr * a
        if s == 1:
            sw = swl - ec.level_buffer_atr * a if np.isfinite(swl) else t1
            cand = min(t1, sw)
        else:
            sw = swh + ec.level_buffer_atr * a if np.isfinite(swh) else t1
            cand = max(t1, sw)
    cand = avoid_levels(cand, s, a, ec.level_buffer_atr)
    cand = s * max(s * cand, s * p["stop0"])
    p["trail"] = cand if p["trail"] is None else s * max(s * p["trail"], s * cand)
    hard = p["trail"] - s * ec.safety_buffer_atr * a
    p["safety"] = s * max(s * p["safety"], s * hard)
