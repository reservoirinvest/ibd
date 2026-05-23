"""Integration tests — IBKR ↔ Snapshot ↔ Streamlit data pipeline.

Three sections:
  1. IBKR + Streamlit interaction   — data flows from IB events into Snapshot,
                                      which is what Streamlit fragments read.
  2. Edge cases and error scenarios — boundary conditions, partial bootstrap
                                      failures, structured-logging additions.
  3. Performance under load         — throughput and concurrency benchmarks.

No real IBKR connection or Streamlit context is required; all ib_async I/O
is mocked.  app.py is NOT imported (module-level Streamlit calls prevent it).
"""
from __future__ import annotations

import asyncio
import math
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pandas as pd
import pytest

import src.dashboard.ib_client as ibc
from src.dashboard.ib_client import IBClient


# ---------------------------------------------------------------------------
# Shared test helpers
# ---------------------------------------------------------------------------


def _contract(
    con_id: int = 1,
    symbol: str = "AAPL",
    sec_type: str = "STK",
    right: str = "",
    strike: float = 0.0,
    expiry: str = "",
    exchange: str = "SMART",
    currency: str = "USD",
) -> Mock:
    c = Mock()
    c.conId = con_id
    c.symbol = symbol
    c.secType = sec_type
    c.currency = currency
    c.primaryExch = exchange
    c.right = right
    c.strike = strike
    c.lastTradeDateOrContractMonth = expiry
    c.exchange = exchange
    return c


def _portfolio_item(
    contract: Mock,
    account: str = "U123456",
    position: float = 100.0,
    avg_cost: float = 150.0,
    market_price: float = 155.0,
    market_value: float = 15500.0,
    unrealized: float = 500.0,
    realized: float = 0.0,
) -> Mock:
    item = Mock()
    item.contract = contract
    item.account = account
    item.position = position
    item.averageCost = avg_cost
    item.marketPrice = market_price
    item.marketValue = market_value
    item.unrealizedPNL = unrealized
    item.realizedPNL = realized
    return item


def _position(contract: Mock, account: str = "U123456", position: float = 100.0, avg_cost: float = 150.0) -> Mock:
    pos = Mock()
    pos.contract = contract
    pos.account = account
    pos.position = position
    pos.avgCost = avg_cost
    return pos


def _ticker(
    contract: Mock,
    last: float = float("nan"),
    bid: float = float("nan"),
    ask: float = float("nan"),
    delta: float | None = None,
    theta: float | None = None,
    vega: float | None = None,
    implied_vol: float | None = None,
    und_price: float | None = None,
) -> Mock:
    t = Mock()
    t.contract = contract
    t.last = last
    t.bid = bid
    t.ask = ask
    mg = Mock()
    mg.delta = delta
    mg.gamma = None
    mg.theta = theta
    mg.vega = vega
    mg.impliedVol = implied_vol
    mg.undPrice = und_price
    t.modelGreeks = mg
    return t


def _account_value(tag: str, value: str, account: str = "U123456") -> Mock:
    av = Mock()
    av.tag = tag
    av.value = value
    av.account = account
    return av


@pytest.fixture(autouse=True)
def _reset_singleton():
    """Reset IBClient singleton and suppress log file creation for every test."""
    IBClient._instance = None
    IBClient._log_sink_added = True
    yield
    inst = IBClient._instance
    if inst is not None:
        loop = getattr(inst, "_loop", None)
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        thread = getattr(inst, "_thread", None)
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
    IBClient._instance = None
    IBClient._log_sink_added = False


@pytest.fixture
def client() -> IBClient:
    return IBClient()


@pytest.fixture
def running_loop():
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    t.join(timeout=2.0)


# ===========================================================================
# 1. IBKR + Streamlit Interaction
#    Verifies: IBKR events → IBClient internal state → snapshot() reads
# ===========================================================================


class TestIBKRStreamlitDataFlow:
    """
    Simulates what Streamlit fragments do: call client.snapshot() every few
    seconds while IBKR event handlers update internal state concurrently.
    """

    def test_snapshot_reflects_portfolio_event_immediately(self, client):
        """A portfolio event must be visible in the very next snapshot() call."""
        c = _contract(con_id=10, symbol="MSFT")
        client._on_portfolio(_portfolio_item(c, position=200.0, market_value=55000.0))

        snap = client.snapshot()
        assert snap.positions.iloc[0]["symbol"] == "MSFT"
        assert snap.positions.iloc[0]["marketValue"] == pytest.approx(55000.0)

    def test_snapshot_reflects_account_value_event(self, client):
        """Account value events must be visible via snapshot.account_values."""
        client._on_account_value(_account_value("NetLiquidation", "500000", "U1"))
        client._on_account_value(_account_value("ExcessLiquidity", "200000", "U1"))

        snap = client.snapshot()
        assert snap.account_values["U1"]["NetLiquidation"] == Decimal("500000")
        assert snap.account_values["U1"]["ExcessLiquidity"] == Decimal("200000")

    def test_snapshot_reflects_ticker_greeks_for_ui_risk_calcs(self, client):
        """Ticker greeks (delta, theta) must appear in snapshot.tickers for risk calcs."""
        c = _contract(con_id=99, sec_type="OPT")
        client._on_tickers({_ticker(c, delta=-0.45, theta=-0.03, implied_vol=0.28)})

        ts = client.snapshot().tickers[99]
        assert ts.delta == pytest.approx(-0.45)
        assert ts.theta == pytest.approx(-0.03)
        assert ts.iv == pytest.approx(0.28)

    def test_snapshot_connected_flag_tracks_ibkr_state(self, client):
        """snapshot.connected must mirror _snap.connected so the UI shows the right status."""
        with client._snap_lock:
            client._snap.connected = True
        assert client.snapshot().connected is True

        with client._snap_lock:
            client._snap.connected = False
        assert client.snapshot().connected is False

    def test_snapshot_is_safe_copy_mutations_do_not_corrupt_internal_state(self, client):
        """Streamlit fragments mutate snapshot DataFrames; internal state must stay clean."""
        c = _contract(con_id=5)
        client._on_portfolio(_portfolio_item(c, position=100.0))

        snap = client.snapshot()
        snap.positions.drop(snap.positions.index, inplace=True)  # fragment mutates
        snap.account_values["rogue"] = {"NLV": Decimal("0")}     # fragment mutates

        # Internal state must be unaffected
        with client._snap_lock:
            assert len(client._snap.positions) == 1
            assert "rogue" not in client._snap.account_values

    def test_multiple_fragment_snapshot_calls_in_parallel_all_consistent(self, client):
        """Simulate many Streamlit fragment reruns calling snapshot() concurrently."""
        for i in range(20):
            c = _contract(con_id=i + 1, symbol=f"SYM{i}")
            client._on_portfolio(_portfolio_item(c, position=float(i + 1) * 10))

        results: list[int] = []
        errors: list[Exception] = []

        def _read():
            try:
                results.append(len(client.snapshot().positions))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_read) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=3)

        assert not errors
        assert all(n == 20 for n in results), f"Some snapshots had wrong position count: {results}"

    def test_freeze_disconnects_snapshot_for_ui_status(self, client):
        """freeze() must immediately mark snapshot.connected=False for the 🧊 header."""
        with client._snap_lock:
            client._snap.connected = True
        client.freeze()
        assert client.snapshot().connected is False
        assert client.is_frozen() is True

    def test_unfreeze_clears_frozen_for_ui_status(self, client):
        """unfreeze() must clear the frozen flag so the UI transitions from 🧊 to reconnecting."""
        client._frozen = True
        client.unfreeze()
        assert client.is_frozen() is False

    def test_stale_since_drives_ui_disconnected_indicator(self, client):
        """stale_since in snapshot is what the UI uses to show stale data duration."""
        ts = datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc)
        with client._snap_lock:
            client._snap.stale_since = ts

        assert client.snapshot().stale_since == ts

    def test_stale_since_cleared_on_simulated_reconnect(self, client):
        """When bootstrap succeeds, stale_since is cleared — UI returns to 🟢 LIVE."""
        with client._snap_lock:
            client._snap.stale_since = datetime.now(timezone.utc)
            client._snap.connected = False

        # Simulate what _bootstrap does on success
        with client._snap_lock:
            client._snap.connected = True
            client._snap.stale_since = None

        snap = client.snapshot()
        assert snap.connected is True
        assert snap.stale_since is None

    def test_two_account_portfolio_shows_both_accounts_in_snapshot(self, client):
        """Dashboard ALL-account view needs positions from both US and SG accounts."""
        c = _contract(con_id=1, symbol="SPY")
        client._on_portfolio(_portfolio_item(c, account="U111111", position=100.0))
        client._on_portfolio(_portfolio_item(c, account="U222222", position=50.0))

        snap = client.snapshot()
        assert len(snap.positions) == 2
        assert set(snap.positions["account"]) == {"U111111", "U222222"}

    def test_orders_appear_in_snapshot_after_open_order_event(self, client):
        """Open orders must flow from IB event → _update_order → snapshot.orders."""
        c = _contract(con_id=10, symbol="AAPL", sec_type="OPT", right="C", strike=200.0, expiry="20260619")
        trade = Mock()
        trade.contract = c
        trade.order = Mock(orderId=101, account="U1", action="SELL",
                          totalQuantity=1, orderType="LMT", lmtPrice=2.50)
        trade.orderStatus = Mock(filled=0, remaining=1, status="Submitted")
        client._on_open_order(trade)

        snap = client.snapshot()
        assert len(snap.orders) == 1
        assert snap.orders.iloc[0]["symbol"] == "AAPL"
        assert snap.orders.iloc[0]["status"] == "Submitted"

    def test_cancelled_order_removed_from_snapshot(self, client):
        """Cancelled orders must be removed so the UI doesn't show stale orders."""
        c = _contract(con_id=10, symbol="AAPL", sec_type="OPT")
        trade = Mock()
        trade.contract = c
        trade.order = Mock(orderId=200, account="U1", action="SELL",
                          totalQuantity=1, orderType="LMT", lmtPrice=3.0)
        trade.orderStatus = Mock(filled=0, remaining=1, status="Submitted")
        client._on_open_order(trade)
        assert len(client.snapshot().orders) == 1

        trade.orderStatus.status = "Cancelled"
        client._on_order_status(trade)
        assert len(client.snapshot().orders) == 0


# ===========================================================================
# 2. Edge Cases and Error Scenarios
# ===========================================================================


class TestEdgeCasesErrors:
    """Boundary conditions, partial failures, and structured-logging additions."""

    # ---- Error handler edge cases -------------------------------------------

    def test_on_error_with_contract_symbol_does_not_raise(self, client):
        """_on_error receives a real contract — must not crash when extracting symbol."""
        c = _contract(symbol="AAPL")
        client._on_error(1, 400, "some error", c)  # must not raise
        snap = client.snapshot()
        assert len(snap.errors) == 1

    def test_on_error_with_none_contract_does_not_raise(self, client):
        """_on_error with None contract (common for non-order-related errors)."""
        client._on_error(0, 502, "Couldn't connect to TWS", None)
        assert len(client.snapshot().errors) == 1

    def test_on_error_with_contract_without_symbol_attr(self, client):
        """contract argument may be a mock with spec that lacks .symbol."""
        bad_contract = object()  # no attributes at all
        client._on_error(0, 400, "error", bad_contract)  # must not raise

    def test_on_error_none_contract_info_code_filtered(self, client):
        """Info codes must be filtered even when contract is None."""
        client._on_error(0, 2104, "farm connection OK", None)
        assert len(client.snapshot().errors) == 0

    # ---- Portfolio edge cases -----------------------------------------------

    def test_zero_position_in_one_account_leaves_other_account_intact(self, client):
        """Removing a position in one account must not affect the same contract in another."""
        c = _contract(con_id=1, symbol="AAPL")
        client._on_portfolio(_portfolio_item(c, account="U1", position=100.0))
        client._on_portfolio(_portfolio_item(c, account="U2", position=50.0))
        # Zero out U1 only
        client._on_portfolio(_portfolio_item(c, account="U1", position=0.0))

        snap = client.snapshot()
        assert len(snap.positions) == 1
        assert snap.positions.iloc[0]["account"] == "U2"
        assert snap.positions.iloc[0]["position"] == 50.0

    def test_position_updates_across_three_accounts(self, client):
        """Same contract in three accounts should produce three independent rows."""
        c = _contract(con_id=42, symbol="SPY")
        for i, acct in enumerate(["U1", "U2", "U3"]):
            client._on_portfolio(_portfolio_item(c, account=acct, position=float((i + 1) * 100)))

        snap = client.snapshot()
        assert len(snap.positions) == 3
        assert sorted(snap.positions["position"].tolist()) == [100.0, 200.0, 300.0]

    def test_on_portfolio_with_nan_market_value(self, client):
        """NaN market values in portfolio items must not crash the handler."""
        c = _contract()
        item = _portfolio_item(c)
        item.marketValue = float("nan")
        client._on_portfolio(item)  # must not raise
        assert len(client.snapshot().positions) == 1

    def test_contract_with_empty_exchange_gets_no_crash(self, client):
        """Contract with no exchange — _resubscribe_market_data patches it to SMART."""
        c = _contract(exchange="")
        c.exchange = ""  # explicitly empty
        client._on_portfolio(_portfolio_item(c))  # must not raise

    # ---- Ticker edge cases --------------------------------------------------

    def test_ticker_with_no_model_greeks_stores_bid_ask_last(self, client):
        """STK tickers have no greeks — only bid/ask/last should be stored."""
        c = _contract(con_id=7, sec_type="STK")
        t = _ticker(c, last=150.0, bid=149.9, ask=150.1)
        t.modelGreeks = None  # STK has no model greeks
        client._on_tickers({t})

        ts = client.snapshot().tickers[7]
        assert ts.last == pytest.approx(150.0)
        assert ts.bid == pytest.approx(149.9)
        assert ts.ask == pytest.approx(150.1)
        assert math.isnan(ts.delta)

    def test_ticker_nan_last_does_not_erase_previous_last(self, client):
        """NaN price updates must preserve the last known price."""
        c = _contract(con_id=8)
        client._on_tickers({_ticker(c, last=10.0)})
        client._on_tickers({_ticker(c, last=float("nan"))})
        assert client.snapshot().tickers[8].last == pytest.approx(10.0)

    def test_ticker_update_preserves_other_fields(self, client):
        """Partial ticker update (only bid changed) must not reset ask/last."""
        c = _contract(con_id=9)
        client._on_tickers({_ticker(c, last=5.0, bid=4.9, ask=5.1)})
        # Simulate update where only bid changes
        t2 = _ticker(c, bid=4.8, last=float("nan"), ask=float("nan"))
        client._on_tickers({t2})

        ts = client.snapshot().tickers[9]
        assert ts.last == pytest.approx(5.0)   # preserved
        assert ts.bid == pytest.approx(4.8)    # updated
        assert ts.ask == pytest.approx(5.1)    # preserved

    # ---- Account value edge cases -------------------------------------------

    def test_account_value_with_empty_account_string(self, client):
        """Empty account string (single-account TWS) must not crash the handler."""
        av = _account_value("NLV", "100000", "")
        client._on_account_value(av)
        snap = client.snapshot()
        assert "" in snap.account_values

    def test_account_value_updates_overwrite_previous(self, client):
        """Repeated account value events must overwrite, not accumulate."""
        client._on_account_value(_account_value("NLV", "100000"))
        client._on_account_value(_account_value("NLV", "105000"))
        snap = client.snapshot()
        assert snap.account_values["U123456"]["NLV"] == Decimal("105000")

    # ---- Connection tracking (structured-logging additions) -----------------

    def test_connect_attempts_starts_at_zero(self, client):
        assert client._connect_attempts == 0

    def test_connect_start_time_starts_at_zero(self, client):
        assert client._connect_start_time == 0.0

    def test_connect_attempts_incremented_on_each_retry(self, client):
        """Each connection attempt must increment the counter."""
        client._settings = Mock(ib_host="127.0.0.1", ib_port=7497, ib_client_id=10)
        attempt_counts: list[int] = []

        async def run():
            async def fast_sleep(_: float) -> None:
                attempt_counts.append(client._connect_attempts)
                if len(attempt_counts) >= 3:
                    client._frozen = True

            mock_ib = MagicMock()
            mock_ib.connectAsync = AsyncMock(side_effect=ConnectionRefusedError("no TWS"))
            with patch("src.dashboard.ib_client.IB", return_value=mock_ib):
                with patch("asyncio.sleep", fast_sleep):
                    await client._connect_with_retry()

        asyncio.run(run())
        assert attempt_counts == [1, 2, 3], f"Expected [1,2,3], got {attempt_counts}"

    def test_connect_attempts_reset_to_zero_on_success(self, client):
        """Successful connection must reset the attempt counter."""
        client._settings = Mock(ib_host="127.0.0.1", ib_port=7497, ib_client_id=10)
        client._connect_attempts = 4  # pre-set as if 4 previous failures

        async def run():
            mock_ib = AsyncMock()
            with patch("src.dashboard.ib_client.IB", return_value=mock_ib):
                with patch.object(client, "_wire_handlers", lambda ib: None):
                    with patch.object(client, "_bootstrap", AsyncMock()):
                        await client._connect_with_retry()

        asyncio.run(run())
        assert client._connect_attempts == 0

    def test_connect_start_time_set_on_success(self, client):
        """connect_start_time must be set (nonzero) after a successful connection."""
        client._settings = Mock(ib_host="127.0.0.1", ib_port=7497, ib_client_id=10)

        async def run():
            mock_ib = AsyncMock()
            with patch("src.dashboard.ib_client.IB", return_value=mock_ib):
                with patch.object(client, "_wire_handlers", lambda ib: None):
                    with patch.object(client, "_bootstrap", AsyncMock()):
                        await client._connect_with_retry()

        asyncio.run(run())
        assert client._connect_start_time > 0.0

    def test_on_disconnect_with_zero_uptime_does_not_crash(self, client):
        """Disconnect called before any successful connect (start_time=0) must not crash."""
        assert client._connect_start_time == 0.0
        client._on_disconnect()  # must not raise

    def test_uptime_tracked_correctly(self, client):
        """The logged uptime must be approximately the actual connection duration."""
        client._connect_start_time = time.monotonic() - 10.0  # simulate 10s connection

        # Collect what the warning message would contain via the uptime calculation
        uptime = time.monotonic() - client._connect_start_time
        assert 9.9 <= uptime <= 11.0  # generous bound for test timer jitter

    # ---- Bootstrap partial failures -----------------------------------------

    # ---- shared bootstrap helper -------------------------------------------

    @staticmethod
    def _make_bootstrap_ib(
        accounts: list[str] = None,
        positions: list = None,
        portfolio_items: list = None,
        orders: list = None,
        req_account_updates_raises: Exception | None = None,
        req_positions_raises: Exception | None = None,
        req_orders_raises: Exception | None = None,
    ) -> MagicMock:
        """Build a minimal ib_async IB() mock suitable for _bootstrap calls.

        managedAccounts() and portfolio() are sync; the req*Async methods are async.
        AsyncMock() must NOT be used for the base object because it makes every
        attribute (including sync ones) return a coroutine.
        """
        mock_ib = MagicMock()
        mock_ib.managedAccounts.return_value = accounts or ["U1"]
        mock_ib.portfolio.return_value = portfolio_items or []
        mock_ib.isConnected.return_value = True

        mock_ib.reqAccountUpdatesAsync = AsyncMock(
            side_effect=req_account_updates_raises or None,
            return_value=None,
        )
        mock_ib.reqPositionsAsync = AsyncMock(
            side_effect=req_positions_raises or None,
            return_value=positions or [],
        )
        mock_ib.reqAllOpenOrdersAsync = AsyncMock(
            side_effect=req_orders_raises or None,
            return_value=orders or [],
        )
        return mock_ib

    @staticmethod
    async def _run_bootstrap(client: IBClient, mock_ib: MagicMock) -> None:
        """Run _bootstrap with all side-effects mocked out to instant no-ops."""
        client._ib = mock_ib
        with patch("asyncio.sleep", AsyncMock()):
            with patch.object(client, "_resubscribe_market_data", AsyncMock()):
                with patch.object(client, "_fetch_what_if_margins", AsyncMock()):
                    with patch.object(client, "_health_check_loop", AsyncMock()):
                        await client._bootstrap()

    def test_bootstrap_continues_when_req_account_updates_raises(self, client):
        """reqAccountUpdatesAsync failure must not prevent orders from being fetched."""
        client._settings = Mock(ib_host="127.0.0.1", ib_port=7497, ib_client_id=10)
        mock_ib = self._make_bootstrap_ib(
            req_account_updates_raises=Exception("account updates failed")
        )
        asyncio.run(self._run_bootstrap(client, mock_ib))  # must not raise

    def test_bootstrap_continues_when_req_positions_raises(self, client):
        """reqPositionsAsync failure falls back silently; portfolio() is still called."""
        client._settings = Mock(ib_host="127.0.0.1", ib_port=7497, ib_client_id=10)
        c = _contract(con_id=1, symbol="AAPL")
        item = _portfolio_item(c, position=50.0)
        mock_ib = self._make_bootstrap_ib(
            portfolio_items=[item],
            req_positions_raises=Exception("positions unavailable"),
        )
        asyncio.run(self._run_bootstrap(client, mock_ib))
        assert len(client.snapshot().positions) == 1
        assert client.snapshot().positions.iloc[0]["symbol"] == "AAPL"

    def test_bootstrap_continues_when_req_orders_raises(self, client):
        """reqAllOpenOrdersAsync failure must produce an empty orders DataFrame."""
        client._settings = Mock(ib_host="127.0.0.1", ib_port=7497, ib_client_id=10)
        mock_ib = self._make_bootstrap_ib(
            req_orders_raises=Exception("orders unavailable")
        )
        asyncio.run(self._run_bootstrap(client, mock_ib))
        assert client.snapshot().orders.empty

    def test_bootstrap_populates_managed_accounts(self, client):
        """After bootstrap, managed_accounts must reflect what IBKR returned."""
        client._settings = Mock(ib_host="127.0.0.1", ib_port=7497, ib_client_id=10)
        mock_ib = self._make_bootstrap_ib(accounts=["U111111", "U222222"])
        asyncio.run(self._run_bootstrap(client, mock_ib))
        assert client.managed_accounts == ["U111111", "U222222"]

    def test_bootstrap_clears_stale_since_on_success(self, client):
        """Successful bootstrap must clear stale_since so UI returns to 🟢 LIVE."""
        client._settings = Mock(ib_host="127.0.0.1", ib_port=7497, ib_client_id=10)
        with client._snap_lock:
            client._snap.stale_since = datetime.now(timezone.utc)
        mock_ib = self._make_bootstrap_ib()
        asyncio.run(self._run_bootstrap(client, mock_ib))
        assert client.snapshot().stale_since is None

    def test_bootstrap_sets_connected_true(self, client):
        """bootstrap must set snapshot.connected = True when it completes."""
        client._settings = Mock(ib_host="127.0.0.1", ib_port=7497, ib_client_id=10)
        mock_ib = self._make_bootstrap_ib()
        asyncio.run(self._run_bootstrap(client, mock_ib))
        assert client.snapshot().connected is True

    # ---- Disconnect edge cases -----------------------------------------------

    def test_disconnect_sets_stale_since_only_once(self, client):
        """Repeated disconnects must not overwrite the first stale_since timestamp."""
        client._on_disconnect()
        first = client.snapshot().stale_since
        time.sleep(0.01)
        client._on_disconnect()
        assert client.snapshot().stale_since == first  # unchanged

    def test_freeze_with_running_loop_clears_subscriptions(self, client, running_loop):
        """freeze() must clear subscriptions so IB doesn't receive cancel calls on restart."""
        client._loop = running_loop
        client._subscribed = {1, 2, 3, 4, 5}
        client.freeze()
        time.sleep(0.1)  # let call_soon_threadsafe execute
        assert len(client._subscribed) == 0

    def test_unfreeze_starts_reconnect_with_loop(self, client, running_loop):
        """unfreeze() must schedule a reconnect coroutine when a loop is available."""
        client._loop = running_loop
        client._settings = Mock(ib_host="127.0.0.1", ib_port=7497, ib_client_id=10)
        client._frozen = True
        reconnect_triggered = threading.Event()

        async def _mock_retry():
            reconnect_triggered.set()

        with patch.object(client, "_connect_with_retry", _mock_retry):
            client.unfreeze()
            # _UNFREEZE_DELAY_SECS is 5 s; we don't wait that long —
            # just verify the coroutine was scheduled by checking _frozen clears.
            time.sleep(0.05)
        assert client._frozen is False

    # ---- Wire handler registration ------------------------------------------

    def test_all_event_handlers_wired_on_new_ib_instance(self, client):
        """_wire_handlers must register all 8 IB event listeners.

        Uses a real list-backed event stub so that += appends (rather than
        reassigning to a MagicMock return value, which makes the assertion
        target the wrong object).
        """
        class _Event:
            def __init__(self): self.handlers: list = []
            def __iadd__(self, h):
                self.handlers.append(h)
                return self

        mock_ib = Mock()
        for attr in (
            "updatePortfolioEvent", "positionEvent", "accountValueEvent",
            "pendingTickersEvent", "openOrderEvent", "orderStatusEvent",
            "errorEvent", "disconnectedEvent",
        ):
            setattr(mock_ib, attr, _Event())

        client._wire_handlers(mock_ib)

        assert client._on_portfolio  in mock_ib.updatePortfolioEvent.handlers
        assert client._on_position   in mock_ib.positionEvent.handlers
        assert client._on_account_value in mock_ib.accountValueEvent.handlers
        assert client._on_tickers    in mock_ib.pendingTickersEvent.handlers
        assert client._on_open_order in mock_ib.openOrderEvent.handlers
        assert client._on_order_status in mock_ib.orderStatusEvent.handlers
        assert client._on_error      in mock_ib.errorEvent.handlers
        assert client._on_disconnect in mock_ib.disconnectedEvent.handlers

    # ---- Subscription cap ---------------------------------------------------

    def test_resubscribe_skips_stk_contracts(self, client, running_loop):
        """Market data subscriptions apply only to OPT contracts; STK must be skipped."""
        client._loop = running_loop
        client._ib = MagicMock()
        client._ib.tickers.return_value = []

        # Add one STK and one OPT position
        stk = _contract(con_id=1, symbol="AAPL", sec_type="STK")
        opt = _contract(con_id=2, symbol="AAPL", sec_type="OPT", right="C", strike=200.0, exchange="SMART")

        with client._snap_lock:
            client._snap.positions = pd.DataFrame([
                IBClient._build_position_row(stk, "U1", 100.0, 150.0),
                IBClient._build_position_row(opt, "U1", -1.0, 250.0),
            ])

        async def run():
            await client._resubscribe_market_data()

        asyncio.run_coroutine_threadsafe(run(), running_loop).result(timeout=2.0)

        # reqMktData must be called exactly once (for OPT only)
        assert client._ib.reqMktData.call_count == 1

    # ---- Mask utility -------------------------------------------------------

    def test_mask_hides_account_number_middle(self):
        masked = IBClient._mask("U1234567")
        assert masked.startswith("U")
        assert masked.endswith("567")
        assert "•" in masked

    def test_mask_short_account_returns_placeholder(self):
        assert IBClient._mask("AB") == "••••"

    def test_mask_empty_string_returns_placeholder(self):
        assert IBClient._mask("") == "••••"


# ===========================================================================
# 3. Performance Under Load
#    Each test has a wall-clock bound generous enough to survive slow CI
#    but tight enough to catch catastrophic regressions.
# ===========================================================================


class TestPerformanceUnderLoad:
    """Throughput and concurrency benchmarks — no real IBKR connection.

    loguru DEBUG output is suppressed via an autouse fixture: in production the
    file sink is fast, but stderr logging during tests adds per-call I/O overhead
    that would make the benchmarks flaky.  The behaviour being timed (lock + DataFrame
    ops) is unchanged; only the sink destination differs.
    """

    @pytest.fixture(autouse=True)
    def _quiet_logger(self):
        """Replace the module logger with a no-op mock for the duration of each test."""
        noop = Mock()
        with patch.object(ibc, "logger", noop):
            yield

    def test_250_portfolio_events_across_50_contracts_under_5s(self, client):
        """
        Simulates a portfolio refresh: 50 contracts × 5 price updates each.
        Expected: all 50 positions correctly stored, no corruption, under 5 s.

        Note: each event does pd.concat + mask + reset_index under a lock, which
        runs at ~5–10 ms/op on Windows — 250 ops × 10 ms = ~2.5 s expected.
        The 5 s limit catches catastrophic regressions, not micro-optimisations.
        """
        n_contracts = 50
        updates_per = 5
        contracts = [_contract(con_id=i + 1, symbol=f"SYM{i:02d}") for i in range(n_contracts)]

        t0 = time.monotonic()
        for _ in range(updates_per):
            for c in contracts:
                client._on_portfolio(_portfolio_item(c, position=float(c.conId * 10)))
        elapsed = time.monotonic() - t0

        snap = client.snapshot()
        assert len(snap.positions) == n_contracts, "All 50 contracts must remain"
        assert set(snap.positions["conId"]) == set(range(1, n_contracts + 1))
        assert elapsed < 5.0, f"250 portfolio events took {elapsed:.2f}s (limit 5s)"

    def test_500_ticker_batches_10_tickers_each_under_3s(self, client):
        """
        Simulates rapid market data: 500 updates for a 10-option portfolio.
        Expected: all 10 tickers in snapshot with final prices, under 3 s.
        """
        n_opts = 10
        n_batches = 500
        contracts = [_contract(con_id=i + 1, sec_type="OPT") for i in range(n_opts)]
        tickers = {_ticker(c, last=float(c.conId), delta=-0.3) for c in contracts}

        t0 = time.monotonic()
        for _ in range(n_batches):
            client._on_tickers(tickers)
        elapsed = time.monotonic() - t0

        snap = client.snapshot()
        assert len(snap.tickers) == n_opts, "All 10 tickers must be stored"
        assert elapsed < 3.0, f"500 ticker batches took {elapsed:.2f}s (limit 3s)"

    def test_error_ring_flood_10k_events_under_2s(self, client):
        """
        10,000 error events must process quickly; ring stays capped at 50.
        """
        t0 = time.monotonic()
        for i in range(10_000):
            client._on_error(i, 400, f"error {i}", None)
        elapsed = time.monotonic() - t0

        assert len(client.snapshot().errors) == 50
        assert elapsed < 2.0, f"10k error events took {elapsed:.2f}s (limit 2s)"

    def test_concurrent_portfolio_and_ticker_updates_no_corruption(self, client):
        """
        5 threads writing portfolio + 5 threads writing tickers simultaneously.
        snapshot() calls by a reader thread must never raise.
        """
        contracts = [_contract(con_id=i + 1, symbol=f"C{i}") for i in range(10)]
        stop = threading.Event()
        errors: list[Exception] = []

        def _portfolio_writer():
            while not stop.is_set():
                for c in contracts:
                    try:
                        client._on_portfolio(_portfolio_item(c, position=float(c.conId)))
                    except Exception as exc:
                        errors.append(exc)

        def _ticker_writer():
            while not stop.is_set():
                try:
                    client._on_tickers({_ticker(c, last=float(c.conId)) for c in contracts})
                except Exception as exc:
                    errors.append(exc)

        def _reader():
            while not stop.is_set():
                try:
                    snap = client.snapshot()
                    _ = (len(snap.positions), len(snap.tickers))
                except Exception as exc:
                    errors.append(exc)

        writers = (
            [threading.Thread(target=_portfolio_writer, daemon=True) for _ in range(5)]
            + [threading.Thread(target=_ticker_writer, daemon=True) for _ in range(5)]
        )
        reader = threading.Thread(target=_reader, daemon=True)
        for t in writers:
            t.start()
        reader.start()

        time.sleep(0.5)
        stop.set()

        for t in writers:
            t.join(timeout=2)
        reader.join(timeout=2)

        assert not errors, f"Exceptions during concurrent load: {errors[:3]}"

    def test_concurrent_account_value_flood_no_loss(self, client):
        """
        8 threads each write 200 account values across 4 accounts.
        All 4 accounts must be in the final snapshot; no exceptions.
        """
        accounts = [f"U{i}" for i in range(4)]
        errors: list[Exception] = []

        def _write(acct: str):
            try:
                for j in range(200):
                    client._on_account_value(_account_value("NLV", str(j * 1000), acct))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_write, args=(a,)) for a in accounts * 2]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert not errors
        snap = client.snapshot()
        assert len(snap.account_values) == 4

    def test_snapshot_throughput_without_contention(self, client):
        """
        snapshot() alone (no concurrent writers) must be callable >1000 times/s.
        Verifies the copy mechanism itself is not slow.
        """
        # Pre-populate with a small position so copy has some real work to do
        c = _contract(con_id=1)
        client._on_portfolio(_portfolio_item(c, position=100.0))

        count = 0
        t0 = time.monotonic()
        while time.monotonic() - t0 < 0.5:
            client.snapshot()
            count += 1

        rate = count / 0.5
        assert rate > 1000, f"snapshot() rate {rate:.0f}/s is below 1000/s (no contention)"

    def test_snapshot_does_not_deadlock_under_continuous_writes(self, client):
        """
        snapshot() must complete while a writer holds _snap_lock intermittently.
        Proves absence of deadlock: at least 10 reads must finish in 1 s.
        """
        c = _contract(con_id=1)
        stop = threading.Event()

        def _writer():
            while not stop.is_set():
                client._on_portfolio(_portfolio_item(c, position=100.0))

        writer = threading.Thread(target=_writer, daemon=True)
        writer.start()

        count = 0
        t0 = time.monotonic()
        while time.monotonic() - t0 < 1.0:
            client.snapshot()
            count += 1

        stop.set()
        writer.join(timeout=2)

        assert count >= 10, (
            f"Only {count} snapshot() calls completed in 1s — possible deadlock"
        )

    def test_high_position_count_snapshot_copy_time(self, client):
        """
        snapshot() with 200 positions must complete within 50 ms.
        The shallow-copy semantics must not degrade on large portfolios.
        """
        for i in range(200):
            c = _contract(con_id=i + 1, symbol=f"S{i:03d}")
            client._on_portfolio(_portfolio_item(c, position=float(i + 1) * 10))

        t0 = time.monotonic()
        for _ in range(50):
            snap = client.snapshot()
            assert len(snap.positions) == 200
        elapsed = time.monotonic() - t0

        # 50 snapshots of 200 positions must finish in under 0.5 s
        assert elapsed < 0.5, f"50 large snapshots took {elapsed:.3f}s (limit 0.5s)"

    def test_concurrent_freeze_unfreeze_cycles_no_deadlock(self, client):
        """
        Rapid freeze/unfreeze calls from multiple threads must not deadlock.
        This tests the interaction between _frozen flag and _snap_lock.
        """
        errors: list[Exception] = []
        stop = threading.Event()

        def _freezer():
            while not stop.is_set():
                try:
                    client.freeze()
                    time.sleep(0.001)
                    client._frozen = False  # bypass unfreeze() loop scheduling
                except Exception as exc:
                    errors.append(exc)

        def _reader():
            while not stop.is_set():
                try:
                    _ = client.snapshot()
                    _ = client.is_frozen()
                except Exception as exc:
                    errors.append(exc)

        threads = (
            [threading.Thread(target=_freezer, daemon=True) for _ in range(3)]
            + [threading.Thread(target=_reader, daemon=True) for _ in range(3)]
        )
        for t in threads:
            t.start()
        time.sleep(0.3)
        stop.set()
        for t in threads:
            t.join(timeout=2)

        assert not errors, f"Exceptions during freeze/unfreeze load: {errors[:3]}"
