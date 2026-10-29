# Handoff: where the build stands

Branch: `claude/amazing-mayer-lj875i`. Read `README.md` (status table, known gaps) and `CLAUDE.md` (rules) first.
The original plan is "Automated Crypto Futures Trading System — Build Plan" (the owner supplies it to the session).

## Done and tested (69 tests pass)
Risk rules, data download/resample/checks, backtester (fees, GST, slippage, funding, 30% tax, smart trail),
three strategies (trend_ema, mean_reversion, regime), walk-forward Phase 1 gate with trial log and Monte Carlo,
paper mode sharing the backtest `Simulator`, Telegram alerts + `/stop`, systemd unit, deploy guide, CI.

## NOT done
1. **No real-data result exists.** The cloud sandbox could not reach any exchange; everything so far ran on synthetic random data.
2. Exchange choice: Shark Exchange is not wired in. Fees in `config.yaml` are placeholders.
3. Exchange minimum order size / qty step is not applied in the sizing path of the Simulator (`position_size` supports it).
4. Paper mode simulates the default 8h funding rate, not live funding. REST polling only, no WebSocket.
5. Daily Claude Code review job (plan section "Monitoring and daily Claude review") is not built.
6. Optional Opus "take it / skip it" signal filter is not built (plan: only after rules pass, paper-test 8 weeks side by side).
7. `LiveExecutor` is intentionally unimplemented. Do not implement or enable it unless the owner explicitly asks, and only after Phase 3.

## Task order for the next session
1. Verify environment: venv, `pip install -r requirements.txt`, `python -m pytest -q` (all pass).
2. `python -m bot.data download --years 3` (fall back to `--exchange okx`/`bybit` if region-blocked). Check the printed quality line per symbol (gaps, duplicates, bad_ohlc). Fix or report data problems before backtesting.
3. Look up the real fee schedule and API docs for Shark Exchange, Delta Exchange and CoinDCX; put the chosen exchange's real maker/taker fees in `config.yaml` `costs:`; summarize the comparison for the owner. Do not guess fees.
4. `python -m backtest.walk_forward` (trend_ema). Do NOT change strategy code or grids after seeing the result to make it pass. Commit `reports/phase1_trend_ema.md` and `reports/trials.csv`.
5. Only if trend_ema passes every gate: run `--strategy regime`, and mean_reversion on its own, and keep the regime switch only if it beats the single strategy on unseen data.
6. If nothing passes: say so plainly. Propose at most one genuinely different idea; do not keep tweaking settings.
7. Build the missing safe pieces regardless of results: min-order-size in sizing, live funding rate in paper mode, daily review report script (`reports/` output from `state/` trade logs, read-only), each with tests.
8. Update README status table and this file. Commit and push to the branch. Open a PR only if the owner asks.

## Prompt to paste into the new session
See the bottom of the owner's chat message; the same prompt is reproduced in `docs/NEXT_SESSION_PROMPT.md`.
