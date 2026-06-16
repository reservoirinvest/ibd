"""Parse Zerodha Console exports into the normalized trade schema.

The Kite Connect API only returns the current day's orders/trades, so full history comes from
**Zerodha Console** downloads (Reports -> Tradebook / P&L, exported as CSV). This module is the
NSE analogue of ibd's ``flex/`` ingestion: it emits the same normalized columns the
broker-agnostic analysis/backtest modules expect:

    dateTime, accountId, symbol, underlyingSymbol, assetCategory (OPT/EQ), putCall (C/P),
    strike, expiry, quantity (signed), tradePrice, pnl, openCloseIndicator

Monthly F&O tradingsymbols (e.g. ``RELIANCE24JUN2900CE``) are parsed for underlying / expiry /
strike / right. Weekly index symbols use a compressed date code; those are best matched against
the live instruments dump when available.
"""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

from .util import save_pickle

TRADES_PKL = "trades.pkl"

_MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1)}

# NAME + YY + MON(3 letters) + STRIKE + CE/PE  (monthly contracts)
_OPT_MONTHLY = re.compile(r"^([A-Z&-]+?)(\d{2})([A-Z]{3})(\d+(?:\.\d+)?)(CE|PE)$")


def parse_tradingsymbol(ts: str) -> dict | None:
    """Best-effort parse of an NFO option tradingsymbol -> components, else None (equity)."""
    m = _OPT_MONTHLY.match(str(ts).upper().strip())
    if not m:
        return None
    name, yy, mon, strike, right = m.groups()
    if mon not in _MONTHS:
        return None
    year = 2000 + int(yy)
    month = _MONTHS[mon]
    # NSE monthly expiry = last Thursday; resolved exactly from the instruments dump when live.
    expiry = _last_weekday(year, month, weekday=3)
    return {
        "underlyingSymbol": name, "expiry": pd.Timestamp(expiry),
        "strike": float(strike), "putCall": "C" if right == "CE" else "P",
    }


def _last_weekday(year: int, month: int, weekday: int) -> pd.Timestamp:
    from calendar import monthrange

    last = pd.Timestamp(year, month, monthrange(year, month)[1])
    return last - pd.Timedelta(days=(last.weekday() - weekday) % 7)


def normalize_tradebook(df: pd.DataFrame, account_id: str = "NSE") -> pd.DataFrame:
    """Normalize a Console *tradebook* export.

    Recognises the common column names; pnl is left NaN (the tradebook has no P&L — merge the
    P&L report for that). openCloseIndicator is inferred per (symbol) buy/sell ordering.
    """
    df = df.rename(columns={c: c.strip().lower() for c in df.columns})
    sym_col = _first(df, ["symbol", "tradingsymbol", "instrument"])
    type_col = _first(df, ["trade_type", "transaction_type", "buy/sell"])
    qty_col = _first(df, ["quantity", "qty"])
    price_col = _first(df, ["price", "trade_price", "average_price"])
    date_col = _first(df, ["trade_date", "order_execution_time", "date", "time"])

    rows = []
    for _, r in df.iterrows():
        ts = str(r[sym_col])
        parsed = parse_tradingsymbol(ts)
        side = str(r[type_col]).strip().lower()
        sign = -1 if side.startswith("s") else 1
        qty = sign * abs(float(r[qty_col]))
        base = {
            "dateTime": pd.to_datetime(r[date_col], errors="coerce"),
            "accountId": account_id, "symbol": ts,
            "quantity": qty, "tradePrice": float(r[price_col]), "pnl": float("nan"),
        }
        if parsed:
            base.update({"assetCategory": "OPT", **parsed})
        else:
            base.update({"assetCategory": "EQ", "underlyingSymbol": ts,
                         "putCall": "", "strike": float("nan"), "expiry": pd.NaT})
        rows.append(base)

    out = pd.DataFrame(rows).sort_values("dateTime").reset_index(drop=True)
    return _infer_open_close(out)


def _infer_open_close(df: pd.DataFrame) -> pd.DataFrame:
    """Tag each row O/C by running net position per (symbol): toward-zero = close."""
    df = df.copy()
    df["openCloseIndicator"] = "O"
    net: dict[str, float] = {}
    for i, r in df.iterrows():
        sym = r["symbol"]
        prev = net.get(sym, 0.0)
        new = prev + r["quantity"]
        if prev != 0 and (abs(new) < abs(prev) or (prev > 0) != (new > 0)):
            df.at[i, "openCloseIndicator"] = "C"
        net[sym] = new
    return df


def normalize_pnl(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize a Console *P&L* export to [symbol, pnl] for merging realized P&L."""
    df = df.rename(columns={c: c.strip().lower() for c in df.columns})
    sym = _first(df, ["symbol", "tradingsymbol"])
    pnl = _first(df, ["realized p&l", "realized_pnl", "pnl", "net pnl"])
    return df[[sym, pnl]].rename(columns={sym: "symbol", pnl: "pnl"})


def _first(df: pd.DataFrame, candidates: list[str]) -> str:
    for c in candidates:
        if c in df.columns:
            return c
    raise KeyError(f"None of {candidates} found in columns {list(df.columns)}")


def update_trades(tradebook_csv: str | Path, account_id: str = "NSE") -> pd.DataFrame:
    """Parse a tradebook CSV, normalize, persist to data/master/trades.pkl, and return it."""
    raw = pd.read_csv(tradebook_csv)
    norm = normalize_tradebook(raw, account_id=account_id)
    save_pickle(norm, TRADES_PKL)
    return norm
