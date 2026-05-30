# IB Monitor — Developer Context for Claude

## Architecture

Single-file Streamlit app (`app.py`, ~4100 lines) + `src/` modules. Three tabs rendered by fragments: **Analysis**, **Orders**, plus an always-visible **Ask AI** dock.

```
app.py                  # All tab rendering + fragment functions + perf chart + LLM context
src/
  dashboard/            # Shared UI: ib_client.py, settings.py, state.py, risk.py, ohlc.py, formatting.py, llm_query.py
  flex/                 # Flex report pipeline: fetch.py (download/merge), parse.py (normalize), analyze.py (symbol perf)
  backtest/             # Backtest scoring: score.py
  build.py              # Fetch qualified contracts + option chains from IBKR
  derive.py             # Generate sow / cover / reap / protect orders
  execute.py            # Submit orders to IBKR via ib_async
  analyze.py            # Portfolio analysis (called from dashboard)
  fetch_ohlc.py         # OHLC history — yfinance primary, IBKR fallback
config/snp_config.yml   # PORT, CID, MINCUSHION, MAX_DTE, strategy params
data/master/            # Protected pickles — gitignored; XMLs are backup
scripts/                # Standalone refresh scripts (update_trades.py, update_symbol_categories.py, ...)
```

## Running

```bash
uv run ibd                          # start dashboard
uv run python src/build.py          # fetch contracts + chains
uv run python src/derive.py         # generate orders
uv run python src/execute.py        # submit orders
uv run python scripts/update_trades.py          # refresh flex pickles
uv run python scripts/update_trades.py --xml-only   # rebuild from local XMLs only
```

## Checks

```bash
uv run ruff check .
uv run pytest tests/ -q
uv run python -c "from src.dashboard import settings, ib_client, state, risk, ohlc; print('ok')"
uv run python -c "from src.flex import fetch, parse, analyze; from src.backtest import score; print('ok')"
```

---

## Critical patterns

### Fragments & session state
- Most UI components are `@st.fragment(run_every=Ns)`. **Never redefine a module-level constant inside a fragment** — the local var causes `UnboundLocalError` on the next tick.
- **Never call `st.rerun()` after `freeze()+Popen`** in a button handler — it races and throws error 326. Rely on the `run_every` timer instead.
- To reset an already-instantiated widget: `st.session_state.pop(key, None)`. Direct assignment raises `StreamlitAPIException`.

### IB client
- `IBClient` is `@st.cache_resource` — any change to `ib_client.py` requires a **full terminal restart**.
- Fragment init is also guarded by `_log_sink_added` class flag (prevents duplicate log sinks across reruns).

### Restart vs Rerun
- `src/` module changes → **full terminal restart**
- `app.py`-only changes → **browser Rerun** is enough
- `ib_client.py` always → **full restart**

### Plotly charts (ALL charts)
- `paper_bgcolor="rgba(0,0,0,0)"` + `plot_bgcolor="rgba(0,0,0,0)"` — transparent so Streamlit background shows through.
- Legend: **`bgcolor="rgba(0,0,0,0)"`** — never hardcode a light colour (`rgba(248,249,250,…)`) or `font.color` in the legend dict. This breaks dark mode. Let Streamlit's theme control legend text colour.

---

## Data files (data/master/ — all gitignored)

| File | Source | Contents |
|---|---|---|
| `flex_trades.pkl` | Flex XML/API — `Trade` topic | Full trade history (OPT + STK) |
| `flex_cash.pkl` | Flex XML/API — `CashTransaction` topic | Deposits, withdrawals, dividends, interest |
| `flex_nav.pkl` | Flex XML/API — `EquitySummaryByReportDateInBase` | Daily consolidated NAV (US + SG summed) |
| `ohlc.pkl` | yfinance / IBKR | OHLC for all symbols + SPY/QQQ benchmarks |
| `symbol_categories.pkl` | Derived from option chain expiry gaps | Weekly vs monthly-only (502 S&P 500 symbols) |

Always **merge, never replace** pickles — use `merge_cash_into_pickle` / `merge_into_pickle` / `merge_nav_into_pickle`. Never `pd.to_pickle()` directly.

---

## Performance chart (`_render_perf_chart` in app.py)

- **`bdays`** is `pd.bdate_range(start=t0, end=_today)` **extended to include today** even if today is a weekend/holiday — so live NLV shows on the chart on Saturdays.
- **Live NAV patch**: appends today's NLV from `client.snapshot()` to `_nav_series_full` when `flex_nav.pkl` is stale (i.e. last entry < today). This is the source of today's KPI NAV value.
- **`_compute_twr`**: internal reindex also extended to include non-business-day `end_ts`.
- **SGD deposits**: excluded from TWR math (no FX rates) but appear as deposit markers on the chart.
- **USD deposits**: stripped via `_compute_twr` cash-flow adjustment (`_cf_daily`).

---

## Business rules

- **derive.py**: open IB connection **after** `classified_results()`. Never `ib = None` at the top of a function body.
- **derive.py**: exclude symbols with active covering/sowing/protecting open orders (open_order_guard) — prevents duplicate orders for manual IBKR entries.
- **US account sow**: weekly S&P 500 only (skip 3rd-Friday expiry). SG account is exempt (LSE, no options).
- **Monthly-only symbols** (~257/502 S&P 500): excluded from weekly sow; derive.py generates breakeven monthly CCs for assigned monthly stock.
- **Win rate / profit factor**: must be computed on closed **OPT** trades only (`assetCategory == "OPT"`). Including STK trades halves win rate for wheel symbols.
- **Flex `_dedup`**: uses ID columns only when ≥80% of rows are non-null — prevents NaN collapse of historical data.

---

## LLM / Ask AI context

Built in `build_llm_context()` near bottom of `app.py`. Keys include: live positions, account metrics, Greeks, trade history (last 200 + global stats), backtest scores, OHLC stats + monthly prices, consolidated NAV history + KPIs (Sharpe, TWR, max drawdown), SPY/QQQ monthly closes, cash transactions, symbol classifications, suggested orders. Ghost-position multi-layer defence prevents stale positions from leaking into context.

---

## Fragment timer reference

| Fragment | `run_every` |
|---|---|
| header nav bar | 2 s |
| `_nav_time` (account header) | 5 s |
| `kpi_strip` | 10 s |
| `render_orders` | 10 s |
| `render_analysis` | no timer |
