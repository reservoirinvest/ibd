"""Synthetic Kite-shaped data for OFFLINE development and tests.

Produces an instruments dump, quotes, positions and a margins blob that mimic the shape of
the real Kite Connect responses, so the whole build -> derive -> execute pipeline runs with
no credentials and no network. Lot sizes here are illustrative; the live system reads real
lot sizes straight from the Kite instruments dump.
"""

from __future__ import annotations

from calendar import monthrange
from datetime import date, timedelta

import pandas as pd

from ..greeks import bs_price

# (name, spot, lot_size, is_index, assumed_iv)
MOCK_UNDERLYINGS: list[tuple[str, float, int, bool, float]] = [
    ("RELIANCE", 2900.0, 250, False, 0.24),
    ("INFY", 1550.0, 400, False, 0.26),
    ("TCS", 3850.0, 175, False, 0.22),
    ("HDFCBANK", 1650.0, 550, False, 0.20),
    ("SBIN", 820.0, 750, False, 0.30),
    ("TATAMOTORS", 980.0, 700, False, 0.34),
    ("NIFTY", 24500.0, 75, True, 0.13),
    ("BANKNIFTY", 52000.0, 35, True, 0.15),
]

_NAV = 2_000_000.0  # mock net liquidation value (INR)


def _monthly_expiries(n: int = 3, weekday: int = 3) -> list[date]:
    """Last `weekday` (default Thursday) of the next `n` months, including current month."""
    today = date.today()
    out: list[date] = []
    y, m = today.year, today.month
    while len(out) < n:
        last_dom = monthrange(y, m)[1]
        d = date(y, m, last_dom)
        d -= timedelta(days=(d.weekday() - weekday) % 7)
        if d >= today:
            out.append(d)
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out


def _strike_step(spot: float, is_index: bool) -> float:
    if is_index:
        return 100.0 if spot > 30000 else 50.0
    if spot < 1000:
        return 10.0
    if spot < 2000:
        return 20.0
    return 50.0


def _strikes(spot: float, is_index: bool) -> list[float]:
    step = _strike_step(spot, is_index)
    atm = round(spot / step) * step
    n = 40 if is_index else 25  # wide enough that far-OTM income legs resolve
    return [atm + i * step for i in range(-n, n + 1)]


def build_instruments_df() -> pd.DataFrame:
    """Generate an NFO-options instruments dump (Kite-shaped)."""
    rows = []
    token = 100_000
    for name, spot, lot, is_index, _iv in MOCK_UNDERLYINGS:
        for exp in _monthly_expiries():
            estr = exp.strftime("%y%b").upper()  # e.g. 25JUN
            for k in _strikes(spot, is_index):
                for itype in ("CE", "PE"):
                    token += 1
                    rows.append(
                        {
                            "instrument_token": token,
                            "exchange_token": token % 100000,
                            "tradingsymbol": f"{name}{estr}{int(k)}{itype}",
                            "name": name,
                            "last_price": 0.0,
                            "expiry": exp.isoformat(),
                            "strike": k,
                            "tick_size": 0.05,
                            "lot_size": lot,
                            "instrument_type": itype,
                            "segment": "NFO-OPT",
                            "exchange": "NFO",
                        }
                    )
    return pd.DataFrame(rows)


def spot_map() -> dict[str, float]:
    return {name: spot for name, spot, _l, _i, _iv in MOCK_UNDERLYINGS}


def iv_map() -> dict[str, float]:
    return {name: iv for name, _s, _l, _i, iv in MOCK_UNDERLYINGS}


def build_quotes(instruments: pd.DataFrame) -> dict[str, dict]:
    """Mock quote() keyed by instrument_token (str). Option LTP via Black-Scholes."""
    spots, ivs = spot_map(), iv_map()
    today = date.today()
    quotes: dict[str, dict] = {}
    for _, r in instruments.iterrows():
        name = r["name"]
        S = spots.get(name, float(r["strike"]))
        iv = ivs.get(name, 0.25)
        exp = pd.to_datetime(r["expiry"]).date()
        T = max((exp - today).days, 0) / 365.0
        right = "C" if r["instrument_type"] == "CE" else "P"
        ltp = round(bs_price(S, float(r["strike"]), max(T, 1e-4), iv, right=right), 2)
        quotes[str(int(r["instrument_token"]))] = {
            "instrument_token": int(r["instrument_token"]),
            "last_price": ltp,
            "oi": 100000,
            "depth": {},
        }
    return quotes


def _pick(instruments: pd.DataFrame, name: str, itype: str, target: float) -> pd.Series:
    """Nearest existing contract to a target strike in the first (nearest) expiry."""
    exp = sorted(pd.to_datetime(instruments["expiry"].unique()))[0]
    sub = instruments[
        (instruments["name"] == name)
        & (instruments["instrument_type"] == itype)
        & (pd.to_datetime(instruments["expiry"]) == exp)
    ]
    return sub.iloc[(sub["strike"] - target).abs().argsort()].iloc[0]


def mock_positions() -> list[dict]:
    """Sample F&O + equity positions in Kite ``positions()['net']`` shape.

    Strikes are picked from the live mock instruments dump so tradingsymbols resolve exactly.
    Covers a sowed stock put (no stock), a covered call (stock + short call), and an index
    short (income-only, cash-settled).
    """
    df = build_instruments_df()
    spots = spot_map()
    lots = {name: lot for name, _s, lot, _i, _iv in MOCK_UNDERLYINGS}

    sbin_put = _pick(df, "SBIN", "PE", spots["SBIN"] * 0.97)
    infy_call = _pick(df, "INFY", "CE", spots["INFY"] * 1.03)
    nifty_call = _pick(df, "NIFTY", "CE", spots["NIFTY"] * 1.02)

    return [
        # Short put on SBIN, no stock -> 'sowed' (wheelable)
        {"tradingsymbol": sbin_put["tradingsymbol"], "exchange": "NFO", "product": "NRML",
         "quantity": -lots["SBIN"], "average_price": 14.0, "last_price": 9.0, "pnl": 3750.0,
         "instrument_token": int(sbin_put["instrument_token"])},
        # Long INFY stock (assigned) + short call -> covered (stock 'unprotected')
        {"tradingsymbol": "INFY", "exchange": "NSE", "product": "CNC",
         "quantity": lots["INFY"], "average_price": 1500.0, "last_price": spots["INFY"],
         "pnl": 20000.0, "instrument_token": 0},
        {"tradingsymbol": infy_call["tradingsymbol"], "exchange": "NFO", "product": "NRML",
         "quantity": -lots["INFY"], "average_price": 22.0, "last_price": 18.0, "pnl": 1600.0,
         "instrument_token": int(infy_call["instrument_token"])},
        # Short NIFTY call, no stock (cash-settled) -> income-only short
        {"tradingsymbol": nifty_call["tradingsymbol"], "exchange": "NFO", "product": "NRML",
         "quantity": -lots["NIFTY"], "average_price": 120.0, "last_price": 60.0, "pnl": 4500.0,
         "instrument_token": int(nifty_call["instrument_token"])},
    ]


def mock_orders() -> list[dict]:
    """No resting orders by default (Kite ``orders()`` shape)."""
    return []


def mock_margins() -> dict:
    """Kite ``margins()`` shape (equity segment)."""
    return {
        "equity": {
            "net": _NAV,
            "available": {"live_balance": _NAV * 0.4, "cash": _NAV * 0.4},
            "utilised": {"debits": _NAV * 0.6, "exposure": 0.0, "span": 0.0},
        }
    }
