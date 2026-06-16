"""Build the option-chain and underlying-snapshot tables for the wheel pipeline.

Outputs (saved to data/master/):
* ``df_chains.pkl`` — one row per tradable option contract: symbol, expiry, strike, right,
  dte, lot_size, tick_size, instrument_token, tradingsymbol, settlement, ltp.
* ``df_unds.pkl`` — one row per underlying: symbol, price (spot), iv, hv, sdev, lot_size,
  settlement, margin_per_lot.

Schema deliberately mirrors ibd's ``build.py`` so that ``classify`` / ``derive`` stay close
to their IBKR counterparts. The NSE-specific value-add is lot_size, settlement, tick_size and
instrument_token threaded through every row.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
from loguru import logger

from . import instruments as instr
from . import ohlc as ohlc_mod
from .broker import KiteClient, get_client
from .config import load_config
from .greeks import implied_vol
from .paths import ensure_dirs
from .util import dte, save_pickle


def build_chains(df_instr: pd.DataFrame, quotes: dict, max_dte: int) -> pd.DataFrame:
    """Expand the instruments dump into a chain table with live LTP and DTE filter."""
    df = df_instr.copy()
    df["dte"] = dte(df["expiry"])
    df = df[(df["dte"] >= 0) & (df["dte"] <= max_dte)].copy()

    df["ltp"] = df["instrument_token"].map(
        lambda t: float(quotes.get(str(int(t)), {}).get("last_price", 0.0))
    )
    cols = ["name", "expiry", "strike", "right", "dte", "lot_size", "tick_size",
            "instrument_token", "tradingsymbol", "settlement", "ltp"]
    out = df[cols].rename(columns={"name": "symbol"})
    return out.sort_values(["symbol", "expiry", "strike"]).reset_index(drop=True)


def build_unds(df_chains: pd.DataFrame, spots: dict[str, float],
               ohlc_store: dict) -> pd.DataFrame:
    """One row per underlying with spot, ATM IV (solved from LTP), HV and σ."""
    rows = []
    for sym, grp in df_chains.groupby("symbol"):
        S = float(spots.get(sym, float("nan")))
        lot = int(grp["lot_size"].iloc[0])
        settlement = grp["settlement"].iloc[0]
        iv = _atm_iv(grp, S)
        h = ohlc_mod.hv(sym, ohlc_store)
        # σ over the nearest expiry horizon, used by derive for strike offsets.
        near_dte = float(grp["dte"].replace(0, np.nan).dropna().min() or 7)
        vol = iv if not math.isnan(iv) and iv > 0 else (h if not math.isnan(h) else 0.0)
        sdev = S * vol * math.sqrt(max(near_dte, 1) / 365.0) if S and vol else float("nan")
        rows.append({
            "symbol": sym, "price": S, "iv": iv, "hv": h, "sdev": sdev,
            "lot_size": lot, "settlement": settlement,
        })
    df = pd.DataFrame(rows)
    return df.sort_values("symbol").reset_index(drop=True)


def _atm_iv(grp: pd.DataFrame, S: float) -> float:
    """Solve IV from the ATM call/put LTPs and average."""
    if not S or math.isnan(S) or grp.empty:
        return float("nan")
    near = grp[grp["expiry"] == grp["expiry"].min()]
    atm_strike = near.iloc[(near["strike"] - S).abs().argsort()].iloc[0]["strike"]
    ivs = []
    for _, r in near[near["strike"] == atm_strike].iterrows():
        T = max(float(r["dte"]), 1) / 365.0
        iv = implied_vol(float(r["ltp"]), S, float(r["strike"]), T, right=r["right"])
        if iv > 0:
            ivs.append(iv)
    return float(np.mean(ivs)) if ivs else float("nan")


def add_margins(df_unds: pd.DataFrame, client: KiteClient) -> pd.DataFrame:
    """Attach an estimated per-lot SPAN+exposure margin for one short ATM option."""
    df = df_unds.copy()
    margins = []
    for _, r in df.iterrows():
        notional = float(r["price"]) * int(r["lot_size"])
        est = client.order_margins([{"quantity": int(r["lot_size"]),
                                     "price": float(r["price"]),
                                     "tradingsymbol": r["symbol"]}])
        margins.append(float(est[0].get("total", notional * 0.15)) if est else notional * 0.15)
    df["margin_per_lot"] = margins
    return df


def run(client: KiteClient | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Full build: instruments -> chains + underlyings. Returns (df_chains, df_unds)."""
    cfg = load_config()
    client = client or get_client()
    max_dte = int(cfg.get("MAX_DTE", 50))

    # Persist the raw instruments dump once, then normalize through a single path.
    if not instr.INSTRUMENTS_PATH.exists():
        ensure_dirs()
        client.instruments(cfg.get("EXCHANGE", "NFO")).to_csv(instr.INSTRUMENTS_PATH, index=False)
    df_instr = instr.load_instruments()
    names = sorted(df_instr["name"].unique())

    tokens = df_instr["instrument_token"].astype(int).tolist()
    quotes = client.quote(tokens)
    spots = client.spots(names)
    ohlc_store = ohlc_mod.load_ohlc()

    df_chains = build_chains(df_instr, quotes, max_dte)
    df_unds = add_margins(build_unds(df_chains, spots, ohlc_store), client)

    save_pickle(df_chains, "df_chains.pkl")
    save_pickle(df_unds, "df_unds.pkl")
    logger.info("Built {} chain rows across {} underlyings.", len(df_chains), len(df_unds))
    return df_chains, df_unds


if __name__ == "__main__":
    run()
