"""Synthetic OHLC-based wheel strategy backtest.

Simulates monthly covered-call (CC) and cash-secured-put (CSP) cycles on
historical daily OHLC, estimating premiums via Black-Scholes with rolling HV
as the IV proxy.  Produces per-symbol BacktestScore objects and suggested
values for COVER_STD_MULT, VIRGIN_PUT_STD_MULT, and MINNAKEDOPTPRICE.

Parameters that cannot be reliably derived from OHLC alone
(COVXPMULT, NAKEDXPMULT, protect variables) are left at current config values.
REAPRATIO is estimated from simulated theta decay at mid-cycle.
"""
from __future__ import annotations

import math
from calendar import monthrange
from datetime import date, timedelta
from pathlib import Path
from typing import NamedTuple

import numpy as np
import pandas as pd
from loguru import logger

from src.backtest.score import BacktestScore

ROOT = Path(__file__).resolve().parents[2]
BACKTEST_OHLC_PATH = ROOT / "data" / "master" / "backtest_ohlc.pkl"
BACKTEST_RESULTS_PATH = ROOT / "data" / "backtest_results.pkl"

RISK_FREE_RATE = 0.05
HV_WINDOW = 20          # trading days for rolling HV
DEFAULT_DTE = 35        # target DTE when selling

COVER_GRID  = [0.30, 0.50, 0.65, 0.80, 1.00, 1.25, 1.50]
PUT_GRID    = [0.50, 0.75, 1.00, 1.10, 1.25, 1.50, 2.00]


# ── Calendar helpers ──────────────────────────────────────────────────────────

def _third_friday(year: int, month: int) -> date:
    """Return the 3rd Friday of the given month (standard monthly expiry)."""
    first_day = date(year, month, 1)
    first_friday = first_day + timedelta(days=(4 - first_day.weekday()) % 7)
    return first_friday + timedelta(weeks=2)


def _monthly_expiries(start: date, end: date) -> list[date]:
    d = date(start.year, start.month, 1)
    expiries: list[date] = []
    while d <= end:
        tf = _third_friday(d.year, d.month)
        if start <= tf <= end:
            expiries.append(tf)
        days_in_month = monthrange(d.year, d.month)[1]
        d = date(d.year, d.month, days_in_month) + timedelta(days=1)
        d = date(d.year, d.month, 1)
    return expiries


# ── Statistics helpers ────────────────────────────────────────────────────────

def _rolling_hv(closes: pd.Series, window: int = HV_WINDOW) -> pd.Series:
    """Annualised HV from log-returns over a rolling window."""
    log_ret = np.log(closes / closes.shift(1))
    return log_ret.rolling(window).std() * math.sqrt(252)


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2)))


def _bs_price(S: float, K: float, T: float, sigma: float,
              r: float = RISK_FREE_RATE, right: str = "C") -> float:
    """Black-Scholes option price.  Returns 0 on degenerate inputs."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.0
    try:
        d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
        d2 = d1 - sigma * math.sqrt(T)
        if right.upper() == "C":
            return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)
        else:
            return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)
    except (ValueError, ZeroDivisionError):
        return 0.0


# ── Per-symbol simulation ─────────────────────────────────────────────────────

class _Cycle(NamedTuple):
    sell_date: date
    expiry: date
    S_entry: float      # price on sell date
    S_expiry: float     # price at expiry
    hv: float           # HV on sell date
    dte: float          # actual DTE


def _build_cycles(prices: pd.Series, dte_target: int = DEFAULT_DTE) -> list[_Cycle]:
    """Map each monthly expiry to a sell date ~dte_target days prior."""
    if len(prices) < dte_target + HV_WINDOW + 5:
        return []

    price_idx = prices.index
    hv = _rolling_hv(prices)

    expiries = _monthly_expiries(
        price_idx.min().date() + timedelta(days=dte_target + HV_WINDOW),
        price_idx.max().date(),
    )

    cycles: list[_Cycle] = []
    for exp in expiries:
        # Nearest trading day on or before expiry
        exp_ts = pd.Timestamp(exp)
        exp_candidates = price_idx[price_idx <= exp_ts]
        if exp_candidates.empty:
            continue
        exp_day = exp_candidates[-1]

        # Sell date ~dte_target calendar days before expiry
        target_sell = exp - timedelta(days=dte_target)
        sell_candidates = price_idx[price_idx.date <= target_sell]  # type: ignore[attr-defined]
        if sell_candidates.empty:
            continue
        sell_day = sell_candidates[-1]

        hv_val = hv.loc[sell_day]
        if pd.isna(hv_val) or hv_val <= 0:
            continue

        dte_actual = (exp_day.date() - sell_day.date()).days
        if dte_actual < 5:
            continue

        cycles.append(_Cycle(
            sell_date=sell_day.date(),
            expiry=exp_day.date(),
            S_entry=float(prices.loc[sell_day]),
            S_expiry=float(prices.loc[exp_day]),
            hv=float(hv_val),
            dte=float(dte_actual),
        ))
    return cycles


def _simulate_cc(cycles: list[_Cycle], cover_std_mult: float) -> list[float]:
    """Return per-cycle P&L for covered-call strategy."""
    pnl = []
    for c in cycles:
        T = c.dte / 365.0
        sdev = c.S_entry * c.hv * math.sqrt(T)
        strike = c.S_entry + cover_std_mult * sdev
        premium = _bs_price(c.S_entry, strike, T, c.hv, right="C")
        if premium < 0.01:
            continue
        option_pnl = premium - max(0.0, c.S_expiry - strike)
        pnl.append(option_pnl)
    return pnl


def _simulate_csp(cycles: list[_Cycle], put_std_mult: float) -> list[float]:
    """Return per-cycle P&L for cash-secured-put strategy."""
    pnl = []
    for c in cycles:
        T = c.dte / 365.0
        sdev = c.S_entry * c.hv * math.sqrt(T)
        strike = c.S_entry - put_std_mult * sdev
        if strike <= 0:
            continue
        premium = _bs_price(c.S_entry, strike, T, c.hv, right="P")
        if premium < 0.01:
            continue
        option_pnl = premium - max(0.0, strike - c.S_expiry)
        pnl.append(option_pnl)
    return pnl


def _score_pnl(pnl: list[float], years: float) -> dict:
    if not pnl:
        return {"win_rate": 0.0, "profit_factor": 0.0, "max_drawdown_pct": 100.0,
                "n_trades": 0, "mean_premium": 0.0}
    arr = np.array(pnl)
    wins = arr[arr > 0]
    losses = arr[arr <= 0]
    gross_loss = float(abs(losses.sum()))
    # Cap at 99.9 when there are no losing trades — avoids /0 blow-up and signals
    # "no loss data" rather than a genuinely infinite edge.
    pf = float(wins.sum()) / gross_loss if gross_loss > 0 else 99.9
    cum = np.cumsum(arr)
    peak = np.maximum.accumulate(cum)
    dd = float(np.min(cum - peak))
    max_dd_pct = abs(dd / float(peak.max())) * 100 if peak.max() > 0 else 0.0
    return {
        "win_rate": len(wins) / len(arr),
        "profit_factor": round(pf, 3),
        "max_drawdown_pct": round(max_dd_pct, 1),
        "n_trades": len(arr),
        "mean_premium": round(float(arr.mean()), 4),
    }


# ── Parameter optimisation per symbol ────────────────────────────────────────

def _best_params(
    cycles: list[_Cycle],
    cover_grid: list[float] = COVER_GRID,
    put_grid: list[float] = PUT_GRID,
) -> dict:
    """Grid-search for params that maximise profit_factor (PF ≥ 1.0 required)."""
    best_cc = {"cover_std_mult": 0.65, "pf": 0.0, "win_rate": 0.0,
               "max_drawdown_pct": 100.0, "n_trades": 0}
    best_csp = {"put_std_mult": 1.10, "pf": 0.0, "win_rate": 0.0,
                "max_drawdown_pct": 100.0, "n_trades": 0}

    for m in cover_grid:
        pnl = _simulate_cc(cycles, m)
        s = _score_pnl(pnl, 0)
        if s["profit_factor"] > best_cc["pf"] and s["n_trades"] >= 10:
            best_cc = {"cover_std_mult": m, "pf": s["profit_factor"],
                       "win_rate": s["win_rate"],
                       "max_drawdown_pct": s["max_drawdown_pct"],
                       "n_trades": s["n_trades"]}

    for m in put_grid:
        pnl = _simulate_csp(cycles, m)
        s = _score_pnl(pnl, 0)
        if s["profit_factor"] > best_csp["pf"] and s["n_trades"] >= 10:
            best_csp = {"put_std_mult": m, "pf": s["profit_factor"],
                        "win_rate": s["win_rate"],
                        "max_drawdown_pct": s["max_drawdown_pct"],
                        "n_trades": s["n_trades"]}

    return {**best_cc, **best_csp}


def _estimate_reapratio(cycles: list[_Cycle], cover_std_mult: float) -> float:
    """
    Estimate REAPRATIO by computing option value at 50% of DTE remaining.
    REAPRATIO = (value_at_half_dte / original_premium) as fraction * 100
    Expressed in config units: abs(avgCost * REAPRATIO / 100) = reap_price.
    """
    ratios = []
    for c in cycles:
        T_full = c.dte / 365.0
        T_half = (c.dte / 2) / 365.0
        sdev = c.S_entry * c.hv * math.sqrt(T_full)
        strike = c.S_entry + cover_std_mult * sdev
        p_orig = _bs_price(c.S_entry, strike, T_full, c.hv, right="C")
        if p_orig < 0.01:
            continue
        # Price at mid-DTE using same spot and vol (conservative estimate)
        p_half = _bs_price(c.S_entry, strike, T_half, c.hv, right="C")
        ratios.append(p_half / p_orig)
    if not ratios:
        return 0.025  # fallback to current config
    # Suggest: reap when option is at the 25th percentile of its mid-DTE value
    # expressed in config-compatible units (fraction × 100 ... then ×100 again for per-contract)
    # derive.py formula: reap_price = abs(avgCost_per_contract × REAPRATIO / 100)
    # avgCost ≈ -premium_per_share × 100, so reap_price = premium × REAPRATIO
    # We want reap_price ≈ p_half, so REAPRATIO ≈ p_half / premium = ratio
    return round(float(np.percentile(ratios, 25)), 3)


def _estimate_min_premium(cycles: list[_Cycle], put_std_mult: float,
                          percentile: float = 25.0) -> float:
    """Suggest MINNAKEDOPTPRICE as the 25th-pct of simulated CSP premiums."""
    premiums = []
    for c in cycles:
        T = c.dte / 365.0
        sdev = c.S_entry * c.hv * math.sqrt(T)
        strike = c.S_entry - put_std_mult * sdev
        if strike <= 0:
            continue
        p = _bs_price(c.S_entry, strike, T, c.hv, right="P")
        if p > 0.01:
            premiums.append(p)
    if not premiums:
        return 2.5
    return round(float(np.percentile(premiums, percentile)), 2)


# ── Wheel-appropriate verdict ────────────────────────────────────────────────

def _wheel_verdict(
    csp_win_rate: float,
    csp_pf: float,
    years: float,
    csp_trades: int,
) -> str:
    """
    Verdict based on CSP leg performance — the relevant risk leg for the wheel.

    CC assignment (stock called away) is a designed exit, not a loss.
    CSP assignment (buying stock that then falls) is the real wheel risk.

    DEPLOY  : CSP win_rate >= 70% AND PF >= 1.0 AND >= 3 years AND >= 20 cycles
    REFINE  : CSP win_rate >= 55% AND PF >= 0.85 AND >= 2 years
    ABANDON : everything else
    """
    if csp_trades < 20 or years < 2:
        return "INSUFFICIENT_DATA"
    if csp_win_rate >= 0.70 and csp_pf >= 1.0 and years >= 3:
        return "DEPLOY"
    if csp_win_rate >= 0.55 and csp_pf >= 0.85:
        return "REFINE"
    return "ABANDON"


# ── Per-symbol full result ────────────────────────────────────────────────────

def backtest_symbol(
    symbol: str,
    prices: pd.Series,
    dte_target: int = DEFAULT_DTE,
    cover_std_mult: float | None = None,
    put_std_mult: float | None = None,
) -> dict:
    """Run full backtest for one symbol.

    cover_std_mult / put_std_mult — when supplied (from snp_config.yml) the
    simulation and scoring use those values so the verdict reflects actual
    strategy performance.  Grid-search still runs to populate the _opt
    reference columns for comparison.
    """
    cycles = _build_cycles(prices, dte_target)
    if len(cycles) < 10:
        return {"symbol": symbol, "verdict": "INSUFFICIENT_DATA",
                "n_cycles": len(cycles)}

    years = (prices.index.max() - prices.index.min()).days / 365.0
    params = _best_params(cycles)   # grid-search — still used for *_opt columns

    # Params actually used for scoring: config values when provided, else grid-optimal
    sim_cover = cover_std_mult if cover_std_mult is not None else params["cover_std_mult"]
    sim_put   = put_std_mult   if put_std_mult   is not None else params["put_std_mult"]

    cc_pnl  = _simulate_cc(cycles,  sim_cover)
    csp_pnl = _simulate_csp(cycles, sim_put)
    cc_s    = _score_pnl(cc_pnl,  years)
    csp_s   = _score_pnl(csp_pnl, years)

    reapratio     = _estimate_reapratio(cycles, sim_cover)
    min_premium   = _estimate_min_premium(cycles, sim_put)

    # BacktestScore using CC for the composite reference score only.
    # CC assignment is a designed wheel exit, not a loss — so cc_pf/drawdown are
    # misleading as a verdict signal at tight COVER_STD_MULT values.
    # Wheel verdict uses CSP leg, where assignment risk is real.
    bt = BacktestScore(
        symbol=symbol, strategy="synthetic_cc",
        total_trades=cc_s["n_trades"],
        win_rate=cc_s["win_rate"],
        profit_factor=cc_s["profit_factor"],
        max_drawdown_pct=cc_s["max_drawdown_pct"],
        years_tested=round(years, 1),
    ).compute()

    wheel_v = _wheel_verdict(
        csp_win_rate=csp_s["win_rate"],
        csp_pf=csp_s["profit_factor"],
        years=years,
        csp_trades=csp_s["n_trades"],
    )

    return {
        "symbol":             symbol,
        "verdict":            wheel_v,
        "composite":          bt.composite,
        "years_tested":       round(years, 1),
        # CC
        "cover_std_mult_opt": params["cover_std_mult"],
        "cc_pf":              cc_s["profit_factor"],
        "cc_win_rate":        round(cc_s["win_rate"], 3),
        "cc_max_dd":          cc_s["max_drawdown_pct"],
        "cc_trades":          cc_s["n_trades"],
        # CSP
        "put_std_mult_opt":   params["put_std_mult"],
        "csp_pf":             csp_s["profit_factor"],
        "csp_win_rate":       round(csp_s["win_rate"], 3),
        "csp_max_dd":         csp_s["max_drawdown_pct"],
        "csp_trades":         csp_s["n_trades"],
        # Derived suggestions
        "suggested_reapratio":      reapratio,
        "suggested_min_premium":    min_premium,
        "red_flags":          bt.red_flags,
    }


# ── Portfolio-level parameter suggestions ────────────────────────────────────

def suggest_config(results: pd.DataFrame) -> dict:
    """
    Aggregate per-symbol optimal params into suggested snp_config.yml values.
    Uses profit-factor-weighted median across DEPLOY+REFINE symbols.
    Returns a dict of {param_name: suggested_value}.
    """
    usable = results[results["verdict"].isin(["DEPLOY", "REFINE"])].copy()
    if usable.empty:
        logger.warning("No DEPLOY/REFINE symbols — returning current config defaults")
        return {}

    def wmedian(col: str, weight_col: str = "cc_pf") -> float:
        vals   = usable[col].dropna().values
        weights = usable.loc[usable[col].notna(), weight_col].clip(lower=0.01).values
        if len(vals) == 0:
            return float("nan")
        order = np.argsort(vals)
        vals, weights = vals[order], weights[order]
        cumw = np.cumsum(weights)
        midpoint = cumw[-1] / 2
        return float(vals[np.searchsorted(cumw, midpoint)])

    reapratio   = round(wmedian("suggested_reapratio"), 3)
    min_premium = round(wmedian("suggested_min_premium", "csp_pf"), 2)

    deploy_pct  = (usable["verdict"] == "DEPLOY").mean()
    n_deploy    = int((usable["verdict"] == "DEPLOY").sum())
    n_refine    = int((usable["verdict"] == "REFINE").sum())
    n_abandon   = int((results["verdict"] == "ABANDON").sum())

    return {
        "REAPRATIO":        reapratio,
        "MINNAKEDOPTPRICE": min_premium,
        "_meta": {
            "n_symbols":  len(results),
            "n_deploy":   n_deploy,
            "n_refine":   n_refine,
            "n_abandon":  n_abandon,
            "deploy_pct": round(deploy_pct * 100, 1),
            "note": (
                "Verdict uses CSP leg (real wheel risk). "
                "DEPLOY: CSP win>=70% & PF>=1.0 & 3+yr. "
                "COVER_STD_MULT / VIRGIN_PUT_STD_MULT not suggested — set from config. "
                "COVXPMULT / NAKEDXPMULT not derivable from OHLC."
            ),
        },
    }


# ── OHLC fetch ────────────────────────────────────────────────────────────────

def fetch_backtest_ohlc(
    symbols: list[str],
    period: str = "5y",
    batch_size: int = 50,
) -> dict[str, pd.DataFrame]:
    """Fetch daily OHLC via yfinance.  Returns {symbol: DataFrame}."""
    import yfinance as yf

    result: dict[str, pd.DataFrame] = {}
    batches = [symbols[i:i + batch_size] for i in range(0, len(symbols), batch_size)]
    n_total = len(symbols)
    n_done = 0

    logger.info("Fetching {}-year OHLC for {} symbols ({} batches of {})",
                period, n_total, len(batches), batch_size)
    for batch_idx, batch in enumerate(batches, 1):
        try:
            raw = yf.download(
                batch, period=period, interval="1d",
                progress=False, auto_adjust=True,
                group_by="ticker",
            )
            for sym in batch:
                try:
                    if len(batch) == 1:
                        df = raw.copy()
                    else:
                        df = raw[sym].copy() if sym in raw.columns.get_level_values(0) else pd.DataFrame()
                    df = df.dropna(how="all")
                    if not df.empty and "Close" in df.columns:
                        result[sym] = df[["Open", "High", "Low", "Close", "Volume"]]
                except Exception:
                    pass
        except Exception as e:
            logger.warning("Batch {} fetch failed: {}", batch_idx, e)
        n_done += len(batch)
        logger.info("OHLC: {}/{} ({:.0f}%) — batch {}/{}",
                    n_done, n_total, 100 * n_done / n_total, batch_idx, len(batches))

    logger.info("Fetched OHLC for {}/{} symbols", len(result), n_total)
    return result


# ── Main entry point ──────────────────────────────────────────────────────────

def run_backtest(
    symbols: list[str] | None = None,
    dte_target: int = DEFAULT_DTE,
    force_refresh_ohlc: bool = False,
) -> tuple[pd.DataFrame, dict]:
    """
    Run full synthetic backtest.

    1. Load or fetch 5-year OHLC.
    2. For each symbol: build cycles, grid-search params, score.
    3. Aggregate portfolio-level suggested config.
    4. Save results to BACKTEST_RESULTS_PATH.

    Returns (results_df, suggested_config).
    """

    # Load symbols list
    sym_path = ROOT / "data" / "symbols.pkl"
    json_path = ROOT / "data" / "ohlc_symbols.json"
    if symbols is None:
        if sym_path.exists():
            import pickle
            with open(sym_path, "rb") as f:
                contracts = pickle.load(f)
            symbols = [c.symbol for c in contracts if hasattr(c, "symbol")]
        elif json_path.exists():
            import json
            with open(json_path) as f:
                specs = json.load(f)
            symbols = [s["symbol"] for s in specs if isinstance(s, dict) and "symbol" in s]
            logger.info("symbols.pkl missing — loaded {} symbols from ohlc_symbols.json", len(symbols))
        else:
            raise FileNotFoundError("symbols.pkl not found — run Build first")

    # Restrict to weekly symbols only — monthly-only symbols have no weekly sow
    # candidates so their synthetic scores are not actionable.
    cat_path = ROOT / "data" / "master" / "symbol_categories.pkl"
    if cat_path.exists():
        _cats = pd.read_pickle(cat_path)
        _weekly_set = set(_cats.loc[_cats["is_weekly"], "symbol"])
        _before = len(symbols)
        symbols = [s for s in symbols if s in _weekly_set]
        logger.info("Restricted to {} weekly symbols ({} monthly-only excluded)",
                    len(symbols), _before - len(symbols))
    else:
        logger.warning("symbol_categories.pkl not found — backtesting all {} symbols", len(symbols))

    # Load or fetch OHLC
    if BACKTEST_OHLC_PATH.exists() and not force_refresh_ohlc:
        logger.info("Loading cached backtest OHLC from {}", BACKTEST_OHLC_PATH)
        ohlc: dict = pd.read_pickle(BACKTEST_OHLC_PATH)
        missing = [s for s in symbols if s not in ohlc]
        if missing:
            logger.info("Fetching OHLC for {} new symbols", len(missing))
            new_data = fetch_backtest_ohlc(missing)
            ohlc.update(new_data)
            pd.to_pickle(ohlc, BACKTEST_OHLC_PATH)
    else:
        logger.info("Fetching 5-year backtest OHLC for {} symbols", len(symbols))
        ohlc = fetch_backtest_ohlc(symbols)
        BACKTEST_OHLC_PATH.parent.mkdir(parents=True, exist_ok=True)
        pd.to_pickle(ohlc, BACKTEST_OHLC_PATH)

    # Load live config params so scoring reflects the user's actual strategy
    try:
        from src.dashboard.settings import load_config as _load_cfg  # noqa: PLC0415
        _cfg = _load_cfg("SNP")
        _cover_mult = float(_cfg.get("COVER_STD_MULT", 0.65))
        _put_mult   = float(_cfg.get("VIRGIN_PUT_STD_MULT", 1.10))
    except Exception:
        _cover_mult, _put_mult = 0.65, 1.10
    logger.info(
        "Scoring with config params: COVER_STD_MULT={}, VIRGIN_PUT_STD_MULT={}",
        _cover_mult, _put_mult,
    )

    # Run per-symbol backtest
    rows = []
    skipped = 0
    n_total = len(symbols)
    _log_every = max(1, n_total // 20)   # ~20 progress lines total
    logger.info("Backtesting {} symbols (DTE target={})…", n_total, dte_target)
    for i, sym in enumerate(symbols, 1):
        if sym not in ohlc or ohlc[sym].empty:
            skipped += 1
        else:
            prices = ohlc[sym]["Close"].dropna()
            rows.append(backtest_symbol(sym, prices, dte_target,
                                        cover_std_mult=_cover_mult,
                                        put_std_mult=_put_mult))
        if i % _log_every == 0 or i == n_total:
            logger.info("Backtest: {}/{} ({:.0f}%){}", i, n_total,
                        100 * i / n_total,
                        f" — {skipped} skipped (no OHLC)" if skipped else "")

    if skipped:
        logger.warning("Skipped {} symbols with no OHLC data", skipped)

    results = pd.DataFrame(rows)
    suggested = suggest_config(results)

    BACKTEST_RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    pd.to_pickle(results, BACKTEST_RESULTS_PATH)
    logger.info("Backtest complete — {} symbols, results saved to {}",
                len(results), BACKTEST_RESULTS_PATH)

    return results, suggested
