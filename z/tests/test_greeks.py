"""Black-Scholes pricing + implied-vol round-trip."""

import math

from src.nsewheel import greeks


def test_put_call_parity():
    S, K, T, sigma, r = 100.0, 100.0, 0.25, 0.2, greeks.RISK_FREE_RATE
    call = greeks.bs_price(S, K, T, sigma, r, "C")
    put = greeks.bs_price(S, K, T, sigma, r, "P")
    # C - P = S - K e^{-rT}
    assert abs((call - put) - (S - K * math.exp(-r * T))) < 1e-6


def test_degenerate_inputs():
    assert greeks.bs_price(100, 100, 0, 0.2) == 0.0
    assert greeks.bs_price(100, 100, 0.25, 0) == 0.0


def test_iv_roundtrip():
    S, K, T, true_iv = 2900.0, 2950.0, 0.08, 0.27
    price = greeks.bs_price(S, K, T, true_iv, right="C")
    solved = greeks.implied_vol(price, S, K, T, right="C")
    assert abs(solved - true_iv) < 1e-3


def test_iv_below_intrinsic_returns_zero():
    # price below intrinsic value -> unsolvable
    assert greeks.implied_vol(1.0, 3000.0, 2000.0, 0.1, right="C") == 0.0


def test_call_delta_bounds():
    d = greeks.bs_delta(100, 100, 0.25, 0.2, right="C")
    assert 0.0 < d < 1.0
