# CLAUDE.md — ibd project guide

Hands-on quant Director repo. Optimize for clarity, speed, minimal token spend.

## Layout

| Path | Purpose |
|---|---|
| `build.py`, `classify.py`, `derive.py`, `execute.py`, `analyze.py` | Batch pipeline. **Do not refactor unless asked.** |
| `fetch_ohlc.py` | OHLC update runner (called by dashboard button). |
| `clear.py` | Delete all files in `data/` (skips subdirs). |
| `app.py` | Streamlit dashboard entrypoint (IB Monitor). |
| `src/dashboard/` | Dashboard modules: settings, ib_client, state, risk, formatting, ohlc. |
| `config/snp_config.yml` | Market knobs: PORT, CID, CURRENCY, MINCUSHION, MAX_DTE, etc. |
| `.env` | Secrets. Must include `US_ACCOUNT`, `SG_ACCOUNT`. |
| `data/` | Pickle files. `data/master/` is protected — never deleted by Clear Data. |
| `PLAN.md` | Architecture + design rationale. |
| `.claude/skills/dashboard/SKILL.md` | IBKR + Streamlit patterns. **Read before any IB work.** |

## Run

```bash
uv sync
uv run streamlit run app.py --server.address=127.0.0.1
uv run python fetch_ohlc.py        # update OHLCs standalone
uv run python clear.py             # clear data/ (keeps master/)
```

## Architecture — IBClient / Snapshot

- `IBClient` is a thread-safe singleton. One daemon thread owns the asyncio event loop.
- `Snapshot` is the read model — all UI reads go through `client.snapshot()`.
- `Snapshot.account_values` → `dict[str, dict[str, Decimal]]` keyed by account number.
  Use `risk._select_account_values(snap, account="")` for a flat dict (one or all summed).
- `Snapshot.positions` DataFrame — unique key `(conId, account)`. Columns include:
  `symbol`, `secType`, `currency`, `primaryExch`, `right`, `strike`, `expiry`,
  `position`, `avgCost`, `marketPrice`, `marketValue`, `unrealizedPNL`, `_contract`.
- `Snapshot.orders` — active open orders only (Cancelled/Filled are dropped).
- Account switcher (ALL/US/SG) is **generic**: dropdown only when 2+ real accounts;
  single account shows a label. Key: `st.session_state["acct_sel"]`.

## IBClient startup rules — CRITICAL

1. **`ib_async` must be imported at module level** — never under `TYPE_CHECKING` or lazily
   inside a coroutine. `ib_async` has a circular import (`__init__` ↔ `contract.py`) that
   manifests as `ImportError: cannot import name 'Contract' from partially initialized module`
   when first imported inside a running asyncio coroutine in a daemon thread.

2. **Event loop must be created inside the daemon thread** (`_run_loop`), not in `start()`.
   On Windows, `ProactorEventLoop`'s IOCP handle is thread-affine: creating it in the main
   thread then running `run_forever()` in a daemon thread silently prevents all coroutines
   from executing. Use `threading.Event` (`_loop_ready`) to synchronise: daemon sets it after
   `asyncio.new_event_loop()`, main thread waits before calling `run_coroutine_threadsafe`.

3. **Log file**: `log/dashboard.log` (daily rotation). The `logger.add()` call lives in
   `IBClient.start()` — idempotent, called once per process. Do not add file sinks elsewhere.

## SUBPROCESS / CID RULES — read before writing any background process

**The dashboard owns CID=10. No other process may connect with CID=10 without freezing first.**

```python
# Correct pattern for any subprocess that might touch IBKR:
st.session_state["frozen_for"] = "mytask"   # "derive" | "ohlc"
client.freeze()                              # disconnects CID=10
proc = subprocess.Popen([sys.executable, "myscript.py"], ...)
st.session_state["mytask_proc"] = proc

# Auto-unfreeze in the fragment's next run_every cycle:
if client.is_frozen() and proc.poll() is not None \
        and st.session_state.get("frozen_for") == "mytask":
    client.unfreeze()   # schedules reconnect after 5 s (IBKR CID release time)
    st.session_state.pop("frozen_for", None)
    st.rerun()
```

- Always pass `PYTHONUTF8=1` and `PYTHONIOENCODING=utf-8` in subprocess env.
- `_connecting` flag must be held `True` during the entire 5-second unfreeze delay.
- Even yfinance subprocesses should freeze if they have an IBKR fallback code path.

## Critical asyncio rules

- **Never** call `asyncio.run` inside Streamlit reruns — the daemon thread owns the loop.
- **Never** call blocking ib-async methods inside coroutines:
  - Use `await ib.reqAccountUpdatesAsync(acct)` not `ib.reqAccountUpdates(acct)`
  - Use `await ib.reqPositionsAsync()` not `ib.reqPositions()`
  - Use `await ib.reqAllOpenOrdersAsync()` not `ib.reqAllOpenOrders()`
- Safe anywhere: `ib.managedAccounts()`, `ib.portfolio()`, `ib.tickers()` (cache reads).
- Safe anywhere: `ib.reqMktData()`, `ib.cancelMktData()` (socket writes, non-blocking).

## OHLC data (data/master/ohlc.pkl)

- `src/dashboard/ohlc.py` manages a `dict[ibkr_symbol, DataFrame]` store.
- Primary fetch: yfinance (`asyncio.to_thread`, concurrency=20).
  Non-US: `ib_to_yf(symbol, exchange, currency)` maps LSE→`.L`, TSX→`.TO`, etc.
  Reads `primaryExch` + `currency` from `snap.positions` — these must be present.
- **Pre-IBKR retry**: when yfinance fails for a bare-symbol ticker (no suffix), the code
  retries with `.L` appended (e.g. `CSPX` → `CSPX.L`). This handles LSE ETFs where IBKR
  reports `primaryExch=SMART`, suppressing the normal suffix mapping.
  Implemented via `"yf_ticker"` override key in SymbolSpec + retry loop in `_run_async`.
- IBKR fallback: CID=12. Dashboard must be frozen first.
- `data/ohlc_symbols.json` is the handoff: written by button, read by subprocess.
- `data/master/` is never deleted by `clear.py` or the Clear Data button.
- `_clean_df` handles tz-aware DatetimeIndex (LSE returns Europe/London) via `tz_convert(None)`.
  Also strips yfinance MultiIndex columns. **Do not use `tz_localize(None)` on tz-aware index.**

## IBKR account value tag names (common mistakes)

| Wrong tag | Correct tag | Notes |
|---|---|---|
| `StockValue` | `StockMarketValue` | Stock position market value |
| `GrossLeverage` | `Leverage-S` | Short leverage ratio (~3×) |

Always verify tags against the Diagnostics "All account tags" expander.

## Dashboard UI conventions

- All money column configs: `format="$%,.0f"` (commas mandatory). Never `"$%.0f"`.
- All symbol text filters: `str.upper().startswith(...)` — strict prefix, not `str.contains`.
- KPI strip: **9 metrics** (NLV, Unreal P&L, Cash, Cushion, Excess Liq, Maint Margin, Σ Δ, Σ Θ, Σ ν). Init Margin moved to Diagnostics key values.
- Header row 2: market drop withstand = `ExcessLiquidity / |Σ Δ $| × 100%`. Dual US / US+SG columns **only** when ALL account view is selected (`acct == ""`); single metric otherwise.
- Tabs order: Positions | Orders | Analysis | Diagnostics.
- Plotly charts: always set `hoverlabel={"bgcolor":"#1e2130","font_color":"#f1f5f9"}` to prevent white-on-white tooltip.
- Plotly hover trace-name badge: rendered as `<rect>` (white) + `<text class="name">`. Global white-text CSS makes it unreadable. Fix: add `.hovertext .name { fill: #1e2130 !important; }` to the page-level `st.markdown` CSS block.
- Analysis chart BB bands + SMA: set `hoverinfo="skip"` — legend already labels them; hover is redundant noise.
- Analysis chart option strike lines: always `line_dash="dot"` (dotted) for all directions.

## st.rerun() inside fragments — CRITICAL

**Never call `st.rerun()` from a non-subprocess button handler inside a `@st.fragment`.**
`st.rerun()` from inside a fragment triggers a **full-page rerun**, which can race with the
IBKR connection manager and produce error 326 ("client id already in use").

Safe uses of `st.rerun()` inside fragments:
- Auto-unfreeze: after `client.unfreeze()` — intentional reconnect cycle.
- Filter clear buttons — these pop session state keys and must rerender the widget tree.

**Not safe**: calling `st.rerun()` after file operations (Clear Data) or config saves.
The fragment's `run_every` timer will refresh it within seconds automatically.

## Conventions

- Python 3.12, type hints, `from __future__ import annotations`.
- `loguru` for logging. **Never log secrets, account numbers, or `.env` contents.**
  Always pass `encoding="utf-8"` to `logger.add()` — Windows default (cp1252) crashes on unicode tqdm chars.
- `ruff` (line-length 100). `basedpyright` off-mode tolerated.
- Money at boundaries: `Decimal`; inside DataFrames: `float64`.
- Streamlit: `width='stretch'` — not `use_container_width` (deprecated).
- `@st.fragment` for config panel and any widget group that must not trigger full reruns.
- tqdm log display: `_log_lines(path, n)` collapses repeated bar lines to one slot each.
- `qualify_me` in `build.py`: wrap `ib.run(_qualify_batch(...))` **per-batch** in try/except with one 3-second retry; check `ib.isConnected()` after failure and break early. Never let a single outer `except` swallow all batches.

## Secrets — hard rules

1. Never print, log, or commit `.env` contents.
2. `pydantic-settings` reads secrets once at boot via `SecretStr` fields.
3. `Settings.currency` comes from `CURRENCY` in `snp_config.yml` (not an env secret).
4. Dashboard binds to `127.0.0.1` only.

## Verify

```bash
uv run ruff check .
uv run python -c "from src.dashboard import settings, ib_client, state, risk, ohlc; print('imports ok')"
```
