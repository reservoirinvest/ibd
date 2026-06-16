"""OHLC history for NSE underlyings.

Primary source is yfinance (``.NS`` for stocks, ``^NSEI``/``^NSEBANK`` for the common
indices). A Kite ``historical_data`` fallback is stubbed for when running live with the
historical-data add-on. Storage is a broker-agnostic ``dict[symbol -> OHLCV DataFrame]``.
"""

from __future__ import annotations

import pandas as pd
from loguru import logger

from .util import load_pickle, save_pickle

OHLC_PKL = "ohlc.pkl"

# yfinance ticker overrides for index underlyings (others default to NAME.NS).
_YF_INDEX = {
    "NIFTY": "^NSEI",
    "BANKNIFTY": "^NSEBANK",
    "FINNIFTY": "NIFTY_FIN_SERVICE.NS",
    "MIDCPNIFTY": "^NSEMDCP50",
    "SENSEX": "^BSESN",
}


def yf_ticker(symbol: str) -> str:
    s = symbol.upper()
    return _YF_INDEX.get(s, f"{s}.NS")


def load_ohlc() -> dict[str, pd.DataFrame]:
    return load_pickle(OHLC_PKL, default={}) or {}


def fetch_ohlc(symbols: list[str], period: str = "2y") -> dict[str, pd.DataFrame]:
    """Fetch daily OHLCV via yfinance. Merges into the existing store and saves."""
    import yfinance as yf  # lazy: keeps import-time light / offline-safe

    store = load_ohlc()
    for sym in symbols:
        tkr = yf_ticker(sym)
        try:
            df = yf.Ticker(tkr).history(period=period, auto_adjust=True)
        except Exception as exc:  # network-restricted sandboxes land here
            logger.warning("yfinance failed for {} ({}): {}", sym, tkr, exc)
            continue
        if df is None or df.empty:
            logger.warning("No OHLC for {} ({})", sym, tkr)
            continue
        df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
        df.index = pd.to_datetime(df.index).tz_localize(None)
        store[sym] = df
    save_pickle(store, OHLC_PKL)
    return store


def hv(symbol: str, store: dict[str, pd.DataFrame] | None = None, window: int = 20) -> float:
    """Annualised historical volatility from the last `window` daily log-returns."""
    import numpy as np

    store = store if store is not None else load_ohlc()
    df = store.get(symbol)
    if df is None or len(df) < window + 1:
        return float("nan")
    logret = np.log(df["Close"] / df["Close"].shift(1)).dropna()
    return float(logret.tail(window).std() * np.sqrt(252))
