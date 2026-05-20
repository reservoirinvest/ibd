"""Normalize and enrich IBKR Flex trade DataFrames.

IBKR Flex Activity query — field name mapping (portal label → XML attribute):
  - "Asset Class"        → assetCategory   ("OPT", "STK", "FUT", ...)
  - "Realized P&L"       → realizedPnl     (use this; identical to FIFO for options)
  - "Date/Time"          → dateTime        ("YYYYMMDD;HHMMSS" — strip ";")
  - "Trade Date"         → tradeDate       ("YYYYMMDD")
  - "Expiry"             → expiry          ("YYYYMMDD")
  - "Open/Close Indicator" → openCloseIndicator  ("O" open / "C" close)
  - "Put/Call"           → putCall         ("P" or "C")
  - "Underlying Symbol"  → underlyingSymbol
  - "IB Commission"      → ibCommission
  - "Trade Price"        → tradePrice
"""
from __future__ import annotations

import pandas as pd


_NUMERIC = [
    "strike", "quantity", "tradePrice", "tradeMoney",
    "proceeds", "ibCommission", "netCash",
    "realizedPnl", "fifoPnlRealized", "realizedPL",
    "multiplier", "closePrice",
]

# All known portal-label → XML-attribute variants for asset category
_CAT_COLS = ("assetCategory", "asset_class", "assetClass", "secType")

# All known P&L column name variants (priority order: Activity "Realized P&L" first)
_PNL_COLS = ("realizedPnl", "fifoPnlRealized", "realizedPL", "pnlRealized")


def normalize(df: pd.DataFrame) -> pd.DataFrame:
    """Return a normalized copy: typed dates, unified column aliases, pnl column."""
    if df.empty:
        return df
    df = df.copy()

    # dateTime: IBKR uses "YYYYMMDD;HHMMSS" — strip the semicolon
    if "dateTime" in df.columns:
        df["dateTime"] = pd.to_datetime(
            df["dateTime"].astype(str).str.replace(";", " ", regex=False),
            format="mixed",
            errors="coerce",
        )

    for col in ("tradeDate", "reportDate", "settleDateTarget"):
        if col in df.columns:
            df[col] = pd.to_datetime(df[col].astype(str), format="%Y%m%d", errors="coerce")

    if "expiry" in df.columns:
        df["expiry"] = pd.to_datetime(df["expiry"].astype(str), format="%Y%m%d", errors="coerce")

    for col in _NUMERIC:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # Unified realised P&L → "pnl" (Activity query uses "realizedPnl")
    for src in _PNL_COLS:
        if src in df.columns:
            df["pnl"] = df[src]
            break

    # Unified asset category → "assetCategory" regardless of portal label variant
    if "assetCategory" not in df.columns:
        for alt in _CAT_COLS[1:]:
            if alt in df.columns:
                df["assetCategory"] = df[alt]
                break

    # underlyingSymbol fallback for stock rows
    if "underlyingSymbol" not in df.columns:
        df["underlyingSymbol"] = df.get("symbol", pd.Series(dtype="object"))
    if "assetCategory" in df.columns:
        stk = df["assetCategory"] == "STK"
        df.loc[stk, "underlyingSymbol"] = df.loc[stk, "symbol"]

    return df.sort_values("dateTime", na_position="last").reset_index(drop=True)


def filter_options(df: pd.DataFrame) -> pd.DataFrame:
    cat = next((c for c in _CAT_COLS if c in df.columns), None)
    if cat is None:
        return df.iloc[0:0]
    return df[df[cat] == "OPT"].copy()


def filter_closed(df: pd.DataFrame) -> pd.DataFrame:
    if "openCloseIndicator" not in df.columns:
        return df.iloc[0:0]
    return df[df["openCloseIndicator"] == "C"].copy()


def normalize_cash(df: pd.DataFrame) -> pd.DataFrame:
    """Return a normalized copy of a CashTransaction DataFrame.

    Produces columns: date, amount, currency, type, description, accountId.
    Handles both 'type' (newer exports) and 'trnsType' (some older formats).
    """
    if df.empty:
        return df
    df = df.copy()

    # dateTime: same YYYYMMDD;HHMMSS format as trades — strip the semicolon
    for _col in ("dateTime", "date", "reportDate", "settleDate"):
        if _col in df.columns:
            df["date"] = (
                pd.to_datetime(
                    df[_col].astype(str).str.replace(";", " ", regex=False),
                    format="mixed",
                    errors="coerce",
                ).dt.normalize()
            )
            break

    if "amount" in df.columns:
        df["amount"] = pd.to_numeric(df["amount"], errors="coerce")

    if "type" not in df.columns and "trnsType" in df.columns:
        df["type"] = df["trnsType"]

    return df.sort_values("date", na_position="last").reset_index(drop=True)


def normalize_nav(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize EquitySummaryByReportDateInBase DataFrame.

    Parses reportDate (YYYYMMDD → Timestamp), converts total to float,
    and drops zero/NaN rows (per-account placeholder rows in multi-account exports).
    """
    if df.empty:
        return df
    df = df.copy()
    if "reportDate" in df.columns:
        df["reportDate"] = pd.to_datetime(
            df["reportDate"].astype(str), format="%Y%m%d", errors="coerce"
        )
    if "total" in df.columns:
        df["total"] = pd.to_numeric(df["total"], errors="coerce")
    return df[df["total"].notna() & (df["total"] != 0)].reset_index(drop=True)


def mask_accounts(df: pd.DataFrame, account_map: dict[str, str]) -> pd.DataFrame:
    """Replace raw IBKR account IDs with labels (e.g. 'US', 'SG') for privacy.

    account_map: {raw_account_number: label}  e.g. {"U12345678": "US", "U87654321": "SG"}
    Unrecognised account IDs are left unchanged.
    """
    if "accountId" not in df.columns or not account_map:
        return df
    df = df.copy()
    df["accountId"] = df["accountId"].map(
        lambda x: account_map.get(str(x).strip(), str(x))
    )
    return df
