"""Position, open-order and underlying state classification.

Ports the ibd ``src/classify.py`` state machine to Kite data, adding **settlement awareness**:
cash-settled (index) short options can never be assigned, so they are labelled ``income_short``
rather than ``sowed`` and never drive a cover/assignment path.

Portfolio states (physical underlyings)
    sowed       short option, no stock for symbol            -> wheelable, reap target
    covering    short option, stock held
    protecting  long option, stock held
    orphaned    long option, no stock
    zen         stock with both a covering and protecting option
    unprotected stock with covering but no protecting
    uncovered   stock with protecting but no covering
    exposed     stock with neither
Cash underlyings
    income_short  short option, no stock (index) -> reap target, no assignment
"""

from __future__ import annotations

import pandas as pd

from . import instruments as instr


# ── parsing Kite positions/orders into a normalized frame ──────────────────────
def _resolver(df_instr: pd.DataFrame | None) -> dict[str, dict]:
    if df_instr is None:
        df_instr = instr.load_instruments() if instr.INSTRUMENTS_PATH.exists() else pd.DataFrame()
    if df_instr.empty:
        return {}
    return {
        str(r["tradingsymbol"]): {
            "symbol": r["name"], "right": r["right"], "strike": float(r["strike"]),
            "expiry": r["expiry"], "lot_size": int(r["lot_size"]),
            "settlement": r["settlement"],
        }
        for _, r in df_instr.iterrows()
    }


def parse_positions(positions: dict, df_instr: pd.DataFrame | None = None) -> pd.DataFrame:
    """Kite ``positions()`` -> df_pf with columns:
    symbol, secType (OPT/STK), right, strike, expiry, position, avgCost, mktPrice,
    lot_size, settlement.
    """
    resolver = _resolver(df_instr)
    rows = []
    for p in positions.get("net", []):
        if int(p.get("quantity", 0)) == 0:
            continue
        ts, exch = str(p["tradingsymbol"]), p.get("exchange", "")
        if exch == "NFO" and ts in resolver:
            r = resolver[ts]
            rows.append({
                "symbol": r["symbol"], "secType": "OPT", "right": r["right"],
                "strike": r["strike"], "expiry": r["expiry"],
                "position": int(p["quantity"]), "avgCost": float(p["average_price"]),
                "mktPrice": float(p.get("last_price", 0.0)),
                "lot_size": r["lot_size"], "settlement": r["settlement"],
            })
        else:  # equity / cash-segment stock
            rows.append({
                "symbol": ts, "secType": "STK", "right": "", "strike": float("nan"),
                "expiry": pd.NaT, "position": int(p["quantity"]),
                "avgCost": float(p["average_price"]), "mktPrice": float(p.get("last_price", 0.0)),
                "lot_size": 0, "settlement": instr.settlement_of(ts),
            })
    return pd.DataFrame(rows)


def parse_orders(orders: list[dict], df_instr: pd.DataFrame | None = None) -> pd.DataFrame:
    """Kite ``orders()`` -> df_openords with: symbol, secType, right, strike, action, qty, status."""
    resolver = _resolver(df_instr)
    active = {"OPEN", "TRIGGER PENDING", "AMO REQ RECEIVED", "PUT ORDER REQ RECEIVED"}
    rows = []
    for o in orders:
        if str(o.get("status", "")).upper() not in active:
            continue
        ts = str(o["tradingsymbol"])
        r = resolver.get(ts, {})
        rows.append({
            "symbol": r.get("symbol", ts),
            "secType": "OPT" if ts in resolver else "STK",
            "right": r.get("right", ""), "strike": r.get("strike", float("nan")),
            "action": str(o["transaction_type"]).upper(), "qty": int(o["quantity"]),
            "status": o["status"],
        })
    return pd.DataFrame(rows)


# ── state machines ─────────────────────────────────────────────────────────────
def classify_pf(pf: pd.DataFrame) -> pd.DataFrame:
    """Assign one ``state`` per portfolio row (rules applied in priority order)."""
    if pf.empty:
        return pf.assign(state=pd.Series(dtype=str))
    df = pf.copy()
    df["state"] = "unclassified"

    stk = df[df.secType == "STK"]
    has_stk = set(stk.symbol)
    short_call = set(df[(df.secType == "OPT") & (df.right == "C") & (df.position < 0)].symbol)
    short_put = set(df[(df.secType == "OPT") & (df.right == "P") & (df.position < 0)].symbol)
    long_opt = set(df[(df.secType == "OPT") & (df.position > 0)].symbol)
    short_opt_syms = short_call | short_put

    is_opt = df.secType == "OPT"
    # sowed: short option, no stock
    df.loc[is_opt & (df.position < 0) & (~df.symbol.isin(has_stk)), "state"] = "sowed"
    # covering: short option, stock held
    df.loc[is_opt & (df.position < 0) & (df.symbol.isin(has_stk)), "state"] = "covering"
    # protecting / orphaned: long option
    df.loc[is_opt & (df.position > 0) & (df.symbol.isin(has_stk)), "state"] = "protecting"
    df.loc[is_opt & (df.position > 0) & (~df.symbol.isin(has_stk)), "state"] = "orphaned"

    # stock states
    for sym in has_stk:
        cov = sym in short_opt_syms
        prot = sym in long_opt
        if cov and prot:
            s = "zen"
        elif cov:
            s = "unprotected"
        elif prot:
            s = "uncovered"
        else:
            s = "exposed"
        df.loc[(df.secType == "STK") & (df.symbol == sym), "state"] = s

    # settlement override: cash-settled shorts can't be assigned -> income-only.
    df.loc[(df.state == "sowed") & (df.settlement == "cash"), "state"] = "income_short"
    return df


def classify_open_orders(df_openords: pd.DataFrame, pf: pd.DataFrame) -> pd.DataFrame:
    """Assign one ``state`` per open order based on portfolio context."""
    if df_openords.empty:
        return df_openords.assign(state=pd.Series(dtype=str))
    df = df_openords.copy()
    has_stk = set(pf[pf.secType == "STK"].symbol) if not pf.empty else set()
    short_opts = set(pf[(pf.secType == "OPT") & (pf.position < 0)].symbol) if not pf.empty else set()
    long_opts = set(pf[(pf.secType == "OPT") & (pf.position > 0)].symbol) if not pf.empty else set()

    df["state"] = "unclassified"
    sell = df.action == "SELL"
    buy = df.action == "BUY"
    opt = df.secType == "OPT"
    df.loc[opt & sell & df.symbol.isin(has_stk), "state"] = "covering"
    df.loc[opt & sell & ~df.symbol.isin(has_stk), "state"] = "sowing"
    df.loc[opt & buy & df.symbol.isin(has_stk), "state"] = "protecting"
    df.loc[opt & buy & df.symbol.isin(short_opts), "state"] = "reaping"
    df.loc[opt & sell & df.symbol.isin(long_opts), "state"] = "de-orphaning"
    return df


def update_unds_status(df_unds: pd.DataFrame, df_pf: pd.DataFrame,
                       df_openords: pd.DataFrame) -> pd.DataFrame:
    """Assign a wheel ``state`` per underlying for the derive loop."""
    df = df_unds.copy()
    df["state"] = "virgin"

    if not df_pf.empty:
        stk_state = df_pf[df_pf.secType == "STK"].set_index("symbol")["state"].to_dict()
        for sym, st in stk_state.items():
            df.loc[df.symbol == sym, "state"] = st

        sowed = set(df_pf[df_pf.state.isin(["sowed", "income_short"])].symbol)
        reaping = set(df_openords[df_openords.state == "reaping"].symbol) \
            if not df_openords.empty else set()
        for sym in sowed:
            if sym not in reaping:
                df.loc[df.symbol == sym, "state"] = "unreaped"
            else:
                df.loc[df.symbol == sym, "state"] = "zen"
    return df


def classified_results(client=None, df_instr: pd.DataFrame | None = None) -> dict:
    """Orchestrator: pull live data and return classified df_pf / df_openords / df_unds."""
    from .broker import get_client
    from .util import load_pickle

    client = client or get_client()
    if df_instr is None and instr.INSTRUMENTS_PATH.exists():
        df_instr = instr.load_instruments()

    df_pf = classify_pf(parse_positions(client.positions(), df_instr))
    df_openords = classify_open_orders(parse_orders(client.orders(), df_instr), df_pf)

    df_unds = load_pickle("df_unds.pkl")
    if df_unds is not None:
        df_unds = update_unds_status(df_unds, df_pf, df_openords)
    return {"df_pf": df_pf, "df_openords": df_openords, "df_unds": df_unds}
