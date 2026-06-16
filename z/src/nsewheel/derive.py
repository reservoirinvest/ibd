"""Generate wheel orders (cover / sow / reap) — lot-aware and settlement-aware.

Ports ibd's ``derive.py`` strike-selection and pricing logic to NSE. The decisive change is
**sizing in lots**: instead of ``qty = funds / margin`` we compute
``lots = floor(fund_budget / margin_per_lot)`` and ``qty = lots * lot_size``, so every order
quantity is a valid NSE multiple. Index (cash-settled) underlyings get income-only sell legs
with no cover/assignment path.

Outputs (data/master/): ``df_cov.pkl``, ``df_nkd.pkl``, ``df_reap.pkl``. Each row carries
symbol, expiry, strike, right, tradingsymbol, instrument_token, lot_size, lots, qty, action,
price (theoretical), xPrice (limit), settlement.
"""

from __future__ import annotations

import math

import pandas as pd
from loguru import logger

from .broker import KiteClient, get_client
from .classify import classified_results
from .config import load_config
from .formatting import round_to_tick
from .util import load_pickle, save_pickle

_ORDER_COLS = ["symbol", "expiry", "strike", "right", "tradingsymbol", "instrument_token",
               "lot_size", "lots", "qty", "action", "price", "xPrice", "settlement"]


# ── helpers ────────────────────────────────────────────────────────────────────
def _nearest_expiry(chain: pd.DataFrame, target_dte: int) -> pd.Timestamp | None:
    if chain.empty:
        return None
    exps = chain[["expiry", "dte"]].drop_duplicates()
    exps = exps[exps["dte"] >= max(target_dte - 3, 0)]
    if exps.empty:
        exps = chain[["expiry", "dte"]].drop_duplicates()
    exps = exps.assign(diff=(exps["dte"] - target_dte).abs()).sort_values("diff")
    return exps.iloc[0]["expiry"]


def _pick_strike(chain: pd.DataFrame, right: str, threshold: float, side: str) -> pd.Series | None:
    """Pick the closest OTM strike beyond ``threshold`` (side='above' for calls, 'below' puts)."""
    c = chain[chain["right"] == right]
    c = c[c["strike"] > threshold] if side == "above" else c[c["strike"] < threshold]
    if c.empty:
        return None
    c = c.assign(diff=(c["strike"] - threshold).abs()).sort_values("diff")
    return c.iloc[0]


def _row(contract: pd.Series, lots: int, lot_size: int, action: str, xprice: float) -> dict:
    return {
        "symbol": contract["symbol"], "expiry": contract["expiry"],
        "strike": float(contract["strike"]), "right": contract["right"],
        "tradingsymbol": contract["tradingsymbol"],
        "instrument_token": int(contract["instrument_token"]),
        "lot_size": lot_size, "lots": lots, "qty": lots * lot_size, "action": action,
        "price": float(contract["ltp"]), "xPrice": xprice, "settlement": contract["settlement"],
    }


# ── cover ──────────────────────────────────────────────────────────────────────
def derive_cover(df_pf, df_chains, df_unds, cfg) -> pd.DataFrame:
    if not cfg.get("COVER_ME", True) or df_pf.empty:
        return pd.DataFrame(columns=_ORDER_COLS)
    mult = float(cfg["COVER_STD_MULT"])
    xpmult = float(cfg["COVXPMULT"])
    min_dte = int(cfg["COVER_MIN_DTE"])
    unds = df_unds.set_index("symbol")
    rows = []
    cands = df_pf[(df_pf.secType == "STK") & (df_pf.state.isin(["exposed", "uncovered"]))]
    for _, stk in cands.iterrows():
        sym = stk["symbol"]
        if sym not in unds.index:
            continue
        u = unds.loc[sym]
        if u["settlement"] != "physical":
            continue
        long = stk["position"] > 0
        right = "C" if long else "P"
        side = "above" if long else "below"
        sdev = float(u["sdev"]) if not math.isnan(u["sdev"]) else 0.0
        cov_price = max(float(stk["avgCost"]), float(u["price"]) + (1 if long else -1) * mult * sdev)
        chain = df_chains[df_chains.symbol == sym]
        exp = _nearest_expiry(chain, min_dte)
        if exp is None:
            continue
        contract = _pick_strike(chain[chain.expiry == exp], right, cov_price, side)
        if contract is None:
            continue
        lot_size = int(contract["lot_size"]) or 1
        lots = max(1, int(abs(stk["position"]) // lot_size))
        xprice = round_to_tick(max(float(contract["ltp"]) * xpmult, contract["tick_size"]),
                               float(contract["tick_size"]))
        rows.append(_row(contract, lots, lot_size, "SELL", xprice))
    return pd.DataFrame(rows, columns=_ORDER_COLS)


# ── sow ────────────────────────────────────────────────────────────────────────
def derive_sow(df_unds, df_chains, cfg, nav: float) -> pd.DataFrame:
    if not cfg.get("SOW_NAKEDS", True):
        return pd.DataFrame(columns=_ORDER_COLS)
    put_mult = float(cfg["VIRGIN_PUT_STD_MULT"])
    call_mult = float(cfg["VIRGIN_CALL_STD_MULT"])
    xpmult = float(cfg["NAKEDXPMULT"])
    min_price = float(cfg["MINNAKEDOPTPRICE"])
    target_dte = int(cfg["VIRGIN_DTE"])
    fund_pct = float(cfg["FUND_PER_SYMBOL_PCT"])
    index_wheel = bool(cfg.get("INDEX_WHEEL", False))
    fund_budget = fund_pct * nav

    rows = []
    for _, u in df_unds.iterrows():
        if u["state"] != "virgin":
            continue
        sym = u["symbol"]
        chain = df_chains[df_chains.symbol == sym]
        exp = _nearest_expiry(chain, target_dte)
        if exp is None or math.isnan(u["sdev"]):
            continue
        legchain = chain[chain.expiry == exp]
        margin_per_lot = float(u["margin_per_lot"]) or (float(u["price"]) * int(u["lot_size"]) * 0.15)
        lots = max(1, int(fund_budget // margin_per_lot)) if margin_per_lot > 0 else 1

        if u["settlement"] == "physical":
            # cash-secured put only (wheel entry)
            legs = [("P", u["price"] - put_mult * u["sdev"], "below")]
        else:
            # index income: sell strangle unless INDEX_WHEEL forces single leg
            legs = [("P", u["price"] - put_mult * u["sdev"], "below"),
                    ("C", u["price"] + call_mult * u["sdev"], "above")]
            if index_wheel:
                legs = legs[:1]

        for right, threshold, side in legs:
            contract = _pick_strike(legchain, right, threshold, side)
            if contract is None:
                continue
            lot_size = int(contract["lot_size"]) or 1
            xprice = round_to_tick(max(float(contract["ltp"]) * xpmult, min_price),
                                   float(contract["tick_size"]))
            rows.append(_row(contract, lots, lot_size, "SELL", xprice))
    return pd.DataFrame(rows, columns=_ORDER_COLS)


# ── reap ───────────────────────────────────────────────────────────────────────
def derive_reap(df_pf, df_chains, cfg) -> pd.DataFrame:
    if not cfg.get("REAP_ME", True) or df_pf.empty:
        return pd.DataFrame(columns=_ORDER_COLS)
    ratio = float(cfg["REAPRATIO"])
    chain_idx = df_chains.set_index(["symbol", "right", "strike", "expiry"]) \
        if not df_chains.empty else None
    rows = []
    shorts = df_pf[(df_pf.secType == "OPT") & (df_pf.state.isin(["sowed", "income_short"]))]
    for _, p in shorts.iterrows():
        lot_size = int(p["lot_size"]) or 1
        lots = max(1, int(abs(p["position"]) // lot_size))
        # buy-to-close at the lower of market price or REAPRATIO * entry credit
        target = min(float(p["mktPrice"]), abs(float(p["avgCost"])) * ratio)
        contract = _lookup_contract(chain_idx, p)
        tick = float(contract["tick_size"]) if contract is not None else 0.05
        ts = contract["tradingsymbol"] if contract is not None else ""
        token = int(contract["instrument_token"]) if contract is not None else 0
        rows.append({
            "symbol": p["symbol"], "expiry": p["expiry"], "strike": float(p["strike"]),
            "right": p["right"], "tradingsymbol": ts, "instrument_token": token,
            "lot_size": lot_size, "lots": lots, "qty": lots * lot_size, "action": "BUY",
            "price": float(p["mktPrice"]), "xPrice": round_to_tick(max(target, tick), tick),
            "settlement": p["settlement"],
        })
    return pd.DataFrame(rows, columns=_ORDER_COLS)


def _lookup_contract(chain_idx, p) -> pd.Series | None:
    if chain_idx is None:
        return None
    key = (p["symbol"], p["right"], float(p["strike"]), p["expiry"])
    try:
        hit = chain_idx.loc[key]
        return hit.iloc[0] if isinstance(hit, pd.DataFrame) else hit
    except (KeyError, TypeError):
        return None


# ── orchestration ──────────────────────────────────────────────────────────────
def run(client: KiteClient | None = None) -> dict[str, pd.DataFrame]:
    cfg = load_config()
    client = client or get_client()
    df_chains = load_pickle("df_chains.pkl")
    if df_chains is None:
        raise RuntimeError("df_chains.pkl missing — run build first.")

    res = classified_results(client)
    df_pf, df_unds, df_openords = res["df_pf"], res["df_unds"], res["df_openords"]
    if df_unds is None:
        df_unds = load_pickle("df_unds.pkl")
        from .classify import update_unds_status
        df_unds = update_unds_status(df_unds, df_pf, df_openords)

    # open-order guard: skip symbols already having a matching resting order
    if not df_openords.empty:
        guard_sow = set(df_openords[df_openords.state == "sowing"].symbol)
        guard_cov = set(df_openords[df_openords.state == "covering"].symbol)
    else:
        guard_sow = guard_cov = set()

    nav = client.nav()
    df_cov = derive_cover(df_pf[~df_pf.symbol.isin(guard_cov)], df_chains, df_unds, cfg)
    df_nkd = derive_sow(df_unds[~df_unds.symbol.isin(guard_sow)], df_chains, cfg, nav)
    df_reap = derive_reap(df_pf, df_chains, cfg)

    save_pickle(df_cov, "df_cov.pkl")
    save_pickle(df_nkd, "df_nkd.pkl")
    save_pickle(df_reap, "df_reap.pkl")
    logger.info("Derived: {} cover, {} sow, {} reap orders.",
                len(df_cov), len(df_nkd), len(df_reap))
    return {"cover": df_cov, "sow": df_nkd, "reap": df_reap}


if __name__ == "__main__":
    run()
