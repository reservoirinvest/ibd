---
name: dashboard
description: Use when building or modifying live IBKR dashboards in this repo. Covers the streaming-thread pattern, Streamlit fragments, pacing-safe market-data subscriptions, and greek aggregation. Trigger on any mention of "dashboard", "live", "streamlit", "positions", "risk monitor", or "ib-async streaming".
---

# Live IBKR Dashboard — Skill

## When to use
Any task that involves the live dashboard — adding a panel, debugging a stuck price, reading positions, or wiring new risk metrics. Do **not** use for the batch programs (`build.py`, `classify.py`, etc.) — those are standalone scripts.

## Mental model

A Streamlit script reruns on every interaction. IBKR connections are expensive and pace-limited. Therefore:

1. **One persistent daemon thread** owns an `asyncio` loop and one `IB()` instance.
2. **One `Snapshot` dataclass** (under a `threading.Lock`) holds the latest portfolio + tickers + account values.
3. **The Streamlit script reads** the snapshot — never connects, never awaits.
4. **`st.fragment(run_every=N)`** triggers partial reruns of just the panel that needs new data.

Key startup rule: `IBClient.start()` is wrapped in `@st.cache_resource` in `app.py` — see `CLAUDE.md § IBClient`.

## Pacing rules (memorize)

- Subscribe **only to held contracts** + their underlyings. ~50 unique tickers is the soft ceiling per session.
- `genericTickList="106"` = model option computation (greeks). Use this; do not compute greeks yourself for live monitoring.
- Cancel `reqMktData` on disconnect or when a position closes — `IB.tickers()` is not a free leak.
- On `error 165` / `error 322`, back off; on `error 1100` (connectivity lost), let `disconnectedEvent` trigger reconnect with exponential backoff.
- Dashboard logs a warning when subscription count exceeds 50 (added in A2).

## Streamlit idioms

```python
# UI side — no awaits, no IB calls
@st.fragment(run_every=2.0)
def kpi_strip():
    snap = client.snapshot()
    cols = st.columns(5)
    cols[0].metric("NLV", money(snap.nlv))
    cols[1].metric("Cushion", pct(snap.cushion),
                   delta_color="inverse" if snap.cushion < 0.20 else "normal")
```

Use `st.cache_data(ttl=...)` only for derivations of the snapshot (e.g. `_cached_ohlc()`), not for the snapshot itself (it must be fresh).

## Greek aggregation

Implemented as `greek_dollar_sums()` in `src/dashboard/risk.py`. Accepts a positions DataFrame and a `tickers: dict[int, TickerSnap]`. Call with `pre_joined=True` when positions have already been through `_join_tickers()` to avoid a redundant merge:

```python
from src.dashboard.risk import greek_dollar_sums
sums = greek_dollar_sums(df, snap.tickers)             # standard call
sums = greek_dollar_sums(joined_df, pre_joined=True)   # skip redundant join
```

Returns `{"delta_$": float, "theta_$": float, "vega_$": float, "gamma_$": float}`.

## Common pitfalls

| Symptom | Cause | Fix |
|---|---|---|
| Dashboard freezes on first load | Calling `IB.connect()` on Streamlit thread | Move to `ib_client.start()` daemon thread |
| Greeks all NaN | Forgot `genericTickList="106"` | Add to `reqMktData` call |
| "Already connected" loop | Multiple `clientId`s racing | Singleton + one `clientId` per process |
| Stale prices | Subscribed but not consuming `pendingTickersEvent` | Hook the event before `connectAsync` returns |
| Cushion shown as 0 | `accountValueEvent` not yet fired | Show `—` until `as_of` is set |
| Logs show `IBClient.start()` then complete silence, dashboard stays 🔴 | `ib_async` imported lazily inside coroutine → circular import → `ImportError` swallowed by asyncio task handler | Import `from ib_async import IB` at module level, never inside `TYPE_CHECKING` or a coroutine |
| Logs show `IBClient.start()` then silence even with eager import | `asyncio.new_event_loop()` called in main thread, `run_forever()` in daemon thread → Windows ProactorEventLoop IOCP thread-affinity breaks coroutine dispatch | Move `asyncio.new_event_loop()` into `_run_loop` (daemon thread); use `threading.Event` to sync before `run_coroutine_threadsafe` |
| `IBClient.start()` logged 2–3× at same millisecond; error 326 cascade | Concurrent Streamlit reruns all execute module-level `client.start()` — thread guard alone cannot block N simultaneous callers that all see dead thread before any creates T2 | Wrap start in `@st.cache_resource(show_spinner=False)` — Streamlit guarantees exactly-once execution per server process. Thread guard is now a safety-net for crash-restart only |
| derive.py: "Connection attempt N failed ()" — empty error for qualify/chains/volatilities | derive.py called `ib = get_ib_connection()` BEFORE `classifed_results()` — its open CID=10 blocks all internal connections in `chains_n_unds` | Move `ib = get_ib_connection()` to AFTER `classifed_results()` and `get_open_orders()` |
| `get_volatilities_snapshot(ib=ib)` / `get_option_chains(ib=ib)` ignored the passed connection | `ib = None` as first line of function body unconditionally overrode the `ib` parameter — always created a fresh connection | Remove the `ib = None` override line; `if ib is None: ib = get_ib_connection()` guard then works correctly |
| derive.py: error 326 cascade after subprocess freeze | Button handler called `st.rerun()` after `Popen()` — full-page rerun fires `_connect_with_retry` on daemon thread, dashboard reclaims CID=10 before subprocess connects | **Never** call `st.rerun()` after `freeze()` + `Popen()` in a button handler; rely on `run_every` timer |
| Generate Orders suggests cover/sow/protect for a symbol that already has a manual IBKR order | `df_openords` was fetched but exclusion sets were not applied to generation filters | After `classify_open_orders`, build `_oo_covering`, `_oo_sowing`, `_oo_protecting` sets and filter `uncov_long`/`uncov_short`, `df_v`, `df_unprot` before each generation section. Use `is_active=True` so cancelled orders don't block. |
| Sow includes monthly-only symbols (AZO, ORLY, ZTS, etc.) | `_is_weekly_expiry` misidentifies holiday-shifted monthly dates (e.g. Jun 18 = Juneteenth shift) as weekly | Load `data/master/symbol_categories.pkl` in derive.py, add `~df_unds.symbol.isin(_monthly_syms)` to `df_v` filter. Regenerate pkl via 'Identify Weeklies' button after each build. |
| `_fetch_what_if_margins` runs 2–3× concurrently (logs show "Fetching what-if margin" 3 times, 3 s apart) | `_margin_pending` cleared at the *top* of the coroutine body; bootstrap doesn't set it before awaiting; portfolio/position events fire during the long gather and each see `_margin_pending=False` | Set `self._margin_pending = True` **before** `await _fetch_what_if_margins()` in `_bootstrap()`; wrap the coroutine body in `try/finally` and reset `_margin_pending = False` only in the `finally` clause. |
| `chains_n_unds` crashes: `ValueError: Cannot set a DataFrame without columns` | `get_volatilities_snapshot` returns `pd.DataFrame()` (no columns) on failure; `apply(axis=1)` returns a DataFrame not a Series; column access also KeyErrors | Guard: `if not df_unds.empty and 'symbol' in df_unds.columns:` before apply AND before column access |

## Don't

- Don't build a new connector per tab. One client, many subscriptions.
- Don't render >5k DataFrame rows in Streamlit — paginate or aggregate.
- Don't echo `.env` content to chat or logs, ever.

---

## risk.py — performance patterns

- `greek_dollar_sums(df, pre_joined=True)` — pass when positions have already gone through `_join_tickers()`; skips a redundant merge.
- `cover_protect_gaps` inner loop — pre-group options once: `{s: g for s, g in opt.groupby("symbol")}` then `opt_by_sym.get(sym, _empty_opt)`. The O(n²) per-stock filter was the 156 ms bottleneck.
- Schema-preserving empty DataFrame — `df.iloc[0:0]`, not `pd.DataFrame()`. Bare empty frame has no columns; `.position`, `.symbol` etc. crash downstream.
- `_join_tickers` — numpy arrays via direct dict lookup, then `df.assign(...)`. Avoid list-of-dicts → `pd.DataFrame` → `merge` pattern (allocates an intermediate frame every render tick).

## Dashboard UI conventions

- Money: `format="$%,.0f"` (commas mandatory). Never `"$%.0f"`.
- `width="stretch"`, not `use_container_width` (deprecated).
- Expanders: `expanded=False` for heavy tables (Positions, Cover/Protect gaps).
- `render_analysis()` `run_every=60.0` — OHLC + position tables are expensive. `header()` and `kpi_strip()` stay at 2 s.
- KPI strip: 8 metrics — NLV, Opt Value, Cushion, Excess Liq, Maint Margin, Σ Δ, Σ Θ, Σ ν.
- Drop withstand: `ExcessLiquidity / |Σ Δ $| × 100%`. Dual US/US+SG columns only in ALL view.
- Nav: `st.columns([2, 3, 1, 2])` — header | radio | account selector | spacer.
- Plotly: `hoverlabel={"bgcolor":"#1e2130","font_color":"#f1f5f9"}`. Option strike lines: `line_dash="dot"`. BB bands + SMA: `hoverinfo="skip"`.
- IBKR tags: `StockMarketValue` (not `StockValue`), `Leverage-S` (not `GrossLeverage`).
- Symbol filters: `str.upper() == sym` exact for positions; `str.upper().startswith(...)` for orders.
- Cover/Protect gaps column order: `cover_strike | mkt_px | protect_strike`.
- `protect_strike`: shows existing long option strikes when held; `~{target}` (= `mkt_px − cover_std_mult × σ`) when no protection. Always call `cover_protect_gaps(protect_me=True)` from dashboard.
- Tables below charts: always render with conditional message when no data — never hide the section.

## Testing patterns (IBClient)

- `MagicMock()` base for IBClient test doubles — **never** `AsyncMock()`. AsyncMock base makes every attribute return a coroutine, causing `TypeError: 'coroutine' object is not iterable` on sync calls like `managedAccounts()`. Set async attrs explicitly: `mock_ib.reqPositionsAsync = AsyncMock(...)`.
- Wire-handler `+=` gotcha — `ib.updatePortfolioEvent += handler` reassigns the attribute to the `__iadd__` return value. Asserting on the original attribute checks the wrong object. Use a custom event class with a `handlers: list` and `__iadd__` that appends in-place.
- Logger isolation in timing tests — patch `ibc.logger` with `Mock()`. Loguru DEBUG sink writes to stderr (~5 ms/call) and inflates throughput assertions.

## IBClient internals (reference)

- Position store: `_positions_store: dict[tuple[int, str], dict]` — key `(conId, account)`. `_on_portfolio` writes O(1). Zero-position event: `_positions_store.pop(key, None)`.
- After each write: `self._snap.positions = pd.DataFrame(self._positions_store.values())` — one allocation, never `pd.concat`.
- `_health_gen` — incremented on each successful connect; each `_health_check_loop` exits when its captured `gen` no longer matches (prevents zombie loops after reconnect).
- `_UNFREEZE_DELAY_SECS = 5.0` (CID release), `_BOOTSTRAP_SETTLE_SECS = 3.0` (TWS push wait).
- `logger.add()` guarded by `_log_sink_added` class-level flag inside `_lock` — one file sink per process.

---

## IBKR subprocess / CID rules

Dashboard owns **CID=10**. No other process may connect without `client.freeze()` first.  
Full pattern in `CLAUDE.md § SUBPROCESS / CID RULES`. Auto-unfreeze uses the `_auto_unfreeze(tag, proc_key)` helper in `render_orders()`.

| Call | Effect |
|---|---|
| `freeze()` | `_frozen=True`, disconnects socket. Snapshot readable (last-known). |
| `unfreeze()` | `_frozen=False`, schedules reconnect after `_UNFREEZE_DELAY_SECS` (5 s). |

Subprocess checklist:
- [ ] Does **not** import `src.dashboard.ib_client`
- [ ] Dashboard frozen **before** `Popen` if script touches IBKR (even as yfinance fallback)
- [ ] Own client ID (not CID=10)
- [ ] `PYTHONUTF8=1`, `PYTHONIOENCODING=utf-8` in env
