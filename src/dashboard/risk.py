"""Risk aggregations - vectorized, no side-effects."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import numpy as np
import pandas as pd

from .ib_client import Snapshot, TickerSnap


def _parse_expiry(s: str) -> date | None:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y%m%d").date()
    except ValueError:
        return None


def _dte_series(expiry: pd.Series, today: date | None = None) -> pd.Series:
    today = today or date.today()
    today_ord = today.toordinal()

    def _to_dte(s: str) -> float:
        if not s:
            return np.nan
        try:
            return date(int(s[:4]), int(s[4:6]), int(s[6:8])).toordinal() - today_ord
        except (ValueError, IndexError):
            return np.nan

    return expiry.map(_to_dte)


def position_delta_dollars(df: pd.DataFrame) -> pd.Series:
    """Dollar delta per row: P&L change for a $1 move in the underlying.

    STK: position × market_price  (delta = 1 by definition)
    OPT: position × delta × 100 × underlying_px  (underlying_px from modelGreeks)
    """
    if df.empty:
        return pd.Series(dtype=float)
    stk = df["secType"] == "STK"
    opt = df["secType"] == "OPT"
    px = df["marketPrice"]  # stock price for STK rows; option price for OPT (fallback)
    und_px = (
        df["underlying_px"]
        if "underlying_px" in df.columns
        else pd.Series(float("nan"), index=df.index)
    )
    delta = df["delta"] if "delta" in df.columns else pd.Series(float("nan"), index=df.index)
    stk_d = df["position"] * px
    opt_d = df["position"] * delta.fillna(0.0) * 100 * und_px
    return pd.Series(
        np.where(stk, stk_d, np.where(opt, opt_d, float("nan"))),
        index=df.index,
    )


def position_margin_est(df: pd.DataFrame) -> pd.Series:
    """Reg T initial margin estimate per position row.

    This is an *approximation* — IBKR's actual margin depends on account type
    (Portfolio Margin vs Reg T), cross-margining, and real-time risk calculations
    which are not exposed per-position via the streaming API.

    Rules applied:
      STK long  : 50% × |market value|
      STK short : 150% × |market value|
      OPT long  : 0 (premium fully paid; max loss is limited to cost)
      OPT short : max(20% × underlying_notional − OTM_amount,
                      10% × underlying_notional)
                  where underlying_notional = underlying_px × |qty| × 100
    """
    if df.empty:
        return pd.Series(dtype=float)

    short = df["position"] < 0
    qty = df["position"].abs()
    mv = df["marketValue"].abs()

    und_px: pd.Series = (
        df["underlying_px"] if "underlying_px" in df.columns else pd.Series(np.nan, index=df.index)
    )
    und_px = und_px.fillna(df.get("marketPrice", pd.Series(np.nan, index=df.index)))

    strike: pd.Series = (
        df["strike"] if "strike" in df.columns else pd.Series(np.nan, index=df.index)
    )
    right: pd.Series = df["right"] if "right" in df.columns else pd.Series("", index=df.index)

    # OTM amount per option contract side (reduces margin for OTM short options)
    put_otm = np.where(right == "P", np.maximum(und_px - strike, 0.0) * qty * 100, 0.0)
    call_otm = np.where(right == "C", np.maximum(strike - und_px, 0.0) * qty * 100, 0.0)
    otm_amount = pd.Series(put_otm + call_otm, index=df.index, dtype=float)

    notional = und_px * qty * 100  # underlying value controlled by the option position
    opt_short_margin = pd.Series(
        np.maximum(0.20 * notional - otm_amount, 0.10 * notional), index=df.index
    )

    result = pd.Series(0.0, index=df.index)
    stk = df["secType"] == "STK"
    opt = df["secType"] == "OPT"
    result = pd.Series(np.where(stk & ~short, 0.5 * mv, result), index=df.index)
    result = pd.Series(np.where(stk & short, 1.5 * mv, result), index=df.index)
    result = pd.Series(np.where(opt & short, opt_short_margin, result), index=df.index)
    return result


_TICKER_COLS = ["delta", "gamma", "theta", "vega", "iv", "underlying_px", "last_px"]


def _join_tickers(positions: pd.DataFrame, tickers: dict[int, TickerSnap]) -> pd.DataFrame:
    """Merge per-contract greeks/prices into the positions DataFrame.

    Idempotent: if ticker columns already exist (e.g. from a prior call) they are
    dropped first so a double-join never produces *_x / *_y suffix columns.
    """
    if positions.empty:
        return positions
    existing = [c for c in _TICKER_COLS if c in positions.columns]
    pos = positions.drop(columns=existing) if existing else positions
    if not tickers:
        return pos.assign(
            delta=np.nan, gamma=np.nan, theta=np.nan, vega=np.nan,
            iv=np.nan, underlying_px=np.nan, last_px=np.nan,
        )
    # Direct per-row dict lookup — avoids building an intermediate DataFrame and merge.
    nan = float("nan")
    ids = pos["conId"].astype(int).values
    return pos.assign(
        delta      =np.array([tickers[k].delta       if k in tickers else nan for k in ids], dtype=float),
        gamma      =np.array([tickers[k].gamma       if k in tickers else nan for k in ids], dtype=float),
        theta      =np.array([tickers[k].theta       if k in tickers else nan for k in ids], dtype=float),
        vega       =np.array([tickers[k].vega        if k in tickers else nan for k in ids], dtype=float),
        iv         =np.array([tickers[k].iv          if k in tickers else nan for k in ids], dtype=float),
        underlying_px=np.array([tickers[k].underlying_px if k in tickers else nan for k in ids], dtype=float),
        last_px    =np.array([tickers[k].last        if k in tickers else nan for k in ids], dtype=float),
    )


def _select_account_values(snap: Snapshot, account: str = "") -> dict[str, Decimal]:
    """Return a flat {tag: Decimal} dict for the requested account (or sum of all)."""
    all_av = snap.account_values  # {acct: {tag: Decimal}}
    if account and account in all_av:
        return dict(all_av[account])
    # Aggregate across all accounts (additive tags like NLV, Excess, Margin are summable)
    merged: dict[str, Decimal] = {}
    for vals in all_av.values():
        for tag, val in vals.items():
            merged[tag] = merged.get(tag, Decimal("0")) + val
    return merged


def account_kpis(
    snap: Snapshot, min_cushion: float = 0.20, account: str = ""
) -> dict[str, float | bool]:
    av = _select_account_values(snap, account)
    nlv = float(av.get("NetLiquidation", Decimal("0")) or 0)
    excess = float(av.get("ExcessLiquidity", Decimal("0")) or 0)
    init_margin = float(av.get("InitMarginReq", Decimal("0")) or 0)
    maint_margin = float(av.get("MaintMarginReq", Decimal("0")) or 0)
    cushion = (excess / nlv) if nlv else 0.0
    day_pnl = float(av.get("RealizedPnL", Decimal("0")) or 0)
    unreal = float(av.get("UnrealizedPnL", Decimal("0")) or 0)
    return {
        "nlv": nlv,
        "excess_liquidity": excess,
        "init_margin": init_margin,
        "maint_margin": maint_margin,
        "cushion": cushion,
        "cushion_breach": cushion < min_cushion,
        "day_pnl": day_pnl,
        "unrealized_pnl": unreal,
    }


def greek_dollar_sums(
    positions: pd.DataFrame,
    tickers: dict[int, TickerSnap] | None = None,
    *,
    pre_joined: bool = False,
) -> dict[str, float]:
    """Sum of dollar delta, theta, vega across the book.

    Pass pre_joined=True when positions has already been through _join_tickers()
    to avoid a redundant merge.
    """
    if positions.empty:
        return {"delta_$": 0.0, "theta_$": 0.0, "vega_$": 0.0, "gamma_$": 0.0}
    df = positions.copy() if pre_joined else _join_tickers(positions, tickers or {}).copy()
    df["mult"] = np.where(df.secType == "OPT", 100, 1)
    delta = df["delta"].where(df.secType == "OPT", 1.0).fillna(0.0)
    theta = df["theta"].where(df.secType == "OPT", 0.0).fillna(0.0)
    vega = df["vega"].where(df.secType == "OPT", 0.0).fillna(0.0)
    gamma = df["gamma"].where(df.secType == "OPT", 0.0).fillna(0.0)
    px = df["underlying_px"].fillna(df["marketPrice"]).fillna(0.0)
    return {
        "delta_$": float((df.position * delta * df.mult * px).sum()),
        "theta_$": float((df.position * theta * df.mult).sum()),
        "vega_$": float((df.position * vega * df.mult).sum()),
        "gamma_$": float((df.position * gamma * df.mult * px).sum()),
    }


_DTE_BINS = [-1, 1, 5, 14, 30, 10_000]
_DTE_LABELS = ["0-1", "2-5", "6-14", "15-30", "31+"]


def dte_buckets(positions: pd.DataFrame) -> pd.DataFrame:
    if positions.empty:
        return pd.DataFrame(columns=["bucket", "count", "abs_notional"])
    opt = positions[positions.secType == "OPT"].copy()
    if opt.empty:
        return pd.DataFrame(columns=["bucket", "count", "abs_notional"])
    opt["dte"] = _dte_series(opt.expiry)
    opt["bucket"] = pd.cut(opt.dte, bins=_DTE_BINS, labels=_DTE_LABELS, right=True)
    return (
        opt.assign(abs_notional=opt.marketValue.abs())
        .groupby("bucket", observed=True)
        .agg(count=("symbol", "count"), abs_notional=("abs_notional", "sum"))
        .reset_index()
    )


def reap_candidates(
    positions: pd.DataFrame,
    tickers: dict[int, TickerSnap],
    *,
    reap_ratio: float = 0.025,
    min_reap_dte: int = 1,
) -> pd.DataFrame:
    """Short options whose last <= reap_ratio * |avgCost| and DTE > min_reap_dte."""
    if positions.empty:
        return positions.iloc[0:0]
    df = _join_tickers(positions, tickers).copy()
    df = df[(df.secType == "OPT") & (df.position < 0)]
    if df.empty:
        return df
    df["dte"] = _dte_series(df.expiry)
    df["target_px"] = (df.avgCost.abs() / 100.0) * reap_ratio
    last_px = df["last_px"]
    mask = last_px.notna() & (last_px <= df["target_px"]) & (df["dte"] > min_reap_dte)
    candidates = df[mask].copy()
    if candidates.empty:
        return candidates
    candidates["pnl_if_reaped"] = (
        (candidates["avgCost"].abs() / 100.0 - candidates["last_px"])
        * candidates["position"].abs()
        * 100
    )
    return candidates.sort_values("pnl_if_reaped", ascending=False).reset_index(drop=True)


def cover_protect_gaps(
    positions: pd.DataFrame,
    tickers: dict[int, TickerSnap] | None = None,
    *,
    protect_me: bool = True,
    cover_std_mult: float = 0.65,
    max_dte: int = 50,
) -> pd.DataFrame:
    """Stock positions missing covering or protecting options.

    Columns returned:
      symbol, shares, mkt_px, avg_cost, needs,
      cover_strike  — target strike for writing a covered call (NaN if no cover gap),
      gain_if_called — capital gain vs avg cost if called at cover_strike (NaN if no cover gap),
      max_downside  — total cost basis at risk (only present when protect_me=True)

    cover_strike = max(avgCost, mkt_px + cover_std_mult × iv_est × sqrt(max_dte / 252))
    where iv_est is sourced from existing option tickers, defaulting to 30% if unavailable.
    If protect_me=False, protect gaps are silently dropped.
    """
    if positions.empty:
        return pd.DataFrame()
    stk = positions[positions.secType == "STK"]
    opt = positions[positions.secType == "OPT"]

    # Build symbol → IV estimate from existing option ticker subscriptions
    sym_iv: dict[str, float] = {}
    if tickers:
        for _, row in positions[positions.secType == "OPT"].iterrows():
            t = tickers.get(int(row["conId"]))
            if t and t.iv is not None:
                try:
                    f = float(t.iv)
                    if not np.isnan(f) and f > 0:
                        sym_iv.setdefault(str(row["symbol"]), f)
                except (TypeError, ValueError):
                    pass

    # Pre-group options by symbol: O(n) once vs O(n) per stock in loop.
    # _empty_opt preserves column schema so downstream attribute access is safe.
    _empty_opt = opt.iloc[0:0]
    opt_by_sym: dict[str, pd.DataFrame] = (
        {s: g for s, g in opt.groupby("symbol")} if not opt.empty else {}
    )

    rows: list[dict] = []
    for sym, grp_stk in stk.groupby("symbol"):
        if (grp_stk.position == 0).all():
            continue
        sym_opt = opt_by_sym.get(str(sym), _empty_opt)
        has_short = (sym_opt.position < 0).any()
        has_long = (sym_opt.position > 0).any()

        needs_cover = not has_short
        needs_protect = protect_me and not has_long
        if not needs_cover and not needs_protect:
            continue

        shares = float(grp_stk.position.sum())
        avg_cost = float(grp_stk["avgCost"].mean())
        mkt_px = float(grp_stk["marketPrice"].mean())

        # Target covered-call strike: at least avg_cost, at least cover_std_mult σ OTM
        iv_est = sym_iv.get(str(sym), 0.30)
        std_move = mkt_px * iv_est * (max_dte / 252) ** 0.5
        cover_strike = max(avg_cost, mkt_px + cover_std_mult * std_move)
        gain_if_called = max(0.0, cover_strike - avg_cost) * shares

        gap_needs: list[str] = []
        if needs_cover:
            gap_needs.append("cover")
        if needs_protect:
            gap_needs.append("protect")

        # Protective strike: existing long strikes when held; target put when gap exists
        protect_strike_str = ""
        if protect_me:
            if has_long and "strike" in sym_opt.columns:
                long_opts = sym_opt[sym_opt["position"] > 0]
                if not long_opts.empty:
                    strikes = sorted(long_opts["strike"].dropna().unique())
                    protect_strike_str = ", ".join(f"{s:.1f}" for s in strikes)
            elif needs_protect:
                protect_target = round(mkt_px - cover_std_mult * std_move, 1)
                protect_strike_str = f"~{protect_target:,.1f}"

        rec: dict = {
            "symbol": sym,
            "shares": shares,
            "mkt_px": round(mkt_px, 2),
            "avg_cost": round(avg_cost, 2),
            "needs": ", ".join(gap_needs),
            "cover_strike": round(cover_strike, 1) if needs_cover else float("nan"),
            "gain_if_called": round(gain_if_called, 0) if needs_cover else float("nan"),
        }
        if protect_me:
            rec["max_downside"] = round(abs(avg_cost * shares), 0)
            rec["protect_strike"] = protect_strike_str
        rows.append(rec)

    return pd.DataFrame(rows)
