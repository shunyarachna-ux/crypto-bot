"""Load and validate config.yaml."""
from __future__ import annotations

import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
HARD_MAX_LEVERAGE = 3.0
MODES = ("backtest", "paper", "live")


class ConfigError(ValueError):
    pass


def load_config(path: str | os.PathLike | None = None) -> dict:
    path = Path(path) if path else ROOT / "config.yaml"
    with open(path) as f:
        cfg = yaml.safe_load(f)
    validate_config(cfg)
    return cfg


def validate_config(cfg: dict) -> None:
    if cfg.get("mode") not in MODES:
        raise ConfigError(f"mode must be one of {MODES}")
    r = cfg["risk"]
    if not 0 < r["max_leverage"] <= HARD_MAX_LEVERAGE:
        raise ConfigError(f"max_leverage must be in (0, {HARD_MAX_LEVERAGE}]")
    if not 0 < r["risk_per_trade"] <= 0.02:
        raise ConfigError("risk_per_trade must be in (0, 2%]")
    for k in ("daily_loss_limit", "max_drawdown"):
        if not 0 < r[k] < 1:
            raise ConfigError(f"{k} must be in (0, 1)")
    if r["max_open_positions"] < 1:
        raise ConfigError("max_open_positions must be >= 1")
    if cfg["mode"] == "live" and os.environ.get("I_UNDERSTAND_LIVE_TRADING") != "yes":
        raise ConfigError(
            "mode is live but I_UNDERSTAND_LIVE_TRADING=yes is not set in the environment"
        )
