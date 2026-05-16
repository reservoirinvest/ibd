"""IBKR streaming client.

A daemon thread owns the asyncio event loop and one IB() instance.
The Streamlit script reads the latest Snapshot via `snapshot()`.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import math
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import pandas as pd
from ib_async import IB, Contract, MarketOrder, PortfolioItem, Ticker, Trade
from loguru import logger

from .settings import Settings, get_settings


# ---------------------------------------------------------------------------
# Snapshot — the read model
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class TickerSnap:
    last: float = float("nan")
    bid: float = float("nan")
    ask: float = float("nan")
    delta: float = float("nan")
    gamma: float = float("nan")
    theta: float = float("nan")
    vega: float = float("nan")
    iv: float = float("nan")
    underlying_px: float = float("nan")


@dataclass(slots=True)
class Snapshot:
    as_of: datetime | None = None
    connected: bool = False
    account: str = ""
    # Nested: {account_number: {ibkr_tag: Decimal}}
    account_values: dict[str, dict[str, Decimal]] = field(default_factory=dict)
    positions: pd.DataFrame = field(default_factory=pd.DataFrame)
    tickers: dict[int, TickerSnap] = field(default_factory=dict)
    orders: pd.DataFrame = field(default_factory=pd.DataFrame)
    errors: deque[tuple] = field(default_factory=lambda: deque(maxlen=50))
    # Set after _fetch_what_if_margins completes; None = not yet fetched
    margins_as_of: datetime | None = None
    # Set when connection drops; cleared when reconnected. Used for UI staleness display.
    stale_since: datetime | None = None


# ---------------------------------------------------------------------------
# Singleton client
# ---------------------------------------------------------------------------


_UNFREEZE_DELAY_SECS = 5.0    # IBKR needs this long to release the CID after disconnect
_BOOTSTRAP_SETTLE_SECS = 3.0  # wait for TWS to push initial portfolio/account events

# Circuit breaker — stops hammering IBKR after repeated failures
_CB_FAILURE_THRESHOLD: int = 5      # consecutive failures before circuit opens
_CB_RESET_SECS: float = 300.0       # seconds the circuit stays OPEN before half-open attempt

# Health check — detects zombie connections where isConnected() is stale
_HEALTH_CHECK_INTERVAL_SECS: float = 30.0

# Market-data pacing ceiling — warn when active OPT reqMktData feeds exceed this.
# IBKR Pro accounts support far more than 50 concurrent streams; 100 is a safe
# operational limit before errors 100/165 become likely under load.
_MKTDATA_PACING_CEILING: int = 100

# Error code classification
_IBKR_INFO_CODES: frozenset[int] = frozenset({2104, 2106, 2158, 2107, 2103, 2105, 2108})
_IBKR_PACING_CODES: frozenset[int] = frozenset({100, 165, 322})

_POSITION_COLUMNS = [
    "account",
    "conId",
    "symbol",
    "secType",
    "right",
    "strike",
    "expiry",
    "position",
    "avgCost",
    "marketPrice",
    "marketValue",
    "unrealizedPNL",
    "realizedPNL",
]

_ORDER_COLUMNS = [
    "orderId",
    "account",
    "symbol",
    "secType",
    "right",
    "strike",
    "expiry",
    "action",
    "qty",
    "filled",
    "remaining",
    "orderType",
    "lmtPrice",
    "status",
]


class IBClient:
    """Thread-safe singleton wrapping ib-async."""

    _instance: IBClient | None = None
    _lock = threading.Lock()
    _log_sink_added: bool = False  # class-level guard — prevents duplicate loguru sinks

    def __new__(cls) -> IBClient:
        with cls._lock:
            if cls._instance is None:
                inst = super().__new__(cls)
                inst._initialized = False  # type: ignore[attr-defined]
                cls._instance = inst
            return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return
        self._initialized = True
        self._snap_lock = threading.Lock()
        self._snap = Snapshot()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ib: IB | None = None
        self._subscribed: set[int] = set()
        self._settings: Settings | None = None
        self._managed_accounts: list[str] = []
        self._connecting = False  # True while _connect_with_retry coroutine is alive
        self._frozen = False  # True while derive.py holds the CID exclusively
        self._loop_ready = threading.Event()  # set once run_forever() is live
        # Circuit breaker state
        self._cb_failures: int = 0
        self._cb_opened_at: float = 0.0
        self._cb_state: str = "closed"  # "closed" | "open" | "half_open"
        # Health check generation counter — incremented on each successful connect
        self._health_gen: int = 0
        # Connection timing and attempt tracking for structured logging
        self._connect_start_time: float = 0.0  # monotonic timestamp of last successful connect
        self._connect_attempts: int = 0         # resets to 0 on each successful connect
        # Dict-backed position store: (conId, account) → row dict.
        # _on_portfolio and _on_position write here (O(1) per event);
        # _snap.positions is rebuilt from this dict after each write.
        # Reading through snapshot() is unaffected.
        self._positions_store: dict[tuple[int, str], dict] = {}
        # Bootstrap gate: False during initial portfolio push so _on_portfolio /
        # _on_position don't queue 100+ resubscription coroutines. Set True once
        # _bootstrap() has called _resubscribe_market_data() directly.
        self._bootstrapped: bool = False
        # Debounce: True while a _resubscribe_market_data coroutine is already queued.
        # Prevents a storm of duplicate coroutines when TWS sends a burst of
        # updatePortfolioEvent refreshes (e.g. periodic account statement updates).
        self._resubscribe_pending: bool = False
        # Tracks conIds with active reqMktData calls (OPT only).
        # Distinct from _subscribed which counts all positions (STK + OPT).
        self._mktdata_subs: set[int] = set()
        self._retry_fut: concurrent.futures.Future | None = None  # held so GC can't destroy pending task

    # ---- public API --------------------------------------------------------

    def start(self, settings: Settings | None = None) -> None:
        """Start the background event loop (idempotent, thread-safe).

        Guards against both race conditions and hot-reload scenarios:
        - Thread object is created inside the lock so any racing concurrent caller
          sees it immediately and returns without double-starting.
        - ident-is-None check treats a just-created (not-yet-started) thread as
          alive-for-guard purposes, closing the window between lock release and
          thread.start().
        - _log_sink_added prevents a second loguru file sink per Python process.
        - Thread-liveness check (ident set + not alive) allows restart if the
          daemon thread has crashed unexpectedly.
        """
        with self._lock:
            if self._thread is not None:
                # Thread alive, OR created but not yet started (ident is None) → no-op
                if self._thread.is_alive() or self._thread.ident is None:
                    return
            # First call, or restart after the daemon thread died
            add_sink = not IBClient._log_sink_added
            if add_sink:
                IBClient._log_sink_added = True
            # Create thread INSIDE the lock so concurrent callers see it immediately
            self._loop_ready.clear()
            self._thread = threading.Thread(target=self._run_loop, name="ib-client", daemon=True)

        self._settings = settings or get_settings()

        if add_sink:
            import sys
            from pyprojroot import here as _here  # avoid circular at module level

            logger.remove()  # clear default stderr sink (DEBUG) that spams terminal
            logger.add(
                sys.stderr,
                level="INFO",
                colorize=True,
                format="<green>{time:HH:mm:ss}</green> | <level>{level:<7}</level> | {message}",
            )

            _log_path = _here() / "log" / "dashboard.log"
            _log_path.parent.mkdir(parents=True, exist_ok=True)
            logger.add(
                str(_log_path),
                rotation="1 day",
                retention="3 days",
                encoding="utf-8",
                level="DEBUG",
                format="{time:HH:mm:ss.SSS} | {level:<7} | {message}",
            )

        logger.info(
            "IBClient.start() — host={} port={} cid={}",
            self._settings.ib_host,
            self._settings.ib_port,
            self._settings.ib_client_id,
        )

        # Create the event loop INSIDE the daemon thread — on Windows the
        # ProactorEventLoop's IOCP handle is thread-affine: creating it in the
        # main thread then running it in a daemon thread silently prevents any
        # coroutines from executing.
        self._thread.start()
        if not self._loop_ready.wait(timeout=5):
            logger.error(
                "ib-client event loop did not start in 5 s — daemon thread may have crashed"
            )
            return
        self._retry_fut = asyncio.run_coroutine_threadsafe(self._connect_with_retry(), self._loop)

        def _on_done(f: concurrent.futures.Future) -> None:
            if f.cancelled():
                return
            exc = f.exception()
            if exc is not None:
                logger.error("_connect_with_retry raised: {!r}", exc)

        self._retry_fut.add_done_callback(_on_done)

    def snapshot(self) -> Snapshot:
        """Return a shallow copy that's safe for the UI to read."""
        with self._snap_lock:
            s = self._snap
            # deep-copy the nested account_values dict
            av_copy = {acct: dict(vals) for acct, vals in s.account_values.items()}
            return Snapshot(
                as_of=s.as_of,
                connected=s.connected,
                account=s.account,
                account_values=av_copy,
                positions=s.positions.copy(deep=False),
                tickers=dict(s.tickers),
                orders=s.orders.copy(deep=False),
                errors=deque(s.errors, maxlen=50),
                margins_as_of=s.margins_as_of,
                stale_since=s.stale_since,
            )

    def freeze(self) -> None:
        """Voluntarily drop the IBKR connection so derive.py can use the same CID.

        Sets _frozen=True BEFORE disconnecting so the _on_disconnect handler
        will not spawn a reconnect loop while derive is running.
        """
        self._frozen = True  # guard must be set first
        self._bootstrapped = False
        if self._loop:
            self._loop.call_soon_threadsafe(self._subscribed.clear)
            self._loop.call_soon_threadsafe(self._mktdata_subs.clear)
        if self._loop and self._ib:
            asyncio.run_coroutine_threadsafe(self._disconnect(), self._loop)
        with self._snap_lock:
            self._snap.connected = False
        logger.info(
            "Dashboard frozen — CID {} released for derive.py",
            self._settings.ib_client_id if self._settings else "?",
        )

    def unfreeze(self) -> None:
        """Clear frozen flag and reconnect after a brief pause.

        Holds _connecting=True for the entire delay so _on_disconnect cannot
        spawn a parallel _connect_with_retry that races against derive.py's
        lingering CID hold.  The 5-second sleep gives IBKR time to fully
        release the CID after derive.py exits.
        """
        self._frozen = False
        if self._loop and not self._connecting:
            self._connecting = True  # block any parallel reconnect attempts

            async def _delayed_connect() -> None:
                try:
                    await asyncio.sleep(_UNFREEZE_DELAY_SECS)
                    if not self._frozen:
                        # _connect_with_retry manages _connecting itself; reset first
                        self._connecting = False
                        await self._connect_with_retry()
                except Exception:  # noqa: BLE001
                    self._connecting = False

            asyncio.run_coroutine_threadsafe(_delayed_connect(), self._loop)
        logger.info("Dashboard unfreezing — reconnecting in 5s")

    def is_frozen(self) -> bool:
        return self._frozen

    @property
    def circuit_state(self) -> str:
        """Current circuit breaker state: 'closed', 'open', or 'half_open'."""
        return self._cb_state

    def reset_circuit_breaker(self) -> None:
        """Manually reset the circuit breaker to closed state and retry immediately."""
        self._cb_failures = 0
        self._cb_state = "closed"
        logger.info("Circuit breaker manually reset to CLOSED")

    def schedule_margin_refresh(self) -> None:
        """Kick off a background what-if margin refresh (non-blocking)."""
        if self._loop and self._ib and self._ib.isConnected():
            asyncio.run_coroutine_threadsafe(self._fetch_what_if_margins(), self._loop)
            logger.info("Margin refresh scheduled")

    @property
    def managed_accounts(self) -> list[str]:
        return list(self._managed_accounts)

    def stop(self) -> None:
        """Gracefully disconnect and stop the daemon event loop. Used in tests and shutdown."""
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._disconnect(), self._loop).result(timeout=3)
            self._loop.call_soon_threadsafe(self._loop.stop)

    # ---- loop --------------------------------------------------------------

    def _run_loop(self) -> None:
        """Daemon thread target — owns the asyncio event loop for its entire lifetime."""
        # Create the loop INSIDE this thread so the Windows ProactorEventLoop's IOCP
        # handle is thread-affine to the thread that runs run_forever().
        try:
            self._loop = asyncio.new_event_loop()
        except Exception as e:  # noqa: BLE001
            logger.error("_run_loop: asyncio.new_event_loop() failed: {!r}", e)
            return
        asyncio.set_event_loop(self._loop)
        self._loop_ready.set()  # unblock start() → run_coroutine_threadsafe
        try:
            self._loop.run_forever()
        except Exception as e:  # noqa: BLE001
            logger.error("ib-client event loop crashed: {}", e)
        finally:
            logger.info("ib-client event loop stopped")

    async def _connect_with_retry(self) -> None:
        """Connect with exponential backoff and circuit breaker.

        Circuit breaker opens after _CB_FAILURE_THRESHOLD consecutive failures and
        pauses all reconnection attempts for _CB_RESET_SECS. After the pause it enters
        HALF_OPEN and allows a single probe attempt; success → CLOSED, failure → OPEN again.
        """
        if self._connecting:
            return
        self._connecting = True
        try:
            assert self._settings is not None, "settings not set — call start() first"
            backoff = 2.0
            while True:
                if self._frozen:
                    logger.info("Connection retry suppressed — dashboard frozen")
                    return

                # ---- Circuit breaker guard ----
                if self._cb_state == "open":
                    elapsed = time.monotonic() - self._cb_opened_at
                    remaining = max(0.0, _CB_RESET_SECS - elapsed)
                    if remaining > 0:
                        logger.warning(
                            "Circuit breaker OPEN ({} failures) — waiting {:.0f}s before retry",
                            self._cb_failures,
                            remaining,
                        )
                        await asyncio.sleep(remaining)
                        continue  # re-check frozen + circuit state
                    # Reset timeout elapsed → allow one probe attempt
                    self._cb_state = "half_open"
                    logger.info("Circuit breaker HALF-OPEN — probing connection")

                try:
                    # always tear down the previous IB before a new attempt
                    if self._ib is not None:
                        try:
                            self._ib.disconnect()
                        except Exception:  # noqa: BLE001
                            pass
                    self._connect_attempts += 1
                    logger.info(
                        "IBKR connect attempt #{} host={} port={} cid={}",
                        self._connect_attempts,
                        self._settings.ib_host,
                        self._settings.ib_port,
                        self._settings.ib_client_id,
                    )
                    self._bootstrapped = False
                    self._ib = IB()
                    self._wire_handlers(self._ib)
                    await self._ib.connectAsync(
                        self._settings.ib_host,
                        self._settings.ib_port,
                        clientId=self._settings.ib_client_id,
                        timeout=10,
                    )
                    self._connect_start_time = time.monotonic()
                    self._connect_attempts = 0
                    logger.info(
                        "IBKR connected host={} port={} cid={}",
                        self._settings.ib_host,
                        self._settings.ib_port,
                        self._settings.ib_client_id,
                    )
                    # Success — reset circuit breaker
                    self._cb_failures = 0
                    self._cb_state = "closed"
                    await self._bootstrap()
                    return
                except Exception as e:  # noqa: BLE001 — network surface
                    self._cb_failures += 1
                    if self._cb_failures >= _CB_FAILURE_THRESHOLD:
                        self._cb_state = "open"
                        self._cb_opened_at = time.monotonic()
                        logger.error(
                            "Circuit breaker OPEN after {} consecutive failures — "
                            "pausing reconnection for {:.0f}s",
                            self._cb_failures,
                            _CB_RESET_SECS,
                        )
                    logger.warning("IBKR connect failed: {} — retry in {:.1f}s", e, backoff)
                    with self._snap_lock:
                        self._snap.connected = False
                        # Surface connection failures in the Diagnostics "Recent errors" panel.
                        # Covers error 326 (CID in use) which may come through the exception
                        # rather than errorEvent when connectAsync itself is rejected.
                        self._snap.errors.append(
                            (datetime.now(timezone.utc), -1, f"Connect failed: {e}")
                        )
                    self._subscribed.clear()
                    self._mktdata_subs.clear()
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 1.7, 60.0)
        finally:
            self._connecting = False

    async def _bootstrap(self) -> None:
        """Initial pull of portfolio + account values + orders + subscribe to greeks."""
        assert self._ib is not None and self._settings is not None
        _bootstrap_t0 = time.monotonic()
        # Discover managed accounts (populated immediately post-connect)
        managed = list(self._ib.managedAccounts())
        self._managed_accounts = managed
        logger.info("Managed accounts: {}", managed)
        with self._snap_lock:
            masked = ", ".join(self._mask(a) for a in managed) if managed else "<default>"
            self._snap.account = masked
            self._snap.connected = True
            self._snap.stale_since = None  # clear staleness on successful reconnect

        # 1) Subscribe to account updates for each managed account.
        #    reqAccountUpdatesAsync is the async form — safe inside a coroutine.
        #    (The sync reqAccountUpdates() calls loop.run_until_complete(), which
        #    raises "This event loop is already running" when called from a coroutine.)
        for acct in managed:
            try:
                await self._ib.reqAccountUpdatesAsync(acct)
                logger.info("Account updates subscribed for {}", self._mask(acct))
            except Exception as e:  # noqa: BLE001
                logger.warning("reqAccountUpdatesAsync({}) failed: {}", self._mask(acct), e)

        # 2) Fetch positions via async API — sync reqPositions() calls
        #    loop.run_until_complete() which raises inside a coroutine.
        try:
            positions = await self._ib.reqPositionsAsync()
            for pos in positions:
                self._on_position(pos)
        except Exception as e:  # noqa: BLE001
            logger.debug("reqPositionsAsync() skipped: {}", e)

        # let TWS push portfolio/account-value events
        await asyncio.sleep(_BOOTSTRAP_SETTLE_SECS)

        # force one immediate portfolio pull (in case events were missed)
        for acct in managed or [""]:
            try:
                for item in self._ib.portfolio(acct):
                    self._on_portfolio(item)
            except Exception as e:  # noqa: BLE001
                logger.debug("portfolio({}) pull skipped: {}", acct, e)

        # 3) Fetch open orders
        try:
            trades = await self._ib.reqAllOpenOrdersAsync()
            for trade in trades:
                self._update_order(trade)
            logger.info("Fetched {} open orders", len(trades))
        except Exception as e:  # noqa: BLE001
            logger.debug("reqAllOpenOrdersAsync() skipped: {}", e)

        with self._snap_lock:
            n = len(self._snap.positions)
        logger.info(
            "Bootstrap complete — {} positions, {:.1f}s elapsed",
            n,
            time.monotonic() - _bootstrap_t0,
        )

        # Use frozen data so greeks arrive even when the market is closed.
        # Type 2 = FROZEN: TWS uses last-known prices after hours and automatically
        # upgrades to live data during market hours when a live subscription is active.
        self._ib.reqMarketDataType(2)
        await self._resubscribe_market_data()
        self._bootstrapped = True
        logger.info("Bootstrap complete — live resubscription enabled")
        await self._fetch_what_if_margins()

        # Start health check for this connection generation. Each successful connect
        # increments _health_gen, which causes any older health check loop to exit.
        self._health_gen += 1
        asyncio.ensure_future(self._health_check_loop(self._health_gen))

    @staticmethod
    def _mask(account: str) -> str:
        if not account or len(account) < 4:
            return "••••"
        return f"{account[0]}{'•' * (len(account) - 4)}{account[-3:]}"

    async def _fetch_what_if_margins(self) -> None:
        """Query IBKR what-if margin for every held position, concurrently.

        Sends a closing what-if order for each position; the absolute value of
        `initMarginChange` / `maintMarginChange` is the margin currently consumed
        by that position.  Results are written to `_snap.positions` as
        `margin_init` and `margin_maint` columns.
        """
        assert self._ib is not None

        with self._snap_lock:
            pos_df = self._snap.positions.copy()

        if pos_df.empty:
            return

        n = len(pos_df)
        logger.info("Fetching what-if margin for {} positions …", n)

        sem = asyncio.Semaphore(10)  # max 10 concurrent requests

        async def _query_one(row: pd.Series) -> tuple[int, str, float, float]:
            """Return (conId, account, init_margin, maint_margin)."""
            contract = row.get("_contract")
            qty = int(abs(row.get("position", 0)))
            if contract is None or qty == 0:
                return int(row["conId"]), str(row.get("account", "")), float("nan"), float("nan")
            # Contracts from portfolioEvent often have a blank exchange field.
            # SMART routes correctly for all US equities and options.
            if not getattr(contract, "exchange", None):
                contract.exchange = "SMART"
            action = "BUY" if row["position"] < 0 else "SELL"
            order = MarketOrder(action, qty)
            # tif="DAY" must be set explicitly — without it TWS sends error 10349
            # ("Order TIF was set to DAY based on order preset") which ib-async
            # treats as a fatal error and resolves the future with [] before the
            # openOrder callback (with margin data) arrives.
            order.tif = "DAY"
            # account routes the what-if to the correct account in a multi-account setup
            order.account = str(row.get("account", ""))
            async with sem:
                try:
                    state = await self._ib.whatIfOrderAsync(contract, order)

                    def _parse(v: object) -> float:
                        try:
                            return abs(float(str(v)))  # type: ignore[arg-type]
                        except (TypeError, ValueError):
                            return float("nan")

                    return (
                        int(row["conId"]),
                        str(row.get("account", "")),
                        _parse(state.initMarginChange),
                        _parse(state.maintMarginChange),
                    )
                except Exception as e:  # noqa: BLE001
                    logger.debug("whatIfOrder {} {}: {}", row.get("symbol", "?"), action, e)
                    return (
                        int(row["conId"]),
                        str(row.get("account", "")),
                        float("nan"),
                        float("nan"),
                    )

        results = await asyncio.gather(*[_query_one(row) for _, row in pos_df.iterrows()])

        # Build (conId, account) → (init, maint)
        margin_map: dict[tuple[int, str], tuple[float, float]] = {
            (con_id, acct): (init_m, maint_m) for con_id, acct, init_m, maint_m in results
        }

        margin_rows = [
            {"conId": con_id, "account": acct, "margin_init": im, "margin_maint": mm}
            for (con_id, acct), (im, mm) in margin_map.items()
        ]
        margin_df = (
            pd.DataFrame(margin_rows)
            if margin_rows
            else pd.DataFrame(columns=["conId", "account", "margin_init", "margin_maint"])
        )

        with self._snap_lock:
            df = self._snap.positions
            if df.empty:
                return
            df = df.drop(columns=["margin_init", "margin_maint"], errors="ignore")
            df = df.merge(margin_df, on=["conId", "account"], how="left")
            self._snap.positions = df.reset_index(drop=True)
            self._snap.margins_as_of = datetime.now(timezone.utc)

        valid = [(im, mm) for _, _, im, mm in results if im == im]  # NaN check
        total_init = sum(im for im, _ in valid)
        logger.info(
            "What-if margins done: {} positions, sum init margin ${:,.0f}",
            len(valid),
            total_init,
        )

    async def _resubscribe_market_data(self) -> None:
        """Subscribe to market data for currently held option contracts only."""
        assert self._ib is not None
        self._resubscribe_pending = False  # debounce: allow next event to queue again
        with self._snap_lock:
            held = self._snap.positions.copy()
        if held.empty:
            return
        wanted: set[int] = set(held["conId"].astype(int).tolist())
        # cancel ones no longer held
        for con_id in self._subscribed - wanted:
            try:
                contract = next(
                    # pyrefly: ignore [missing-attribute]
                    (t.contract for t in self._ib.tickers() if t.contract.conId == con_id),
                    None,
                )
                if contract is not None:
                    self._ib.cancelMktData(contract)
                    self._mktdata_subs.discard(con_id)
            except Exception as e:  # noqa: BLE001
                logger.debug("cancelMktData {}: {}", con_id, e)
        # subscribe to new option contracts
        new_cons: list[Contract] = []
        for _, row in held.iterrows():
            con_id = int(row["conId"])
            if con_id in self._subscribed:
                continue
            self._subscribed.add(con_id)
            if row.get("secType") != "OPT":
                continue  # greeks only for options; skip STK/ETF subscriptions
            contract = row.get("_contract")
            if contract is None:
                continue
            if not contract.exchange:
                contract.exchange = "SMART"
            new_cons.append(contract)
        for c in new_cons:
            # 106 = OptionImpliedVolatility / model greeks
            self._ib.reqMktData(c, genericTickList="106", snapshot=False)
            self._mktdata_subs.add(c.conId)
        if new_cons:
            logger.info("Subscribed to {} new tickers ({} total active feeds)",
                        len(new_cons), len(self._mktdata_subs))
        if len(self._mktdata_subs) > _MKTDATA_PACING_CEILING:
            logger.warning(
                "Active market-data subscriptions {} exceeds pacing ceiling ({}) — "
                "risk of error 100/165 under load",
                len(self._mktdata_subs),
                _MKTDATA_PACING_CEILING,
            )

    async def _health_check_loop(
        self, gen: int, interval: float = _HEALTH_CHECK_INTERVAL_SECS
    ) -> None:
        """Periodic liveness check while connected.

        Detects zombie connections where the socket appears open but IBKR has stopped
        responding. Exits automatically when superseded by a newer generation (reconnect)
        or when frozen. Schedules _connect_with_retry if connection is found dead.

        The `interval` parameter is intentionally overridable for tests (pass a tiny value).
        """
        while True:
            await asyncio.sleep(interval)
            if gen != self._health_gen or self._frozen:
                return  # superseded or frozen — exit silently
            if self._ib is None or not self._ib.isConnected():
                logger.warning(
                    "Health check: connection lost (gen={}) — scheduling reconnect", gen
                )
                with self._snap_lock:
                    self._snap.connected = False
                    if self._snap.stale_since is None:
                        self._snap.stale_since = datetime.now(timezone.utc)
                self._subscribed.clear()
                self._mktdata_subs.clear()
                if not self._connecting:
                    asyncio.ensure_future(self._connect_with_retry())
                return
            logger.debug("Health check OK (gen={})", gen)

    async def _disconnect(self) -> None:
        if self._ib and self._ib.isConnected():
            self._ib.disconnect()

    # ---- event handlers ----------------------------------------------------

    def _wire_handlers(self, ib: IB) -> None:
        ib.updatePortfolioEvent += self._on_portfolio
        ib.positionEvent += self._on_position
        ib.accountValueEvent += self._on_account_value
        ib.pendingTickersEvent += self._on_tickers
        ib.openOrderEvent += self._on_open_order
        ib.orderStatusEvent += self._on_order_status
        ib.errorEvent += self._on_error
        ib.disconnectedEvent += self._on_disconnect

    @staticmethod
    def _build_position_row(
        contract: Contract,
        account: str,
        position: float,
        avg_cost: float,
        market_price: float = float("nan"),
        market_value: float = float("nan"),
        unrealized_pnl: float = float("nan"),
        realized_pnl: float = float("nan"),
    ) -> dict[str, object]:
        c = contract
        return {
            "account":      account,
            "conId":        c.conId,
            "symbol":       c.symbol,
            "secType":      c.secType,
            "currency":     getattr(c, "currency", "") or "",
            "primaryExch":  getattr(c, "primaryExch", "") or "",
            "right":        getattr(c, "right", "") or "",
            "strike":       float(getattr(c, "strike", 0.0) or 0.0),
            "expiry":       getattr(c, "lastTradeDateOrContractMonth", "") or "",
            "position":     position,
            "avgCost":      avg_cost,
            "marketPrice":  market_price,
            "marketValue":  market_value,
            "unrealizedPNL": unrealized_pnl,
            "realizedPNL":  realized_pnl,
            "_contract":    c,
        }

    def _on_portfolio(self, item: PortfolioItem) -> None:
        c = item.contract
        acct = item.account or ""
        logger.debug(
            "Portfolio event: {} {} {} pos={:.0f} val=${:,.0f}",
            self._mask(acct),
            c.symbol,
            c.secType,
            float(item.position),
            float(item.marketValue),
        )
        # pyrefly: ignore [unnecessary-type-conversion]
        row = self._build_position_row(
            c, acct,
            float(item.position),
            float(item.averageCost),
            float(item.marketPrice),
            float(item.marketValue),
            float(item.unrealizedPNL),
            float(item.realizedPNL),
        )
        key = (c.conId, acct)
        with self._snap_lock:
            # O(1) dict update; DataFrame rebuilt from the full dict in one shot.
            # This replaces the old mask→filter→concat→reset_index pattern which
            # allocated O(n) new arrays on every event even for existing-row updates.
            if item.position == 0:
                self._positions_store.pop(key, None)
            else:
                self._positions_store[key] = row
            self._snap.positions = (
                pd.DataFrame(self._positions_store.values())
                if self._positions_store
                else pd.DataFrame()
            )
            self._snap.as_of = datetime.now(timezone.utc)
        if self._loop is not None and self._bootstrapped and not self._resubscribe_pending:
            self._resubscribe_pending = True
            asyncio.run_coroutine_threadsafe(self._resubscribe_market_data(), self._loop)

    def _on_position(self, position: Any) -> None:
        """Fallback handler from reqPositions — only adds rows not already seen
        via updatePortfolioEvent (which carries richer marketValue/PnL data)."""
        c = position.contract
        acct = position.account or ""
        key = (c.conId, acct)
        with self._snap_lock:
            if key in self._positions_store:
                return  # already have a richer PortfolioItem for this (conId, account)
            if position.position == 0:
                return
            # pyrefly: ignore [unnecessary-type-conversion]
            row = self._build_position_row(
                c, acct, float(position.position), float(position.avgCost)
            )
            self._positions_store[key] = row
            self._snap.positions = pd.DataFrame(self._positions_store.values())
            self._snap.as_of = datetime.now(timezone.utc)
        if self._loop is not None and self._bootstrapped and not self._resubscribe_pending:
            self._resubscribe_pending = True
            asyncio.run_coroutine_threadsafe(self._resubscribe_market_data(), self._loop)

    def _on_account_value(self, value: Any) -> None:
        try:
            d = Decimal(str(value.value))
        except Exception:  # noqa: BLE001
            return
        acct = value.account or ""
        with self._snap_lock:
            if acct not in self._snap.account_values:
                self._snap.account_values[acct] = {}
            self._snap.account_values[acct][value.tag] = d
            self._snap.as_of = datetime.now(timezone.utc)

    def _on_tickers(self, tickers: set[Ticker]) -> None:
        with self._snap_lock:
            for t in tickers:
                # pyrefly: ignore [missing-attribute]
                con_id = t.contract.conId
                snap = self._snap.tickers.get(con_id) or TickerSnap()
                if not math.isnan(t.last):
                    # pyrefly: ignore [unnecessary-type-conversion]
                    snap.last = float(t.last)
                if not math.isnan(t.bid):
                    # pyrefly: ignore [unnecessary-type-conversion]
                    snap.bid = float(t.bid)
                if not math.isnan(t.ask):
                    # pyrefly: ignore [unnecessary-type-conversion]
                    snap.ask = float(t.ask)
                mg = t.modelGreeks
                if mg is not None:
                    # pyrefly: ignore [unnecessary-type-conversion]
                    snap.delta = float(mg.delta) if mg.delta is not None else snap.delta
                    # pyrefly: ignore [unnecessary-type-conversion]
                    snap.gamma = float(mg.gamma) if mg.gamma is not None else snap.gamma
                    # pyrefly: ignore [unnecessary-type-conversion]
                    snap.theta = float(mg.theta) if mg.theta is not None else snap.theta
                    # pyrefly: ignore [unnecessary-type-conversion]
                    snap.vega = float(mg.vega) if mg.vega is not None else snap.vega
                    snap.iv = (
                        # pyrefly: ignore [unnecessary-type-conversion]
                        float(mg.impliedVol) if mg.impliedVol is not None else snap.iv
                    )
                    snap.underlying_px = (
                        # pyrefly: ignore [unnecessary-type-conversion]
                        float(mg.undPrice) if mg.undPrice is not None else snap.underlying_px
                    )
                self._snap.tickers[con_id] = snap
            self._snap.as_of = datetime.now(timezone.utc)

    def _update_order(self, trade: Trade) -> None:
        """Upsert a Trade into the orders DataFrame."""
        c = trade.contract
        o = trade.order
        s = trade.orderStatus
        lmt = float(o.lmtPrice) if getattr(o, "lmtPrice", None) not in (None, 0) else float("nan")
        row = {
            "orderId": o.orderId,
            "account": getattr(o, "account", "") or "",
            "symbol": c.symbol,
            "secType": c.secType,
            "right": getattr(c, "right", "") or "",
            "strike": float(getattr(c, "strike", 0.0) or 0.0),
            "expiry": getattr(c, "lastTradeDateOrContractMonth", "") or "",
            "action": o.action,
            "qty": float(o.totalQuantity),
            "filled": float(s.filled),
            "remaining": float(s.remaining),
            "orderType": o.orderType,
            "lmtPrice": lmt,
            "status": s.status,
        }
        with self._snap_lock:
            df = self._snap.orders
            done = s.status in {"Cancelled", "ApiCancelled", "Filled"}
            if df.empty:
                df = pd.DataFrame(columns=_ORDER_COLUMNS) if done else pd.DataFrame([row])
            else:
                df = df[df["orderId"] != o.orderId]
                if not done:
                    df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
            self._snap.orders = df.reset_index(drop=True)
            self._snap.as_of = datetime.now(timezone.utc)

    def _on_open_order(self, trade: Trade) -> None:
        self._update_order(trade)

    def _on_order_status(self, trade: Trade) -> None:
        self._update_order(trade)

    def _on_error(self, reqId: int, errorCode: int, errorString: str, contract) -> None:  # noqa: N803
        if errorCode in _IBKR_INFO_CODES:
            return  # farm-connection chatter — not actionable
        with self._snap_lock:
            self._snap.errors.append((datetime.now(timezone.utc), errorCode, errorString))
        sym = getattr(contract, "symbol", None) if contract is not None else None
        if errorCode in _IBKR_PACING_CODES:
            # Pacing violations degrade data quality and indicate over-subscription.
            # Log at ERROR so they surface in dashboard.log even when scrolled past.
            logger.error(
                "IB PACING err {} ({}){}: {} — active subscriptions: {}",
                errorCode,
                reqId,
                f" [{sym}]" if sym else "",
                errorString,
                len(self._subscribed),
            )
        else:
            if sym:
                logger.warning("IB err {} ({}) [{}]: {}", errorCode, reqId, sym, errorString)
            else:
                logger.warning("IB err {} ({}): {}", errorCode, reqId, errorString)

    def _on_disconnect(self) -> None:
        uptime = time.monotonic() - self._connect_start_time if self._connect_start_time else 0.0
        with self._snap_lock:
            self._snap.connected = False
            if self._snap.stale_since is None:
                self._snap.stale_since = datetime.now(timezone.utc)
        if self._frozen:
            logger.info(
                "IBKR disconnected (frozen — derive.py has the CID) uptime={:.0f}s", uptime
            )
            return
        logger.warning("IBKR disconnected after {:.0f}s — reconnecting", uptime)
        if self._loop is not None:
            self._subscribed.clear()
            if not self._connecting:  # don't spawn a second retry loop
                self._retry_fut = asyncio.run_coroutine_threadsafe(self._connect_with_retry(), self._loop)


def get_client() -> IBClient:
    """Module-level accessor."""
    return IBClient()
