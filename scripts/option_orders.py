"""
option_orders.py — generate specific order parameters for screener results.

Max worst-case loss per symbol: $5,000 (2% of $250k portfolio).

Strategy → Order mapping
  EARNINGS STRANGLE SELL   → Iron Condor (4 legs, defined risk)
  SHORT STRANGLE           → Iron Condor (4 legs, defined risk)
  CASH-SECURED PUT         → Sell Put (single leg; managed at 2x credit stop)
  COVERED CALL / BEAR CALL → Bear Call Spread (2 legs, defined risk)
  IRON CONDOR              → Iron Condor (4 legs)
  BULL PUT SPREAD          → Bull Put Spread (2 legs)
  BEAR CALL SPREAD         → Bear Call Spread (2 legs)
  CALENDAR (pre-earnings)  → Calendar Spread (short near, long far, same strike)

Run:
  uv run python scripts/option_orders.py
  uv run python scripts/option_orders.py --min-score 60
  uv run python scripts/option_orders.py --symbol TTWO WMT ADI
"""
from __future__ import annotations

import argparse
import asyncio
import warnings
from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

MAX_LOSS = 5_000        # $ per symbol
PORTFOLIO = 250_000     # for reference / sizing note
CSP_STOP_MULT = 2.0     # close CSP/CC when loss = N x credit received
MIN_BID = 0.05          # ignore legs with mid < this (illiquid)
CSP_MAX_NOTIONAL = 50_000  # hard cap: CSP notional exposure (20% of portfolio)

# ── Helpers ───────────────────────────────────────────────────────────────────

def _mid(row: pd.Series) -> float:
    b, a = float(row.get("bid", 0) or 0), float(row.get("ask", 0) or 0)
    if b > 0 and a > 0:
        return round((b + a) / 2, 2)
    lp = float(row.get("lastPrice", 0) or 0)
    return round(lp, 2) if lp > 0 else float("nan")


def _nearest(df: pd.DataFrame, target: float) -> pd.Series:
    idx = (df["strike"] - target).abs().idxmin()
    return df.loc[idx]


def _find_expiry(exps: list[str], today, min_dte: int, max_dte: int) -> Optional[str]:
    for exp in exps:
        dte = (datetime.strptime(exp, "%Y-%m-%d").date() - today).days
        if min_dte <= dte <= max_dte:
            return exp
    return exps[-1] if exps else None


def _sd_strike(spot: float, iv: float, dte: int, z: float) -> float:
    """Log-normal z-sigma strike."""
    T = max(dte, 1) / 365
    return spot * np.exp(z * iv * np.sqrt(T))


def _qty(max_loss_per_contract: float, notional_cap: float = 0.0) -> int:
    if max_loss_per_contract <= 0:
        return 1
    q = int(MAX_LOSS / max_loss_per_contract)
    if notional_cap > 0:
        q = min(q, int(notional_cap / 100))
    return max(1, q)


# ── Order builders ────────────────────────────────────────────────────────────

def _csp(calls, puts, spot, iv, dte, exp) -> list[dict]:
    """Sell slightly OTM put (~0.25-delta). Size by 2x-credit stop + notional cap."""
    put_target = _sd_strike(spot, iv, dte, -0.7)
    row = _nearest(puts, put_target)
    credit = _mid(row)
    if np.isnan(credit) or credit < MIN_BID:
        return []
    max_loss_per = CSP_STOP_MULT * credit * 100
    # notional cap: max contracts so total cash secured <= CSP_MAX_NOTIONAL
    notional_per = row["strike"] * 100
    qty = _qty(max_loss_per, notional_cap=CSP_MAX_NOTIONAL / notional_per * 100)
    return [{
        "action": "SELL", "pc": "P", "strike": row["strike"],
        "expiry": exp, "cr_dr": "CR", "price": credit, "qty": qty,
        "max_loss": round(max_loss_per * qty),
        "note": f"Stop: close @ loss = {CSP_STOP_MULT:.0f}x credit (notional cap ${CSP_MAX_NOTIONAL:,})",
    }]


def _bear_call_spread(calls, puts, spot, iv, dte, exp) -> list[dict]:
    """Sell 1-SD call, buy 1.5-SD call."""
    short_row = _nearest(calls, _sd_strike(spot, iv, dte, +1.0))
    long_row  = _nearest(calls, _sd_strike(spot, iv, dte, +1.5))
    short_cr  = _mid(short_row)
    long_dr   = _mid(long_row)
    if any(np.isnan(x) or x < MIN_BID for x in [short_cr, long_dr]):
        return []
    net_cr    = short_cr - long_dr
    if net_cr <= 0:
        return []
    width     = long_row["strike"] - short_row["strike"]
    max_loss_per = max(0.01, width - net_cr) * 100
    qty = _qty(max_loss_per)
    return [
        {"action": "SELL", "pc": "C", "strike": short_row["strike"],
         "expiry": exp, "cr_dr": "CR", "price": short_cr, "qty": qty,
         "max_loss": round(max_loss_per * qty), "note": f"Net CR={net_cr:.2f}/contract"},
        {"action": "BUY",  "pc": "C", "strike": long_row["strike"],
         "expiry": exp, "cr_dr": "DR", "price": long_dr,  "qty": qty,
         "max_loss": 0, "note": "wing hedge"},
    ]


def _bull_put_spread(calls, puts, spot, iv, dte, exp) -> list[dict]:
    """Sell 1-SD put, buy 1.5-SD put."""
    short_row = _nearest(puts, _sd_strike(spot, iv, dte, -1.0))
    long_row  = _nearest(puts, _sd_strike(spot, iv, dte, -1.5))
    short_cr  = _mid(short_row)
    long_dr   = _mid(long_row)
    if any(np.isnan(x) or x < MIN_BID for x in [short_cr, long_dr]):
        return []
    net_cr    = short_cr - long_dr
    if net_cr <= 0:
        return []
    width     = short_row["strike"] - long_row["strike"]
    max_loss_per = max(0.01, width - net_cr) * 100
    qty = _qty(max_loss_per)
    return [
        {"action": "SELL", "pc": "P", "strike": short_row["strike"],
         "expiry": exp, "cr_dr": "CR", "price": short_cr, "qty": qty,
         "max_loss": round(max_loss_per * qty), "note": f"Net CR={net_cr:.2f}/contract"},
        {"action": "BUY",  "pc": "P", "strike": long_row["strike"],
         "expiry": exp, "cr_dr": "DR", "price": long_dr,  "qty": qty,
         "max_loss": 0, "note": "wing hedge"},
    ]


def _next_otm_strike(df: pd.DataFrame, ref_strike: float, direction: str) -> pd.Series:
    """Return the next available strike further OTM from ref_strike."""
    if direction == "down":
        candidates = df[df["strike"] < ref_strike]
        return candidates.iloc[-1] if not candidates.empty else df.iloc[0]
    else:
        candidates = df[df["strike"] > ref_strike]
        return candidates.iloc[0] if not candidates.empty else df.iloc[-1]


def _iron_condor(calls, puts, spot, iv, dte, exp) -> list[dict]:
    """Sell 1-SD put+call, buy 1.5-SD put+call (auto-expand wings if same strike)."""
    sp_row = _nearest(puts,  _sd_strike(spot, iv, dte, -1.0))
    lp_row = _nearest(puts,  _sd_strike(spot, iv, dte, -1.5))
    sc_row = _nearest(calls, _sd_strike(spot, iv, dte, +1.0))
    lc_row = _nearest(calls, _sd_strike(spot, iv, dte, +1.5))

    # Ensure wings are strictly further OTM than shorts
    if lp_row["strike"] >= sp_row["strike"]:
        lp_row = _next_otm_strike(puts, sp_row["strike"], "down")
    if lc_row["strike"] <= sc_row["strike"]:
        lc_row = _next_otm_strike(calls, sc_row["strike"], "up")

    sp_cr = _mid(sp_row); lp_dr = _mid(lp_row)
    sc_cr = _mid(sc_row); lc_dr = _mid(lc_row)
    if any(np.isnan(x) or x < MIN_BID for x in [sp_cr, lp_dr, sc_cr, lc_dr]):
        return []

    net_cr   = sp_cr + sc_cr - lp_dr - lc_dr
    if net_cr <= 0:
        return []
    put_w    = sp_row["strike"] - lp_row["strike"]
    call_w   = lc_row["strike"] - sc_row["strike"]
    max_w    = max(put_w, call_w)
    max_loss_per = max(0.01, max_w - net_cr) * 100
    qty = _qty(max_loss_per)
    return [
        {"action": "SELL", "pc": "P", "strike": sp_row["strike"],
         "expiry": exp, "cr_dr": "CR", "price": sp_cr,  "qty": qty,
         "max_loss": round(max_loss_per * qty), "note": f"Net CR={net_cr:.2f}/contract"},
        {"action": "BUY",  "pc": "P", "strike": lp_row["strike"],
         "expiry": exp, "cr_dr": "DR", "price": lp_dr,  "qty": qty,
         "max_loss": 0, "note": "put wing"},
        {"action": "SELL", "pc": "C", "strike": sc_row["strike"],
         "expiry": exp, "cr_dr": "CR", "price": sc_cr,  "qty": qty,
         "max_loss": 0, "note": ""},
        {"action": "BUY",  "pc": "C", "strike": lc_row["strike"],
         "expiry": exp, "cr_dr": "DR", "price": lc_dr,  "qty": qty,
         "max_loss": 0, "note": "call wing"},
    ]


def _calendar(calls, puts, spot, iv, dte_near, exp_near, exp_far, pc="P") -> list[dict]:
    """Short near-term OTM, long far-term same strike."""
    pool = puts if pc == "P" else calls
    z = -0.7 if pc == "P" else +0.7
    strike_target = _sd_strike(spot, iv, dte_near, z)
    row = _nearest(pool, strike_target)
    strike = row["strike"]

    # near leg
    near_cr = _mid(row)

    # far leg — fetch separately
    try:
        t_tmp = yf.Ticker(row.name if hasattr(row, "name") else "")
    except Exception:
        t_tmp = None
    # We re-use the far chain fetched by the caller — pass as arg instead
    return [
        {"action": "SELL", "pc": pc, "strike": strike,
         "expiry": exp_near, "cr_dr": "CR", "price": near_cr, "qty": 1,
         "max_loss": MAX_LOSS, "note": "calendar — short near"},
        {"action": "BUY",  "pc": pc, "strike": strike,
         "expiry": exp_far,  "cr_dr": "DR", "price": float("nan"), "qty": 1,
         "max_loss": 0,       "note": "calendar — long far (fetch live price)"},
    ]


# ── Per-symbol dispatcher ─────────────────────────────────────────────────────

async def _build_orders(row: pd.Series, sem: asyncio.Semaphore) -> list[dict]:
    symbol   = row["symbol"]
    strategy = str(row.get("strategy", ""))
    spot     = float(row.get("spot", 0) or 0)
    iv_pct   = float(row.get("iv_pct", 0) or 0)
    dte_earn = row.get("dte_earn")
    iv       = iv_pct / 100 if iv_pct > 0 else 0.30

    async with sem:
        try:
            t = yf.Ticker(symbol)
            exps = await asyncio.to_thread(lambda: t.options)
            if not exps:
                return [{"symbol": symbol, "strategy": strategy, "note": "no options"}]

            # Refresh spot from live history if screener value is missing/stale
            if spot <= 0 or np.isnan(spot):
                hist = await asyncio.to_thread(lambda: t.history(period="5d", auto_adjust=True))
                spot = float(hist["Close"].iloc[-1]) if not hist.empty else 0.0
            if spot <= 0:
                return [{"symbol": symbol, "strategy": strategy, "note": "cannot determine spot price"}]

            today = datetime.now(tz=timezone.utc).date()

            # ── choose expiry ──────────────────────────────────────────────
            if "EARNINGS" in strategy and dte_earn is not None:
                earn_date = today + timedelta(days=int(dte_earn))
                exp = next(
                    (e for e in exps if datetime.strptime(e, "%Y-%m-%d").date() >= earn_date),
                    exps[0],
                )
                exp_far = _find_expiry(exps, today, 28, 55) or exps[min(3, len(exps)-1)]
            elif "CALENDAR" in strategy:
                exp     = _find_expiry(exps, today, 10, 22) or exps[0]
                exp_far = _find_expiry(exps, today, 30, 55) or exps[min(3, len(exps)-1)]
            elif "CASH-SECURED" in strategy:
                exp = _find_expiry(exps, today, 25, 50) or exps[min(3, len(exps)-1)]
                exp_far = None
            elif "COVERED" in strategy or "BEAR CALL" in strategy:
                exp = _find_expiry(exps, today, 18, 40) or exps[min(2, len(exps)-1)]
                exp_far = None
            elif "BULL PUT" in strategy:
                exp = _find_expiry(exps, today, 18, 40) or exps[min(2, len(exps)-1)]
                exp_far = None
            else:
                exp = _find_expiry(exps, today, 28, 50) or exps[min(3, len(exps)-1)]
                exp_far = None

            dte = (datetime.strptime(exp, "%Y-%m-%d").date() - today).days

            chain = await asyncio.to_thread(lambda: t.option_chain(exp))
            calls, puts = chain.calls, chain.puts

            # ── build legs ─────────────────────────────────────────────────
            if "CASH-SECURED" in strategy:
                legs = _csp(calls, puts, spot, iv, dte, exp)
            elif "COVERED" in strategy or ("BEAR CALL" in strategy and "BULL" not in strategy):
                legs = _bear_call_spread(calls, puts, spot, iv, dte, exp)
            elif "BULL PUT" in strategy:
                legs = _bull_put_spread(calls, puts, spot, iv, dte, exp)
            elif "CALENDAR" in strategy:
                # For calendar: need far chain too
                far_chain = await asyncio.to_thread(lambda: t.option_chain(exp_far))
                legs = _calendar_full(puts, far_chain.puts, spot, iv, dte, exp, exp_far, "P")
            else:
                # IRON CONDOR covers: SHORT STRANGLE, EARNINGS STRANGLE SELL, IRON CONDOR
                legs = _iron_condor(calls, puts, spot, iv, dte, exp)

            if not legs:
                return [{"symbol": symbol, "strategy": strategy,
                         "note": "illiquid / no valid strikes"}]

            for leg in legs:
                leg["symbol"]   = symbol
                leg["strategy"] = strategy
                leg["spot"]     = spot
                leg["iv_pct"]   = iv_pct
                leg["dte"]      = dte

            return legs

        except Exception as e:
            return [{"symbol": symbol, "strategy": strategy, "note": str(e)[:100]}]


def _calendar_full(near_puts, far_puts, spot, iv, dte, exp_near, exp_far, pc="P") -> list[dict]:
    z = -0.7 if pc == "P" else +0.7
    target = _sd_strike(spot, iv, dte, z)
    near_row = _nearest(near_puts, target)
    far_row  = _nearest(far_puts, near_row["strike"])

    near_cr = _mid(near_row)
    far_dr  = _mid(far_row)
    if np.isnan(near_cr) or np.isnan(far_dr):
        return []
    net_dr = far_dr - near_cr
    max_loss_per = net_dr * 100
    qty = _qty(max(0.01, max_loss_per))
    return [
        {"action": "BUY",  "pc": pc, "strike": far_row["strike"],
         "expiry": exp_far,  "cr_dr": "DR", "price": far_dr,  "qty": qty,
         "max_loss": round(max_loss_per * qty), "note": "calendar long leg"},
        {"action": "SELL", "pc": pc, "strike": near_row["strike"],
         "expiry": exp_near, "cr_dr": "CR", "price": near_cr, "qty": qty,
         "max_loss": 0, "note": "calendar short leg"},
    ]


# ── Main ──────────────────────────────────────────────────────────────────────

async def _run(sym_rows: list[pd.Series]) -> pd.DataFrame:
    sem = asyncio.Semaphore(8)
    tasks = [_build_orders(r, sem) for _, r in sym_rows]
    all_legs = []
    done = 0
    for coro in asyncio.as_completed(tasks):
        legs = await coro
        all_legs.extend(legs)
        done += 1
        print(f"  {done}/{len(tasks)} done...", end="\r", flush=True)
    print()
    return pd.DataFrame(all_legs)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--screener", default="data/screener_weekly.csv")
    ap.add_argument("--min-score", type=float, default=60.0)
    ap.add_argument("--symbol", nargs="+")
    ap.add_argument("--out", default="data/option_orders.csv")
    args = ap.parse_args()

    screen = pd.read_csv(args.screener)
    screen["dte_earn"] = pd.to_numeric(screen["dte_earn"], errors="coerce")

    if args.symbol:
        screen = screen[screen["symbol"].isin([s.upper() for s in args.symbol])]
    else:
        screen = screen[screen["score"] >= args.min_score]

    if screen.empty:
        print("No symbols match filters.")
        return

    print(f"Building orders for {len(screen)} symbols (max loss ${MAX_LOSS:,}/symbol)...")
    df = asyncio.run(_run([(i, r) for i, r in screen.iterrows()]))

    # clean up display columns
    display_cols = ["symbol", "strategy", "spot", "action", "qty", "pc",
                    "strike", "expiry", "cr_dr", "price", "max_loss", "note"]
    for c in display_cols:
        if c not in df.columns:
            df[c] = None
    df = df[display_cols]

    df.to_csv(args.out, index=False)
    print(f"Saved -> {args.out} ({len(df)} legs)\n")

    pd.set_option("display.max_colwidth", 45)
    pd.set_option("display.width", 240)
    pd.set_option("display.float_format", "{:.2f}".format)

    # group print by symbol
    for sym, grp in df.groupby("symbol", sort=False):
        strat = grp["strategy"].iloc[0]
        spot  = grp["spot"].iloc[0]
        ml    = grp["max_loss"].max()
        print(f"{'='*90}")
        print(f"  {sym:6s}  |  {strat}  |  Spot=${spot:.2f}  |  Max Loss=${ml:,.0f}")
        print(f"{'='*90}")
        sub = grp[["action", "qty", "pc", "strike", "expiry", "cr_dr", "price", "note"]]
        print(sub.to_string(index=False))
        print()


if __name__ == "__main__":
    main()
