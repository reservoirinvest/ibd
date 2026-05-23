---

name: talk-to-data

description: Build LLM integration for Streamlit dashboard to query pickled data

disable-model-invocation: true

---

## Implementation

Files:
- `src/dashboard/llm_query.py` — LLM backends (Claude / Gemini / DeepSeek) + prompt + context formatter
- `app.py` — `_build_live_context()` assembles the context dict; `_render_llm_chat()` renders the UI dock

## Context sources passed to every query

All context is built in `_build_live_context()` and formatted by `_format_context()` in `llm_query.py`.

| Context key | Source | Notes |
|---|---|---|
| `positions` | `client.snapshot().positions` → `data/df_pf.pkl` fallback | Filtered to selected account. Columns: symbol, secType, **right, strike, expiry**, position, marketPrice, marketValue, **avgCost**, delta, theta, vega |
| `positions_is_live` | bool | True = live IBKR, False = cached pickle |
| `positions_as_of` | str timestamp | Shown in header so AI qualifies stale data |
| `greeks` | `greek_dollar_sums()` | Dollar-weighted delta/theta/vega |
| `metrics` | `_select_account_values()` + override | NLV, cash, buying power. **Cushion** key overridden with `account_kpis()["cushion"]` (ExcessLiquidity/NLV as %) — raw IBKR tag uses different formula and reads ~0.01 |
| `global_stats` | `data/master/flex_trades.pkl` | Total trades, win rate, profit factor |
| `per_symbol` | `data/master/flex_trades.pkl` | Per-symbol: trades, win%, pnl, strategies |
| `trade_log` | `data/master/flex_trades.pkl` | Chronological CLOSED trades only — NOT current positions |
| `backtest_scores` | `data/master/flex_trades.pkl` | BacktestExpert: score 0–100, verdict, 4 sub-scores, flags |
| `ohlc_stats` | `data/master/ohlc.pkl` via `_cached_ohlc()` | Last price, 20/90d return, pos52w, trend, hv20 |
| `benchmark_prices` | `data/master/ohlc.pkl` via `_cached_ohlc()` | SPY & QQQ monthly closes from Jan 2020 |
| `nav_summary` | `data/master/flex_nav.pkl` | Month-end NAV full history (~2020 onward) |
| `cash_summary` | `data/master/flex_cash.pkl` | Deposits/withdrawals 2yr; dividends + interest by year |
| `open_orders` | `client.snapshot().orders` | Live pending IBKR orders |
| `orders_cover` | `data/df_cov.pkl` | Suggested covered-call orders |
| `orders_sow` | `data/df_nkd.pkl` | Suggested naked put/call orders |
| `orders_reap` | `data/df_reap.pkl` **filtered by live conId** | Suggested buy-to-close — only rows whose conId is in live positions |
| `orders_protect` | `data/df_protect.pkl` | Suggested protective put/call orders |

**Order keys**: each row has `symbol, right, strike, expiry, dte, qty, undPrice, xPrice` (plus type-specific columns). `_format_context` pre-computes the total dollar value (xPrice × qty × 100) and, for Cover, adds the capital-gain component so the LLM can answer max-earnings questions directly.

## Ghost-position anti-hallucination pattern

LLMs hallucinate "ghost" positions (e.g. recommending to close AZO P3480 when the portfolio holds AZO C3600) by mixing `orders_reap` market price data with `trade_log` closed-trade right/strike details. Four-layer defence:

1. **positions includes right/strike/expiry** — AI has one authoritative source for all current contract details
2. **orders_reap filtered by conId** — `df_reap = df_reap[df_reap["conId"].isin(live_conids)]` — stale derive.py entries can't reach AI
3. **Formatter header** — positions section opens with "AUTHORITATIVE: for options the right/strike/expiry columns below are definitive. Do NOT use trade_log to infer any option's right, strike, or expiry."
4. **System prompt** — trade_log bullet says "CRITICAL: this log is HISTORICAL — closed trades only"; orders_reap bullet says "every row is a currently open option — use Reap table (not trade_log) for current details"

## Cushion metric fix

IBKR's raw `Cushion` account value tag (`updateAccountValue`) uses a different internal formula and typically reads ~0.01, not the dashboard's 23.6%. In `_build_live_context()`:

```python
kpis = account_kpis(snap, min_cushion=settings.min_cushion, account=acct)
metrics = {k: str(v) for k, v in av.items()}
metrics["Cushion"] = (
    f"{kpis['cushion']:.1%} = ExcessLiquidity / NLV"
    f" (alert threshold {settings.min_cushion:.0%}; {'BREACH' if kpis['cushion_breach'] else 'OK'})"
)
```

System prompt also warns the AI not to recalculate Cushion or use GrossPositionValue as a leverage denominator.

## Positions fallback (df_pf.pkl)

`_PF_PICKLE = data/df_pf.pkl` — auto-saved on every live query. When positions are empty (offline), loaded via `else: try/except` (no TOCTOU `.exists()` check). Single `datetime.now()` call reused for both `_pf_meta["as_of"]` and `_pos_as_of` display string.

## OHLC cache

`benchmark_prices` uses `_cached_ohlc()` (@st.cache_data ttl=120), not raw `pd.read_pickle`. Avoids ~9.5 MB re-read on every Ask AI call.

## Adding a new data source

1. Load the pickle/data in `_build_live_context()` using `_load_pkl()` or `_cached_ohlc()`
2. Add a context key
3. Add a formatter block in `_format_context()` in `llm_query.py`
4. Update `_SYSTEM_PROMPT_TEMPLATE` to mention the new source

## Token optimization

- Haiku: simple queries (cheapest)
- Sonnet/Gemini: complex analysis
- DeepSeek: cost-effective alternative
- Orders DataFrames capped at 100 rows via `.head(100)` before formatting
- Positions text capped at 2000 chars
