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

    def start(self, host, port, client_id):
        if self._started: return
        self._started = True
        # Create loop INSIDE the daemon thread (Windows ProactorEventLoop IOCP
        # handles are thread-affine — creating in main thread then running in
        # daemon thread silently prevents all coroutines from executing).
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        self._loop_ready.wait(timeout=5)   # blocks until _run_loop signals
        fut = asyncio.run_coroutine_threadsafe(
            self._connect(host, port, client_id), self._loop
        )
        def _on_done(f: concurrent.futures.Future):
            if (exc := f.exception()) is not None:
                print(f"connect coroutine raised: {exc!r}")
        fut.add_done_callback(_on_done)

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

## Don't

- Don't build a new connector per tab. One client, many subscriptions.
- Don't render >5k DataFrame rows in Streamlit — paginate or aggregate.
- Don't echo `.env` content to chat or logs, ever.

---

## IBKR subprocess / CID rules ← READ BEFORE WRITING ANY SUBPROCESS

**The hardest rule in this repo:** Every OS process connecting to IBKR must use a *unique* client ID.
The live dashboard owns **CID=10** (from `snp_config.yml → CID`).
No other code path may connect with CID=10 without first calling `client.freeze()`.

### The freeze / unfreeze pattern (mandatory for all subprocesses that touch IBKR)

```python
# 1. Button handler — freeze BEFORE launching subprocess
client.freeze()                              # disconnect CID=10
st.session_state["frozen_for"] = "mytask"   # track why we froze
proc = subprocess.Popen([sys.executable, "myscript.py"], ...)
st.session_state["mytask_proc"] = proc

# 2. Same fragment, next run_every cycle — auto-unfreeze when done
if client.is_frozen() and proc.poll() is not None \
        and st.session_state.get("frozen_for") == "mytask":
    client.unfreeze()                        # schedules 5-second delayed reconnect
    st.session_state.pop("frozen_for", None)
    st.rerun()
```

### `client.freeze()` / `client.unfreeze()` semantics

| Call | Effect |
|---|---|
| `freeze()` | Sets `_frozen=True`, disconnects the IB socket. Snapshot remains readable (last-known data). |
| `unfreeze()` | Sets `_frozen=False`, schedules `_connect_with_retry` after 5 s (IBKR needs time to release CID). |

The 5-second delay in `unfreeze` is critical. If you reconnect immediately after the subprocess exits, IBKR hasn't freed the CID yet → **error 326**.

### Subprocess design checklist

- [ ] Script does **not** import `src.dashboard.ib_client` (no singleton side-effects)
- [ ] If the script needs IBKR: dashboard must be frozen **before** `subprocess.Popen`
- [ ] If the script uses only yfinance / HTTP: no freeze needed, no CID conflict
- [ ] Script uses its **own** client ID (e.g., CID from settings, or a dedicated offset)
- [ ] UTF-8 env vars set: `PYTHONUTF8=1`, `PYTHONIOENCODING=utf-8` (Windows cp1252 breaks Unicode tqdm output)

### Why yfinance subprocesses still break the dashboard

yfinance itself is fine — it never touches IBKR.
The failure mode is: yfinance misses some symbols → code falls through to an IBKR fallback →
fallback connects with wrong CID (or right CID but without freeze) → 326.
**Always freeze before any subprocess that might touch IBKR, even as a fallback.**

### asyncio loop ownership

The dashboard daemon thread owns the event loop. Never call `asyncio.run()` inside that thread.
Subprocesses get their own process + event loop via `asyncio.run()` — that is fine.
The pattern `asyncio.run_coroutine_threadsafe(coro, self._loop)` is the only safe bridge
between Streamlit's main thread and the daemon loop.
