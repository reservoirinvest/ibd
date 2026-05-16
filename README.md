# IB Monitor — IBKR Portfolio Dashboard

## Start

```bash
uv run streamlit run app.py --server.address=127.0.0.1
```

## What it does

Real-time portfolio risk monitor for IBKR accounts. Connects via IB Gateway/TWS and streams live positions, Greeks, and account metrics.

## Tabs

| Tab | Purpose |
|---|---|
| **Analysis** | Symbol OHLC chart (candlestick + Bollinger Bands + RSI + Volume), portfolio treemap, per-symbol position detail |
| **Orders** | Generate derive orders, fetch OHLCs, execute — each as a subprocess that freezes/unfreezes the IBKR connection |
| **History** | 5-year trade history from IBKR Flex Queries; per-symbol backtest scoring; Black-Scholes Greeks calculator; strategy P/L simulation |
| **Diagnostics** | Raw account values, connection health, open orders |

## Configuration

Edit `config/snp_config.yml` for PORT, CID, MINCUSHION, MAX_DTE, and strategy parameters.
Set secrets in `.env`:

```
US_ACCOUNT=your_us_account_id
SG_ACCOUNT=your_sg_account_id
TOKEN=your_flex_web_service_token
TRADES_FLEXID=your_activity_flex_query_id
```

`TOKEN` and `TRADES_FLEXID` are required for the History tab → **🔄 Refresh via API** button.
See `.claude/skills/flex-backtest/SKILL.md` for portal setup and the one-time XML bootstrap process.

## Batch pipeline (run from project root)

| Command | Purpose |
|---|---|
| `uv run python src/build.py` | Fetch qualified contracts and option chains |
| `uv run python src/derive.py` | Generate optimal option orders |
| `uv run python src/execute.py` | Execute derived orders in IBKR |
| `uv run python src/analyze.py` | Portfolio analysis |
| `uv run python src/fetch_ohlc.py` | Update OHLC history |
| `uv run python src/clear.py` | Clear data files (preserves `data/master/`) |

Add `--debug` to any script to show DEBUG output in terminal. DEBUG always goes to `log/<script>.log`.

## Checks

```bash
uv run ruff check .
uv run pytest tests/ -q
uv run python -c "from src.dashboard import settings, ib_client, state, risk, ohlc; print('dashboard ok')"
uv run python -c "from src.flex import fetch, parse, analyze; from src.backtest import greeks, strategy, score; print('flex/backtest ok')"
```

## Requirements

- IBKR Gateway or TWS running with API enabled on the port in `snp_config.yml` (default 1300)
- `127.0.0.1` in the TWS trusted IP list
- Python 3.12 · `uv` package manager
