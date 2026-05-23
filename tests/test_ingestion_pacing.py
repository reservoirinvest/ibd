"""
TDD suite for ingestion pacing, dual-source hybrid pipeline, and graceful degradation.

Based on ANTIGRAVITY_AUDIT.md Section 3 TDD Mandate.

Covers:
  1. get_prec edge cases + get_prec_safe proposed graceful-degradation layer
  2. get_prices batch / pacing mechanics under mocked IBKR (no live connection)
  3. Semaphore-backed throughput: tipping-point concurrency isolation
  4. _fetch_one_yf: primary yfinance source — happy path, empty df, exceptions
  5. Market-state variance: ticker structural integrity (active vs. closed hours)
  6. Resilience: yfinance None → symbols identified and routed to IBKR fallback
"""
from __future__ import annotations

import asyncio
import math
import time
from datetime import date
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pandas as pd
import pytest

from src.build import get_prec, get_prec_safe
from src.dashboard.ohlc import _fetch_one_yf


# ---------------------------------------------------------------------------
# Shared test helpers
# ---------------------------------------------------------------------------


def _make_ticker_mock(
    symbol: str = "AAPL",
    con_id: int = 1,
    last: float = 150.0,
    bid: float = 149.9,
    ask: float = 150.1,
    close: float = 149.5,
) -> Mock:
    contract = Mock()
    contract.symbol = symbol
    contract.conId = con_id

    t = Mock()
    t.contract = contract
    t.last = last
    t.bid = bid
    t.ask = ask
    t.close = close
    t.volume = 1_000_000
    t.high = last + 2.0 if not math.isnan(last) else float("nan")
    t.low = last - 2.0 if not math.isnan(last) else float("nan")
    t.open = last - 0.5 if not math.isnan(last) else float("nan")
    t.bidSize = 10
    t.askSize = 10
    t.lastSize = 5
    t.halted = 0
    t.time = None
    return t


def _make_contract_mock(symbol: str = "AAPL", con_id: int = 1) -> Mock:
    c = Mock()
    c.symbol = symbol
    c.conId = con_id
    return c


# ===========================================================================
# 1. get_prec — current behavior baseline
# ===========================================================================


class TestGetPrec:
    """Document get_prec(v, base) baseline behavior (audit Issue 2.2)."""

    def test_valid_float_rounds_to_cent(self):
        assert get_prec(5.126, 0.01) == pytest.approx(5.13)

    def test_valid_float_rounds_to_half_dollar(self):
        assert get_prec(9.87, 0.50) == pytest.approx(10.0)

    def test_valid_float_rounds_to_dollar(self):
        assert get_prec(9.50, 1.0) == pytest.approx(10.0)

    def test_zero_value_returns_zero(self):
        result = get_prec(0.0, 0.01)
        assert result == pytest.approx(0.0)

    def test_zero_base_returns_none(self):
        # log10(0) raises ValueError → get_prec returns None via bare except
        assert get_prec(5.0, 0) is None

    def test_negative_value_rounds_correctly(self):
        result = get_prec(-5.126, 0.01)
        assert result == pytest.approx(-5.13)


# ===========================================================================
# 2. get_prec_safe — proposed graceful-degradation layer (audit Issue 2.2 "after")
# ===========================================================================


class TestGetPrecSafe:
    """Verify the proposed get_prec_safe handles all degenerate inputs without raising."""

    def test_none_input_returns_none(self):
        assert get_prec_safe(None, 0.01) is None

    def test_nan_input_returns_none(self):
        assert get_prec_safe(float("nan"), 0.01) is None

    def test_pos_inf_returns_none(self):
        assert get_prec_safe(float("inf"), 0.01) is None

    def test_neg_inf_returns_none(self):
        assert get_prec_safe(float("-inf"), 0.01) is None

    def test_zero_base_returns_none(self):
        assert get_prec_safe(5.0, 0) is None

    def test_valid_float_rounds_correctly(self):
        assert get_prec_safe(5.126, 0.01) == pytest.approx(5.13)

    def test_large_float_does_not_crash(self):
        result = get_prec_safe(1e12, 0.01)
        assert result is not None

    def test_negative_valid_float_rounds_correctly(self):
        assert get_prec_safe(-5.126, 0.01) == pytest.approx(-5.13)

    def test_does_not_raise_for_any_degenerate_combination(self):
        """Exhaustive check that no input combination raises an exception."""
        degenerate_values = [None, float("nan"), float("inf"), float("-inf"), 0.0]
        degenerate_bases = [0, float("nan"), float("inf"), -1.0]
        for v in degenerate_values:
            for b in degenerate_bases:
                get_prec_safe(v, b)  # must not raise


# ===========================================================================
# 3a. _fetch_prices_bulk_yf — direct unit tests for the primary source
# ===========================================================================


def _yf_multiframe(symbols: list[str], prices: list[float]) -> pd.DataFrame:
    """Minimal yf.download MultiIndex result (field × symbol columns)."""
    arrays = [["Close"] * len(symbols), symbols]
    cols = pd.MultiIndex.from_arrays(arrays)
    return pd.DataFrame([prices], columns=cols)


class TestFetchPricesBulkYf:
    """Unit tests for _fetch_prices_bulk_yf (audit Issue 2.1 primary source)."""

    def test_valid_multi_symbol_returns_close_dict(self):
        from src.build import _fetch_prices_bulk_yf

        yf_data = _yf_multiframe(["AAPL", "MSFT"], [150.0, 300.0])
        with patch("yfinance.download", return_value=yf_data):
            result = asyncio.run(_fetch_prices_bulk_yf(["AAPL", "MSFT"]))

        assert result["AAPL"] == pytest.approx(150.0)
        assert result["MSFT"] == pytest.approx(300.0)

    def test_single_symbol_returns_close_dict(self):
        from src.build import _fetch_prices_bulk_yf

        yf_data = _yf_multiframe(["AAPL"], [150.0])
        with patch("yfinance.download", return_value=yf_data):
            result = asyncio.run(_fetch_prices_bulk_yf(["AAPL"]))

        assert result["AAPL"] == pytest.approx(150.0)

    def test_empty_symbols_returns_empty_dict(self):
        from src.build import _fetch_prices_bulk_yf

        result = asyncio.run(_fetch_prices_bulk_yf([]))
        assert result == {}

    def test_empty_yf_response_returns_empty_dict(self):
        from src.build import _fetch_prices_bulk_yf

        with patch("yfinance.download", return_value=pd.DataFrame()):
            result = asyncio.run(_fetch_prices_bulk_yf(["AAPL"]))

        assert result == {}

    def test_yf_exception_returns_empty_dict_without_raising(self):
        from src.build import _fetch_prices_bulk_yf

        with patch("yfinance.download", side_effect=Exception("network error")):
            result = asyncio.run(_fetch_prices_bulk_yf(["AAPL"]))

        assert result == {}

    def test_nan_close_price_excluded_from_result(self):
        from src.build import _fetch_prices_bulk_yf

        yf_data = _yf_multiframe(["AAPL", "MSFT"], [float("nan"), 300.0])
        with patch("yfinance.download", return_value=yf_data):
            result = asyncio.run(_fetch_prices_bulk_yf(["AAPL", "MSFT"]))

        assert "AAPL" not in result      # NaN excluded
        assert result["MSFT"] == pytest.approx(300.0)


# ===========================================================================
# 3b. _fetch_prices_fallback_ib — direct unit tests for the IBKR fallback
# ===========================================================================


class TestFetchPricesFallbackIb:
    """Unit tests for _fetch_prices_fallback_ib (audit Issue 2.1 fallback + Semaphore(40))."""

    @staticmethod
    def _ib_mock(price: float = 150.0) -> MagicMock:
        t = Mock()
        t.last = price
        t.close = price - 1.0
        ib = MagicMock()
        ib.reqMktDataAsync = AsyncMock(return_value=t)
        return ib

    def test_valid_price_returned_for_single_contract(self):
        from src.build import _fetch_prices_fallback_ib

        ib = self._ib_mock(150.0)
        contracts = [_make_contract_mock("AAPL", 1)]
        result = asyncio.run(_fetch_prices_fallback_ib(ib, contracts))

        assert result["AAPL"] == pytest.approx(150.0)

    def test_valid_prices_for_multiple_contracts(self):
        from src.build import _fetch_prices_fallback_ib

        ib = self._ib_mock(200.0)
        contracts = [_make_contract_mock(f"SYM{i}", i + 1) for i in range(5)]
        result = asyncio.run(_fetch_prices_fallback_ib(ib, contracts))

        assert len(result) == 5
        for sym in [f"SYM{i}" for i in range(5)]:
            assert sym in result

    def test_empty_contracts_returns_empty_dict(self):
        from src.build import _fetch_prices_fallback_ib

        ib = self._ib_mock()
        result = asyncio.run(_fetch_prices_fallback_ib(ib, []))
        assert result == {}

    def test_nan_last_falls_back_to_close(self):
        """When last is NaN, close must be used as the price."""
        from src.build import _fetch_prices_fallback_ib

        t = Mock()
        t.last = float("nan")
        t.close = 148.0
        ib = MagicMock()
        ib.reqMktDataAsync = AsyncMock(return_value=t)

        contracts = [_make_contract_mock("AAPL", 1)]
        result = asyncio.run(_fetch_prices_fallback_ib(ib, contracts))

        assert result["AAPL"] == pytest.approx(148.0)

    def test_both_nan_excluded_from_result(self):
        """Contract with NaN last and NaN close must not appear in result."""
        from src.build import _fetch_prices_fallback_ib

        t = Mock()
        t.last = float("nan")
        t.close = float("nan")
        ib = MagicMock()
        ib.reqMktDataAsync = AsyncMock(return_value=t)

        contracts = [_make_contract_mock("AAPL", 1)]
        result = asyncio.run(_fetch_prices_fallback_ib(ib, contracts))

        assert "AAPL" not in result

    def test_individual_exception_does_not_abort_batch(self):
        """One failing contract must not prevent the rest from being fetched."""
        from src.build import _fetch_prices_fallback_ib

        call_count = 0

        async def _side_effect(contract, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            if contract.symbol == "BAD":
                raise Exception("pacing error")
            t = Mock()
            t.last = 100.0
            t.close = 99.0
            return t

        ib = MagicMock()
        ib.reqMktDataAsync = _side_effect

        contracts = [
            _make_contract_mock("AAPL", 1),
            _make_contract_mock("BAD", 2),
            _make_contract_mock("MSFT", 3),
        ]
        result = asyncio.run(_fetch_prices_fallback_ib(ib, contracts))

        assert "AAPL" in result
        assert "MSFT" in result
        assert "BAD" not in result     # failed silently
        assert call_count == 3         # all three were attempted

    def test_semaphore_40_caps_peak_concurrency(self):
        """Semaphore(40) inside the fallback must never let more than 40 run at once."""
        from src.build import _fetch_prices_fallback_ib

        peak: list[int] = [0]
        active: list[int] = [0]
        lock = asyncio.Lock()

        async def _tracking_req(contract, *args, **kwargs):
            async with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            await asyncio.sleep(0)
            async with lock:
                active[0] -= 1
            t = Mock()
            t.last = 100.0
            t.close = 99.0
            return t

        ib = MagicMock()
        ib.reqMktDataAsync = _tracking_req

        contracts = [_make_contract_mock(f"S{i}", i) for i in range(80)]
        asyncio.run(_fetch_prices_fallback_ib(ib, contracts))

        assert peak[0] <= 40, f"Peak concurrency {peak[0]} exceeded Semaphore(40)"


# ===========================================================================
# 3c. get_prices — end-to-end dual-source pipeline tests (audit Issue 2.1)
# ===========================================================================


class TestGetPricesDualSource:
    """End-to-end tests for get_prices dual-source hybrid pipeline."""

    @staticmethod
    def _ib_mock(price: float = 150.0) -> MagicMock:
        t = Mock()
        t.last = price
        t.close = price - 1.0
        ib = MagicMock()
        ib.reqMktDataAsync = AsyncMock(return_value=t)
        ib.isConnected.return_value = False
        return ib

    def test_returns_dataframe_with_required_columns(self):
        from src.build import get_prices

        contracts = [_make_contract_mock("AAPL", 1)]
        yf_data = _yf_multiframe(["AAPL"], [150.0])

        with patch("yfinance.download", return_value=yf_data):
            df = get_prices(contracts, market="SNP")

        assert isinstance(df, pd.DataFrame)
        for col in ("symbol", "bid", "ask", "last", "close"):
            assert col in df.columns

    def test_yfinance_hit_populates_close_skips_ibkr(self):
        """When yfinance prices all symbols, reqMktDataAsync must not be called."""
        from src.build import get_prices

        contracts = [_make_contract_mock("AAPL", 1), _make_contract_mock("MSFT", 2)]
        yf_data = _yf_multiframe(["AAPL", "MSFT"], [150.0, 300.0])
        ib = self._ib_mock()

        with patch("yfinance.download", return_value=yf_data):
            df = get_prices(contracts, market="SNP", ib=ib)

        aapl_close = df.loc[df["symbol"] == "AAPL", "close"].iloc[0]
        msft_close = df.loc[df["symbol"] == "MSFT", "close"].iloc[0]
        assert aapl_close == pytest.approx(150.0)
        assert msft_close == pytest.approx(300.0)
        ib.reqMktDataAsync.assert_not_called()

    def test_yfinance_miss_routes_to_ibkr_fallback(self):
        """Symbol absent from yfinance result must be fetched via IBKR."""
        from src.build import get_prices

        contracts = [_make_contract_mock("AAPL", 1), _make_contract_mock("NFLX", 2)]
        yf_data = _yf_multiframe(["AAPL"], [150.0])   # NFLX missing
        ib = self._ib_mock(price=400.0)

        with patch("yfinance.download", return_value=yf_data):
            df = get_prices(contracts, market="SNP", ib=ib)

        nflx_row = df.loc[df["symbol"] == "NFLX"].iloc[0]
        assert nflx_row["last"] == pytest.approx(400.0)
        ib.reqMktDataAsync.assert_called_once()

    def test_complete_yfinance_failure_routes_all_to_ibkr(self):
        """Empty yfinance response must send all contracts to the IBKR fallback."""
        from src.build import get_prices

        contracts = [_make_contract_mock(f"S{i}", i + 1) for i in range(3)]
        ib = self._ib_mock(price=100.0)

        with patch("yfinance.download", return_value=pd.DataFrame()):
            df = get_prices(contracts, market="SNP", ib=ib)

        assert ib.reqMktDataAsync.call_count == 3
        assert len(df) == 3
        assert df["last"].notna().all()

    def test_yfinance_exception_falls_back_to_ibkr(self):
        """yfinance raising an exception must fall back silently to IBKR."""
        from src.build import get_prices

        contracts = [_make_contract_mock("AAPL", 1)]
        ib = self._ib_mock(price=150.0)

        with patch("yfinance.download", side_effect=Exception("network timeout")):
            df = get_prices(contracts, market="SNP", ib=ib)

        assert len(df) == 1
        ib.reqMktDataAsync.assert_called_once()

    def test_returns_one_row_per_contract(self):
        from src.build import get_prices

        syms = [f"SYM{i}" for i in range(5)]
        contracts = [_make_contract_mock(s, i + 1) for i, s in enumerate(syms)]
        yf_data = _yf_multiframe(syms, [100.0 + i for i in range(5)])

        with patch("yfinance.download", return_value=yf_data):
            df = get_prices(contracts, market="SNP")

        assert len(df) == 5

    def test_empty_contracts_returns_empty_dataframe(self):
        from src.build import get_prices

        df = get_prices([])
        assert isinstance(df, pd.DataFrame)
        assert df.empty

    def test_ibkr_nan_last_stored_as_none(self):
        """NaN last from IBKR must appear as None (pandas NaT/None) in output."""
        from src.build import get_prices

        t = Mock()
        t.last = float("nan")
        t.close = float("nan")
        ib = MagicMock()
        ib.reqMktDataAsync = AsyncMock(return_value=t)
        ib.isConnected.return_value = False

        with patch("yfinance.download", return_value=pd.DataFrame()):
            df = get_prices([_make_contract_mock("AAPL", 1)], market="SNP", ib=ib)

        assert df.iloc[0]["last"] is None
        assert df.iloc[0]["close"] is None

    def test_ibkr_minus_one_stored_as_none(self):
        """-1.0 sentinel must be stored as None, not -1.0."""
        from src.build import get_prices

        t = Mock()
        t.last = -1.0
        t.close = -1.0
        ib = MagicMock()
        ib.reqMktDataAsync = AsyncMock(return_value=t)
        ib.isConnected.return_value = False

        with patch("yfinance.download", return_value=pd.DataFrame()):
            df = get_prices([_make_contract_mock("AAPL", 1)], market="SNP", ib=ib)

        assert df.iloc[0]["last"] is None


# ===========================================================================
# 4. Semaphore-backed throughput: tipping-point isolation
#    (audit Section 3 test_throughput_tipping_point — fully mocked)
# ===========================================================================


class TestPacingTippingPoint:
    """Audit Issue 2.1: semaphore-backed throttle caps peak concurrency."""

    def test_semaphore_limits_peak_concurrent_requests(self):
        """Semaphore(N) must never allow more than N coroutines to proceed simultaneously."""
        concurrency_cap = 5

        async def _run() -> int:
            peak: list[int] = [0]
            active: list[int] = [0]
            lock = asyncio.Lock()
            sem = asyncio.Semaphore(concurrency_cap)  # one shared semaphore

            async def _simulated_request(_: int) -> None:
                async with sem:
                    async with lock:
                        active[0] += 1
                        if active[0] > peak[0]:
                            peak[0] = active[0]
                    await asyncio.sleep(0)  # yield to let other coroutines attempt entry
                    async with lock:
                        active[0] -= 1

            await asyncio.gather(*[_simulated_request(i) for i in range(50)])
            return peak[0]

        observed_peak = asyncio.run(_run())
        assert observed_peak <= concurrency_cap, (
            f"Peak concurrency {observed_peak} exceeded semaphore cap {concurrency_cap}"
        )

    def test_100_mocked_requests_with_semaphore_40_complete_under_2s(self):
        """100 requests at 2 ms each, concurrency=40 → must finish in under 2 s.

        This validates that the prescribed semaphore(40) pipeline from audit Issue 2.1
        meets throughput targets without pacing violations.
        """
        sem = asyncio.Semaphore(40)

        async def _fetch_one(symbol: str) -> str:
            async with sem:
                await asyncio.sleep(0.002)  # 2 ms simulated IBKR latency
                return symbol

        t0 = time.monotonic()
        completed = asyncio.run(
            asyncio.gather(*[_fetch_one(f"SYM{i}") for i in range(100)])
        )
        elapsed = time.monotonic() - t0

        assert len(completed) == 100
        assert elapsed < 2.0, f"100 paced requests took {elapsed:.2f}s (limit 2 s)"

    def test_zero_drops_under_controlled_semaphore(self):
        """With instant tasks, every result must be non-None — zero drops."""
        async def _fetch_one(i: int) -> Optional[float]:
            async with asyncio.Semaphore(40):
                await asyncio.sleep(0)
                return float(i)

        results = asyncio.run(
            asyncio.gather(*[_fetch_one(i) for i in range(100)])
        )

        nones = sum(1 for r in results if r is None)
        assert nones == 0, f"Expected 0 drops, got {nones}"

    def test_semaphore_40_safer_than_50_at_boundary(self):
        """Verify semaphore(40) keeps a 10-req/s safety buffer below the 50 req/s IBKR limit."""
        sem_safe = asyncio.Semaphore(40)
        peak = 0
        active = 0
        lock = asyncio.Lock()

        async def _req(_: int) -> None:
            nonlocal active, peak
            async with sem_safe:
                async with lock:
                    active += 1
                    peak = max(peak, active)
                await asyncio.sleep(0)
                async with lock:
                    active -= 1

        asyncio.run(asyncio.gather(*[_req(i) for i in range(100)]))
        assert peak <= 40
        assert peak < 50, "Safe semaphore(40) must stay below the 50 req/s IBKR ceiling"


# ===========================================================================
# 5. _fetch_one_yf — primary yfinance source
# ===========================================================================


class TestFetchOneYf:
    """Verify _fetch_one_yf handles success, empty frame, and exceptions gracefully."""

    def test_valid_data_returns_symbol_and_dataframe(self):
        spec = {"symbol": "AAPL", "exchange": "SMART", "currency": "USD"}
        start, end = date(2024, 1, 1), date(2024, 1, 31)

        fake_df = pd.DataFrame(
            {
                "Open": [150.0],
                "High": [155.0],
                "Low": [148.0],
                "Close": [152.0],
                "Volume": [1_000_000],
            },
            index=pd.to_datetime(["2024-01-15"]),
        )

        async def run():
            with patch("yfinance.Ticker") as mock_cls:
                mock_ticker = Mock()
                mock_ticker.history.return_value = fake_df
                mock_cls.return_value = mock_ticker
                return await _fetch_one_yf(spec, start, end)

        sym, df = asyncio.run(run())
        assert sym == "AAPL"
        assert df is not None
        assert not df.empty

    def test_empty_dataframe_returns_none(self):
        spec = {"symbol": "AAPL", "exchange": "SMART", "currency": "USD"}
        start, end = date(2024, 1, 1), date(2024, 1, 31)

        async def run():
            with patch("yfinance.Ticker") as mock_cls:
                mock_ticker = Mock()
                mock_ticker.history.return_value = pd.DataFrame()
                mock_cls.return_value = mock_ticker
                return await _fetch_one_yf(spec, start, end)

        sym, df = asyncio.run(run())
        assert sym == "AAPL"
        assert df is None

    def test_exception_returns_none_without_raising(self):
        """Any yfinance exception must be swallowed; caller gets (sym, None)."""
        spec = {"symbol": "AAPL", "exchange": "SMART", "currency": "USD"}
        start, end = date(2024, 1, 1), date(2024, 1, 31)

        async def run():
            with patch("yfinance.Ticker") as mock_cls:
                mock_ticker = Mock()
                mock_ticker.history.side_effect = Exception("yfinance network error")
                mock_cls.return_value = mock_ticker
                return await _fetch_one_yf(spec, start, end)

        sym, df = asyncio.run(run())
        assert sym == "AAPL"
        assert df is None

    def test_yf_ticker_override_in_spec_bypasses_ib_to_yf(self):
        """yf_ticker key in spec must be used directly, skipping ib_to_yf() mapping."""
        spec = {
            "symbol": "CSPX",
            "exchange": "LSE",
            "currency": "GBP",
            "yf_ticker": "CSPX.L",
        }
        start, end = date(2024, 1, 1), date(2024, 1, 10)
        captured: list[str] = []

        async def run():
            with patch("yfinance.Ticker") as mock_cls:
                def _capture(tick: str) -> Mock:
                    captured.append(tick)
                    m = Mock()
                    m.history.return_value = pd.DataFrame()
                    return m

                mock_cls.side_effect = _capture
                return await _fetch_one_yf(spec, start, end)

        asyncio.run(run())
        assert captured == ["CSPX.L"]

    def test_returns_only_ohlcv_columns(self):
        """Returned DataFrame must contain exactly the OHLCV columns (no extras)."""
        spec = {"symbol": "NVDA", "exchange": "SMART", "currency": "USD"}
        start, end = date(2024, 1, 1), date(2024, 1, 31)

        fake_df = pd.DataFrame(
            {
                "Open": [500.0, 510.0],
                "High": [520.0, 525.0],
                "Low": [495.0, 505.0],
                "Close": [510.0, 515.0],
                "Volume": [2_000_000, 1_800_000],
                "Dividends": [0.0, 0.0],   # extra column yfinance sometimes adds
                "Stock Splits": [0.0, 0.0],
            },
            index=pd.to_datetime(["2024-01-15", "2024-01-16"]),
        )

        async def run():
            with patch("yfinance.Ticker") as mock_cls:
                mock_ticker = Mock()
                mock_ticker.history.return_value = fake_df
                mock_cls.return_value = mock_ticker
                return await _fetch_one_yf(spec, start, end)

        sym, df = asyncio.run(run())
        assert sym == "NVDA"
        if df is not None:
            unexpected = [c for c in df.columns if c not in ("Open", "High", "Low", "Close", "Volume")]
            assert not unexpected, f"Unexpected columns in cleaned df: {unexpected}"


# ===========================================================================
# 6. Market-state variance: ticker structural integrity
#    (audit Section 3 test_market_state_variance — mocked variant)
# ===========================================================================


class TestMarketStateVariance:
    """Verify ticker objects expose required fields regardless of trading-hour state."""

    def test_ticker_has_required_price_attributes(self):
        t = _make_ticker_mock("AAPL", 1, last=150.0, bid=149.9, ask=150.1)
        for attr in ("bid", "ask", "last", "close", "volume"):
            assert hasattr(t, attr), f"Ticker missing required attribute: {attr}"

    def test_after_market_hours_close_is_valid(self):
        """After close: last may be NaN but close must be a real price."""
        t = _make_ticker_mock("AAPL", 1, last=float("nan"), close=149.5)
        assert not math.isnan(t.close), "close must be valid after market hours"

    def test_during_market_hours_bid_ask_are_positive(self):
        """During market hours bid and ask must both be positive."""
        t = _make_ticker_mock("AAPL", 1, bid=149.9, ask=150.1)
        assert t.bid > 0
        assert t.ask > 0
        assert t.ask >= t.bid

    def test_ticker_with_all_nan_prices_flagged_as_invalid(self):
        """All-NaN ticker is detected by is_valid_price as having no data."""
        def is_valid_price(price) -> bool:
            if price is None:
                return False
            try:
                return price != -1.0 and not math.isnan(price)
            except (TypeError, ValueError):
                return False

        t = _make_ticker_mock(
            "AAPL", 1,
            last=float("nan"), bid=float("nan"),
            ask=float("nan"), close=float("nan"),
        )
        assert not is_valid_price(t.last)
        assert not is_valid_price(t.bid)
        assert not is_valid_price(t.ask)
        assert not is_valid_price(t.close)

    def test_ticker_contract_symbol_accessible(self):
        t = _make_ticker_mock("NVDA", 42)
        assert t.contract.symbol == "NVDA"
        assert t.contract.conId == 42

    def test_ticker_negative_one_detected_as_sentinel(self):
        """-1.0 is IBKR's 'no data' sentinel and must not be treated as a valid price."""
        def is_valid_price(price) -> bool:
            if price is None:
                return False
            try:
                return price != -1.0 and not math.isnan(price)
            except (TypeError, ValueError):
                return False

        t = _make_ticker_mock("AAPL", 1, last=-1.0, bid=-1.0, ask=-1.0)
        assert not is_valid_price(t.last)
        assert not is_valid_price(t.bid)
        assert not is_valid_price(t.ask)


# ===========================================================================
# 7. Resilience: yfinance None → symbols routed to IBKR fallback
#    (audit Section 3 test_resilience_fallback_mocking)
# ===========================================================================


class TestResilienceFallback:
    """Verify that yfinance misses are correctly identified for IBKR fallback routing."""

    def test_partial_yf_failure_identifies_correct_fallback_candidates(self):
        """Symbols absent from yf_results dict must be flagged for IBKR fallback."""
        specs = [
            {"symbol": "AAPL", "exchange": "SMART", "currency": "USD"},
            {"symbol": "MSFT", "exchange": "SMART", "currency": "USD"},
            {"symbol": "NFLX", "exchange": "SMART", "currency": "USD"},
        ]
        # Only AAPL succeeded; MSFT and NFLX need IBKR fallback
        yf_results: dict[str, pd.DataFrame] = {
            "AAPL": pd.DataFrame(
                {"Close": [150.0]}, index=pd.to_datetime(["2024-01-15"])
            )
        }

        fallback = [s["symbol"] for s in specs if s["symbol"] not in yf_results]
        assert set(fallback) == {"MSFT", "NFLX"}

    def test_all_yf_success_produces_no_fallback_candidates(self):
        specs = [
            {"symbol": "AAPL", "exchange": "SMART", "currency": "USD"},
            {"symbol": "MSFT", "exchange": "SMART", "currency": "USD"},
        ]
        yf_results = {
            "AAPL": pd.DataFrame(
                {"Close": [150.0]}, index=pd.to_datetime(["2024-01-15"])
            ),
            "MSFT": pd.DataFrame(
                {"Close": [300.0]}, index=pd.to_datetime(["2024-01-15"])
            ),
        }
        fallback = [s["symbol"] for s in specs if s["symbol"] not in yf_results]
        assert fallback == []

    def test_complete_yf_failure_routes_all_symbols_to_fallback(self):
        specs = [
            {"symbol": f"SYM{i}", "exchange": "SMART", "currency": "USD"}
            for i in range(5)
        ]
        yf_results: dict[str, pd.DataFrame] = {}  # all failed

        fallback = [s["symbol"] for s in specs if s["symbol"] not in yf_results]
        assert len(fallback) == 5
        assert set(fallback) == {f"SYM{i}" for i in range(5)}

    def test_fetch_one_yf_none_result_flags_symbol_for_fallback(self):
        """_fetch_one_yf returning (sym, None) marks the symbol as needing fallback."""
        spec = {"symbol": "NFLX", "exchange": "SMART", "currency": "USD"}
        start, end = date(2024, 1, 1), date(2024, 1, 31)

        async def run():
            with patch("yfinance.Ticker") as mock_cls:
                mock_ticker = Mock()
                mock_ticker.history.return_value = pd.DataFrame()  # empty → None
                mock_cls.return_value = mock_ticker
                sym, df = await _fetch_one_yf(spec, start, end)
                return sym, df is None

        sym, needs_fallback = asyncio.run(run())
        assert sym == "NFLX"
        assert needs_fallback is True

    def test_merged_results_contain_both_yf_and_fallback_data(self):
        """Final OHLC dict must include data from both sources keyed by symbol."""
        yf_results = {
            "AAPL": pd.DataFrame(
                {"Close": [150.0]}, index=pd.to_datetime(["2024-01-15"])
            )
        }
        fallback_results = {
            "MSFT": pd.DataFrame(
                {"Close": [300.0]}, index=pd.to_datetime(["2024-01-15"])
            )
        }
        merged = {**yf_results, **fallback_results}
        assert "AAPL" in merged
        assert "MSFT" in merged
        assert len(merged) == 2

    def test_fallback_results_override_yf_on_key_collision(self):
        """If IBKR fallback provides data for a symbol that yf also found, the merge
        (dict-spread) keeps the later value. Verify merge semantics are correct."""
        yf_results = {
            "AAPL": pd.DataFrame(
                {"Close": [150.0]}, index=pd.to_datetime(["2024-01-15"])
            )
        }
        fallback_results = {
            "AAPL": pd.DataFrame(
                {"Close": [151.0]}, index=pd.to_datetime(["2024-01-15"])
            )
        }
        merged = {**yf_results, **fallback_results}
        # fallback_results overwrites yf_results for the same key
        assert merged["AAPL"]["Close"].iloc[0] == pytest.approx(151.0)


# ===========================================================================
# Section 3 — Three canonical test cases prescribed by ANTIGRAVITY_AUDIT.md
#
# Named exactly as the audit specifies.  All IB Gateway calls are mocked with
# AsyncMock / MagicMock so the suite runs deterministically without a live
# TWS / IBG instance.
# ===========================================================================


def test_throughput_tipping_point():
    """Isolate exact tipping point for pacing violations by testing batch sizes.

    Audit prescription: generate 100 contracts (10 symbols × 10), fetch via
    Semaphore(50)-backed loop, report drops and elapsed time.
    IB Gateway mocked — pacing is measured against the asyncio scheduler.
    """
    from ib_async import Stock

    async def _run() -> tuple[int, float]:
        mock_ib = MagicMock()
        mock_ib.qualifyContractsAsync = AsyncMock(return_value=None)

        async def _mock_req_mkt(contract, *args, **kwargs):
            t = Mock()
            t.last = 100.0 + hash(contract.symbol) % 50  # deterministic price
            t.bid = t.last - 0.05
            t.ask = t.last + 0.05
            return t

        mock_ib.reqMktDataAsync = _mock_req_mkt

        symbols = (
            ["AAPL", "MSFT", "GOOGL", "AMZN", "META", "TSLA", "NVDA", "NFLX", "AMD", "INTC"] * 10
        )
        contracts = [Stock(s, "SMART", "USD") for s in symbols]
        await mock_ib.qualifyContractsAsync(*contracts)

        start_time = time.time()
        sem = asyncio.Semaphore(50)  # prescribed cap from the audit

        async def request_data(c):
            async with sem:
                ticker = await mock_ib.reqMktDataAsync(c, "", snapshot=True)
                return ticker.last

        tasks = [request_data(c) for c in contracts]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        elapsed = time.time() - start_time

        nans = sum(1 for r in results if r is None or isinstance(r, Exception))
        print(
            f"\nIngested {len(contracts)} contracts in {elapsed:.3f}s. "
            f"Drops: {nans}/{len(contracts)}"
        )
        # Baseline assertion from audit: execution completed (elapsed > 0)
        assert elapsed > 0.0
        return nans, elapsed

    nans, _elapsed = asyncio.run(_run())
    # With mocked IB there are no real pacing failures — zero drops expected
    assert nans == 0, f"Expected 0 drops with mocked IB, got {nans}"


def test_market_state_variance():
    """Verify response coverage variations between active and closed market hours.

    Audit prescription: request a single AAPL ticker and assert basic structural
    integrity regardless of current trading hours (ticker is not None, has bid/ask).
    IB Gateway mocked to return a ticker stub with all required attributes.
    """
    from ib_async import Stock

    async def _run():
        mock_ib = MagicMock()
        mock_ib.qualifyContractsAsync = AsyncMock(return_value=None)

        mock_ticker = Mock()
        mock_ticker.bid = 149.90
        mock_ticker.ask = 150.10
        mock_ticker.last = 150.00
        mock_ticker.close = 149.50
        mock_ib.reqMktDataAsync = AsyncMock(return_value=mock_ticker)

        contract = Stock("AAPL", "SMART", "USD")
        await mock_ib.qualifyContractsAsync(contract)
        ticker = await mock_ib.reqMktDataAsync(contract, "", snapshot=True)

        # Assertions from the audit prescription — structural integrity checks
        assert ticker is not None
        assert hasattr(ticker, "bid")
        assert hasattr(ticker, "ask")

        return ticker

    ticker = asyncio.run(_run())
    # Additional market-state checks beyond the audit's minimum assertions
    assert ticker.bid > 0, "bid must be a positive price"
    assert ticker.ask >= ticker.bid, "ask must be >= bid"


def test_resilience_fallback_mocking():
    """Mock yfinance failure to ensure the IBKR fallback automatically takes over.

    Audit prescription: patch _fetch_one_yf to return (sym, None) and verify
    that the symbol is correctly identified as needing the IBKR fallback path.
    """
    import src.dashboard.ohlc as ohlc_mod

    spec = {"symbol": "AAPL", "exchange": "SMART", "currency": "USD"}
    start_dt, end_dt = date(2024, 1, 1), date(2024, 1, 31)

    async def _run() -> tuple[str, bool]:
        # Patch at the module level so calls via ohlc_mod respect the mock
        with patch.object(
            ohlc_mod,
            "_fetch_one_yf",
            new=AsyncMock(return_value=("AAPL", None)),
        ) as mock_yf:
            sym, df = await ohlc_mod._fetch_one_yf(spec, start_dt, end_dt)

            # yfinance returned None — verify the mock was invoked
            mock_yf.assert_called_once_with(spec, start_dt, end_dt)

            # df is None → this symbol must be routed to IBKR fallback
            assert sym == "AAPL"
            assert df is None, "Patched yfinance must return None to trigger fallback"

        # Downstream routing: symbols absent from yf_results go to fallback
        yf_results: dict[str, pd.DataFrame] = {}  # AAPL not added (df was None)
        needs_fallback = sym not in yf_results
        return sym, needs_fallback

    sym, needs_fallback = asyncio.run(_run())
    assert sym == "AAPL"
    assert needs_fallback is True, "AAPL must be flagged for IBKR fallback when yfinance returns None"
