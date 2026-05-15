"""
Hot-path profiler for the IBKR dashboard.

Measures the four main bottlenecks under realistic portfolio sizes and prints
a before/after table.  Run directly:

    uv run python profile_hotpaths.py
"""
from __future__ import annotations

import timeit
from typing import Any
from unittest.mock import Mock

# Suppress loguru so DEBUG lines don't skew timings or flood output
from loguru import logger as _loguru
_loguru.remove()  # remove default stderr sink; file sink not added in this script

import pandas as pd  # noqa: E402


# ── helpers ──────────────────────────────────────────────────────────────────

def _contract(con_id: int, symbol: str = "AAPL", sec_type: str = "STK",
               right: str = "", strike: float = 0.0, expiry: str = "") -> Mock:
    c = Mock()
    c.conId = con_id
    c.symbol = symbol
    c.secType = sec_type
    c.currency = "USD"
    c.primaryExch = "SMART"
    c.right = right
    c.strike = strike
    c.lastTradeDateOrContractMonth = expiry
    c.exchange = "SMART"
    return c


def _portfolio_item(contract: Mock, position: float = 100.0) -> Mock:
    item = Mock()
    item.contract = contract
    item.account = "U123456"
    item.position = position
    item.averageCost = 150.0
    item.marketPrice = 155.0
    item.marketValue = position * 155.0
    item.unrealizedPNL = 500.0
    item.realizedPNL = 0.0
    return item


def _make_50_positions() -> pd.DataFrame:
    """Return a 50-row positions DataFrame matching the internal format."""
    from src.dashboard.ib_client import IBClient
    rows = []
    for i in range(50):
        c = _contract(i + 1, f"SYM{i:02d}", "STK" if i % 3 else "OPT",
                      "C" if i % 3 == 0 else "", 200.0 if i % 3 == 0 else 0.0,
                      "20261219" if i % 3 == 0 else "")
        rows.append(IBClient._build_position_row(c, "U123456", float((i + 1) * 10), 150.0,
                                                  155.0, float((i + 1) * 1550), 500.0, 0.0))
    return pd.DataFrame(rows)


def _make_tickers(n: int = 50) -> dict:
    """Return a dict of n TickerSnap instances keyed by conId."""
    from src.dashboard.ib_client import TickerSnap
    return {
        i + 1: TickerSnap(
            last=155.0 + i * 0.1,
            bid=154.9 + i * 0.1,
            ask=155.1 + i * 0.1,
            delta=-0.3 - i * 0.001,
            gamma=0.05,
            theta=-0.02,
            vega=0.10,
            iv=0.25,
            underlying_px=155.0 + i * 0.1,
        )
        for i in range(n)
    }


# ── captured baseline (pre-optimization, measured on 2026-05-13) ──────────────
# These were measured on the original code (mask→filter→concat, list-of-dicts→merge,
# two-pass map, O(n²) linear scan) and are kept here as the reference "before" values.

_BASELINE: dict[str, float] = {
    "on_portfolio":  9.499,   # ms/call — mask→concat per event on 50-row portfolio
    "join_tickers":  6.787,   # ms/call — list-of-dicts → DataFrame → left-merge
    "dte_series":    0.873,   # ms/call — two sequential .map() passes
    "cover_protect": 156.075, # ms/call — opt[opt.symbol == sym] linear scan per stock
}


# ── after measurements (run against the current, optimized code) ──────────────

def bench_on_portfolio_after(n_reps: int = 200) -> float:
    """
    Optimized _on_portfolio: dict-backed store, O(1) update, one DataFrame rebuild.
    """
    from src.dashboard.ib_client import IBClient
    IBClient._instance = None
    IBClient._log_sink_added = True
    client = IBClient()
    for i in range(50):
        c = _contract(i + 1, f"SYM{i:02d}")
        client._on_portfolio(_portfolio_item(c, 100.0))

    target = _contract(25, "SYM24")
    item = _portfolio_item(target, 200.0)

    def _run():
        client._on_portfolio(item)

    elapsed = timeit.timeit(_run, number=n_reps)
    IBClient._instance = None
    IBClient._log_sink_added = False
    return elapsed / n_reps * 1000


def bench_join_tickers_after(n_reps: int = 500) -> float:
    """
    Optimized _join_tickers: direct dict lookup + np.array per column + df.assign().
    """
    from src.dashboard.risk import _join_tickers
    positions = _make_50_positions()
    tickers = _make_tickers(50)

    def _run():
        _join_tickers(positions, tickers)

    elapsed = timeit.timeit(_run, number=n_reps)
    return elapsed / n_reps * 1000


def bench_dte_series_after(n_reps: int = 2000) -> float:
    """
    Optimized _dte_series: single-pass ordinal arithmetic, no intermediate date objects.
    """
    from src.dashboard.risk import _dte_series
    expiries = pd.Series(
        ["20261219" if i % 2 == 0 else "" for i in range(50)]
    )

    def _run():
        _dte_series(expiries)

    elapsed = timeit.timeit(_run, number=n_reps)
    return elapsed / n_reps * 1000


def bench_cover_protect_gaps_after(n_reps: int = 200) -> float:
    """
    Optimized cover_protect_gaps: pre-group options by symbol, O(1) per-stock lookup.
    """
    from src.dashboard.risk import cover_protect_gaps
    from src.dashboard.ib_client import IBClient
    IBClient._instance = None
    IBClient._log_sink_added = True
    positions = _make_50_positions()
    IBClient._instance = None
    IBClient._log_sink_added = False
    tickers = _make_tickers(50)

    def _run():
        cover_protect_gaps(positions, tickers)

    elapsed = timeit.timeit(_run, number=n_reps)
    return elapsed / n_reps * 1000


# ── entry point ───────────────────────────────────────────────────────────────

_LABELS: dict[str, str] = {
    "on_portfolio":  "_on_portfolio      (200 reps, 50-row portfolio)",
    "join_tickers":  "_join_tickers      (500 reps, 50 pos × 50 tickers)",
    "dte_series":    "_dte_series        (2000 reps, 50 expiry strings)",
    "cover_protect": "cover_protect_gaps (200 reps, 50 positions)",
}

_AFTER_BENCHES: dict[str, Any] = {
    "on_portfolio":  bench_on_portfolio_after,
    "join_tickers":  bench_join_tickers_after,
    "dte_series":    bench_dte_series_after,
    "cover_protect": bench_cover_protect_gaps_after,
}


def run_after() -> dict[str, float]:
    print("\n── After optimization ──────────────────────────────────────────")
    results: dict[str, float] = {}
    for name, fn in _AFTER_BENCHES.items():
        print(f"  {_LABELS[name]} ...", end=" ", flush=True)
        results[name] = fn()
        print(f"{results[name]:.3f} ms/call")
    return results


def print_summary(before: dict[str, float], after: dict[str, float]) -> None:
    print("\n── Summary ─────────────────────────────────────────────────────")
    print(f"  {'Function':<22} {'Before':>10} {'After':>10} {'Speedup':>10}")
    print(f"  {'-'*22} {'-'*10} {'-'*10} {'-'*10}")
    for k in before:
        b, a = before[k], after[k]
        sp = b / a if a > 0 else float("inf")
        print(f"  {k:<22} {b:>9.3f}ms {a:>9.3f}ms {sp:>9.1f}x")
    print()


if __name__ == "__main__":
    after = run_after()
    print_summary(_BASELINE, after)
