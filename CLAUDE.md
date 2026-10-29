# Rules for Claude Code in this repo

This repo is an automated crypto futures trading bot. Real money can be lost.

## Never
- Read, print, log or ask for API keys or the contents of `.env`. Secrets live on the server only.
- Set `mode: live` in `config.yaml`, or set `I_UNDERSTAND_LIVE_TRADING`. The owner does this by hand.
- Raise any risk limit (`risk:` in `config.yaml`, defaults in `bot/risk.py`) without the owner's written approval in a pull request.
- Skip, disable or weaken a test to get CI green.
- Deploy to the server. Open a pull request and let the owner merge and deploy.
- Let an LLM place or size orders. The bot is rule-based; any LLM filter may only say "take it" or "skip it".

## Always
- Keep backtest, paper and live on the same strategy code (`bot/strategies/`).
- Include fees, 18% GST on fees, slippage, funding and 30% per-trade tax (no loss set-off) in every backtest.
- Record every strategy variant tried in `reports/trials.csv`.
- Report backtest results honestly, including when a strategy loses money.
- Run `python -m pytest` before committing.

## Layout
See README.md. Risk logic: `bot/risk.py`. Backtest engine: `backtest/engine.py`.
