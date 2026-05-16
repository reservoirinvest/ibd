"""Option strategy P/L simulation at expiry.

Sign convention for Leg:
  quantity > 0 = long  (paid premium — debit)
  quantity < 0 = short (received premium — credit)
  premium      = option price per share (always positive)

P/L per leg at expiry = quantity * (intrinsic_at_expiry - premium) * multiplier
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class Leg:
    option_type: str    # "C" | "P" | "STK"
    strike: float       # strike or cost basis for STK
    premium: float      # option price per share (positive)
    quantity: int       # +N long, -N short
    multiplier: float = 100.0


@dataclass
class StrategyResult:
    name: str
    legs: list[Leg]
    max_profit: float
    max_loss: float
    breakevens: list[float]
    pnl_at_expiry: pd.Series  # index = stock price, values = total P&L


def _leg_pnl(leg: Leg, prices: np.ndarray) -> np.ndarray:
    if leg.option_type == "STK":
        return (prices - leg.strike) * leg.quantity * leg.multiplier
    intrinsic = (np.maximum(prices - leg.strike, 0.0) if leg.option_type == "C"
                 else np.maximum(leg.strike - prices, 0.0))
    return leg.quantity * (intrinsic - leg.premium) * leg.multiplier


def simulate(
    name: str,
    legs: list[Leg],
    und_price: float,
    price_range_pct: float = 0.35,
    n_points: int = 300,
) -> StrategyResult:
    lo = und_price * (1.0 - price_range_pct)
    hi = und_price * (1.0 + price_range_pct)
    prices = np.linspace(lo, hi, n_points)
    total = sum(_leg_pnl(leg, prices) for leg in legs)

    series = pd.Series(total, index=prices)
    max_profit = float(total.max())
    max_loss = float(total.min())

    signs = np.sign(total)
    bes = []
    for i in range(len(signs) - 1):
        if signs[i] != signs[i + 1] and signs[i] != 0:
            span = abs(total[i]) + abs(total[i + 1])
            be = prices[i] + (prices[i + 1] - prices[i]) * abs(total[i]) / span if span else prices[i]
            bes.append(round(float(be), 2))

    return StrategyResult(
        name=name, legs=legs,
        max_profit=max_profit, max_loss=max_loss,
        breakevens=bes, pnl_at_expiry=series,
    )


def covered_call(und_price: float, strike: float, premium: float) -> StrategyResult:
    """Long 100 shares + short 1 call."""
    return simulate("Covered Call", [
        Leg("STK", und_price, 0.0, 100, 1.0),
        Leg("C", strike, premium, -1, 100.0),
    ], und_price)


def cash_secured_put(und_price: float, strike: float, premium: float) -> StrategyResult:
    """Short 1 put (cash-secured)."""
    return simulate("Cash-Secured Put", [
        Leg("P", strike, premium, -1, 100.0),
    ], und_price)


def bull_put_spread(und_price: float, k_long: float, k_short: float,
                    p_long: float, p_short: float) -> StrategyResult:
    """Short higher-strike put + long lower-strike put (credit spread)."""
    return simulate("Bull Put Spread", [
        Leg("P", k_short, p_short, -1, 100.0),
        Leg("P", k_long, p_long, +1, 100.0),
    ], und_price)


def iron_condor(und_price: float,
                k_put_long: float, k_put_short: float,
                k_call_short: float, k_call_long: float,
                p_put_long: float, p_put_short: float,
                p_call_short: float, p_call_long: float) -> StrategyResult:
    return simulate("Iron Condor", [
        Leg("P", k_put_long,   p_put_long,   +1, 100.0),
        Leg("P", k_put_short,  p_put_short,  -1, 100.0),
        Leg("C", k_call_short, p_call_short, -1, 100.0),
        Leg("C", k_call_long,  p_call_long,  +1, 100.0),
    ], und_price)
