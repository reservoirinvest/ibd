"""Black-Scholes option pricing and Greeks — pure Python/scipy, no external API."""
from __future__ import annotations

import math

import pandas as pd
from scipy.stats import norm


def black_scholes(
    S: float,
    K: float,
    T: float,       # years to expiry
    r: float,       # risk-free rate, annualised decimal
    sigma: float,   # implied vol, annualised decimal
    option_type: str = "C",
) -> dict[str, float]:
    """European Black-Scholes price + Delta, Gamma, Theta, Vega, Rho.

    Theta is per-calendar-day (divide by 365).
    Vega and Rho are per 1% move in vol/rates.
    """
    if T <= 0 or sigma <= 0:
        intrinsic = max(S - K, 0.0) if option_type == "C" else max(K - S, 0.0)
        return {"price": intrinsic, "delta": 1.0 if (option_type == "C" and S > K) else 0.0,
                "gamma": 0.0, "theta": 0.0, "vega": 0.0, "rho": 0.0}

    sqT = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * sqT)
    d2 = d1 - sigma * sqT
    pdf1 = norm.pdf(d1)

    if option_type == "C":
        price = S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)
        delta = norm.cdf(d1)
        rho = K * T * math.exp(-r * T) * norm.cdf(d2) / 100.0
    else:
        price = K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)
        delta = norm.cdf(d1) - 1.0
        rho = -K * T * math.exp(-r * T) * norm.cdf(-d2) / 100.0

    gamma = pdf1 / (S * sigma * sqT)
    theta = (-(S * pdf1 * sigma) / (2.0 * sqT)
             - r * K * math.exp(-r * T) * (norm.cdf(d2) if option_type == "C" else norm.cdf(-d2))
             ) / 365.0
    vega = S * pdf1 * sqT / 100.0

    return {"price": round(price, 4), "delta": round(delta, 4),
            "gamma": round(gamma, 6), "theta": round(theta, 4),
            "vega": round(vega, 4), "rho": round(rho, 4)}


def implied_vol(
    market_price: float,
    S: float,
    K: float,
    T: float,
    r: float,
    option_type: str = "C",
    tol: float = 1e-5,
    max_iter: int = 150,
) -> float:
    """Implied volatility via bisection. Returns NaN if convergence fails."""
    lo, hi = 1e-4, 10.0
    for _ in range(max_iter):
        mid = (lo + hi) / 2.0
        p = black_scholes(S, K, T, r, mid, option_type)["price"]
        if abs(p - market_price) < tol:
            return round(mid, 6)
        if p < market_price:
            lo = mid
        else:
            hi = mid
    return float("nan")


def greeks_table(
    chains: pd.DataFrame,
    und_price: float,
    r: float = 0.053,
    multiplier: float = 100.0,
) -> pd.DataFrame:
    """Add Black-Scholes columns to an options chain DataFrame.

    Expects columns: strike (float), dte (int days), right ("C"/"P").
    Optional: iv (annualised decimal); defaults to 0.25.
    Adds: bs_price, delta, gamma, theta, theta_$, vega, vega_$, rho.
    """
    df = chains.copy()
    rows = []
    for _, row in df.iterrows():
        T = max(float(row["dte"]) / 365.0, 1e-6)
        iv = float(row["iv"]) if "iv" in row and row["iv"] else 0.25
        g = black_scholes(und_price, float(row["strike"]), T, r, iv, str(row.get("right", "C")))
        rows.append(g)
    gdf = pd.DataFrame(rows)
    gdf.rename(columns={"price": "bs_price"}, inplace=True)
    gdf["theta_$"] = gdf["theta"] * multiplier
    gdf["vega_$"] = gdf["vega"] * multiplier
    return pd.concat([df.reset_index(drop=True), gdf], axis=1)
