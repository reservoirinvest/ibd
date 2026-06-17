# IB Monitor — Developer Context for Claude

## Architecture

Single-file Streamlit app (`app.py`, ~4700 lines) + `src/` modules. **Single linear page** (no tabs): header bar (incl. ☀️/🌙 theme toggle) → KPIs expander → Actions pipeline expander → Ask AI expander → one global filter bar → **three master collapsible sections**: **Performance** (`render_analysis`: Positions, Performance Dashboard, Deep-Dive, Gaps, Trade Analysis) → **Orders** (`render_orders`: Open Orders + Suggested Orders = Cover/Monthly CC/Sow→REFINE Overrides/Reap/Protect) → **Config** (`render_config_panel`, last & separate). The old NiceGUI `web/` package has been removed.

**Master sections** (`_master_section(title, key, default)`, bottom of `app.py`): Streamlit forbids expander-in-expander, so each master is a **button-toggled banner** (session_state `_master_<key>`, full `st.rerun()` on click) that conditionally renders its sub-expanders. Styled via CSS class `.st-key-btn_master_*` (clay banner). When a master is collapsed its inner fragment isn't called (timer pauses); the `_SuppressFragmentMissing` log filter swallows the resulting "fragment does not exist" warnings.

**REFINE Overrides** (`render_refine_overrides`, no longer a fragment) is called **inside `render_orders` directly under the Sow expander**. Its base table is sorted by **Override σ descending** (objective: push overrides higher). Controls row = Select All | "Change all overrides to:" + number_input (default 2.00) + ↵ button (sets every Override σ) | Clear All.

The **single global filter bar** (`render_filter_bar` / `apply_global_filter`, session_state keys `flt_symbol`/`flt_right`/`flt_state`/`flt_itm`) drives every panel: Positions, Open Orders, Cover/Sow/Reap/Protect tables, Gaps, Trade Analysis, Deep-Dive. Order tables are shown/hidden by state via `_order_table_visible`.

```
app.py                  # All page rendering + fragments (render_actions/orders/analysis/...) + perf chart + LLM context
.streamlit/config.toml  # Claude-palette theme (clay #D97757 primary, ivory bg); CSS in app.py adds serif headings
src/
  dashboard/            # Shared UI: ib_client.py, settings.py, state.py, risk.py, ohlc.py, formatting.py, llm_query.py, progress.py (rich)
  flex/                 # Flex report pipeline: fetch.py (download/merge), parse.py (normalize), analyze.py (symbol perf)
  backtest/             # Backtest scoring: score.py
  build.py              # Fetch qualified contracts + option chains from IBKR
  derive.py             # Generate sow / cover / reap / protect orders
  execute.py            # Submit orders to IBKR via ib_async
  analyze.py            # Portfolio analysis (called from dashboard)
  fetch_ohlc.py         # OHLC history — yfinance primary, IBKR fallback
config/snp_config.yml   # PORT, CID, MINCUSHION, MAX_DTE, strategy params
data/master/            # Pickles TRACKED in git (accountId masked to US/SG) = backup; raw year-named XMLs are gitignored
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
uv run python -c "from src.dashboard import settings, ib_client, state, risk, ohlc, progress; print('ok')"
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

## Data files (data/master/ — pickles TRACKED in git; raw XMLs gitignored)

The `data/master/*.pkl` files (and `data/df_chains.pkl`, `data/df_unds.pkl`, `data/symbols.pkl`) are **committed to git** and serve as the backup — their `accountId` is normalised to `US`/`SG`, so no raw account number is exposed. The raw year-named flex reports `data/master/*.xml` (which *do* contain real account numbers) are **gitignored** (`.gitignore: data/master/*.xml`) and are the local-only source for rebuilding the flex pickles. `data/t*.pkl` (traded_*) are also gitignored.

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
- **Focus date** (`FOCUS_DATE` in `snp_config.yml`, default 2025-08-08; dashboard date-picker in Config panel `cfg_focus_date`): anchors the Performance Dashboard display window + reference NAV (`_DEFAULT_PERF_START`/`_PERF_CHART_REF_DATE` = `focus_date()`). When `SCORE_FROM_FOCUS` (config + `cfg_score_from_focus` toggle) is on, `score_from_trades(df, sym, since=score_since())` restricts trade-history ABANDON/REFINE/DEPLOY scoring to trades on/after the focus date (excludes pre-focus mistakes). All four score call sites thread `since`. The **synthetic OHLC backtest is never focus-bound** — it always uses the full available history (~5y and growing in `data/master/`). `focus_date()`/`score_since()` read `cfg_focus_date`/`cfg_score_from_focus` (session) falling back to YAML; the Config fragment full-reruns on change so other fragments update live.
- **Covered call `covPrice`**: default floor is `max(avgCost + longPutCost, vol_based_price)` — prevents selling calls below cost basis while in the wheel cycle. Exception: if the stock has been held > `COV_AGED_DTE` days (default 180, config key `COV_AGED_DTE`) since the most recent STK BUY (from `flex_trades.pkl`), `covPrice = vol_based_price` only — prioritise income over cost recovery for long-held positions. Aged symbols are logged at INFO level.
- **Flex `_dedup`**: uses ID columns only when ≥80% of rows are non-null — prevents NaN collapse of historical data.

---

## Position & symbol state logic

### `df_pf` states — set by `classify_pf()` in `src/classify.py`

Each portfolio row gets exactly one state. Rules apply in order; later rules override earlier ones:

| State | Condition |
|---|---|
| `sowed` | Short option (`position < 0`) **and no STK position for that symbol** |
| `covering` | Short option AND symbol has long STK (short call) or short STK (short put) |
| `protecting` | Long option (`position > 0`) |
| `orphaned` | Long option with no STK position for that symbol |
| `zen` | STK with both a `covering` and a `protecting` option |
| `unprotected` | STK with `covering` but no `protecting` option |
| `uncovered` | STK with `protecting` but no `covering` option |
| `exposed` | STK with neither covering nor protecting option |
| `unclassified` | Anything unmatched |

**Key invariant:** `sowed` excludes symbols with any STK position — a covering option can never be `sowed`. The `covering` rule runs after to label those options, but it is never an override of `sowed`.

### `df_unds` states — set by `update_unds_status()` in `src/classify.py`

Each symbol in the universe gets one state, applied in priority order:

1. **Copied from STK row in df_pf** — `exposed`, `uncovered`, `unprotected`, `zen`
2. **`virgin`** — symbol not in df_pf at all
3. **`zen`** (override) — triggered by: pending covering+protecting orders, straddled position, active sowing order, uncovered+covering order, unprotected+protecting order, orphaned+de-orphaning order, sowed+reaping order
4. **`unreaped`** — df_pf state is `sowed` (short option, no STK) AND no active `reaping` open order

### Open order states — set by `classify_open_orders()` in `src/classify.py`

| State | Condition |
|---|---|
| `covering` | SELL option where symbol has a matching STK position |
| `protecting` | BUY option where symbol has a matching STK position |
| `sowing` | SELL option where symbol has no STK position |
| `reaping` | BUY option matching an existing short option in df_pf |
| `de-orphaning` | SELL option matching an existing option in df_pf |

### How `derive.py` consumes these states

- **Reap candidates**: `df_pf[state == "sowed"]` — directly usable, no secondary STK check needed
- **Open order guard** (`_oo_covering`, `_oo_sowing`, `_oo_protecting`): symbols excluded from each generation section to prevent duplicate orders
- **`unreaped` in df_unds**: drives reap order generation loop

---

## LLM / Ask AI context

Built in `build_llm_context()` near bottom of `app.py`. Keys include: live positions, account metrics, Greeks, trade history (last 200 + global stats), backtest scores, OHLC stats + monthly prices, consolidated NAV history + KPIs (Sharpe, TWR, max drawdown), SPY/QQQ monthly closes, cash transactions, symbol classifications, suggested orders. Ghost-position multi-layer defence prevents stale positions from leaking into context.

---

## Fragment timer reference

| Fragment | `run_every` |
|---|---|
| `header` (status bar) | 2 s |
| `render_actions` (action pipeline + freeze/subprocess state) | 5 s |
| `kpi_strip` (inside KPIs expander) | 10 s |
| `_master_orders` (wraps `render_orders`) | 10 s |
| `render_orders` | no timer (content-only) |
| `render_analysis` | no timer |

`render_actions` owns the whole action subprocess + freeze state machine (derive / execute / ohlc / backtest / trades). `render_orders` and `render_analysis` are now content-only. `_nav_time` (clock) was removed with the tabbed nav.

## Progress bars

`src/dashboard/progress.py` (`progress_bar` / `track`) replaces tqdm. On a TTY it renders a live `rich` bar; to a log file it writes plain `desc ━━━ NN%` lines that `app.py`'s `_RICH_PROG_RE` parser reads (drives `_ohlc_progress`). `execute.py` keeps stderr routing + the ib_async 399 suppression.
