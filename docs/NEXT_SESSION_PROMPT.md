You are continuing work on a crypto futures trading bot repo, on the owner's own PC (which can reach exchange APIs).

SETUP
- Repo: this folder (a clone of shunyarachna-ux/crypto-bot). Run `git checkout claude/amazing-mayer-lj875i` and `git pull`.
- Read, in this order: README.md, CLAUDE.md, docs/HANDOFF.md, then the attached build plan file (Automated Crypto Futures Trading System — Build Plan). CLAUDE.md rules are absolute.
- Create a venv, `pip install -r requirements.txt`, run `python -m pytest -q`. All tests must pass before you change anything.

HARD RULES (from CLAUDE.md, repeated)
- Never read, print, log or ask for API keys or .env contents. No keys are needed for this work.
- Never set `mode: live` or `I_UNDERSTAND_LIVE_TRADING`. Do not implement `LiveExecutor`.
- Never raise any risk limit (config.yaml `risk:` or defaults in bot/risk.py).
- Never skip, weaken or delete a test to get green. Run `python -m pytest -q` before every commit.
- Report results honestly, including when a strategy loses money. Do not invent or estimate numbers; if something fails or can't be reached, say so.
- Do not tune strategy code, parameter grids, or the gate thresholds after seeing real results in order to make a strategy pass. Every variant tested must be logged in reports/trials.csv.

TASKS, IN ORDER
1. Download real data: `python -m bot.data download --years 3`. If the exchange blocks my region, retry with `--exchange okx` or `--exchange bybit`. Check the printed quality line (gaps, duplicates, bad_ohlc) for BTC and ETH and report problems before backtesting.
2. Exchange fees: look up the CURRENT official fee pages and API docs for Shark Exchange, Delta Exchange and CoinDCX (check each is FIU-IND registered and offers INR-friendly futures, API keys with withdrawals off, and IP whitelisting). Give me a short comparison table with sources. Put the chosen exchange's real maker/taker fees into config.yaml `costs:` only after I confirm the choice; until then run with the existing placeholder fees and say so in the report.
3. Run `python -m backtest.walk_forward` (trend_ema). Commit reports/phase1_trend_ema.md and reports/trials.csv. Explain the result in plain language: which gate checks passed or failed, the unseen-period profit factor after all costs and 30% tax, drawdown, comparison with buy and hold, and the exit comparison table (fixed 3x ATR vs 2x ATR trail vs smart trail vs smart trail without the half exit).
4. Only if trend_ema passes EVERY gate: run `--strategy regime` and `--strategy mean_reversion`, and keep the ADX regime switch only if it beats trend_ema alone on unseen data. If nothing passes, say so clearly; that is a valid, useful outcome. You may propose at most one genuinely different strategy idea for me to approve; do not implement it without approval.
5. Build the missing safe pieces regardless of the backtest result, each with tests:
   a. Exchange minimum order size and quantity step applied in the Simulator sizing path (position_size already supports min_qty/qty_step; pass real market values via bot/exchange.py `min_order`).
   b. Live funding rate in paper mode instead of the default 8h constant.
   c. A read-only daily review script that reads state/ trade logs and writes reports/daily_YYYY-MM-DD.md: trades, win rate, P&L after costs, slippage vs assumed, errors, drawdown, losing streak vs the backtest's expected range. It must never change code or config.
6. Update the README status table and docs/HANDOFF.md to reflect what is now done. Commit in small commits with clear messages and push to branch claude/amazing-mayer-lj875i. Do NOT open a pull request unless I ask.
7. Finish with a short report: what you ran, exact commands, the gate result, anything that failed, and what you recommend as the next step (paper trading for 8+ weeks is only appropriate if a strategy passed Phase 1).

Do not go beyond these tasks. In particular: no live trading code, no API keys, no changes to risk limits.
