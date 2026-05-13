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

## The streaming pattern (canonical)

```python
# src/dashboard/ib_client.py
import asyncio, concurrent.futures, threading
# IMPORTANT: import ib_async at MODULE LEVEL — never lazily inside a coroutine.
# ib_async has a circular import (ib_async.__init__ ↔ ib_async.contract) that
# raises ImportError when first imported inside a running asyncio event loop.
from ib_async import IB

class IBClient:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls, *a, **kw):
        with cls._lock:
            if cls._instance is None:
                inst = super().__new__(cls)
                inst._started = False
                inst._loop_ready = threading.Event()
                cls._instance = inst
            return cls._instance

    def start(self, settings):
        with self._lock:
            if self._thread is not None:
                if self._thread.is_alive() or self._thread.ident is None:
                    return   # already running or just created
            self._loop_ready.clear()
            self._thread = threading.Thread(target=self._run_loop, daemon=True)
        # Create loop INSIDE daemon thread — Windows ProactorEventLoop IOCP
        # handles are thread-affine; creating in main thread breaks coroutines.
        self._thread.start()
        self._loop_ready.wait(timeout=5)
        asyncio.run_coroutine_threadsafe(self._connect_with_retry(), self._loop)

# In app.py — wrap in st.cache_resource to prevent concurrent reruns double-starting:
# @st.cache_resource(show_spinner=False)
# def _start_ib_client():
#     c = get_client(); c.start(settings); return c
# client = _start_ib_client()

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop_ready.set()    # unblocks start()
        self._loop.run_forever()

    async def _connect(self, host, port, cid):
        self.ib = IB()
        self.ib.portfolioEvent += self._on_portfolio
        self.ib.accountValueEvent += self._on_acct
        self.ib.pendingTickersEvent += self._on_tickers
        await self.ib.connectAsync(host, port, clientId=cid)
```

## Pacing rules (memorize)

- Subscribe **only to held contracts** + their underlyings. ~50 unique tickers is the soft ceiling per session.
- `genericTickList="106"` = model option computation (greeks). Use this; do not compute greeks yourself for live monitoring.
- Cancel `reqMktData` on disconnect or when a position closes — `IB.tickers()` is not a free leak.
- On `error 165` / `error 322`, back off; on `error 1100` (connectivity lost), let `disconnectedEvent` trigger reconnect with exponential backoff.

## Streamlit idioms

```python
# UI side — no awaits, no IB calls
@st.fragment(run_every=2.0)
def kpi_strip():
    snap = ib_client.snapshot()
    cols = st.columns(5)
    cols[0].metric("NLV", money(snap.nlv))
    cols[1].metric("Cushion", pct(snap.cushion),
                   delta=None,
                   delta_color="inverse" if snap.cushion < 0.20 else "normal")
    ...
```

Use `st.cache_data(ttl=...)` only for derivations of the snapshot, not for the snapshot itself (it must be fresh).

## Greek aggregation (vectorized)

```python
# risk.py
def greek_sums(positions: pd.DataFrame, tickers: dict) -> dict:
    df = positions.merge(
        pd.DataFrame.from_records(
            [(k, v.delta, v.gamma, v.theta, v.vega) for k, v in tickers.items()],
            columns=["conId", "delta", "gamma", "theta", "vega"]
        ),
        on="conId", how="left",
    )
    df["mult"] = np.where(df.secType == "OPT", 100, 1)
    df["dollar_delta"] = df.position * df.delta.fillna(1) * df.mult * df.underlying_px
    return {
        "delta_$":  df.dollar_delta.sum(),
        "theta_$":  (df.position * df.theta.fillna(0) * df.mult).sum(),
        "vega_$":   (df.position * df.vega.fillna(0)  * df.mult).sum(),
    }
```

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
| derive.py: "Connection attempt N failed ()" — subprocess freeze was called first | Button handler called `st.rerun()` after `Popen()` — full-page rerun fires `_connect_with_retry` on daemon thread, dashboard reclaims CID=10 before subprocess connects | **Never** call `st.rerun()` after `freeze()` + `Popen()` in a button handler; rely on `run_every` timer |
| `chains_n_unds` crashes: `ValueError: Cannot set a DataFrame without columns` | `get_volatilities_snapshot` returns `pd.DataFrame()` (no columns) on failure; `apply(axis=1)` returns a DataFrame not a Series; column access also KeyErrors | Guard: `if not df_unds.empty and 'symbol' in df_unds.columns:` before apply AND before column access |

## Don't

- Don't build a new connector per tab. One client, many subscriptions.
- Don't render >5k DataFrame rows in Streamlit — paginate or aggregate.
- Don't echo `.env` content to chat or logs, ever.

---

## IBKR subprocess / CID rules ← READ BEFORE WRITING ANY SUBPROCESS

Dashboard owns **CID=10**. No other process may connect without `client.freeze()` first.
Full rules in `CLAUDE.md § SUBPROCESS / CID RULES`. Key pattern:

```python
# Button handler — freeze BEFORE Popen, NO st.rerun() after
st.session_state["frozen_for"] = "mytask"
client.freeze()
proc = subprocess.Popen([sys.executable, "myscript.py"], ...)
st.session_state["mytask_proc"] = proc
# run_every timer handles UI refresh — no st.rerun() needed here

# Auto-unfreeze in next run_every cycle
if client.is_frozen() and proc.poll() is not None \
        and st.session_state.get("frozen_for") == "mytask":
    client.unfreeze()   # 5-second delayed reconnect — IBKR needs time to release CID
    st.session_state.pop("frozen_for", None)
    st.rerun()
```

| Call | Effect |
|---|---|
| `freeze()` | `_frozen=True`, disconnects socket. Snapshot readable (last-known). |
| `unfreeze()` | `_frozen=False`, schedules reconnect after 5 s. |

Subprocess checklist:
- [ ] Does **not** import `src.dashboard.ib_client`
- [ ] Dashboard frozen **before** `Popen` if script touches IBKR (even as yfinance fallback)
- [ ] Own client ID (not CID=10)
- [ ] `PYTHONUTF8=1`, `PYTHONIOENCODING=utf-8` in env
