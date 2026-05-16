---
name: project-flex-backtest
description: Flex Query trade history pipeline + backtest modules added to ibd project
metadata:
  type: project
---

Implemented IBKR Flex Query pipeline + options backtest framework, wired into a new History dashboard tab.

**Why:** User wants 5-year trade history analysis, symbol-specific strategy design, Greeks calculation, and backtesting — all using free tools (IBKR Flex + yfinance, no Alpaca/FMP).

**How to apply:** When touching `src/flex/`, `src/backtest/`, or the History tab in `app.py`, refer to `.claude/skills/flex-backtest/SKILL.md` for field names, patterns, and portal setup steps.

Key facts:
- `ib_async.FlexReport` is already installed — use it, don't re-implement HTTP polling
- `settings.token` (env: TOKEN) = Flex web service token
- `settings.trades_flexid` (env: TRADES_FLEXID) = Trade history query ID
- Flex trades saved to `data/master/flex_trades.pkl` (protected from Clear Data)
- `parse.normalize()` converts IBKR's `dateTime` format ("YYYYMMDD;HHMMSS") and adds `pnl` alias for `fifoPnlRealized`
- Greeks: `black_scholes()` returns theta per calendar day; vega and rho per 1% move
- Backtest scoring: DEPLOY ≥70, REFINE 40-69, ABANDON <40 or any CRITICAL flag
- Strategy P/L: `Leg.quantity > 0` = long (paid premium), `< 0` = short (received)

Related: [[feedback_ib_client_start_idempotency]], [[project_weekly_options_rule]]
