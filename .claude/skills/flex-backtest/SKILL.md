# Flex Query & Backtest Skill

Use when modifying `src/flex/`, `src/backtest/`, or the **History** dashboard tab.

---

## 0. Flex Pickles — Load Paths and Safety Rules

Three pickles in `data/master/`, all **gitignored** (contain raw account IDs). XMLs are the only backup.

| Pickle | Source topic | Built by |
|---|---|---|
| `flex_trades.pkl` | `Trade` | 🔄 Update Trades (API + XML) or `scripts/update_trades.py` |
| `flex_cash.pkl` | `CashTransaction` | 🔄 Update Trades (API + XML) |
| `flex_nav.pkl` | `EquitySummaryByReportDateInBase` | 🔄 Update Trades (XML only) |

**XML naming:** Year-named files — `2021.xml`, `2022.xml`, … `2026.xml` in `data/master/`. One per year; IBKR caps each portal query run at 365 days. All globs use `*.xml`.

**Update paths — all MERGE, never replace:**

| Method | When to use | How |
|---|---|---|
| **🔄 Update Trades** (dashboard) | Routine top-up | API (last 365 days) + any XML files → all three pickles |
| `scripts/update_trades.py` | CLI version, trades only | `--xml-only` or `--api-only` flags |
| **Rebuild all from XML** | After pkl corruption | click 🔄 Update Trades in dashboard |

`merge_into_pickle()` deduplication: IBKR ID columns (`tradeID`, `ibExecID`, `ibOrderID`) only when ≥80% non-null; falls back to composite natural key. `merge_nav_into_pickle()` deduplicates by `reportDate` keep-last. `merge_cash_into_pickle()` deduplicates on `(accountId, date, amount, currency, description)`.

### First-time full history load (Python, no dashboard needed)

1. Portal → Reports → Flex Queries → find your `TradeHistory` query
2. **Enable 3 sections**: Trades (19 fields), Cash Transactions, Equity Summary by Report Date in Base Currency
3. Run for each year with Period = Custom Date Range, Format = XML, save as `data/master/2021.xml` … `2026.xml`
4. Run from Python:

```python
from pathlib import Path
from src.flex.fetch import load_xml, load_cash_xml, load_nav_xml
from src.flex.fetch import merge_into_pickle, merge_cash_into_pickle, merge_nav_into_pickle
from src.flex.parse import normalize, normalize_cash, mask_accounts
from src.dashboard.settings import get_settings

s = get_settings()
acct_map = {s.us_account.get_secret_value(): "US", s.sg_account.get_secret_value(): "SG"}
master = Path("data/master")

# Trades
df_trades = mask_accounts(normalize(load_xml(master)), acct_map)
df_merged  = merge_into_pickle(df_trades, master / "flex_trades.pkl")
mask_accounts(df_merged, acct_map).to_pickle(master / "flex_trades.pkl")

# Cash
df_cash = mask_accounts(normalize_cash(load_cash_xml(master)), acct_map)
merge_cash_into_pickle(df_cash, master / "flex_cash.pkl")

# NAV (no mask needed — no accountId in output)
df_nav = load_nav_xml(master)
merge_nav_into_pickle(df_nav, master / "flex_nav.pkl")
print(f"Trades: {len(df_merged):,}  Cash: {len(df_cash):,}  NAV: {len(df_nav):,}")
```

5. After the initial load, click **🔄 Update Trades** for incremental quarterly top-ups.

### Date columns in Flex Activity XML

`tradeDate` is often `NaT` in Activity query exports — use `dateTime` instead.
`parse.normalize()` ensures `dateTime` is always populated.
`_build_history_context()` in `app.py` prefers `dateTime` over `tradeDate` for the trade_log.

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

5. **CRITICAL — Options sub-reports**: Under the Trades section there is an **Options** button
   for sub-reports (Symbol Summary, Asset Class, Order, **Execution**, Closed Lots, Wash Sales).
   **Leave ALL of these UNCHECKED.** Enabling "Execution" (or any other sub-report) changes the
   XML element tag from `<Trade>` to `<Execution>`, which `ib_async.FlexReport` cannot find —
   the download returns 0 rows with no error. Only the 19 field-level checkboxes should be on.
6. Format: **XML** (not CSV — structure is richer)
7. Save → copy the **Query ID** shown in the URL or query list

### Step 2 — Enable Flex Web Service Token

1. Menu: **Settings → Account Settings → Flex Web Service**
2. Click **Generate** (or reveal) your token
3. Copy the token value

**Token pitfalls:**
- The token must be generated by the **same IBKR user account** that owns the query. If you log in as a different user to regenerate, error 1004 ("token and query ID don't match") results.
- IBKR weekends (Sat/Sun): the API returns an empty `<Trades>` section — this is IBKR's data freeze, not a config problem. Re-run on Monday.
- Use `scripts/diagnose_flex_api.py` to get the raw XML structure and IBKR error codes.

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

| File | Key functions |
|---|---|
| `src/flex/fetch.py` | `download_trades`, `load_xml`, `merge_into_pickle` (trades); `download_cash_transactions`, `load_cash_xml`, `merge_cash_into_pickle` (cash); `load_nav_xml`, `merge_nav_into_pickle` (consolidated NAV) |
| `src/flex/parse.py` | `normalize` (trades), `normalize_cash` (cash), `normalize_nav` (NAV — drops zero/NaN rows), `filter_options`, `filter_closed`, `mask_accounts` |
| `src/flex/analyze.py` | `symbol_performance()`, `dte_distribution()`, `strategy_recommendation()` |
| `src/backtest/greeks.py` | `black_scholes()`, `implied_vol()`, `greeks_table()` |
| `src/backtest/strategy.py` | `simulate()`, `covered_call()`, `cash_secured_put()`, `bull_put_spread()`, `iron_condor()` |
| `src/backtest/score.py` | `score_from_trades()` → `BacktestScore` (adapted Backtest Expert) |

**`load_nav_xml()` detail:** extracts `EquitySummaryByReportDateInBase` from each `*.xml`, drops zero/NaN `total` rows (per-account placeholder rows in multi-account exports), then `groupby("reportDate")["total"].sum()` to get a single consolidated daily NAV.

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

**History tab controls:**
- **🔄 Update Trades** — rebuilds all three pickles: trades (API + XML), cash (API + XML), NAV (XML). Shows per-source log lines (✓/✗/—) in the success message.
- XML files must be year-named (`2021.xml` … `2026.xml`) in `data/master/`. Dashboard instructs user to use this naming in the help tooltip.

**Performance chart** (`_render_perf_chart` in `app.py`, first section in History tab):
- Requires `flex_trades.pkl`, `ohlc.pkl`, `flex_cash.pkl`, `flex_nav.pkl`
- "Consolidated" line (purple) = true daily NAV from `flex_nav.pkl`, rebased at display start
- "OPT P&L" line (blue dashed) = cumulative realized options P&L, rebased at display start
- SPY / QQQ benchmarks; Consolidated NAV bars on secondary y-axis
- Hover % values pre-formatted server-side (Plotly d3 format unreliable in unified hover)
