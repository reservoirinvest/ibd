# CLAUDE.md — ibd project guide

Hands-on quant Director repo. Optimize for clarity, speed, minimal token spend.

## Strategy — US account sowing

**Sow ONLY weekly S&P 500 options** (non-3rd-Friday expiries). Monthly-only symbols must never be sow candidates.
The weekly filter lives in `derive.py` at the sow stage (`sow_chains`). `df_chains.pkl` keeps all expiries — cover/protect need the full chain for monthly-assigned stock (e.g. AZO).
**SG account**: LSE stocks only, no option strategy — skip sow/cover/protect entirely for SG positions.

## CRITICAL — IBKR Connection

> Any edit to `src/dashboard/ib_client.py` or `app.py` startup: run dashboard, confirm 🟢 LIVE within 15 s.
> `uv run python -c "from src.dashboard import settings, ib_client; print('ok')"`
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

## Run

```bash
uv run streamlit run app.py --server.address=127.0.0.1
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

## Conventions

- Python 3.12, `from __future__ import annotations`, type hints. `ruff` line-length 100.
- `logger.info("x={}", val)` — never f-strings (bypass level guard). `self._mask(acct)` for account numbers. `encoding="utf-8"` in `logger.add()`.
- Money at boundaries: `Decimal`; DataFrames: `float64`.
- `width="stretch"`, not `use_container_width` (deprecated in Streamlit).
- Never print, log, or commit `.env` contents. Dashboard binds to `127.0.0.1` only.

## Talk-to-Data

- Data files pickled in `./data/`. LLM: DeepSeek (default). Load only relevant subsets to minimize tokens.
