# CLAUDE.md — ibd project guide

Hands-on quant Director repo. Optimize for clarity, speed, minimal token spend.

## Layout

| Path | Purpose |
|---|---|
| `build.py`, `classify.py`, `derive.py`, `execute.py`, `analyze.py` | Batch pipeline. **Do not refactor unless asked.** |
| `fetch_ohlc.py` | OHLC update runner (called by dashboard button). |
| `app.py` | Streamlit dashboard entrypoint (IB Monitor). |
| `src/dashboard/` | `settings`, `ib_client`, `state`, `risk`, `formatting`, `ohlc`. |
| `config/snp_config.yml` | PORT, CID, CURRENCY, MINCUSHION, MAX_DTE, etc. |
| `.env` | Secrets: `US_ACCOUNT`, `SG_ACCOUNT`. |
| `data/master/` | Protected — never deleted by Clear Data or `clear.py`. |
| `.claude/skills/dashboard/SKILL.md` | IBKR + Streamlit patterns. **Read before any IB work.** |

## Run

```bash
uv run streamlit run app.py --server.address=127.0.0.1
uv run python fetch_ohlc.py
uv run ruff check .
uv run python -c "from src.dashboard import settings, ib_client, state, risk, ohlc; print('imports ok')"
```

## IBClient — startup & singleton

- One daemon thread owns the asyncio loop. All UI reads go through `client.snapshot()`.
- **`@st.cache_resource` wraps `client.start()`** — the only safe way to start once per Streamlit server process (survives hot-reload + concurrent reruns without double-connecting):
  ```python
  @st.cache_resource(show_spinner=False)
  def _start_ib_client():
      c = get_client()
      c.start(settings)
      return c
  client = _start_ib_client()
  ```
  Do **not** call `client.start()` at bare module level — concurrent Streamlit reruns will all see the module execute and race past the thread guard (observed: 2–3 simultaneous starts → error 326 cascade).

- **`ib_async` imported at module level** — never lazy/inside coroutine. Circular import (`__init__` ↔ `contract.py`) raises `ImportError` if first imported inside a running asyncio loop.
- **Event loop created inside daemon thread** (`_run_loop`) — Windows ProactorEventLoop IOCP handles are thread-affine; creating in main thread then running in daemon silently breaks coroutines.
- **`logger.add()` guarded by `_log_sink_added`** (class-level, inside `_lock`) — one file sink per process.
- `start()` uses thread-liveness + `ident is None` guard for dead-thread restart. `st.cache_resource` is the primary idempotency layer; the guard is a safety net for thread-crash restarts only.

## SUBPROCESS / CID RULES

**Dashboard owns CID=10. `client.freeze()` must come BEFORE `subprocess.Popen()`.**

```python
# Button handler pattern — order is non-negotiable:
st.session_state["frozen_for"] = "mytask"   # "derive" | "ohlc"
client.freeze()                              # disconnect BEFORE spawning
proc = subprocess.Popen([sys.executable, "myscript.py"], ...)
st.session_state["mytask_proc"] = proc
# NO st.rerun() — run_every timer handles UI refresh

# Auto-unfreeze in run_every cycle:
if client.is_frozen() and proc.poll() is not None \
        and st.session_state.get("frozen_for") == "mytask":
    client.unfreeze()   # 5-second delayed reconnect (IBKR CID release time)
    st.session_state.pop("frozen_for", None)
    st.rerun()          # safe here — intentional reconnect cycle
```

- `PYTHONUTF8=1`, `PYTHONIOENCODING=utf-8` in subprocess env always.
- Even yfinance subprocesses freeze if they have an IBKR fallback.

## Batch scripts — IBKR connection rules

- **`derive.py` must open `ib` AFTER `classifed_results()`** — `chains_n_unds` opens CID=10 internally; holding it first → all internal connections fail (error 326).
- **No `ib = None` as first line** of functions accepting `ib: IB = None` — silently overrides the parameter. Use `if ib is None: ib = get_ib_connection()` correctly.
- **Empty DataFrame guard** — `get_volatilities_snapshot` returns `pd.DataFrame()` on failure; guard before `.apply(axis=1)` and column access.

## asyncio rules

- Never call `asyncio.run` inside Streamlit reruns.
- Always use async methods inside coroutines: `reqAccountUpdatesAsync`, `reqPositionsAsync`, `reqAllOpenOrdersAsync`.
- Safe anywhere (non-blocking): `managedAccounts()`, `portfolio()`, `tickers()`, `reqMktData()`, `cancelMktData()`.

## OHLC data (`data/master/ohlc.pkl`)

- Primary fetch: yfinance (`asyncio.to_thread`, concurrency=20). Non-US: `ib_to_yf()` maps exchange → suffix.
- Pre-IBKR retry: bare-symbol yfinance failure retries with `.L` (handles LSE ETFs with `primaryExch=SMART`).
- IBKR fallback: CID=12. Dashboard must be frozen first.
- `_clean_df`: use `tz_convert(None)` on tz-aware DatetimeIndex — **not** `tz_localize(None)`.

## Dashboard UI conventions

- Money column config: `format="$%,.0f"` (commas mandatory). Never `"$%.0f"`.
- Symbol filters: `str.upper().startswith(...)` — strict prefix, not `str.contains`.
- KPI strip: **9 metrics** — NLV, Unreal P&L, Cash, Cushion, Excess Liq, Maint Margin, Σ Δ, Σ Θ, Σ ν.
- Drop withstand: `ExcessLiquidity / |Σ Δ $| × 100%`. Dual US/US+SG columns only when ALL account view.
- Nav: `st.columns([4, 1, 6])` for radio + account selector + spacer (left-justifies selector after Diagnostics).
- Plotly: `hoverlabel={"bgcolor":"#1e2130","font_color":"#f1f5f9"}`. Add `.hovertext .name { fill: #1e2130 !important; }` to CSS for trace-name badge readability.
- Option strike lines: `line_dash="dot"` always. BB bands + SMA: `hoverinfo="skip"`.
- Tables below charts: always render with conditional message when no data — never hide the section.
- IBKR tag names: `StockMarketValue` (not `StockValue`), `Leverage-S` (not `GrossLeverage`).

## st.rerun() inside fragments

**Safe**: auto-unfreeze after `client.unfreeze()`; filter-clear buttons (pop session state keys).
**Never**: after `client.freeze()` + `Popen()`, after file ops (Clear Data), after config saves.
The `run_every` timer handles those automatically.

## Conventions

- Python 3.12, `from __future__ import annotations`, type hints.
- `loguru` — never log secrets/account numbers. `encoding="utf-8"` in `logger.add()` (Windows cp1252 crashes on unicode).
- `ruff` line-length 100. Money at boundaries: `Decimal`; DataFrames: `float64`.
- `width="stretch"` not `use_container_width` (deprecated in Streamlit).
- `@st.fragment` for config panel and widget groups that must not trigger full reruns.
- Never print, log, or commit `.env` contents. Dashboard binds to `127.0.0.1` only.
