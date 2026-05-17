"""
Option strategy screener: ranks S&P 500 symbols by strategy fit.

Signals:
  IV/HV ratio  -- ATM IV (nearest >=14 DTE expiry) / 30-day historical vol
                  >1.3 = expensive premium (sell side)
                  <0.8 = cheap premium (buy side)
  RSI(14)      -- momentum / mean-reversion
  Earnings DTE -- event-risk proximity via yfinance

Note: yfinance has no historical IV, so IV Rank is approximated as a
percentile of IV/HV ratios across the same expiry (not a true 52-wk IVR).
The IV/HV ratio is the more reliable signal here.

Run:
  uv run python scripts/option_strategy_screener.py
  uv run python scripts/option_strategy_screener.py --weekly-only --top 50
  uv run python scripts/option_strategy_screener.py --symbol AAPL MSFT TSLA
"""
from __future__ import annotations

import argparse
import asyncio
import warnings
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

RSI_PERIOD = 14
HV_PERIOD = 30
CONCURRENCY = 15
MIN_OI = 50


# ── Technical indicators ──────────────────────────────────────────────────────

def _rsi(closes: pd.Series, period: int = RSI_PERIOD) -> float:
    delta = closes.diff().dropna()
    if len(delta) < period:
        return float("nan")
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    return float((100 - 100 / (1 + rs)).iloc[-1])


def _hv(closes: pd.Series, period: int = HV_PERIOD) -> float:
    log_ret = np.log(closes / closes.shift(1)).dropna()
    if len(log_ret) < period:
        return float("nan")
    return float(log_ret.rolling(period).std().iloc[-1] * np.sqrt(252))


def _atm_iv(ticker: yf.Ticker, spot: float) -> float:
    """Return ATM IV from the nearest options expiry with >=14 DTE."""
    try:
        exps = ticker.options
        today = datetime.now(tz=timezone.utc).date()
        for exp in exps:
            exp_date = datetime.strptime(exp, "%Y-%m-%d").date()
            if (exp_date - today).days < 14:
                continue
            chain = ticker.option_chain(exp)
            ivs = []
            for side in (chain.calls, chain.puts):
                row = side.iloc[(side["strike"] - spot).abs().argsort().iloc[:1]]
                iv = float(row["impliedVolatility"].values[0])
                oi = float(row["openInterest"].fillna(0).values[0])
                if iv > 0.01 and oi >= MIN_OI:
                    ivs.append(iv)
            if ivs:
                return float(np.mean(ivs))
        return float("nan")
    except Exception:
        return float("nan")


def _days_to_earnings(ticker: yf.Ticker) -> Optional[int]:
    try:
        cal = ticker.calendar
        if cal is None:
            return None
        today = datetime.now(tz=timezone.utc)
        # dict form (yfinance >= 0.2.x)
        if isinstance(cal, dict):
            earns = cal.get("Earnings Date", [])
        elif hasattr(cal, "loc") and "Earnings Date" in cal.index:
            earns = cal.loc["Earnings Date"].tolist()
        else:
            return None

        future = []
        for e in earns:
            try:
                ts = pd.Timestamp(e)
                if ts.tzinfo is None:
                    ts = ts.tz_localize("UTC")
                if ts > today:
                    future.append(ts)
            except Exception:
                pass
        if not future:
            return None
        delta = min(future) - today
        return max(0, delta.days)
    except Exception:
        return None


# ── Strategy classification ───────────────────────────────────────────────────

def _strategy(
    iv: float,
    hv: float,
    rsi: float,
    dte_earn: Optional[int],
    is_weekly: bool,
) -> dict:
    if any(np.isnan(x) for x in [iv, hv, rsi]) or hv == 0:
        return {"strategy": "NO DATA", "score": 0.0,
                "rationale": f"IV={iv:.0%} HV={hv:.0%} RSI={rsi:.0f}" if not np.isnan(iv) else "missing data"}

    ratio = iv / hv  # IV/HV — the core premium signal
    earn_str = f"earnings {dte_earn}d" if dte_earn is not None else "no earnings"

    near_earn = dte_earn is not None and dte_earn <= 7
    pre_earn = dte_earn is not None and 8 <= dte_earn <= 21

    candidates = []

    # ── EXPENSIVE PREMIUM  (ratio > 1.25) ─────────────────────────────────────
    if ratio > 1.25:
        base = min(60.0, (ratio - 1.0) * 60)   # 0–60 from ratio alone

        if near_earn and ratio > 1.5:
            candidates.append({
                "strategy": "EARNINGS STRANGLE SELL",
                "score": base + 35,
                "rationale": f"IV/HV={ratio:.2f}, RSI={rsi:.0f}, {earn_str} — sell IV crush",
                "size_note": "25% size — gamma spikes",
                "dte_target": "Sell 1-2d before event; close same day",
            })

        if rsi <= 42:
            candidates.append({
                "strategy": "CASH-SECURED PUT",
                "score": base + (42 - rsi) * 0.6,
                "rationale": f"IV/HV={ratio:.2f}, RSI={rsi:.0f} oversold — rich premium + dip entry",
                "size_note": "Full size",
                "dte_target": "30-45 DTE",
            })

        if 40 <= rsi <= 62 and not near_earn:
            candidates.append({
                "strategy": "SHORT STRANGLE",
                "score": base + (30 - abs(rsi - 51)) * 0.4,
                "rationale": f"IV/HV={ratio:.2f}, RSI neutral={rsi:.0f} — delta-neutral premium",
                "size_note": "1-SD strikes both sides",
                "dte_target": "30-45 DTE",
            })

        if rsi >= 62:
            candidates.append({
                "strategy": "COVERED CALL / BEAR CALL SPREAD",
                "score": base + (rsi - 62) * 0.5,
                "rationale": f"IV/HV={ratio:.2f}, RSI={rsi:.0f} elevated — fade + collect",
                "size_note": "0.20-delta OTM call",
                "dte_target": "21-35 DTE",
            })

    # ── MODERATE PREMIUM  (0.85 < ratio <= 1.25) ─────────────────────────────
    elif ratio > 0.85:
        base = (ratio - 0.85) / 0.40 * 30  # 0–30

        if rsi <= 38:
            candidates.append({
                "strategy": "BULL PUT SPREAD",
                "score": base + (38 - rsi) * 0.5,
                "rationale": f"IV/HV={ratio:.2f}, RSI={rsi:.0f} — defined-risk bullish",
                "size_note": "Risk ~1% portfolio",
                "dte_target": "21-35 DTE",
            })
        elif rsi >= 62:
            candidates.append({
                "strategy": "BEAR CALL SPREAD",
                "score": base + (rsi - 62) * 0.5,
                "rationale": f"IV/HV={ratio:.2f}, RSI={rsi:.0f} — defined-risk bearish",
                "size_note": "Risk ~1% portfolio",
                "dte_target": "21-35 DTE",
            })
        else:
            candidates.append({
                "strategy": "IRON CONDOR",
                "score": base + 5,
                "rationale": f"IV/HV={ratio:.2f}, RSI neutral={rsi:.0f} — balanced wings",
                "size_note": "1-SD wings; hedge at 1.5-SD",
                "dte_target": "30-45 DTE",
            })

        if pre_earn:
            candidates.append({
                "strategy": "CALENDAR (pre-earnings)",
                "score": base + 15,
                "rationale": f"IV/HV={ratio:.2f}, {earn_str} — sell near, buy far for IV term expansion",
                "size_note": "Short inside earnings window",
                "dte_target": "Short: 7-14d / Long: 30-45d",
            })

    # ── CHEAP PREMIUM  (ratio <= 0.85) ───────────────────────────────────────
    else:
        base = (0.85 - ratio) / 0.45 * 25  # 0–25 from cheapness

        if rsi <= 35 and pre_earn:
            candidates.append({
                "strategy": "DEBIT CALL SPREAD (pre-earn)",
                "score": base + (35 - rsi) * 0.7,
                "rationale": f"IV/HV={ratio:.2f} cheap, RSI={rsi:.0f} dip, {earn_str}",
                "size_note": "ATM to slight OTM; 2:1+ reward:risk",
                "dte_target": "Expire after earnings; 14-21 DTE",
            })
        elif rsi >= 65 and pre_earn:
            candidates.append({
                "strategy": "DEBIT PUT SPREAD (pre-earn)",
                "score": base + (rsi - 65) * 0.7,
                "rationale": f"IV/HV={ratio:.2f} cheap, RSI={rsi:.0f} extended, {earn_str}",
                "size_note": "ATM to slight OTM; 2:1+ reward:risk",
                "dte_target": "Expire after earnings; 14-21 DTE",
            })
        else:
            candidates.append({
                "strategy": "CALENDAR / DIAGONAL",
                "score": base,
                "rationale": f"IV/HV={ratio:.2f} cheap — sell near, buy far; bet on IV expansion",
                "size_note": "Same strike or 1 OTM long leg",
                "dte_target": "Short: 14-21d / Long: 45-60d",
            })

    if not candidates:
        return {"strategy": "SKIP", "score": 0.0, "rationale": "no clear edge"}

    best = max(candidates, key=lambda x: x["score"])
    best.update({
        "iv_pct": round(iv * 100, 1),
        "hv_pct": round(hv * 100, 1),
        "iv_hv": round(ratio, 2),
        "rsi": round(rsi, 1),
        "dte_earn": dte_earn,
        "score": round(best["score"], 1),
    })
    return best


# ── Per-symbol async fetch ────────────────────────────────────────────────────

async def _fetch(symbol: str, is_weekly: bool, sem: asyncio.Semaphore) -> dict:
    async with sem:
        try:
            t = yf.Ticker(symbol)
            hist = await asyncio.to_thread(lambda: t.history(period="1y", auto_adjust=True))
            if hist.empty or len(hist) < RSI_PERIOD + 5:
                return {"symbol": symbol, "is_weekly": is_weekly,
                        "strategy": "NO DATA", "score": 0.0, "rationale": "no price history"}
            closes = hist["Close"]
            spot = float(closes.iloc[-1])
            rsi = _rsi(closes)
            hv = _hv(closes)
            iv = await asyncio.to_thread(_atm_iv, t, spot)
            earn_dte = await asyncio.to_thread(_days_to_earnings, t)
            result = _strategy(iv, hv, rsi, earn_dte, is_weekly)
            result["symbol"] = symbol
            result["is_weekly"] = is_weekly
            result["spot"] = round(spot, 2)
            return result
        except Exception as e:
            return {"symbol": symbol, "is_weekly": is_weekly,
                    "strategy": "ERROR", "score": 0.0, "rationale": str(e)[:100]}


# ── Main ──────────────────────────────────────────────────────────────────────

async def _run(sym_df: pd.DataFrame, top_n: int, min_score: float) -> pd.DataFrame:
    sem = asyncio.Semaphore(CONCURRENCY)
    tasks = [_fetch(r["symbol"], r["is_weekly"], sem) for _, r in sym_df.iterrows()]
    total = len(tasks)
    results, done = [], 0
    for coro in asyncio.as_completed(tasks):
        results.append(await coro)
        done += 1
        if done % 20 == 0 or done == total:
            print(f"  {done}/{total} fetched...", end="\r", flush=True)
    print()
    cols = ["symbol", "is_weekly", "strategy", "score", "spot",
            "iv_pct", "hv_pct", "iv_hv", "rsi", "dte_earn",
            "rationale", "dte_target", "size_note"]
    df = pd.DataFrame(results)
    for c in cols:
        if c not in df.columns:
            df[c] = None
    df = df[cols].sort_values("score", ascending=False)
    return df[df["score"] >= min_score].head(top_n) if top_n else df[df["score"] >= min_score]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=100)
    ap.add_argument("--min-score", type=float, default=15.0)
    ap.add_argument("--weekly-only", action="store_true")
    ap.add_argument("--monthly-only", action="store_true")
    ap.add_argument("--symbol", nargs="+")
    ap.add_argument("--out", default="data/screener_weekly.csv")
    args = ap.parse_args()

    sym_df = pd.read_pickle("data/master/symbol_categories.pkl")[["symbol", "is_weekly"]]

    if args.symbol:
        syms = [s.upper() for s in args.symbol]
        sub = sym_df[sym_df["symbol"].isin(syms)]
        if sub.empty:
            sub = pd.DataFrame([{"symbol": s, "is_weekly": True} for s in syms])
        sym_df = sub
    elif args.weekly_only:
        sym_df = sym_df[sym_df["is_weekly"]]
    elif args.monthly_only:
        sym_df = sym_df[~sym_df["is_weekly"]]

    print(f"Screening {len(sym_df)} symbols  (concurrency={CONCURRENCY})")
    df = asyncio.run(_run(sym_df, top_n=args.top, min_score=args.min_score))
    df.to_csv(args.out, index=False)
    print(f"Saved -> {args.out} ({len(df)} rows)\n")

    pd.set_option("display.max_colwidth", 55)
    pd.set_option("display.width", 220)
    print(df[["symbol", "strategy", "score", "iv_hv", "rsi",
              "dte_earn", "iv_pct", "hv_pct", "rationale"]].to_string(index=False))


if __name__ == "__main__":
    main()
