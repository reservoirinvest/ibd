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
| `positions` | `client.snapshot().positions` | Filtered to selected account |
| `greeks` | `greek_dollar_sums()` | Dollar-weighted delta/theta/vega |
| `metrics` | `_select_account_values()` | NLV, cash, buying power |
| `global_stats` | `data/master/flex_trades.pkl` | Total trades, win rate, profit factor |
| `per_symbol` | `data/master/flex_trades.pkl` | Per-symbol: trades, win%, pnl, strategies |
| `ohlc_stats` | `data/master/ohlc.pkl` | Last price, 20/90d return, pos52w, trend, hv20 |
| `orders_cover` | `data/df_cov.pkl` | Suggested covered-call orders |
| `orders_sow` | `data/df_nkd.pkl` | Suggested naked put/call orders |
| `orders_reap` | `data/df_reap.pkl` | Suggested buy-to-close orders |
| `orders_protect` | `data/df_protect.pkl` | Suggested protective put/call orders |

**Order keys**: each row has `symbol, right, strike, expiry, dte, qty, undPrice, xPrice` (plus type-specific columns). `_format_context` pre-computes the total dollar value (xPrice × qty × 100) and, for Cover, adds the capital-gain component so the LLM can answer max-earnings questions directly.

## Adding a new data source

1. Load the pickle/data in `_build_live_context()` using `_load_pkl()` or direct `pd.read_pickle()`
2. Add a context key (e.g. `"orders_sow"`)
3. Add a formatter block in `_format_context()` in `llm_query.py`
4. Update `_SYSTEM_PROMPT_TEMPLATE` to mention the new source

## Token optimization

- Haiku: simple queries (cheapest)
- Sonnet/Gemini: complex analysis
- DeepSeek: cost-effective alternative
- Orders DataFrames are capped at 100 rows via `.head(100)` before formatting
