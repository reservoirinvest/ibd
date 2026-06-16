"""Black-Scholes pricing, greeks, and implied-volatility solving.

Kite Connect quotes do not include option greeks, so we compute them. The pricing core is
ported from the ibd backtest (``src/backtest/synthetic.py:_bs_price``) and extended with
delta/vega and a Newton-with-bisection IV solver driven off the option LTP.
"""

from __future__ import annotations

import math

RISK_FREE_RATE = 0.065  # India ~ repo/T-bill proxy
TRADING_DAYS = 252


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def _d1_d2(S: float, K: float, T: float, sigma: float, r: float) -> tuple[float, float]:
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    return d1, d2


def bs_price(S: float, K: float, T: float, sigma: float,
             r: float = RISK_FREE_RATE, right: str = "C") -> float:
    """Black-Scholes price per share. Returns 0 on degenerate inputs.

    ``right`` accepts C/CE for calls, P/PE for puts.
    """
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.0
    try:
        d1, d2 = _d1_d2(S, K, T, sigma, r)
        if right.upper().startswith("C"):
            return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)
        return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)
    except (ValueError, ZeroDivisionError):
        return 0.0


def bs_delta(S: float, K: float, T: float, sigma: float,
             r: float = RISK_FREE_RATE, right: str = "C") -> float:
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.0
    d1, _ = _d1_d2(S, K, T, sigma, r)
    if right.upper().startswith("C"):
        return _norm_cdf(d1)
    return _norm_cdf(d1) - 1.0


def bs_vega(S: float, K: float, T: float, sigma: float, r: float = RISK_FREE_RATE) -> float:
    """Vega per 1.0 (100%) change in vol, per share."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.0
    d1, _ = _d1_d2(S, K, T, sigma, r)
    return S * _norm_pdf(d1) * math.sqrt(T)


def implied_vol(price: float, S: float, K: float, T: float,
                r: float = RISK_FREE_RATE, right: str = "C") -> float:
    """Solve implied volatility from an observed option price.

    Newton-Raphson with a bisection fallback over [1e-4, 5.0]. Returns 0.0 when the price is
    below intrinsic or inputs are degenerate.
    """
    if price <= 0 or T <= 0 or S <= 0 or K <= 0:
        return 0.0
    intrinsic = max(0.0, (S - K) if right.upper().startswith("C") else (K - S))
    if price < intrinsic - 1e-6:
        return 0.0

    lo, hi = 1e-4, 5.0
    sigma = 0.3
    for _ in range(50):
        diff = bs_price(S, K, T, sigma, r, right) - price
        if abs(diff) < 1e-4:
            return sigma
        v = bs_vega(S, K, T, sigma, r)
        if v < 1e-8:
            break
        step = diff / v
        sigma -= step
        if sigma <= lo or sigma >= hi:
            break  # leave Newton, finish with bisection

    # Bisection fallback
    lo, hi = 1e-4, 5.0
    for _ in range(100):
        mid = 0.5 * (lo + hi)
        if bs_price(S, K, T, mid, r, right) > price:
            hi = mid
        else:
            lo = mid
    return 0.5 * (lo + hi)
