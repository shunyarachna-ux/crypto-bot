# crypto-bot

Rule-based crypto futures bot (BTC/ETH) with a cost-aware backtester, a walk-forward gate, a paper-trading
mode and Telegram alerts. Built from the *Automated Crypto Futures Trading System — Build Plan*.
Claude Code builds and reviews the code; the bot itself is plain Python and places no orders by AI judgement.

> **No profit is promised.** Most retail futures traders lose money. This repo exists to find out cheaply
> whether a strategy works and to cap the damage if it does not. Not financial or tax advice.

## Status

| Plan step | State |
| --- | --- |
| 1 Scaffold, `CLAUDE.md`, config | done |
| 2 Data download / resample / checks (`bot/data.py`) | done, tested with fakes. **Not yet run against a real exchange** |
| 3 Risk rules (`bot/risk.py`) + tests | done |
| 4 Backtester with fees, GST, slippage, funding, 30% tax | done, unit-tested on hand-made candles |
| 5 Strategies, ADX regime switch, smart trail, walk-forward gate | done. **No result on real data exists yet** |
| 6 Paper executor, Telegram alerts, `/stop` | done. Paper = same `Simulator` as the backtest (a test asserts identical trades) |
| 7 systemd unit + deploy guide | done (`deploy/`) |
| Live trading | **not implemented on purpose** (`LiveExecutor` refuses) |

The sandbox this was built in could not reach any exchange, so every number produced so far comes from
synthetic random data. That only proves the plumbing (random data correctly *fails* the gate).

## Quick start

```bash
pip install -r requirements.txt
python -m pytest -q
python -m bot.data download --years 3        # needs internet; research data from config `exchange`
python -m backtest.run_backtest              # one backtest with config.yaml settings
python -m backtest.walk_forward              # Phase 1 gate → reports/phase1_<strategy>.md
python -m backtest.walk_forward --strategy regime   # only after trend_ema passes on its own
python -m bot.main                           # paper mode (Telegram optional, see deploy/DEPLOY.md)
```

Add `--synthetic` to the backtest commands for a plumbing check without data.

## How it fits together

- `bot/strategies/*` turn 1h candles (+ 4h trend) into signals. Same code in backtest and paper.
- `backtest/engine.py` `Simulator` is a candle-by-candle simulator (no lookahead). Backtest loops over history;
  paper mode feeds it one live candle at a time and saves state to `state/`.
- `bot/risk.py`: sizing from the stop, leverage cap (shrink, never raise), liquidation buffer, daily loss limit,
  drawdown kill switch, data sanity. Limits are validated in `bot/config.py` (leverage can never exceed 3x).
- Fills are pessimistic: stop wins ties inside a candle, limits fill only if price trades *through*,
  close-based exits fill at the next open.
- Exits (`ExitCfg.mode`): `fixed`, `trail2`, `smart` (breakeven, bank half at +2R, wider-of-two trail,
  tighten when the move looks done, 24-bar time stop, stops kept off round numbers) and `smart_no_half`.
  `walk_forward` compares all of them on unseen data.
- Every variant tried is appended to `reports/trials.csv`.

## Phase 1 gate (all must pass)

Tuned on first 70%, judged on last 30% · profit factor ≥ 1.3 after all costs and tax (≥ 30 trades) · nearby settings
also profitable · other symbol works without retuning · beats unleveraged buy & hold · realistic fills ·
≤ 100 logged variants · Monte Carlo 95th-percentile drawdown under the 15% kill switch.

## Known gaps and assumptions (read before trusting any number)

- **Shark Exchange** is not wired in: it has no ccxt adapter I could verify. Data/paper use a ccxt exchange
  (`exchange:` in config). Fees in `config.yaml` are placeholders: check the real fee page.
- Paper mode simulates the default 8-hourly funding rate, not live funding. Backtests use real funding history if downloaded.
- Intrabar stop/target checks in paper mode run when each 1h candle closes, not tick by tick; no WebSocket feed yet (REST polling).
- Tax model: 30% on every winning trade, no loss set-off (plan's assumption). Confirm treatment of futures with a CA.
- Paper mode splits equity equally per symbol (each symbol has its own risk state, plus an account-level drawdown/daily guard).
- Not built: the daily Claude Code review job, exchange minimum-order checks inside the sizing path, the optional Opus signal filter.
- Before ever going live: write and testnet-test `LiveExecutor` (bracket orders, isolated margin, safe stop replace),
  run the plan's paper period, and follow the security checklist. Only the owner sets `mode: live`.
