"""Risk rules. The bot refuses any order that breaks them.

Everything here is pure and deterministic so it can be tested exhaustively and
shared by backtest, paper and live modes.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

HARD_MAX_LEVERAGE = 3.0


@dataclass(frozen=True)
class RiskLimits:
    risk_per_trade: float = 0.01
    max_leverage: float = 2.0
    daily_loss_limit: float = 0.03
    max_drawdown: float = 0.15
    max_open_positions: int = 1
    liquidation_buffer: float = 3.0
    maintenance_margin: float = 0.005
    max_data_age_candles: int = 2
    max_candle_jump: float = 0.15

    def __post_init__(self):
        if not 0 < self.max_leverage <= HARD_MAX_LEVERAGE:
            raise ValueError(f"max_leverage must be in (0, {HARD_MAX_LEVERAGE}]")
        if not 0 < self.risk_per_trade <= 0.02:
            raise ValueError("risk_per_trade must be in (0, 0.02]")

    @classmethod
    def from_config(cls, cfg: dict) -> "RiskLimits":
        r = cfg["risk"] if "risk" in cfg else cfg
        return cls(**{k: r[k] for k in cls.__dataclass_fields__ if k in r})


@dataclass
class SizeResult:
    qty: float            # contracts / coins (0 if rejected)
    notional: float
    leverage: float
    reason: str = "ok"    # "ok", "capped_by_leverage", or a rejection reason

    @property
    def ok(self) -> bool:
        return self.qty > 0


def liquidation_price(entry: float, side: int, leverage: float, mmr: float) -> float:
    """Isolated-margin liquidation price (side +1 long, -1 short)."""
    if side == 1:
        return entry * (1 - 1 / leverage + mmr)
    return entry * (1 + 1 / leverage - mmr)


def position_size(equity: float, entry: float, stop: float, side: int,
                  limits: RiskLimits, min_qty: float = 0.0,
                  qty_step: float | None = None) -> SizeResult:
    """Size from the stop, then cap by leverage (shrink, never raise leverage).

    qty = risk_per_trade * equity / |entry - stop|; if notional/equity exceeds
    max_leverage the trade is shrunk. Rejects if the liquidation price is not
    at least `liquidation_buffer` times farther away than the stop.
    """
    if equity <= 0 or entry <= 0:
        return SizeResult(0, 0, 0, "bad_inputs")
    if side not in (1, -1):
        return SizeResult(0, 0, 0, "bad_side")
    dist = (entry - stop) * side
    if dist <= 0:
        return SizeResult(0, 0, 0, "stop_on_wrong_side")

    qty = limits.risk_per_trade * equity / dist
    reason = "ok"
    max_qty = limits.max_leverage * equity / entry
    if qty > max_qty:
        qty, reason = max_qty, "capped_by_leverage"

    if qty_step:
        qty = math.floor(qty / qty_step + 1e-9) * qty_step
    if qty <= 0 or qty < min_qty:
        return SizeResult(0, 0, 0, "below_min_order_size")

    notional = qty * entry
    leverage = notional / equity
    liq = liquidation_price(entry, side, max(leverage, 1.0), limits.maintenance_margin)
    liq_dist = abs(entry - liq)
    if liq_dist < limits.liquidation_buffer * dist:
        return SizeResult(0, 0, 0, "liquidation_too_close")
    return SizeResult(qty, notional, leverage, reason)


def check_candle_sanity(prev_close: float, candle_open: float, candle_close: float,
                        candle_ts, now_ts, timeframe_seconds: int,
                        limits: RiskLimits) -> tuple[bool, str]:
    """Skip trading on stale or abnormal data. Timestamps in seconds."""
    for v in (prev_close, candle_open, candle_close):
        if v is None or not math.isfinite(v) or v <= 0:
            return False, "bad_price"
    if now_ts - candle_ts > limits.max_data_age_candles * timeframe_seconds:
        return False, "stale_data"
    if abs(candle_close / prev_close - 1) > limits.max_candle_jump:
        return False, "abnormal_jump"
    return True, "ok"


@dataclass
class RiskState:
    """Account-level guards: daily loss limit and max drawdown kill switch."""
    limits: RiskLimits
    equity: float
    peak: float = field(init=False)
    day: date | None = None
    day_start_equity: float = field(init=False)
    killed: bool = False          # needs manual restart
    day_halted: bool = False
    open_positions: int = 0

    def __post_init__(self):
        self.peak = self.equity
        self.day_start_equity = self.equity

    def update_equity(self, equity: float, today: date) -> None:
        if self.day != today:
            self.day = today
            self.day_start_equity = equity
            self.day_halted = False
        self.equity = equity
        self.peak = max(self.peak, equity)
        if equity <= self.day_start_equity * (1 - self.limits.daily_loss_limit):
            self.day_halted = True
        if equity <= self.peak * (1 - self.limits.max_drawdown):
            self.killed = True

    @property
    def drawdown(self) -> float:
        return 1 - self.equity / self.peak if self.peak else 0.0

    @property
    def must_flatten(self) -> bool:
        return self.killed or self.day_halted

    def can_open(self) -> tuple[bool, str]:
        if self.killed:
            return False, "kill_switch"
        if self.day_halted:
            return False, "daily_loss_limit"
        if self.open_positions >= self.limits.max_open_positions:
            return False, "max_positions"
        return True, "ok"

    def reset_kill_switch(self) -> None:
        """Manual restart only (never called automatically)."""
        self.killed = False
        self.peak = self.equity
