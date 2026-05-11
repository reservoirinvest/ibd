# IBKR Live Risk Dashboard — Plan

**Audience:** future maintainers + Claude.
**Scope:** multi-account local Streamlit dashboard streaming from IBKR via `ib-async`.

## 1. Goals

1. **Live** — sub-second updates from IBKR for held positions only (pacing-safe).
2. **Risk-first** — greeks, P&L, NLV/cushion, state exposure, DTE/reap candidates.
3. **Reuse** — leverage existing `build.py` / `classify.py` state logic, do not fork it.
4. **Cheap** — runtime never calls an LLM.
5. **Safe** — secrets via `.env` (never committed, never echoed).

## 2. Architecture

```
+------------------------------------------------------------------+
|  Streamlit UI (main thread)                                       |
|   app.py: header, KPI strip, Positions, Orders+Config,           |
|           Analysis, Diagnostics                                   |
|   reads Snapshot via ib_client.snapshot()                        |
|   account switcher (ALL/US/SG or single label) in session_state  |
+--------------------------+---------------------------------------+
                           | thread-safe read (shallow copy)
+--------------------------v---------------------------------------+
|  IBClient (daemon thread) — owns asyncio event loop              |
|   one IB() instance, CID=10 (from snp_config.yml)               |
|   freeze() / unfreeze() for subprocess handoff                   |
|   _connecting flag prevents duplicate retry coroutines           |
+--------------------------+---------------------------------------+
                           | socket
                    IBKR TWS / Gateway (port 1300 live / 1301 paper)
```

### Subprocess freeze pattern
Any subprocess that may connect to IBKR must call `client.freeze()` first (see CLAUDE.md).
`frozen_for` session-state key ("derive" | "ohlc") routes display and auto-unfreeze.

## 3. Module layout

```
ibd/
├── app.py                        # Streamlit entrypoint (IB Monitor)
├── fetch_ohlc.py                 # OHLC update runner
├── clear.py                      # delete data/ top-level files
├── CLAUDE.md / PLAN.md
├── config/snp_config.yml         # PORT, CID, CURRENCY, market knobs
├── .claude/skills/dashboard/SKILL.md
└── src/dashboard/
    ├── settings.py               # pydantic-settings (env + YAML); currency field added
    ├── ib_client.py              # IBClient singleton, Snapshot, freeze/unfreeze
    ├── state.py                  # portfolio state classification
    ├── risk.py                   # greeks, DTE buckets, reap candidates
    ├── formatting.py             # money/pct formatters
    └── ohlc.py                   # OHLC fetch, store, incremental update
```

Data on disk:
```
data/
├── symbols.pkl                   # 501 S&P500 Stock objects
├── df_chains.pkl, df_unds.pkl    # static batch inputs
├── df_cov.pkl, df_nkd.pkl, df_reap.pkl, df_protect.pkl, df_deorph.pkl  # derive outputs
├── ohlc_symbols.json             # handoff: button → fetch_ohlc.py subprocess
└── master/
    └── ohlc.pkl                  # dict[ibkr_symbol, DataFrame]; NEVER deleted by Clear Data
```

## 4. Data contract — Snapshot

| field | type | source |
|---|---|---|
| `as_of` | `datetime \| None` | wall clock at last write |
| `connected` | `bool` | `IB.isConnected()` |
| `account_values` | `dict[str, dict[str, Decimal]]` | `accountValueEvent` — outer key = acct |
| `positions` | `pd.DataFrame` | `portfolioEvent` + `positionEvent` |
| `tickers` | `dict[int, TickerSnap]` | `pendingTickersEvent` |
| `orders` | `pd.DataFrame` | bootstrap + `openOrderEvent` / `orderStatusEvent` |
| `errors` | `deque[tuple]` | `errorEvent` — last 50 |

`positions` columns: `account`, `conId`, `symbol`, `secType`, `currency`, `primaryExch`,
`right`, `strike`, `expiry`, `position`, `avgCost`, `marketPrice`, `marketValue`,
`unrealizedPNL`, `realizedPNL`, `_contract`.
(`currency` and `primaryExch` were added to fix OHLC LSE ETF symbol mapping.)

## 5. Connection / Bootstrap flow

```
IBClient.start()
  → daemon thread → asyncio.run_forever()
  → _connect_with_retry() [guarded by _connecting flag]
    → IB().connectAsync(host, port, clientId=10)
    → _wire_handlers()
    → _bootstrap():
        1. managedAccounts()                   [cache, safe]
        2. await reqAccountUpdatesAsync(acct)  [one per account]
        3. await reqPositionsAsync()
        4. asyncio.sleep(3.0)
        5. await reqAllOpenOrdersAsync()
        6. _resubscribe_market_data()
```

`_on_disconnect` only spawns a new retry when `not _connecting`.
`unfreeze()` holds `_connecting=True` during the 5-second delay so `_on_disconnect`
cannot race a parallel reconnect.

## 6. Multi-Account Model

- `US_ACCOUNT` / `SG_ACCOUNT` from `.env`. Either or both may be absent.
- `_REAL_ACCOUNTS` dict built from whichever are non-empty. "ALL" only added if 2+.
- Account switcher: selectbox when 2+ accounts, plain label when 1, nothing when 0.
- Position uniqueness: `(conId, account)`.
- For ALL view: tags summed via `_select_account_values(snap, account="")`.

## 7. Orders / Config tab

- **Generate Orders** button: freeze → subprocess `derive.py` (CID=10, needs exclusive) → auto-unfreeze.
- **Generate OHLCs** button: freeze → subprocess `fetch_ohlc.py` (yfinance primary, IBKR CID=12 fallback) → auto-unfreeze.
- **Clear Data** button: deletes all top-level files in `data/`; skips `data/master/`.
- Config panel is `@st.fragment` so toggle clicks don't trigger full page reruns (which would accidentally call `unfreeze()`).
- Save Config button disabled unless `_cfg_dirty()` detects a change vs. YAML on disk.

## 8. OHLC system (data/master/ohlc.pkl)

- `run_update()` in `ohlc.py`: load existing store → compute per-symbol fetch start →
  yfinance pass (async, semaphore=20) → IBKR fallback for misses → merge → save.
- Symbol handoff: button writes `data/ohlc_symbols.json` (S&P500 from `symbols.pkl` +
  portfolio from `snap.positions` using `primaryExch`/`currency` columns).
- `ib_to_yf(symbol, exchange, currency)`: hard overrides (BRK B→BRK-B) + exchange suffix
  table (LSE→.L, TSX→.TO, …) + currency fallback (GBP→.L, CAD→.TO, …).
- Min history: 548 days (~1.5 yr). Existing symbols only fetch from last known date.

## 9. Blocking vs Async ib-async Methods

| Method | Type | Safe in coroutine? |
|---|---|---|
| `ib.reqAccountUpdates(acct)` | BLOCKING | ❌ |
| `await ib.reqAccountUpdatesAsync(acct)` | async | ✅ |
| `ib.reqPositions()` | BLOCKING | ❌ |
| `await ib.reqPositionsAsync()` | async | ✅ |
| `ib.reqAllOpenOrders()` | BLOCKING | ❌ |
| `await ib.reqAllOpenOrdersAsync()` | async | ✅ |
| `ib.managedAccounts()`, `ib.portfolio()` | cache read | ✅ |
| `ib.reqMktData()`, `ib.cancelMktData()` | socket write | ✅ |

## 10. Risk metrics

| KPI | Formula |
|---|---|
| NLV | `account_values[acct]['NetLiquidation']` |
| Cushion | `ExcessLiquidity / NetLiquidation`; red if < `MINCUSHION` (0.20) |
| Unreal P&L | `account_values[acct]['UnrealizedPnL']` |
| Cash | `account_values[acct]['CashBalance']` |
| Σ Delta ($) | `Σ position × delta × multiplier × underlying_price` |
| Σ Theta ($/day) | `Σ position × theta × multiplier` |
| Σ Vega ($) | `Σ position × vega × multiplier` |
| Drop withstand | `ExcessLiquidity / |Σ Δ $| × 100%` — % market drop before excess liquidity → 0 |

State exposure via `classify_portfolio()`. DTE buckets: `[0–1, 2–5, 6–14, 15–30, 31+]`.
Reap candidates: `secType=='OPT' & position<0 & last <= REAPRATIO×avgCost & dte > MINREAPDTE`.

Drop withstand shows "N/A (short)" when net portfolio delta ≤ 0 (short book: rising market is
the risk). When both US+SG accounts are configured, header shows separate "US drop withstand"
and "US+SG drop withstand" columns.

## 11. Settings (snp_config.yml keys)

`PORT`, `PAPER`, `CID`, `CURRENCY`, `MINCUSHION`, `MAX_DTE`, `REAPRATIO`, `MINREAPDTE`,
`PROTECT_ME`, `COVER_STD_MULT`, `COVXPMULT`, `COVER_ME`, `SOW_NAKEDS`, `REAP_ME`, etc.

`Settings.currency` (str) is loaded from `CURRENCY` YAML key; shown in header.

## 12. Completed work

- [x] CID loaded from `snp_config.yml` via `merge_yaml()`
- [x] Dashboard renamed "IB Monitor"; Risk tab merged into Positions tab
- [x] Reconnect guard (`_connecting` flag; held during 5-s unfreeze delay)
- [x] `reqPositionsAsync` / `reqAccountUpdatesAsync` (blocking variants removed)
- [x] Multi-account: US/SG/ALL switcher, generic (dropdown only when 2+ accounts)
- [x] Orders tab with live events, filter bar, expander labels with counts + expected reward
- [x] `@st.fragment` isolation for config panel (prevents accidental unfreeze on toggle)
- [x] Generate Orders: freeze → derive.py → unfreeze; `PYTHONUTF8=1` for Windows
- [x] Generate OHLCs: freeze → fetch_ohlc.py → unfreeze; incremental, yfinance + IBKR fallback
- [x] `frozen_for` session state routes display and auto-unfreeze per subprocess
- [x] tqdm log collapse: `_log_lines(path, n)` overwrites bar slots in-place
- [x] Clear Data: inline deletion (no subprocess), protects `data/master/`
- [x] `currency` + `primaryExch` in position rows (fixes LSE ETF OHLC mapping)
- [x] `CURRENCY` in snp_config.yml → `Settings.currency` → header display
- [x] SKILL.md updated with subprocess/CID freeze rules
- [x] LSE ETF yfinance tz fix: `tz_convert(None)` in `ohlc._clean_df` (was `tz_localize`)
- [x] yfinance MultiIndex column fix: `droplevel(1)` when `isinstance(df.columns, pd.MultiIndex)`
- [x] Error 326 captured in `ib_client._connect_with_retry` except block → `snap.errors`
- [x] Currency shown as green bold badge in dashboard header title
- [x] KPI strip: Unreal P&L + Cash added after NLV (10 columns total)
- [x] All money columns use `$%,.0f` format (commas mandatory)
- [x] All symbol text filters use strict `startswith` prefix match (not `contains`)
- [x] Positions: ITM options filter checkbox (computed pre-filter on full DataFrame)
- [x] Orders tab: timestamp shown directly under each button; Clear Data in separate divider section
- [x] Diagnostics: 7 key account value metrics shown de-duplicated (CashBalance, StockMarketValue,
      AccruedDividend, AvailableFunds, BuyingPower, Leverage-S, UnrealizedPnL); full raw table in expander
- [x] "Recent IB errors" renamed "Recent errors"; captures error 326 + yfinance errors
- [x] Analysis tab (before Diagnostics): OHLC table with prefix filter; candlestick + Bollinger Bands
      (from `VIRGIN_PUT_STD_MULT`) + RSI-14 + Volume chart; position summary for selected symbol
- [x] Analysis chart: strike price horizontal lines with annotation (right, DTE, state, σ, margin);
      Plotly hoverlabel dark theme fix (white-on-white tooltip eliminated)
- [x] Market drop withstand in header: `ExcessLiquidity / |Σ Δ $| × 100%`; US and US+SG columns
      when both accounts configured
- [x] Tab order: Positions → Orders → Analysis → Diagnostics
- [x] Orders tab: Generate Orders / Generate OHLCs / Clear Data on one row (`st.columns([2,2,2,5])`)
- [x] Orders tab: "✕ Clear" button added to filter bar (clears symbol prefix + C/P multiselect)
- [x] Clear Data: removed `st.rerun()` — fragment auto-refreshes via `run_every=3.0`; avoids error 326 race
- [x] Drop withstand: dual US / US+SG columns only when ALL account view is selected (`not acct` guard)
- [x] OHLC scope confirmed: `symbols.pkl` = S&P500 weekly underlyings only (CBOE list + SPY/QQQ); portfolio extras added at button-click time
- [x] OHLC pre-IBKR retry: bare symbols (no exchange suffix) retried with `.L` before slow IBKR fallback; `"yf_ticker"` override key in SymbolSpec enables this
- [x] Analysis chart BB/SMA: `hoverinfo="skip"` — legend labels are sufficient, hover was redundant
- [x] Analysis chart option strike lines: always `line_dash="dot"` (was dash/dot by direction)
- [x] KPI strip: Init Margin removed (10→9 columns); moved to Diagnostics key account values (`InitMarginReq` tag)
- [x] Diagnostics key values: now 8 metrics — added Init Margin (`InitMarginReq`)
- [x] derive.py: removed 20-line settings dump at startup; log now starts from `Getting financials...`
- [x] derive.py loguru: added `encoding="utf-8"` to `logger.add()` — prevents cp1252 crash on Windows
- [x] build.py `qualify_me`: replaced print-pyramid batch output with single tqdm progress bar (`_log_lines` collapses it correctly)
- [x] build.py `qualify_me`: per-batch try/except with one 3-second retry; early exit when `not ib.isConnected()` — fixes "Socket disconnect" killing entire qualification run
- [x] Analysis chart candlestick hover: trace-name badge text set to dark via `.hovertext .name { fill: #1e2130 }` CSS — was white-on-white (unreadable)
- [x] `ib_async` moved to module-level import in `ib_client.py` — fixes circular import (`ImportError: partially initialized module 'ib_async.contract'`) when imported lazily inside asyncio coroutine running in daemon thread
- [x] asyncio event loop created inside `_run_loop` (daemon thread) — fixes Windows ProactorEventLoop IOCP thread-affinity: loop created in main thread + `run_forever()` in daemon thread silently prevented all coroutines from executing
- [x] `threading.Event` (`_loop_ready`) synchronises `start()` and `_run_loop()`: daemon sets it after loop creation; main thread waits before `run_coroutine_threadsafe`
- [x] Future exception callback on `_connect_with_retry` — `_on_done` surfaces any unhandled exception in `log/dashboard.log`; `log/dashboard.log` added (daily rotation, loguru file sink in `start()`)

## 13. Known mistakes to avoid repeating

| Mistake | Fix |
|---|---|
| Subprocess touches IBKR without freeze → error 326 | `client.freeze()` + `frozen_for` before any subprocess; `_connecting` held during 5-s delay |
| Config panel not `@st.fragment` → toggle → full rerun → accidental unfreeze → 326 | All widget panels that must not trigger full reruns get `@st.fragment` |
| `clear.py` as subprocess unlinking files held by parent → PermissionError | Delete inline in same process |
| Unicode crash in Windows subprocess (`cp1252` can't encode tqdm `✓`) | Pass `PYTHONUTF8=1` + `PYTHONIOENCODING=utf-8` to all subprocess envs |
| Position rows missing `currency`/`primaryExch` → LSE ETFs get wrong yf ticker | Both `_on_portfolio` and `_on_position` now include these columns |
| `tz_localize(None)` on tz-aware yfinance DatetimeIndex → TypeError | Check `if idx.tz is not None: idx = idx.tz_convert(None)` in `_clean_df` |
| yfinance newer versions return MultiIndex columns on single-ticker download | `if isinstance(df.columns, pd.MultiIndex): df = df.droplevel(1, axis=1)` |
| Error 326 arrives as Python Exception in `_connect_with_retry`, not `errorEvent` | Append to `snap.errors` in the except block with a synthetic code=-1 entry |
| Wrong IBKR account value tag `StockValue` → always 0 | Correct tag is `StockMarketValue` |
| Wrong IBKR account value tag `GrossLeverage` → always 0 | Correct tag is `Leverage-S` (short leverage ratio, ~3×) |
| Plotly hoverlabel white-on-white in Streamlit dark theme | Set `hoverlabel={"bgcolor":"#1e2130","font_color":"#f1f5f9","bordercolor":"#475569","font_size":11}` in `update_layout` |
| Symbol filter using `str.contains` → substring matches mid-symbol | All filters use `str.upper().str.startswith(q.strip().upper())` |
| ITM flag computed after user filter → ITM checkbox hides non-filtered ITM rows | Compute `_itm` mask on full unfiltered DataFrame; apply after all other filters |
| Money formats without commas (`$%.0f`) → unreadable large numbers | Always use `$%,.0f` in all `st.column_config.NumberColumn` |
| `st.rerun()` after Clear Data inside fragment → full-page rerun → IBKR reconnect race → error 326 | Remove `st.rerun()` from non-subprocess button handlers; let `run_every` refresh the fragment |
| Drop withstand shows both US and US+SG even when a single account is selected | Condition: `if len(_REAL_ACCOUNTS) > 1 and _US and not acct:` — only dual when ALL view |
| LSE ETFs (CSPX, IWDA, EIMI, IGLN, VWRA) fail yfinance with bare ticker | IBKR reports `primaryExch=SMART` for many LSE ETFs → suffix not added. Fix: retry with `.L` before IBKR fallback in `_run_async` |
| qualify_me prints a half-pyramid of batch lines in derive_progress.log | Replace `print(f"Processing batch {n}/{B}...")` with a tqdm bar; `_log_lines` already collapses repeated bars |
| loguru `logger.add(path)` without `encoding="utf-8"` crashes on Windows | cp1252 can't encode tqdm unicode chars (✓, …); always pass `encoding="utf-8"` |
| derive.py prints all config settings at startup, cluttering the progress log | Remove the settings dump block; log starts from `Getting financials...` |
| Analysis chart BB/SMA hover labels repeat what the legend already shows | Set `hoverinfo="skip"` on BB Upper, BB Lower, SMA 20 traces |
| Init Margin in KPI strip adds width without high-frequency utility | Moved to Diagnostics key account values (`InitMarginReq` tag); KPI strip is now 9 metrics |
| `qualify_me` single outer `try/except` swallows all batches on first socket disconnect | Wrap `ib.run()` per-batch; retry once after 3 s; break early if `not ib.isConnected()` |
| Plotly hover `.hovertext text` CSS forces white on all elements including trace-name badge — name unreadable on white `<rect>` | Add `.hovertext .name { fill: #1e2130 !important; }` to page CSS block; this class is specifically the trace-name text |
| `ib_async` imported lazily (`TYPE_CHECKING` guard or inside coroutine) → `ImportError: cannot import name 'Contract' from partially initialized module 'ib_async.contract'` — dashboard connects and shows `IBClient.start()` log then goes silent forever | Move all `from ib_async import …` to module level in `ib_client.py`; do not use `TYPE_CHECKING` guard for this package |
| `asyncio.new_event_loop()` called in main thread, `run_forever()` called in daemon thread → Windows ProactorEventLoop IOCP handle is thread-affine → all coroutines silently never execute; dashboard logs `IBClient.start()` then nothing | Create loop INSIDE `_run_loop` (daemon thread); use `threading.Event` to synchronise before calling `run_coroutine_threadsafe` |
| OHLC→Generate Orders rapid sequence: delayed reconnect fires while Orders subprocess is re-freezing → `IBKR connect failed: Not connected` warning → retry sees `_frozen=True` → `Connection retry suppressed` | Expected/harmless; `_connecting` is reset in `finally` so Orders unfreeze schedules a clean reconnect |

## 14. Out of scope

- Order placement from the dashboard.
- Historical analytics / backtest.
- Alerting (Slack/email).
- Auth (single local user).

## 15. Analysis tab — design notes

- `render_analysis()` in `app.py`: reads `data/master/ohlc.pkl` directly (no IBKR needed).
- Symbol selector: dropdown of all OHLC symbols; prefix text filter above it.
- Chart: 3-row Plotly subplot (candlestick+BBands row 1, RSI row 2, Volume row 3).
- Bollinger Bands: `window=20`, `num_std=VIRGIN_PUT_STD_MULT` (from snp_config.yml).
- RSI: 14-period Wilder EWM. Overbought/oversold bands at 70/30.
- Strike lines: `fig.add_hline(y=strike, row=1, col=1, ...)` — skips strikes outside ±70%
  of chart price range to avoid clutter. Annotated with right/DTE/state/σ/margin.
- Position summary table shown below chart: stocks, options, states for selected symbol.
- Hoverlabel: dark background (`#1e2130`) with light text (`#f1f5f9`) — required for all
  Plotly charts in this app (default Streamlit theme produces white-on-white).

---
*Last updated: 2026-05-11 (session 4)*
