"""NSE F&O instrument master: lot sizes, settlement type, expiries, contract resolution.

This is the NSE-specific replacement for IBKR contract qualification. The Kite instruments
dump (CSV from ``kite.instruments("NFO")`` or https://api.kite.trade/instruments) carries
everything we need per contract, including the all-important ``lot_size``.

Dump columns (subset we rely on):
    instrument_token, tradingsymbol, name, expiry, strike, tick_size, lot_size,
    instrument_type (CE/PE/FUT), segment (NFO-OPT/NFO-FUT), exchange

Settlement rule
---------------
* Index underlyings (NIFTY, BANKNIFTY, ...) are **cash-settled** -> income-only legs.
* Every other F&O underlying is a single stock -> **physically settled** -> wheelable
  (assignment delivers ``lot_size`` shares).
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from .config import load_config
from .paths import MASTER_DIR

INSTRUMENTS_PATH = MASTER_DIR / "instruments_nfo.csv"

# Canonical right codes: Kite uses CE/PE; we map to C/P to match the ibd/normalized schema.
_RIGHT_MAP = {"CE": "C", "PE": "P"}


def index_symbols() -> set[str]:
    return {s.upper() for s in load_config().get("INDEX_SYMBOLS", [])}


def settlement_of(name: str) -> str:
    """'cash' for index underlyings (income-only), 'physical' for stocks (wheelable)."""
    return "cash" if str(name).upper() in index_symbols() else "physical"


def is_wheelable(name: str) -> bool:
    return settlement_of(name) == "physical"


def load_instruments(path: Path | None = None) -> pd.DataFrame:
    """Load and normalize the NFO instruments dump.

    Returns a DataFrame of *option* contracts only (CE/PE), with normalized dtypes and a
    derived ``right`` (C/P) and ``settlement`` column.
    """
    path = path or INSTRUMENTS_PATH
    df = pd.read_csv(path)

    # Options only.
    df = df[df["instrument_type"].isin(_RIGHT_MAP)].copy()

    df["name"] = df["name"].astype(str).str.upper()
    df["right"] = df["instrument_type"].map(_RIGHT_MAP)
    df["expiry"] = pd.to_datetime(df["expiry"], errors="coerce")
    for col in ("strike", "tick_size", "lot_size"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["lot_size"] = df["lot_size"].fillna(0).astype(int)
    df["settlement"] = df["name"].map(settlement_of)

    return df.dropna(subset=["expiry", "strike"]).reset_index(drop=True)


def lot_size_map(df: pd.DataFrame) -> dict[str, int]:
    """{underlying name -> lot_size}. Lot size is fixed per underlying within an expiry."""
    return (
        df.sort_values("expiry")
        .groupby("name")["lot_size"]
        .last()
        .astype(int)
        .to_dict()
    )


def universe(df: pd.DataFrame) -> pd.DataFrame:
    """One row per underlying: name, settlement, lot_size, is_weekly."""
    rows = []
    for name, grp in df.groupby("name"):
        rows.append(
            {
                "symbol": name,
                "settlement": grp["settlement"].iloc[0],
                "lot_size": int(grp.sort_values("expiry")["lot_size"].iloc[-1]),
                "is_weekly": _has_weekly(grp["expiry"]),
            }
        )
    return pd.DataFrame(rows).sort_values("symbol").reset_index(drop=True)


def _has_weekly(expiries: pd.Series) -> bool:
    """Weekly contracts present if any two consecutive unique expiries are < 20 days apart."""
    uniq = sorted(pd.to_datetime(expiries.dropna().unique()))
    if len(uniq) < 2:
        return False
    gaps = [(b - a).days for a, b in zip(uniq, uniq[1:])]
    return min(gaps) < 20


def expiries_for(df: pd.DataFrame, name: str) -> list[pd.Timestamp]:
    """Sorted unique future-or-equal expiries for an underlying."""
    sub = df[df["name"] == str(name).upper()]
    return sorted(pd.to_datetime(sub["expiry"].dropna().unique()))


def resolve(df: pd.DataFrame, name: str, expiry, strike: float, right: str) -> dict | None:
    """Resolve a single contract to its instrument_token / tradingsymbol / tick / lot.

    ``right`` accepts C/P or CE/PE. Returns ``None`` if no matching contract exists.
    """
    right_c = _RIGHT_MAP.get(right.upper(), right.upper())
    exp = pd.to_datetime(expiry)
    match = df[
        (df["name"] == str(name).upper())
        & (df["right"] == right_c)
        & (df["expiry"] == exp)
        & (df["strike"].round(2) == round(float(strike), 2))
    ]
    if match.empty:
        return None
    row = match.iloc[0]
    return {
        "instrument_token": int(row["instrument_token"]),
        "tradingsymbol": str(row["tradingsymbol"]),
        "tick_size": float(row["tick_size"]),
        "lot_size": int(row["lot_size"]),
        "settlement": str(row["settlement"]),
    }
