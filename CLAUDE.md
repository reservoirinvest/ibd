# CLAUDE.md — ibd project guide

Hands-on quant Director repo. Optimize for clarity, speed, minimal token spend.

## CRITICAL — IBKR Connection Stability

> **Any change to `src/dashboard/ib_client.py` or `app.py` startup requires a connection test.**
> After editing, run the dashboard and confirm the header shows 🟢 LIVE within 15 s.
> Verify: `uv run python -c "from src.dashboard import settings, ib_client; print('ok')"`
> The IBKR asyncio connection is fragile by design (one CID, one daemon thread, one event loop).
> Breaking it silently shows as a permanent 🔴 DISCONNECTED with no Python exception.

## Layout

| Path | Purpose |
|---|---|
| `src/build.py`, `src/classify.py`, `src/derive.py`, `src/execute.py`, `src/analyze.py` | Batch pipeline. **Do not refactor unless asked.** |
| `src/fetch_ohlc.py` | OHLC update runner (called by dashboard button). |
| `src/clear.py` | Data cleanup script (preserves `data/master/`). |
| `scripts/profile_hotpaths.py` | Hot-path profiler. `uv run python scripts/profile_hotpaths.py` |
| `app.py` | Streamlit dashboard entrypoint (IB Monitor). |
| `src/dashboard/` | `settings`, `ib_client`, `state`, `risk`, `formatting`, `ohlc`. |
| `config/snp_config.yml` | PORT, CID, CURRENCY, MINCUSHION, MAX_DTE, etc. |
| `.env` | Secrets: `US_ACCOUNT`, `SG_ACCOUNT`. |
| `data/master/` | Protected — never deleted by Clear Data or `src/clear.py`. |
| `.claude/skills/dashboard/SKILL.md` | IBKR + Streamlit patterns. **Read before any IB work.** |

## Run

```bash
uv run streamlit run app.py --server.address=127.0.0.1
uv run ruff check .
uv run python -c "from src.dashboard import settings, ib_client, state, risk, ohlc; print('imports ok')"
```

## IBClient — startup & singleton

- One daemon thread owns the asyncio loop. All UI reads go through `client.snapshot()`.
- **`@st.cache_resource` wraps `client.start()`** — the only safe way to start once per Streamlit server process:
  ```python
  @st.cache_resource(show_spinner=False)
  def _start_ib_client():
      c = get_client()
      c.start(settings)
      return c
  client = _start_ib_client()
  ```
  Do **not** call `client.start()` at bare module level — concurrent Streamlit reruns race past the thread guard (observed: 2–3 simultaneous starts → error 326 cascade).

- **`ib_async` imported at module level** — never lazy/inside coroutine. Circular import raises `ImportError` if first imported inside a running asyncio loop.
- **Event loop created inside daemon thread** (`_run_loop`) — Windows ProactorEventLoop IOCP handles are thread-affine; creating in main thread silently breaks coroutines.
- **`logger.add()` guarded by `_log_sink_added`** (class-level, inside `_lock`) — one file sink per process.
- Two named constants control timing: `_UNFREEZE_DELAY_SECS = 5.0` (CID release), `_BOOTSTRAP_SETTLE_SECS = 3.0` (TWS push wait).
- **`_health_gen`** — incremented on each successful connect; each `_health_check_loop` coroutine exits when its captured `gen` no longer matches, preventing zombie loops after reconnect.

### Position store (hot path)

- Source of truth: `_positions_store: dict[tuple[int, str], dict]` — key is `(conId, account)`.
- `_on_portfolio` writes O(1); `_on_position` deduplicates with `key in self._positions_store` (never a boolean-mask scan across the DataFrame).
- After each write: `self._snap.positions = pd.DataFrame(self._positions_store.values())` — one allocation, never `pd.concat`.
- Zero-position event (`item.position == 0`): `_positions_store.pop(key, None)` removes the row cleanly.

## SUBPROCESS / CID RULES

**Dashboard owns CID=10. `client.freeze()` must come BEFORE `subprocess.Popen()`. Order is non-negotiable.**

```python
# Button handler — _auto_unfreeze() helper in render_orders() implements this pattern:
st.session_state["frozen_for"] = "mytask"   # "derive" | "ohlc" | "execute"
client.freeze()                              # disconnect BEFORE spawning
proc = subprocess.Popen([sys.executable, "myscript.py"], ...)
st.session_state["mytask_proc"] = proc
# NO st.rerun() — run_every timer handles UI refresh

# Auto-unfreeze fires in next run_every cycle (see _auto_unfreeze in app.py):
#   client.unfreeze()   # 5-second delayed reconnect (_UNFREEZE_DELAY_SECS)
#   st.rerun()          # safe here — intentional reconnect cycle
```

- `PYTHONUTF8=1`, `PYTHONIOENCODING=utf-8` in subprocess env always.
- Even yfinance subprocesses freeze if they have an IBKR fallback.

## Batch scripts — IBKR connection rules

- **`derive.py` must open `ib` AFTER `classifed_results()`** — `chains_n_unds` opens CID=10 internally; holding it first → all internal connections fail (error 326).
- **No `ib = None` as first line** of functions accepting `ib: IB = None` — silently overrides the parameter. Use `if ib is None: ib = get_ib_connection()` correctly.
- **Empty DataFrame guard** — `get_volatilities_snapshot` returns `pd.DataFrame()` on failure; guard with `if not df.empty and 'symbol' in df.columns:` before `.apply(axis=1)`.

## risk.py — performance patterns

- **`greek_dollar_sums(df, pre_joined=True)`** — pass when positions have already gone through `_join_tickers()`; skips a redundant merge. Callers that already hold a joined frame must use this.
- **`cover_protect_gaps` inner loop** — pre-group options once before the stock loop: `{s: g for s, g in opt.groupby("symbol")}` then `opt_by_sym.get(sym, _empty_opt)`. The O(n²) `opt[opt.symbol == sym]` inside the loop was the 156 ms bottleneck.
- **Schema-preserving empty DataFrame** — use `df.iloc[0:0]` as a loop-default, not `pd.DataFrame()`. A bare `pd.DataFrame()` has no columns and crashes on `.position`, `.symbol`, etc. when code reaches it.
- **`_join_tickers`** — builds numpy arrays per ticker column via direct dict lookup, then `df.assign(...)`. Avoid re-introducing a list-of-dicts → `pd.DataFrame(rows)` → `merge()` pattern; that allocates an intermediate frame for every render tick.

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
- Symbol filters: `str.upper() == sym` — exact match for position filter; `str.upper().startswith(...)` for order filter.
- KPI strip: **8 metrics** — NLV, Opt Value, Cushion, Excess Liq, Maint Margin, Σ Δ, Σ Θ, Σ ν.
- Drop withstand: `ExcessLiquidity / |Σ Δ $| × 100%`. Dual US/US+SG columns only when ALL account view.
- Nav: `st.columns([2, 3, 1, 2])` — header | radio tabs | account selector | spacer. Spacer preserves right edge for native Streamlit Deploy/burger controls.
- Plotly: `hoverlabel={"bgcolor":"#1e2130","font_color":"#f1f5f9"}`.
- Option strike lines: `line_dash="dot"` always. BB bands + SMA: `hoverinfo="skip"`.
- Tables below charts: always render with conditional message when no data — never hide the section.
- IBKR tag names: `StockMarketValue` (not `StockValue`), `Leverage-S` (not `GrossLeverage`).
- **Expanders default to `expanded=False`** for heavy tables (Positions, Cover/Protect gaps) — avoids initial render cost and visual noise.
- **`render_analysis()` run_every=60.0** — OHLC chart + position tables are expensive; 2 s and 10 s intervals cause visible dimming every refresh. `header()` and `kpi_strip()` stay at 2 s (lightweight HTML only).
- **Cover/Protect gaps column order**: `cover_strike | mkt_px | protect_strike` — target call strike, market price, target/existing put strike side by side. Unrealized P&L merged from positions by symbol.
- **`protect_strike` in `cover_protect_gaps()`**: shows existing long option strikes when held; shows `~{target}` (computed as `mkt_px − cover_std_mult × σ`) when no protection exists. Always call with `protect_me=True` from the dashboard so the column is always present.

## Fragment scope pitfall — never shadow module-level constants

Local variables inside `if` branches of a `@st.fragment` function shadow module-level names. When the branch doesn't execute on a subsequent run_every tick the local is unset, raising `UnboundLocalError` at the reference site.

```python
# BAD — _EXECUTE_LOG shadowed locally; crashes when branch is skipped
if st.session_state.get("_exec_confirmed"):
    _EXECUTE_LOG = _here() / "log" / "execute.log"   # ← shadows module-level
    ...
_render_log_expander("...", _EXECUTE_LOG, ...)   # UnboundLocalError on next tick

# GOOD — use the module-level constant directly; no local re-definition
_EXECUTE_LOG = _here() / "log" / "execute.log"   # module level only
...
if st.session_state.get("_exec_confirmed"):
    _EXECUTE_LOG.parent.mkdir(...)
    ...
_render_log_expander("...", _EXECUTE_LOG, ...)   # always defined
```

## st.rerun() inside fragments

**Safe**: after `client.unfreeze()` in `_auto_unfreeze()`; filter-clear buttons (pop session state keys).
**Never**: after `client.freeze()` + `Popen()`, after file ops (Clear Data), after config saves.
The `run_every` timer handles those automatically.

## Config

`_CFG_KEYS` list in `app.py` is the single registry for all 19 YAML ↔ session_state mappings.
Adding a config key requires one entry there — `_init_cfg_state`, `_save_cfg`, `_cfg_dirty` all iterate it.

## Testing patterns

- **`MagicMock()` base for IBClient test doubles** — never `AsyncMock()`. An `AsyncMock` base makes every attribute access (including sync ones like `managedAccounts()`) return a coroutine, causing `TypeError: 'coroutine' object is not iterable`. Use `MagicMock()` and set async attributes explicitly: `mock_ib.reqPositionsAsync = AsyncMock(...)`.
- **Wire-handler `+=` gotcha** — `ib.updatePortfolioEvent += handler` reassigns `ib.updatePortfolioEvent` to the return value of `__iadd__` (a new `MagicMock`). Asserting on the *original* attribute checks the wrong object. Use a custom event class with a `handlers: list` and `__iadd__` that appends in-place.
- **Logger isolation in timing tests** — patch `ibc.logger` with a no-op `Mock()` for any `_on_portfolio` / `_on_position` performance tests. Loguru's DEBUG sink writes to stderr and adds ~5 ms per call, which inflates measurements and can cause throughput assertions to fail.

## Conventions

- Python 3.12, `from __future__ import annotations`, type hints.
- **`loguru` lazy interpolation** — always `logger.info("x={}", val)`, never `logger.info(f"x={val}")`. f-strings evaluate eagerly and bypass loguru's level guard. Use `self._mask(acct)` for account numbers — never log raw account IDs. `encoding="utf-8"` in `logger.add()` (Windows cp1252 crashes on unicode). In benchmark scripts: `_loguru.remove()` at module top to suppress stderr I/O that skews timings.
- `ruff` line-length 100. Money at boundaries: `Decimal`; DataFrames: `float64`.
- `width="stretch"` not `use_container_width` (deprecated in Streamlit).
- `@st.fragment` for config panel and widget groups that must not trigger full reruns.
- Never print, log, or commit `.env` contents. Dashboard binds to `127.0.0.1` only.

## Talk-to-Data Feature

- Data files are pickled in ./data/ subdirectory
- LLM integration should support Claude (default), with options to switch models
- Streamlit dashboard loads and caches pickled data
- Minimize token usage by loading only relevant data subsets
