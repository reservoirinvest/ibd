"""Validation/guard tests for the bulk-extraction pipeline (src/build.py).

These run WITHOUT an IBKR connection — they exercise the pure helpers and the
coverage/next-step summary logic on synthetic DataFrames.
"""
import numpy as np
import pandas as pd
import pytest

from src.build import (
    _coverage,
    _is_valid_price,
    get_prec_safe,
    write_build_summary,
)


# ---------------------------------------------------------------------------
# _is_valid_price
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "value,expected",
    [
        (10.5, True),
        (0.0, True),          # zero is finite/non-sentinel → valid here
        (-1.0, False),        # IBKR "no data" sentinel
        (None, False),
        (float("nan"), False),
        (np.nan, False),
        (float("inf"), False),
        (float("-inf"), False),
        ("abc", False),       # non-numeric
    ],
)
def test_is_valid_price(value, expected):
    assert _is_valid_price(value) is expected


# ---------------------------------------------------------------------------
# get_prec_safe
# ---------------------------------------------------------------------------
def test_get_prec_safe_rounds_and_guards():
    assert get_prec_safe(10.237, 0.01) == pytest.approx(10.24)
    assert get_prec_safe(float("nan"), 0.01) is None
    assert get_prec_safe(float("inf"), 0.01) is None
    assert get_prec_safe(None, 0.01) is None


# ---------------------------------------------------------------------------
# _coverage
# ---------------------------------------------------------------------------
def test_coverage():
    assert _coverage(0, 0) == 0.0            # no divide-by-zero
    assert _coverage(50, 100) == 50.0
    assert _coverage(1, 3) == 33.3           # rounded to 1 dp


# ---------------------------------------------------------------------------
# write_build_summary
# ---------------------------------------------------------------------------
def _unds(rows):
    return pd.DataFrame(rows, columns=["symbol", "price", "iv", "hv"])


def test_summary_full_coverage_no_warnings(tmp_path):
    chains = pd.DataFrame({"symbol": ["AAA", "BBB"]})
    unds = _unds([("AAA", 100.0, 0.25, 0.20), ("BBB", 50.0, 0.30, 0.22)])
    out = tmp_path / "build_summary.json"

    summary = write_build_summary(2, chains, unds, path=out)

    assert out.exists()
    assert summary["chains"]["coverage_pct"] == 100.0
    assert summary["prices"]["coverage_pct"] == 100.0
    assert summary["iv"]["coverage_pct"] == 100.0
    assert summary["warnings"] == []
    assert summary["next_steps"] == []


def test_summary_flags_missing_iv(tmp_path):
    chains = pd.DataFrame({"symbol": ["AAA", "BBB", "CCC", "DDD"]})
    # 2 of 4 have NaN / non-positive IV → 50% IV coverage → warning + next step
    unds = _unds([
        ("AAA", 100.0, 0.25, 0.20),
        ("BBB", 50.0, np.nan, 0.22),
        ("CCC", 25.0, 0.0, 0.10),
        ("DDD", 10.0, 0.30, 0.15),
    ])
    out = tmp_path / "build_summary.json"

    summary = write_build_summary(4, chains, unds, path=out)

    assert summary["iv"]["coverage_pct"] == 50.0
    assert any("IV coverage" in w for w in summary["warnings"])
    assert summary["next_steps"]            # non-empty actionable guidance
    assert set(summary["missing_iv_symbols"]) == {"BBB", "CCC"}


def test_summary_empty_unds_is_safe(tmp_path):
    chains = pd.DataFrame()
    unds = pd.DataFrame()
    out = tmp_path / "build_summary.json"

    summary = write_build_summary(10, chains, unds, path=out)

    assert out.exists()
    assert summary["chains"]["coverage_pct"] == 0.0
    assert summary["prices"]["rows"] == 0
    assert summary["iv"]["rows"] == 0


# ---------------------------------------------------------------------------
# Predicate parity: the NaN/≤0 price+iv rule used by derive.py's exclusion
# (mirrors `_invalid_price_iv` in src/derive.py — kept in sync here).
# ---------------------------------------------------------------------------
def test_invalid_price_iv_predicate():
    df = _unds([
        ("AAA", 100.0, 0.25, 0.2),   # valid
        ("BBB", np.nan, 0.30, 0.2),  # bad price
        ("CCC", 50.0, np.nan, 0.2),  # bad iv
        ("DDD", 0.0, 0.30, 0.2),     # non-positive price
        ("EEE", 50.0, 0.0, 0.2),     # non-positive iv
    ])
    p = pd.to_numeric(df["price"], errors="coerce")
    v = pd.to_numeric(df["iv"], errors="coerce")
    invalid = ~((p > 0) & (v > 0))
    bad = set(df.loc[invalid, "symbol"])
    assert bad == {"BBB", "CCC", "DDD", "EEE"}


@pytest.mark.live
def test_iv_speed_smoke():
    """Optional live smoke test (skipped unless -m live): time the IV stage.

    Run with: uv run pytest tests/test_build_validation.py -m live
    """
    import time

    from ib_async import Stock

    from src.build import get_volatilities_snapshot

    contracts = [Stock(s, "SMART", "USD") for s in ("AAPL", "MSFT", "SPY")]
    t0 = time.perf_counter()
    df = get_volatilities_snapshot(contracts, market="SNP")
    elapsed = time.perf_counter() - t0
    assert not df.empty
    # Event-driven IV should resolve 3 liquid names far faster than the old
    # fixed 10s+2s-per-contract path.
    assert elapsed < 15, f"IV stage too slow: {elapsed:.1f}s"
