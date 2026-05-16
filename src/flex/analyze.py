"""Symbol-specific performance analysis from Flex trade history."""
from __future__ import annotations

import pandas as pd

from src.flex.parse import filter_closed, filter_options


def symbol_performance(df: pd.DataFrame) -> pd.DataFrame:
    """P&L stats per underlying symbol from closed options trades.

    Returns columns: symbol, trades, win_rate, profit_factor, avg_win, avg_loss, total_pnl.
    """
    closed = filter_closed(filter_options(df))
    if closed.empty or "pnl" not in closed.columns:
        return pd.DataFrame()

    und = "underlyingSymbol" if "underlyingSymbol" in closed.columns else "symbol"
    records = []
    for sym, grp in closed.groupby(und):
        pnl = grp["pnl"].dropna()
        n = len(pnl)
        if n == 0:
            continue
        wins = pnl[pnl > 0]
        losses = pnl[pnl <= 0]
        gross_loss = abs(float(losses.sum())) or 1.0
        records.append({
            "symbol": sym,
            "trades": n,
            "win_rate": len(wins) / n,
            "profit_factor": round(float(wins.sum()) / gross_loss, 3),
            "avg_win": round(float(wins.mean()) if len(wins) else 0.0, 2),
            "avg_loss": round(float(losses.mean()) if len(losses) else 0.0, 2),
            "total_pnl": round(float(pnl.sum()), 2),
        })
    return pd.DataFrame(records).sort_values("total_pnl", ascending=False).reset_index(drop=True)


def dte_distribution(df: pd.DataFrame) -> pd.DataFrame:
    """DTE-at-open statistics per underlying from option OPEN trades.

    Returns columns: symbol, avg_dte, median_dte, min_dte, max_dte, opens.
    """
    opts = filter_options(df)
    if "openCloseIndicator" not in opts.columns:
        return pd.DataFrame()
    opens = opts[opts["openCloseIndicator"] == "O"].copy()
    if opens.empty or "expiry" not in opens.columns or "dateTime" not in opens.columns:
        return pd.DataFrame()

    opens["dte_at_open"] = (opens["expiry"] - opens["dateTime"].dt.normalize()).dt.days.clip(lower=0)
    und = "underlyingSymbol" if "underlyingSymbol" in opens.columns else "symbol"
    grp = opens.groupby(und)["dte_at_open"].agg(["mean", "median", "min", "max", "count"])
    grp.columns = ["avg_dte", "median_dte", "min_dte", "max_dte", "opens"]
    return (
        grp.reset_index()
        .rename(columns={und: "symbol"})
        .round(1)
    )


def strategy_recommendation(
    perf: pd.DataFrame, dte_df: pd.DataFrame, symbol: str
) -> str:
    """One-liner strategy recommendation based on historical edge."""
    row = perf[perf["symbol"] == symbol]
    if row.empty:
        return f"No closed options history for {symbol}."
    r = row.iloc[0]

    dte_row = dte_df[dte_df["symbol"] == symbol] if not dte_df.empty else pd.DataFrame()
    dte_str = f"~{dte_row.iloc[0]['median_dte']:.0f} DTE" if not dte_row.empty else "unknown DTE"

    verdict = "EDGE" if r["profit_factor"] >= 1.5 else ("MARGINAL" if r["profit_factor"] >= 1.0 else "NO EDGE")
    strat = "Cash-Secured Put" if r["avg_win"] > abs(r["avg_loss"]) else "Covered Call"

    return (
        f"{symbol}: {r['trades']} trades | WR {r['win_rate']:.0%} | PF {r['profit_factor']:.2f} [{verdict}]\n"
        f"Typical open: {dte_str} | Suggested strategy: {strat}\n"
        f"Avg win ${r['avg_win']:,.2f} | Avg loss ${r['avg_loss']:,.2f} | Total P&L ${r['total_pnl']:,.2f}"
    )
