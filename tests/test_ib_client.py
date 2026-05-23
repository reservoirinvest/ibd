"""
Comprehensive tests for src/dashboard/ib_client.py.

Coverage:
  1. Singleton pattern and get_client()
  2. start() idempotency and daemon thread lifecycle
  3. freeze() / unfreeze() CID handoff cycle
  4. snapshot() copy semantics and thread safety
  5. _build_position_row static helper
  6. Event handlers: _on_portfolio, _on_position, _on_account_value,
                     _on_tickers, _on_error, _on_disconnect
  7. _connect_with_retry: exponential backoff, frozen guard, idempotency
  8. Thread safety: concurrent portfolio updates
"""
from __future__ import annotations

import asyncio
import math
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from src.dashboard.ib_client import IBClient, get_client


# ---------------------------------------------------------------------------
# Test helpers — build ib_async stub objects without a real IBKR connection
# ---------------------------------------------------------------------------


def _make_contract(
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


def _make_portfolio_item(
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


def _make_position(
    contract: Mock,
    account: str = "U123456",
    position: float = 100.0,
    avg_cost: float = 150.0,
) -> Mock:
    pos = Mock()
    pos.contract = contract
    pos.account = account
    pos.position = position
    pos.avgCost = avg_cost
    return pos


def _make_account_value(
    tag: str, value: str, account: str = "U123456"
) -> Mock:
    av = Mock()
    av.tag = tag
    av.value = value
    av.account = account
    return av


def _make_ticker(
    contract: Mock,
    last: float = float("nan"),
    bid: float = float("nan"),
    ask: float = float("nan"),
    delta: float | None = None,
    gamma: float | None = None,
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
    mg.gamma = gamma
    mg.theta = theta
    mg.vega = vega
    mg.impliedVol = implied_vol
    mg.undPrice = und_price
    t.modelGreeks = mg
    return t


@pytest.fixture
def mock_settings() -> Mock:
    s = Mock()
    s.ib_host = "127.0.0.1"
    s.ib_port = 7497
    s.ib_client_id = 10
    return s


@pytest.fixture(autouse=True)
def reset_ib_client():
    """Reset the IBClient singleton before and after every test.

    Sets _log_sink_added=True to prevent loguru from trying to create
    log/dashboard.log during tests.
    """
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
    """Fresh IBClient with no event loop or IBKR connection."""
    return IBClient()


@pytest.fixture
def running_loop():
    """Real asyncio event loop running in a background thread."""
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    t.join(timeout=2.0)


# ===========================================================================
# 1. Singleton pattern
# ===========================================================================


class TestSingleton:
    def test_two_constructions_return_same_instance(self):
        a = IBClient()
        b = IBClient()
        assert a is b

    def test_get_client_returns_singleton(self):
        a = IBClient()
        assert get_client() is a

    def test_initialized_flag_prevents_double_init(self):
        c = IBClient()
        original_snap = c._snap
        IBClient()  # second construction must not re-run __init__
        assert c._snap is original_snap


# ===========================================================================
# 2. start() — daemon thread lifecycle
# ===========================================================================


class TestStart:
    def test_start_creates_alive_daemon_thread(self, mock_settings):
        client = IBClient()

        async def _no_op():
            pass

        with patch.object(client, "_connect_with_retry", _no_op):
            client.start(mock_settings)

        assert client._thread is not None
        assert client._thread.is_alive()
        assert client._thread.daemon

    def test_start_creates_running_event_loop(self, mock_settings):
        client = IBClient()

        async def _no_op():
            pass

        with patch.object(client, "_connect_with_retry", _no_op):
            client.start(mock_settings)

        assert client._loop is not None
        assert client._loop.is_running()

    def test_start_idempotent_second_call_noop(self, mock_settings):
        """Calling start() twice must start exactly one thread."""
        client = IBClient()
        thread_starts: list[int] = []
        original_run_loop = client._run_loop

        def _counting_run_loop():
            thread_starts.append(1)
            original_run_loop()

        async def _no_op():
            pass

        with patch.object(client, "_connect_with_retry", _no_op):
            with patch.object(client, "_run_loop", _counting_run_loop):
                client.start(mock_settings)
                client.start(mock_settings)

        assert sum(thread_starts) == 1, "Daemon thread must start exactly once"

    def test_start_stores_settings(self, mock_settings):
        client = IBClient()

        async def _no_op():
            pass

        with patch.object(client, "_connect_with_retry", _no_op):
            client.start(mock_settings)

        assert client._settings is mock_settings

    def test_start_loop_ready_event_set_after_start(self, mock_settings):
        client = IBClient()

        async def _no_op():
            pass

        with patch.object(client, "_connect_with_retry", _no_op):
            client.start(mock_settings)

        assert client._loop_ready.is_set()


# ===========================================================================
# 3. freeze() / unfreeze()
# ===========================================================================


class TestFreezeUnfreeze:
    def test_freeze_sets_frozen_flag(self, client):
        client.freeze()
        assert client._frozen is True

    def test_freeze_marks_snapshot_disconnected(self, client):
        with client._snap_lock:
            client._snap.connected = True
        client.freeze()
        assert not client.snapshot().connected

    def test_freeze_with_loop_clears_subscriptions(self, client, running_loop):
        client._loop = running_loop
        client._subscribed = {1, 2, 3}
        client.freeze()
        time.sleep(0.1)  # let the threadsafe call execute
        assert len(client._subscribed) == 0

    def test_unfreeze_clears_frozen_flag(self, client):
        client._frozen = True
        client.unfreeze()
        assert client._frozen is False

    def test_is_frozen_reflects_state(self, client):
        assert not client.is_frozen()
        client._frozen = True
        assert client.is_frozen()

    def test_on_disconnect_when_frozen_does_not_reconnect(
        self, client, running_loop
    ):
        client._loop = running_loop
        client._frozen = True

        reconnect_called = threading.Event()

        async def _mock_retry():
            reconnect_called.set()

        with patch.object(client, "_connect_with_retry", _mock_retry):
            client._on_disconnect()

        assert not reconnect_called.wait(timeout=0.2), (
            "_connect_with_retry must NOT fire when frozen"
        )


# ===========================================================================
# 4. snapshot() — copy semantics
# ===========================================================================


class TestSnapshot:
    def test_snapshot_returns_different_object_each_call(self, client):
        assert client.snapshot() is not client.snapshot()

    def test_mutating_snapshot_positions_does_not_affect_internal(self, client):
        c = _make_contract()
        client._on_portfolio(_make_portfolio_item(c))

        snap = client.snapshot()
        snap.positions.drop(snap.positions.index, inplace=True)

        assert len(client._snap.positions) == 1

    def test_snapshot_account_values_deep_copied(self, client):
        with client._snap_lock:
            client._snap.account_values = {"U1": {"NLV": Decimal("100000")}}

        snap = client.snapshot()
        snap.account_values["U1"]["NLV"] = Decimal("0")

        with client._snap_lock:
            assert client._snap.account_values["U1"]["NLV"] == Decimal("100000")

    def test_snapshot_errors_deque_is_copy(self, client):
        with client._snap_lock:
            client._snap.errors.append((datetime.now(), 400, "test"))

        snap = client.snapshot()
        snap.errors.clear()

        with client._snap_lock:
            assert len(client._snap.errors) == 1


# ===========================================================================
# 5. _build_position_row static helper
# ===========================================================================


class TestBuildPositionRow:
    def test_stk_row_has_all_fields(self):
        c = _make_contract(con_id=42, symbol="MSFT", sec_type="STK")
        row = IBClient._build_position_row(
            c, "U1", 200.0, 300.0, 310.0, 62000.0, 2000.0, 50.0
        )
        assert row["conId"] == 42
        assert row["symbol"] == "MSFT"
        assert row["secType"] == "STK"
        assert row["account"] == "U1"
        assert row["position"] == 200.0
        assert row["avgCost"] == 300.0
        assert row["marketPrice"] == 310.0
        assert row["marketValue"] == 62000.0
        assert row["unrealizedPNL"] == 2000.0
        assert row["realizedPNL"] == 50.0
        assert row["_contract"] is c

    def test_opt_row_carries_right_strike_expiry(self):
        c = _make_contract(
            con_id=99,
            symbol="SPY",
            sec_type="OPT",
            right="P",
            strike=450.0,
            expiry="20261219",
        )
        row = IBClient._build_position_row(c, "U2", -1.0, 250.0)
        assert row["right"] == "P"
        assert row["strike"] == 450.0
        assert row["expiry"] == "20261219"

    def test_market_fields_default_to_nan(self):
        c = _make_contract()
        row = IBClient._build_position_row(c, "U1", 10.0, 50.0)
        assert math.isnan(row["marketPrice"])
        assert math.isnan(row["marketValue"])
        assert math.isnan(row["unrealizedPNL"])
        assert math.isnan(row["realizedPNL"])

    def test_currency_and_exchange_stored(self):
        c = _make_contract(exchange="NYSE", currency="USD")
        row = IBClient._build_position_row(c, "U1", 5.0, 100.0)
        assert row["currency"] == "USD"
        assert row["primaryExch"] == "NYSE"


# ===========================================================================
# 6a. _on_portfolio
# ===========================================================================


class TestOnPortfolio:
    def test_adds_new_position(self, client):
        c = _make_contract(con_id=1, symbol="AAPL")
        client._on_portfolio(_make_portfolio_item(c, position=100.0))

        snap = client.snapshot()
        assert len(snap.positions) == 1
        assert snap.positions.iloc[0]["symbol"] == "AAPL"
        assert snap.positions.iloc[0]["position"] == 100.0

    def test_updates_existing_position_same_con_id(self, client):
        c = _make_contract(con_id=1)
        client._on_portfolio(_make_portfolio_item(c, position=100.0, market_price=150.0))
        client._on_portfolio(_make_portfolio_item(c, position=200.0, market_price=160.0))

        snap = client.snapshot()
        assert len(snap.positions) == 1
        assert snap.positions.iloc[0]["position"] == 200.0
        assert snap.positions.iloc[0]["marketPrice"] == 160.0

    def test_zero_position_removes_row(self, client):
        c = _make_contract(con_id=1)
        client._on_portfolio(_make_portfolio_item(c, position=100.0))
        client._on_portfolio(_make_portfolio_item(c, position=0.0))

        assert len(client.snapshot().positions) == 0

    def test_same_contract_different_accounts_coexist(self, client):
        c = _make_contract(con_id=1)
        client._on_portfolio(_make_portfolio_item(c, account="U1", position=50.0))
        client._on_portfolio(_make_portfolio_item(c, account="U2", position=75.0))

        snap = client.snapshot()
        assert len(snap.positions) == 2
        assert set(snap.positions["account"]) == {"U1", "U2"}

    def test_sets_as_of_timestamp(self, client):
        c = _make_contract()
        client._on_portfolio(_make_portfolio_item(c))
        assert client._snap.as_of is not None

    def test_multiple_contracts_all_stored(self, client):
        for i in range(5):
            c = _make_contract(con_id=i + 1, symbol=f"SYM{i}")
            client._on_portfolio(_make_portfolio_item(c))

        assert len(client.snapshot().positions) == 5


# ===========================================================================
# 6b. _on_position
# ===========================================================================


class TestOnPosition:
    def test_adds_position_when_snapshot_empty(self, client):
        c = _make_contract(con_id=2, symbol="GOOG")
        client._on_position(_make_position(c, position=10.0))

        snap = client.snapshot()
        assert len(snap.positions) == 1
        assert snap.positions.iloc[0]["symbol"] == "GOOG"

    def test_skips_when_already_present_via_portfolio(self, client):
        c = _make_contract(con_id=2)
        client._on_portfolio(_make_portfolio_item(c, position=10.0, market_price=200.0))
        # _on_position for the same (conId, account) is a deduplication no-op
        client._on_position(_make_position(c, position=10.0, avg_cost=999.0))

        snap = client.snapshot()
        assert len(snap.positions) == 1
        # portfolio data preserved — not overwritten by position event
        assert snap.positions.iloc[0]["marketPrice"] == 200.0

    def test_skips_zero_position(self, client):
        c = _make_contract(con_id=3)
        client._on_position(_make_position(c, position=0.0))
        assert len(client.snapshot().positions) == 0


# ===========================================================================
# 6c. _on_account_value
# ===========================================================================


class TestOnAccountValue:
    def test_stores_decimal_value(self, client):
        client._on_account_value(_make_account_value("NetLiquidation", "250000.50", "U1"))

        snap = client.snapshot()
        assert snap.account_values["U1"]["NetLiquidation"] == Decimal("250000.50")

    def test_multiple_tags_same_account(self, client):
        client._on_account_value(_make_account_value("NetLiquidation", "100000"))
        client._on_account_value(_make_account_value("ExcessLiquidity", "50000"))

        acct = client.snapshot().account_values["U123456"]
        assert "NetLiquidation" in acct
        assert "ExcessLiquidity" in acct

    def test_multiple_accounts(self, client):
        client._on_account_value(_make_account_value("NLV", "100000", "U1"))
        client._on_account_value(_make_account_value("NLV", "200000", "U2"))

        snap = client.snapshot()
        assert snap.account_values["U1"]["NLV"] == Decimal("100000")
        assert snap.account_values["U2"]["NLV"] == Decimal("200000")

    def test_invalid_value_gracefully_ignored(self, client):
        client._on_account_value(_make_account_value("BadTag", "not-a-number"))
        # Must not raise, and tag must not be stored
        snap = client.snapshot()
        assert "BadTag" not in snap.account_values.get("U123456", {})

    def test_updates_as_of(self, client):
        client._on_account_value(_make_account_value("NLV", "100000"))
        assert client._snap.as_of is not None


# ===========================================================================
# 6d. _on_tickers
# ===========================================================================


class TestOnTickers:
    def test_greeks_stored_from_model_greeks(self, client):
        c = _make_contract(con_id=10, sec_type="OPT")
        t = _make_ticker(
            c,
            delta=-0.30,
            gamma=0.05,
            theta=-0.02,
            vega=0.10,
            implied_vol=0.25,
            und_price=450.0,
        )
        client._on_tickers({t})

        ts = client.snapshot().tickers[10]
        assert ts.delta == pytest.approx(-0.30)
        assert ts.gamma == pytest.approx(0.05)
        assert ts.theta == pytest.approx(-0.02)
        assert ts.vega == pytest.approx(0.10)
        assert ts.iv == pytest.approx(0.25)
        assert ts.underlying_px == pytest.approx(450.0)

    def test_last_bid_ask_stored(self, client):
        c = _make_contract(con_id=11)
        client._on_tickers({_make_ticker(c, last=5.50, bid=5.40, ask=5.60)})

        ts = client.snapshot().tickers[11]
        assert ts.last == pytest.approx(5.50)
        assert ts.bid == pytest.approx(5.40)
        assert ts.ask == pytest.approx(5.60)

    def test_nan_last_does_not_overwrite_existing(self, client):
        c = _make_contract(con_id=12)
        client._on_tickers({_make_ticker(c, last=3.0)})
        client._on_tickers({_make_ticker(c, last=float("nan"))})  # NaN — no overwrite
        assert client.snapshot().tickers[12].last == pytest.approx(3.0)

    def test_none_greek_does_not_overwrite_existing(self, client):
        c = _make_contract(con_id=13, sec_type="OPT")
        client._on_tickers({_make_ticker(c, delta=-0.50)})
        client._on_tickers({_make_ticker(c, delta=None)})  # None — no overwrite
        assert client.snapshot().tickers[13].delta == pytest.approx(-0.50)

    def test_multiple_tickers_in_one_event(self, client):
        c1 = _make_contract(con_id=20)
        c2 = _make_contract(con_id=21)
        client._on_tickers({_make_ticker(c1, last=10.0), _make_ticker(c2, last=20.0)})

        snap = client.snapshot()
        assert 20 in snap.tickers
        assert 21 in snap.tickers

    def test_updates_as_of(self, client):
        c = _make_contract(con_id=30)
        client._on_tickers({_make_ticker(c, last=1.0)})
        assert client._snap.as_of is not None


# ===========================================================================
# 6e. _on_error
# ===========================================================================


class TestOnError:
    @pytest.mark.parametrize("code", [2104, 2106, 2158, 2107, 2103, 2105, 2108])
    def test_info_codes_not_stored(self, client, code):
        client._on_error(0, code, "farm connection OK", None)
        assert len(client.snapshot().errors) == 0

    def test_real_error_stored_in_ring(self, client):
        client._on_error(1, 326, "Unable to connect — duplicate CID", None)
        snap = client.snapshot()
        assert len(snap.errors) == 1
        _, code, msg = snap.errors[0]
        assert code == 326
        assert "CID" in msg

    def test_ring_buffer_capped_at_50(self, client):
        for i in range(60):
            client._on_error(i, 400, f"error {i}", None)
        assert len(client.snapshot().errors) == 50

    def test_oldest_error_evicted_when_full(self, client):
        for i in range(51):
            client._on_error(i, 400, f"error {i}", None)
        snap = client.snapshot()
        # oldest (error 0) should have been evicted
        messages = [msg for _, _, msg in snap.errors]
        assert "error 0" not in messages
        assert "error 50" in messages


# ===========================================================================
# 6f. _on_disconnect
# ===========================================================================


class TestOnDisconnect:
    def test_marks_snapshot_disconnected(self, client):
        with client._snap_lock:
            client._snap.connected = True
        client._on_disconnect()
        assert not client.snapshot().connected

    def test_schedules_reconnect_when_not_frozen(self, client, running_loop):
        client._loop = running_loop
        reconnect_called = threading.Event()

        async def _mock_retry():
            reconnect_called.set()

        with patch.object(client, "_connect_with_retry", _mock_retry):
            client._on_disconnect()

        assert reconnect_called.wait(timeout=1.0), (
            "_connect_with_retry should fire after disconnect"
        )

    def test_no_reconnect_when_frozen(self, client, running_loop):
        client._loop = running_loop
        client._frozen = True
        reconnect_called = threading.Event()

        async def _mock_retry():
            reconnect_called.set()

        with patch.object(client, "_connect_with_retry", _mock_retry):
            client._on_disconnect()

        assert not reconnect_called.wait(timeout=0.2)

    def test_no_reconnect_when_already_connecting(self, client, running_loop):
        client._loop = running_loop
        client._connecting = True
        reconnect_called = threading.Event()

        async def _mock_retry():
            reconnect_called.set()

        with patch.object(client, "_connect_with_retry", _mock_retry):
            client._on_disconnect()

        assert not reconnect_called.wait(timeout=0.2)

    def test_clears_subscriptions_on_disconnect(self, client, running_loop):
        client._loop = running_loop
        client._subscribed = {1, 2, 3}

        async def _no_op():
            pass

        with patch.object(client, "_connect_with_retry", _no_op):
            client._on_disconnect()

        time.sleep(0.1)
        assert len(client._subscribed) == 0


# ===========================================================================
# 7. _connect_with_retry: backoff, frozen guard, idempotency
# ===========================================================================


class TestConnectWithRetry:
    def _make_client_with_settings(self) -> IBClient:
        c = IBClient()
        c._settings = Mock(ib_host="127.0.0.1", ib_port=7497, ib_client_id=10)
        return c

    def test_returns_immediately_when_already_connecting(self):
        client = self._make_client_with_settings()
        client._connecting = True  # guard is up

        mock_ib_class = Mock()

        async def run():
            with patch("src.dashboard.ib_client.IB", mock_ib_class):
                await client._connect_with_retry()

        asyncio.run(run())
        mock_ib_class.assert_not_called()
        # connecting flag unchanged — not reset by the early-return path
        assert client._connecting

    def test_aborts_when_frozen(self):
        client = self._make_client_with_settings()
        client._frozen = True

        mock_ib_class = Mock()

        async def run():
            with patch("src.dashboard.ib_client.IB", mock_ib_class):
                await client._connect_with_retry()

        asyncio.run(run())
        mock_ib_class.assert_not_called()
        assert not client._connecting

    def test_exponential_backoff_increases_each_retry(self):
        client = self._make_client_with_settings()
        sleep_calls: list[float] = []

        async def run():
            async def fast_sleep(delay: float) -> None:
                sleep_calls.append(delay)
                if len(sleep_calls) >= 3:
                    client._frozen = True

            mock_ib = MagicMock()
            mock_ib.connectAsync = AsyncMock(side_effect=ConnectionRefusedError("no IBKR"))

            with patch("src.dashboard.ib_client.IB", return_value=mock_ib):
                with patch("asyncio.sleep", fast_sleep):
                    await client._connect_with_retry()

        asyncio.run(run())

        assert len(sleep_calls) >= 2
        for i in range(1, len(sleep_calls)):
            assert sleep_calls[i] > sleep_calls[i - 1], (
                f"Each backoff delay must exceed the previous: {sleep_calls}"
            )

    def test_backoff_starts_at_2s_and_multiplies_by_1_7(self):
        client = self._make_client_with_settings()
        sleep_calls: list[float] = []

        async def run():
            async def fast_sleep(delay: float) -> None:
                sleep_calls.append(delay)
                if len(sleep_calls) >= 3:
                    client._frozen = True

            mock_ib = MagicMock()
            mock_ib.connectAsync = AsyncMock(side_effect=ConnectionRefusedError("no IBKR"))

            with patch("src.dashboard.ib_client.IB", return_value=mock_ib):
                with patch("asyncio.sleep", fast_sleep):
                    await client._connect_with_retry()

        asyncio.run(run())

        assert sleep_calls[0] == pytest.approx(2.0)
        assert sleep_calls[1] == pytest.approx(2.0 * 1.7)

    def test_backoff_capped_at_60s(self):
        """After many failures, the per-retry backoff delay must not exceed 60 s.

        The circuit breaker threshold is raised above the iteration count so this
        test isolates the backoff cap without triggering the CB's 300 s pause.
        """
        import src.dashboard.ib_client as ibc

        client = self._make_client_with_settings()
        sleep_calls: list[float] = []

        async def run():
            async def fast_sleep(delay: float) -> None:
                sleep_calls.append(delay)
                if len(sleep_calls) >= 25:
                    client._frozen = True

            mock_ib = MagicMock()
            mock_ib.connectAsync = AsyncMock(side_effect=ConnectionRefusedError("no IBKR"))

            with patch.object(ibc, "_CB_FAILURE_THRESHOLD", 100):
                with patch("src.dashboard.ib_client.IB", return_value=mock_ib):
                    with patch("asyncio.sleep", fast_sleep):
                        await client._connect_with_retry()

        asyncio.run(run())
        assert max(sleep_calls) <= 60.0

    def test_connecting_flag_cleared_on_success(self):
        client = self._make_client_with_settings()

        async def run():
            mock_ib = AsyncMock()
            with patch("src.dashboard.ib_client.IB", return_value=mock_ib):
                with patch.object(client, "_wire_handlers", lambda ib: None):
                    with patch.object(client, "_bootstrap", AsyncMock()):
                        await client._connect_with_retry()

        asyncio.run(run())
        assert not client._connecting

    def test_connecting_flag_cleared_on_early_frozen_return(self):
        client = self._make_client_with_settings()
        client._frozen = True

        async def run():
            await client._connect_with_retry()

        asyncio.run(run())
        assert not client._connecting

    def test_error_appended_on_connection_failure(self):
        client = self._make_client_with_settings()

        async def run():
            async def fast_sleep(delay: float) -> None:
                client._frozen = True  # stop after first retry

            mock_ib = MagicMock()
            mock_ib.connectAsync = AsyncMock(side_effect=ConnectionRefusedError("refused"))

            with patch("src.dashboard.ib_client.IB", return_value=mock_ib):
                with patch("asyncio.sleep", fast_sleep):
                    await client._connect_with_retry()

        asyncio.run(run())

        snap = client.snapshot()
        assert len(snap.errors) > 0
        _, code, msg = snap.errors[-1]
        assert code == -1  # sentinel for connect-failed errors
        assert "Connect failed" in msg


# ===========================================================================
# 8. Thread safety: concurrent portfolio updates
# ===========================================================================


class TestConcurrency:
    def test_concurrent_portfolio_updates_no_corruption(self, client):
        """Concurrent _on_portfolio calls must not corrupt the DataFrame index."""
        contracts = [_make_contract(con_id=i + 1, symbol=f"SYM{i}") for i in range(20)]
        errors: list[Exception] = []

        def _update_all():
            try:
                for c in contracts:
                    client._on_portfolio(
                        _make_portfolio_item(c, position=float(c.conId * 10))
                    )
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_update_all) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert not errors, f"Exceptions during concurrent updates: {errors}"
        snap = client.snapshot()
        assert len(snap.positions) == 20
        assert set(snap.positions["conId"]) == set(range(1, 21))

    def test_snapshot_safe_during_concurrent_writes(self, client):
        """snapshot() must never raise or return a broken result while writers run."""
        contracts = [_make_contract(con_id=i + 1) for i in range(10)]
        stop = threading.Event()
        reader_errors: list[Exception] = []

        def _writer():
            while not stop.is_set():
                for c in contracts:
                    client._on_portfolio(_make_portfolio_item(c, position=float(c.conId)))

        def _reader():
            while not stop.is_set():
                try:
                    snap = client.snapshot()
                    _ = len(snap.positions)
                except Exception as exc:
                    reader_errors.append(exc)

        writer = threading.Thread(target=_writer, daemon=True)
        reader = threading.Thread(target=_reader, daemon=True)
        writer.start()
        reader.start()
        time.sleep(0.3)
        stop.set()
        writer.join(timeout=2)
        reader.join(timeout=2)

        assert not reader_errors, (
            f"snapshot() raised during concurrent writes: {reader_errors}"
        )

    def test_concurrent_account_value_updates(self, client):
        """Concurrent _on_account_value calls must not lose updates."""
        errors: list[Exception] = []

        def _update_values(account: str):
            try:
                for i in range(50):
                    client._on_account_value(
                        _make_account_value("NLV", str(i * 1000), account)
                    )
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=_update_values, args=(f"U{i}",))
            for i in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert not errors
        snap = client.snapshot()
        # All 4 accounts should have been recorded
        assert len(snap.account_values) == 4


# ===========================================================================
# 9. Circuit breaker
# ===========================================================================


class TestCircuitBreaker:
    def _make_client_with_settings(self) -> IBClient:
        c = IBClient()
        c._settings = Mock(ib_host="127.0.0.1", ib_port=7497, ib_client_id=10)
        return c

    def test_initial_state_is_closed(self):
        assert IBClient().circuit_state == "closed"

    def test_opens_after_threshold_failures(self):
        client = self._make_client_with_settings()

        async def run():
            async def fast_sleep(delay: float) -> None:
                if client._cb_state == "open":
                    client._frozen = True  # stop as soon as circuit opens

            mock_ib = MagicMock()
            mock_ib.connectAsync = AsyncMock(side_effect=ConnectionRefusedError("down"))
            with patch("src.dashboard.ib_client.IB", return_value=mock_ib):
                with patch("asyncio.sleep", fast_sleep):
                    await client._connect_with_retry()

        asyncio.run(run())
        assert client._cb_state == "open"
        assert client._cb_failures >= 5

    def test_open_circuit_sleeps_reset_duration(self):
        """Circuit breaker OPEN must sleep ~_CB_RESET_SECS, not the short backoff."""
        import src.dashboard.ib_client as ibc

        client = self._make_client_with_settings()
        sleep_calls: list[float] = []

        async def run():
            async def fast_sleep(delay: float) -> None:
                sleep_calls.append(delay)
                if client._cb_state == "open" and delay > 60.0:
                    client._frozen = True  # stop after seeing the CB sleep

            mock_ib = MagicMock()
            mock_ib.connectAsync = AsyncMock(side_effect=ConnectionRefusedError("down"))
            with patch("src.dashboard.ib_client.IB", return_value=mock_ib):
                with patch("asyncio.sleep", fast_sleep):
                    await client._connect_with_retry()

        asyncio.run(run())
        # At least one sleep call must be the CB reset time (300 s)
        assert any(d >= ibc._CB_RESET_SECS for d in sleep_calls), (
            f"Expected a sleep ≥ _CB_RESET_SECS in {sleep_calls}"
        )

    def test_transitions_to_half_open_after_reset_timeout(self):
        """After _CB_RESET_SECS elapsed, circuit should allow one probe attempt."""
        import src.dashboard.ib_client as ibc

        client = self._make_client_with_settings()
        # Pre-set the circuit to OPEN with _cb_opened_at = 0.0
        client._cb_state = "open"
        client._cb_opened_at = 0.0
        client._cb_failures = ibc._CB_FAILURE_THRESHOLD

        connect_attempts: list[int] = []

        async def run():
            async def fast_sleep(_: float) -> None:
                pass  # no waiting

            mock_ib = MagicMock()

            async def mock_connect(*args, **kwargs):
                connect_attempts.append(1)
                client._frozen = True  # stop after the probe attempt
                raise ConnectionRefusedError("still down")

            mock_ib.connectAsync = mock_connect

            # time.monotonic() returning > _CB_RESET_SECS makes elapsed > threshold
            with patch("time.monotonic", return_value=ibc._CB_RESET_SECS + 1.0):
                with patch("src.dashboard.ib_client.IB", return_value=mock_ib):
                    with patch("asyncio.sleep", fast_sleep):
                        await client._connect_with_retry()

        asyncio.run(run())
        assert len(connect_attempts) > 0, "Half-open circuit should have attempted a probe"

    def test_success_in_half_open_closes_circuit(self):
        import src.dashboard.ib_client as ibc

        client = self._make_client_with_settings()
        client._cb_state = "half_open"
        client._cb_failures = ibc._CB_FAILURE_THRESHOLD

        async def run():
            mock_ib = AsyncMock()
            with patch("src.dashboard.ib_client.IB", return_value=mock_ib):
                with patch.object(client, "_wire_handlers", lambda ib: None):
                    with patch.object(client, "_bootstrap", AsyncMock()):
                        await client._connect_with_retry()

        asyncio.run(run())
        assert client._cb_state == "closed"
        assert client._cb_failures == 0

    def test_failure_in_half_open_reopens_circuit(self):
        import src.dashboard.ib_client as ibc

        client = self._make_client_with_settings()
        # Put circuit in half_open state
        client._cb_state = "half_open"
        client._cb_failures = ibc._CB_FAILURE_THRESHOLD

        async def run():
            async def fast_sleep(_: float) -> None:
                client._frozen = True  # stop after first retry

            mock_ib = MagicMock()
            mock_ib.connectAsync = AsyncMock(side_effect=ConnectionRefusedError("still down"))
            with patch("src.dashboard.ib_client.IB", return_value=mock_ib):
                with patch("asyncio.sleep", fast_sleep):
                    await client._connect_with_retry()

        asyncio.run(run())
        assert client._cb_state == "open"

    def test_reset_circuit_breaker_clears_state(self):
        client = self._make_client_with_settings()
        client._cb_state = "open"
        client._cb_failures = 10
        client.reset_circuit_breaker()
        assert client.circuit_state == "closed"
        assert client._cb_failures == 0

    def test_success_resets_failure_count(self):
        client = self._make_client_with_settings()
        client._cb_failures = 3  # some prior failures

        async def run():
            mock_ib = AsyncMock()
            with patch("src.dashboard.ib_client.IB", return_value=mock_ib):
                with patch.object(client, "_wire_handlers", lambda ib: None):
                    with patch.object(client, "_bootstrap", AsyncMock()):
                        await client._connect_with_retry()

        asyncio.run(run())
        assert client._cb_failures == 0
        assert client._cb_state == "closed"


# ===========================================================================
# 10. Health check loop
# ===========================================================================


class TestHealthCheck:
    def test_exits_when_generation_superseded(self, client, running_loop):
        """Health check with old gen should exit immediately after first sleep."""
        client._health_gen = 1  # current gen is 1; we'll start check with gen=0

        done = threading.Event()

        async def _start():
            await client._health_check_loop(0, interval=0.01)
            done.set()

        asyncio.run_coroutine_threadsafe(_start(), running_loop)
        assert done.wait(timeout=1.0), "Stale health check should exit promptly"

    def test_exits_when_frozen(self, client, running_loop):
        client._health_gen = 0
        client._frozen = True

        done = threading.Event()

        async def _start():
            await client._health_check_loop(0, interval=0.01)
            done.set()

        asyncio.run_coroutine_threadsafe(_start(), running_loop)
        assert done.wait(timeout=1.0)

    def test_schedules_reconnect_on_lost_connection(self, client, running_loop):
        client._loop = running_loop
        client._health_gen = 0
        client._ib = Mock()
        client._ib.isConnected.return_value = False

        reconnect_called = threading.Event()

        async def _mock_retry():
            reconnect_called.set()

        with patch.object(client, "_connect_with_retry", _mock_retry):
            asyncio.run_coroutine_threadsafe(
                client._health_check_loop(0, interval=0.01), running_loop
            )
            assert reconnect_called.wait(timeout=1.0), (
                "_connect_with_retry should fire when health check detects disconnection"
            )

    def test_does_not_reconnect_when_frozen(self, client, running_loop):
        client._loop = running_loop
        client._health_gen = 0
        client._frozen = True
        client._ib = Mock()
        client._ib.isConnected.return_value = False

        reconnect_called = threading.Event()

        async def _mock_retry():
            reconnect_called.set()

        with patch.object(client, "_connect_with_retry", _mock_retry):
            asyncio.run_coroutine_threadsafe(
                client._health_check_loop(0, interval=0.01), running_loop
            )
            assert not reconnect_called.wait(timeout=0.3)

    def test_does_not_reconnect_when_already_connecting(self, client, running_loop):
        client._loop = running_loop
        client._health_gen = 0
        client._connecting = True
        client._ib = Mock()
        client._ib.isConnected.return_value = False

        reconnect_called = threading.Event()

        async def _mock_retry():
            reconnect_called.set()

        with patch.object(client, "_connect_with_retry", _mock_retry):
            asyncio.run_coroutine_threadsafe(
                client._health_check_loop(0, interval=0.01), running_loop
            )
            assert not reconnect_called.wait(timeout=0.3)

    def test_sets_stale_since_on_detected_disconnection(self, client, running_loop):
        client._loop = running_loop
        client._health_gen = 0
        client._ib = Mock()
        client._ib.isConnected.return_value = False

        done = threading.Event()

        async def _no_op_retry():
            done.set()

        with patch.object(client, "_connect_with_retry", _no_op_retry):
            asyncio.run_coroutine_threadsafe(
                client._health_check_loop(0, interval=0.01), running_loop
            )
            done.wait(timeout=1.0)

        assert client.snapshot().stale_since is not None

    def test_does_not_overwrite_existing_stale_since(self, client, running_loop):
        first_stale = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with client._snap_lock:
            client._snap.stale_since = first_stale

        client._loop = running_loop
        client._health_gen = 0
        client._ib = Mock()
        client._ib.isConnected.return_value = False

        done = threading.Event()

        async def _no_op_retry():
            done.set()

        with patch.object(client, "_connect_with_retry", _no_op_retry):
            asyncio.run_coroutine_threadsafe(
                client._health_check_loop(0, interval=0.01), running_loop
            )
            done.wait(timeout=1.0)

        assert client.snapshot().stale_since == first_stale


# ===========================================================================
# 11. Graceful degradation — stale_since tracking
# ===========================================================================


class TestGracefulDegradation:
    def test_stale_since_set_on_disconnect(self, client):
        with client._snap_lock:
            client._snap.connected = True
        client._on_disconnect()
        assert client.snapshot().stale_since is not None

    def test_stale_since_not_overwritten_on_repeated_disconnect(self, client):
        """First disconnect sets the timestamp; subsequent ones leave it alone."""
        first_stale = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with client._snap_lock:
            client._snap.stale_since = first_stale
        client._on_disconnect()
        assert client.snapshot().stale_since == first_stale

    def test_stale_since_included_in_snapshot_copy(self, client):
        ts = datetime(2026, 5, 1, tzinfo=timezone.utc)
        with client._snap_lock:
            client._snap.stale_since = ts
        assert client.snapshot().stale_since == ts

    def test_stale_since_cleared_on_reconnect(self, client):
        """Simulates what _bootstrap does: set connected=True + clear stale_since."""
        with client._snap_lock:
            client._snap.stale_since = datetime.now(timezone.utc)
        # _bootstrap sets connected=True and stale_since=None atomically
        with client._snap_lock:
            client._snap.connected = True
            client._snap.stale_since = None
        assert client.snapshot().stale_since is None

    def test_snapshot_connected_false_when_disconnected(self, client):
        with client._snap_lock:
            client._snap.connected = True
        client._on_disconnect()
        assert not client.snapshot().connected


# ===========================================================================
# 12. Error categorization
# ===========================================================================


class TestErrorCategorization:
    @pytest.mark.parametrize("code", [100, 165, 322])
    def test_pacing_codes_stored_in_ring(self, client, code):
        """Pacing violations are stored in the error ring (unlike info codes)."""
        client._on_error(0, code, "pacing violation", None)
        snap = client.snapshot()
        assert len(snap.errors) == 1
        _, stored_code, _ = snap.errors[0]
        assert stored_code == code

    @pytest.mark.parametrize("code", [100, 165, 322])
    def test_pacing_codes_do_not_raise(self, client, code):
        """_on_error must never raise regardless of error code."""
        client._on_error(1, code, "pacing", None)  # must not raise

    def test_unknown_error_code_stored(self, client):
        client._on_error(0, 9999, "unknown error", None)
        assert len(client.snapshot().errors) == 1

    @pytest.mark.parametrize("code", [2104, 2106, 2158, 2107, 2103, 2105, 2108])
    def test_info_codes_still_filtered(self, client, code):
        """Info codes must still not appear in the error ring after refactor."""
        client._on_error(0, code, "farm connection", None)
        assert len(client.snapshot().errors) == 0
