# Flex Query & Backtest Skill

Use when modifying `src/flex/`, `src/backtest/`, or the **History** dashboard tab.

---

## 0. Bootstrap — One-Time XML History Load

The dashboard only shows **🔄 Refresh via API** (365-day rolling).
To seed the initial 5-year history, load manually downloaded XML files.
This is a one-time operation; after that, quarterly API refreshes keep the data current.

### Steps

1. Portal → Reports → Flex Queries → find your `TradeHistory` query → hit ▶ run
2. Period: **Custom Date Range**, Format: **XML**
3. Run 6 times with these windows, saving each file to `data/master/`:

| Run | From | To | Save as |
|---|---|---|---|
| 1 | 2025-05-17 | 2026-05-16 | `flex_1.xml` |
| 2 | 2024-05-17 | 2025-05-16 | `flex_2.xml` |
| 3 | 2023-05-17 | 2024-05-16 | `flex_3.xml` |
| 4 | 2022-05-17 | 2023-05-16 | `flex_4.xml` |
| 5 | 2021-05-17 | 2022-05-16 | `flex_5.xml` |
| 6 | 2020-05-17 | 2021-05-16 | `flex_6.xml` |

4. Run from Python (no dashboard needed):

```python
from pathlib import Path
from src.flex.fetch import load_xml
from src.flex.parse import normalize, mask_accounts
from src.dashboard.settings import get_settings

s = get_settings()
acct_map = {s.us_account.get_secret_value(): "US", s.sg_account.get_secret_value(): "SG"}
master = Path("data/master")

df = mask_accounts(normalize(load_xml(master)), acct_map)
df.to_pickle(master / "flex_trades.pkl")
print(f"Saved {len(df):,} rows")
```

5. After saving, use the dashboard **🔄 Refresh via API** button quarterly.

---

## 1. IBKR Flex Query — Portal Setup (one-time)

### Step 1 — Create the Trade History Query

Use **Activity Flex Query** (NOT Trade Confirmation — that type has no date range and only covers recent confirms).
Activity produces `Trade` XML elements; Trade Confirmation produces `TradeConfirm`.

1. Portal → Reports → Flex Queries → Activity → create query (name e.g. `TradeHistory`)
2. Set **Period** to `Last 365 Calendar Days` (used by the API refresh path)
3. Format = **XML**
4. Under **Sections**, expand **Trades** and enable exactly these 19 fields
   (use the portal label name in the left column):

| Portal Label | XML column | Notes |
|---|---|---|
| Account ID | `accountId` | |
| Asset Class | `assetCategory` | portal says "Asset Class", XML is `assetCategory` |
| Symbol | `symbol` | |
| Underlying Symbol | `underlyingSymbol` | |
| Put/Call | `putCall` | "P" or "C" |
| Strike | `strike` | |
| Expiry | `expiry` | YYYYMMDD in XML |
| Multiplier | `multiplier` | 100 for equity options |
| Quantity | `quantity` | |
| Trade Price | `tradePrice` | |
| Proceeds | `proceeds` | |
| IB Commission | `ibCommission` | |
| Net Cash | `netCash` | |
| Realized P&L | `realizedPnl` | use this; "FIFO P&L Realized" not available in portal |
| Date/Time | `dateTime` | YYYYMMDD;HHMMSS in XML |
| Trade Date | `tradeDate` | YYYYMMDD in XML |
| Open/Close Indicator | `openCloseIndicator` | "O" open / "C" close |
| Buy/Sell | `buySell` | |
| Currency | `currency` | |

6. Format: **XML** (not CSV — structure is richer)
7. Save → copy the **Query ID** shown in the URL or query list

### Step 2 — Enable Flex Web Service Token

1. Menu: **Settings → Account Settings → Flex Web Service**
2. Click **Generate** (or reveal) your token
3. Copy the token value

### Step 3 — Add to `.env`

```
TOKEN=your_flex_web_service_token
TRADES_FLEXID=your_single_query_id
```

Only needed for API refresh (Path 2). Not required if using manual XML load only.
`settings.py` exposes these as `settings.token` and `settings.trades_flexid`.

### Step 4 — Verify

```bash
uv run python -c "
from src.dashboard.settings import get_settings
s = get_settings()
print('token set:', bool(s.token.get_secret_value()))
print('flexid set:', bool(s.trades_flexid.get_secret_value()))
"
```

---

## 2. Module Map

| File | Purpose |
|---|---|
| `src/flex/fetch.py` | `download_trades(token, qid)` — thin wrapper around `ib_async.FlexReport` |
| `src/flex/parse.py` | `normalize(df)` — fix date types, add `pnl` alias, `assetCategory` alias |
| `src/flex/analyze.py` | `symbol_performance()`, `dte_distribution()`, `strategy_recommendation()` |
| `src/backtest/greeks.py` | `black_scholes()`, `implied_vol()`, `greeks_table()` |
| `src/backtest/strategy.py` | `simulate()`, `covered_call()`, `cash_secured_put()`, `bull_put_spread()`, `iron_condor()` |
| `src/backtest/score.py` | `score_from_trades()` → `BacktestScore` (adapted Backtest Expert) |

---

## 3. Key Patterns

### Download and cache

```python
from src.dashboard.settings import get_settings
from src.flex.fetch import download_trades
from src.flex.parse import normalize
from pyprojroot import here

settings = get_settings()
df = download_trades(
    token=settings.token.get_secret_value(),
    query_id=settings.trades_flexid.get_secret_value(),
    save_path=here() / "data" / "master" / "flex_trades.pkl",
)
df = normalize(df)
```

### Greeks calculation

```python
from src.backtest.greeks import black_scholes, implied_vol

g = black_scholes(S=450.0, K=455.0, T=30/365, r=0.053, sigma=0.22, option_type="C")
# g = {"price": ..., "delta": ..., "gamma": ..., "theta": ..., "vega": ..., "rho": ...}
# Theta is per calendar day. Vega/Rho are per 1% move.

iv = implied_vol(market_price=3.50, S=450.0, K=455.0, T=30/365, r=0.053, option_type="C")
```

### Strategy P/L simulation

```python
from src.backtest.strategy import covered_call, cash_secured_put

result = covered_call(und_price=450.0, strike=460.0, premium=3.50)
# result.max_profit, result.max_loss, result.breakevens, result.pnl_at_expiry (Series)
```

### Backtest scoring

```python
from src.backtest.score import score_from_trades

score = score_from_trades(df_normalized, symbol="AAPL")
# score.composite (0-100), score.verdict ("DEPLOY"/"REFINE"/"ABANDON"), score.red_flags
```

---

## 4. IBKR Flex XML Field Names (Activity Query → Trade elements)

| XML attribute | Portal label | Values / format |
|---|---|---|
| `assetCategory` | Asset Class | "OPT", "STK", "FUT" |
| `underlyingSymbol` | Underlying Symbol | e.g. "AAPL" |
| `putCall` | Put/Call | "P" or "C" |
| `strike` | Strike | numeric |
| `expiry` | Expiry | `YYYYMMDD` |
| `multiplier` | Multiplier | 100 for equity options |
| `quantity` | Quantity | signed (negative = sold) |
| `tradePrice` | Trade Price | numeric |
| `proceeds` | Proceeds | signed |
| `ibCommission` | IB Commission | negative |
| `netCash` | Net Cash | proceeds + commission |
| `realizedPnl` | Realized P&L | closing trades only |
| `openCloseIndicator` | Open/Close Indicator | "O" open / "C" close |
| `buySell` | Buy/Sell | "BUY" / "SELL" |
| `dateTime` | Date/Time | `YYYYMMDD;HHMMSS` — normalize() strips ";" |
| `tradeDate` | Trade Date | `YYYYMMDD` |
| `currency` | Currency | "USD", "GBP", etc. |

`parse.normalize()` handles all type conversions and adds a unified `pnl` column from `realizedPnl`.

---

## 5. Backtest Score Reference

| Dimension | Full score (25) | Threshold |
|---|---|---|
| Sample | 25 | ≥100 trades with good density |
| Expectancy | 25 | Profit factor ≥ 2.5 |
| Risk | 25 | Max drawdown < 15% |
| Robustness | 25 | ≥ 8 years tested |

**CRITICAL flags** → ABANDON regardless of composite score:
- < 30 trades
- Profit factor < 1.0
- Drawdown > 40%
- Test period < 3 years

---

## 6. Dashboard Integration

The **History** tab is `render_history()` in `app.py` (a `@st.fragment` — no `run_every`).
It uses `settings.token` / `settings.trades_flexid` directly — no separate env setup.
Flex data is saved to `data/master/flex_trades.pkl` (protected from Clear Data).

To add a new strategy to the P/L selector:
1. Implement in `src/backtest/strategy.py` following the `simulate()` pattern
2. Add a radio option in the `_strat` selector in `render_history()`
3. Wire the new function call in the `_result = ...` block
