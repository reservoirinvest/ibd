# CLAUDE.md — ibd project guide

Hands-on quant Director repo. Optimize for clarity, speed, minimal token spend.

## Strategy — US account sowing

**Sow ONLY weekly S&P 500 options** (non-3rd-Friday expiries). Monthly-only symbols must never be sow candidates.
The weekly filter lives in `derive.py` at the sow stage (`sow_chains`). `df_chains.pkl` keeps all expiries — cover/protect need the full chain for monthly-assigned stock (e.g. AZO).
**Monthly-only symbol list**: `data/master/symbol_categories.pkl` — built by `scripts/update_symbol_categories.py` (chain gap <20 days = weekly). ~257 of 502 S&P 500 symbols are monthly-only. derive.py loads this at startup to exclude monthly-only from sow and to generate breakeven monthly CCs (saved as `df_monthly_cov.pkl`). Regenerate via History tab → **Identify Weeklies** after each build.
**SG account**: LSE stocks only, no option strategy — skip sow/cover/protect entirely for SG positions.

## CRITICAL — IBKR Connection

> Any edit to `src/dashboard/ib_client.py` or `app.py` startup: run dashboard, confirm 🟢 LIVE within 15 s.
> `uv run ibd`  (or `uv run python -c "from src.dashboard import settings, ib_client; print('ok')"` for a quick import check)
> Silent failure shows as permanent 🔴 DISCONNECTED with no Python exception.

Full IBClient patterns, pitfalls, and UI conventions → `.claude/skills/dashboard/SKILL.md`

## Layout

| Path | Purpose |
|---|---|
| `src/build.py`, `src/classify.py`, `src/derive.py`, `src/execute.py`, `src/analyze.py` | Batch pipeline — do not refactor unless asked. |
| `src/fetch_ohlc.py` | OHLC update runner (dashboard button). |
| `app.py` | Streamlit dashboard entrypoint. |
| `src/dashboard/` | `settings`, `ib_client`, `state`, `risk`, `formatting`, `ohlc`. |
| `config/snp_config.yml` | PORT, CID, CURRENCY, MINCUSHION, MAX_DTE, etc. |
| `.env` | Secrets: `US_ACCOUNT`, `SG_ACCOUNT`, `TOKEN` (Flex), `TRADES_FLEXID`. |
| `data/master/` | Protected — never deleted by Clear Data or `src/clear.py`. |
| `src/flex/` | `fetch`, `parse`, `analyze` — IBKR Flex Query download + trade history analysis. |
| `src/backtest/` | `greeks` (Black-Scholes), `strategy` (P/L sim), `score` (Backtest Expert). |
| `scripts/update_trades.py` | Standalone trade refresh: API + XML → `flex_trades.pkl`. |
| `scripts/diagnose_flex_api.py` | Diagnose Flex API connectivity and query config issues. |
| `scripts/update_symbol_categories.py` | Classify symbols as weekly/monthly from chain gap analysis → `data/master/symbol_categories.pkl`. Run after each build (or via History tab → Identify Weeklies). |

## Run

```bash
uv run ibd
uv run ruff check .
uv run python -c "from src.dashboard import settings, ib_client, state, risk, ohlc; print('imports ok')"
```

## IBClient startup

- `@st.cache_resource` wraps `client.start()` — the only safe entry point. Never call at bare module level (races → error 326).
- `ib_async` imported at module level — never lazy/inside coroutine (circular import, swallowed by asyncio).
- Event loop created inside daemon thread (`_run_loop`) — Windows IOCP is thread-affine; creating in main thread silently breaks coroutines.

## Subprocess / CID rules

**CID=10 belongs to the dashboard. `client.freeze()` before `subprocess.Popen()`. No exceptions.**

- `derive.py`: open `ib` AFTER `classifed_results()` — `chains_n_unds` holds CID=10 internally.
- Never write `ib = None` as first line of a function accepting `ib: IB = None` — silently overrides the parameter.
- `get_volatilities_snapshot` returns empty DataFrame on failure — guard `if not df.empty and 'symbol' in df.columns`.
- `PYTHONUTF8=1`, `PYTHONIOENCODING=utf-8` in subprocess env always.
- No `st.rerun()` after `freeze()` + `Popen()` — rely on `run_every` timer.

## asyncio

- Never `asyncio.run` inside Streamlit reruns.
- Async inside coroutines: `reqAccountUpdatesAsync`, `reqPositionsAsync`, `reqAllOpenOrdersAsync`.
- Non-blocking anywhere: `managedAccounts()`, `portfolio()`, `tickers()`, `reqMktData()`, `cancelMktData()`.

## OHLC (`data/master/ohlc.pkl`)

- Primary: yfinance (`asyncio.to_thread`, concurrency=20). Non-US: `ib_to_yf()` maps exchange → suffix.
- LSE retry: bare symbol → `.L` on failure (`primaryExch=SMART` ETFs).
- IBKR fallback: CID=12. Dashboard must be frozen first.
- `_clean_df`: `tz_convert(None)` on tz-aware index — never `tz_localize(None)`.

## Dashboard

- Fragment `if`-branch locals shadow module-level names → `UnboundLocalError` on next `run_every` tick. Never redefine module-level constants inside fragments.
- `st.rerun()` safe after `client.unfreeze()` and filter-clear buttons. Never after `freeze()+Popen()`, file ops, or config saves.
- `_CFG_KEYS` in `app.py` is the sole registry for all YAML ↔ session_state mappings. One entry there covers `_init_cfg_state`, `_save_cfg`, `_cfg_dirty`.
- `st.radio(horizontal=True)` inside `st.columns()` matches the nav CSS selector `[data-testid="stHorizontalBlock"]:has([data-testid="stRadio"])` — floats to top of page. Use `st.selectbox` instead.

## Restart vs. Rerun

| Changed | Action |
|---|---|
| `app.py` only | Click **Rerun** (or "Always rerun") in the browser |
| Any file in `src/` | **Full terminal restart** — Python caches imported modules; Streamlit rerun uses the stale import |
| `src/dashboard/ib_client.py` | Full restart — `@st.cache_resource` holds IBClient until process dies |

After restart, confirm 🟢 LIVE within 15 s.

## Conventions

- Python 3.12, `from __future__ import annotations`, type hints. `ruff` line-length 100.
- `logger.info("x={}", val)` — never f-strings (bypass level guard). `self._mask(acct)` for account numbers. `encoding="utf-8"` in `logger.add()`.
- Money at boundaries: `Decimal`; DataFrames: `float64`.
- `width="stretch"`, not `use_container_width` (deprecated in Streamlit).
- Never print, log, or commit `.env` contents. Dashboard binds to `127.0.0.1` only.

## Talk-to-Data (Ask AI)

Fixed dock visible on every tab. Implementation: `src/dashboard/llm_query.py` (backends + prompt + formatter) + `_build_live_context()` / `_render_llm_chat()` in `app.py`.

**Context sent on every query** (built in `_build_live_context`):

| Key | Source |
|---|---|
| `positions`, `greeks`, `metrics` | Live snapshot |
| `global_stats`, `per_symbol` | `data/master/flex_trades.pkl` — OPT-only closes (matches Symbol Deep-Dive win rates) |
| `trade_log` | Same pickle — chronological per-trade rows with exact date, symbol, PC, strike, expiry, qty, pnl |
| `ohlc_stats` | `data/master/ohlc.pkl` |
| `orders_cover/sow/reap/protect` | `data/df_cov.pkl`, `df_nkd.pkl`, `df_reap.pkl`, `df_protect.pkl` |
| `orders_monthly_cc` | `data/df_monthly_cov.pkl` — breakeven CCs for monthly-only held stocks |

**Win rate consistency**: `per_symbol` uses all closed OPT trades (including pnl=0 assignments) so trade count matches `trade_log`. `wr%` = pnl>0 / n. STK assignment trades excluded — their inclusion halves the apparent rate for wheel symbols.

**Conversation history**: rolling 5-turn window stored in `st.session_state["llm_history"]`. The `💬 N` button (next to `📋`) counts down from 5 and clears history on click. History is only appended on successful responses; errors don't corrupt the conversation.

**Adding new data sources**: load pickle in `_build_live_context()`, add a context key, add a formatter block in `_format_context()`, update `_SYSTEM_PROMPT_TEMPLATE`.
