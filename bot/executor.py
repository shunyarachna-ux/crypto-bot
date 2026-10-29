"""Executors: paper fills on live prices, or (gated, not implemented) real orders."""
from __future__ import annotations

import json
import os
from pathlib import Path

from backtest.engine import Costs, ExitCfg, Simulator
from .risk import RiskLimits


class PaperExecutor:
    """One simulated account per symbol, using the SAME Simulator as the backtest.

    State is saved to disk after every candle so a restart carries on where it left off.
    """

    def __init__(self, symbols, equity: float, costs: Costs, limits: RiskLimits,
                 exit_cfg: ExitCfg, state_dir: str | Path):
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        share = equity / len(symbols)
        self.sims = {s: Simulator(share, costs, limits, exit_cfg) for s in symbols}
        self.exit_cfg = exit_cfg
        self._load()

    def _path(self, sym):
        return self.dir / ("paper_" + sym.replace("/", "").replace(":", "_") + ".json")

    def _load(self):
        for s, sim in self.sims.items():
            if self._path(s).exists():
                sim.restore(json.loads(self._path(s).read_text()))

    def save(self):
        for s, sim in self.sims.items():
            tmp = self._path(s).with_suffix(".tmp")
            tmp.write_text(json.dumps(sim.snapshot(), default=float))
            tmp.replace(self._path(s))

    def equity(self) -> float:
        return sum(sim.last_mtm for sim in self.sims.values())

    def flatten_all(self, reason="manual_stop"):
        for sim in self.sims.values():
            sim.request_flatten(reason)


class LiveExecutor:
    """Real orders. Deliberately unimplemented.

    Before this can exist: write exchange-specific bracket orders (entry + stop-market
    + take-profit sent together), isolated margin, cancel-and-replace stop updates that
    keep the old stop if a replace fails, then test every path on the exchange testnet
    and run paper mode for the required weeks. See CLAUDE.md: Claude never enables live.
    """

    def __init__(self, *a, **k):
        if os.environ.get("I_UNDERSTAND_LIVE_TRADING") != "yes":
            raise RuntimeError("live trading is not unlocked")
        raise NotImplementedError("LiveExecutor is not implemented; see its docstring")
