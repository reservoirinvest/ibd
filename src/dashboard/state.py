"""Portfolio state classification - pure functions.

Mirrors the rules in README.md and classify.py but operates on the live
Snapshot's positions DataFrame, with no IBKR side-effects.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def _flag_by_symbol(df: pd.DataFrame, mask: pd.Series, all_syms: np.ndarray) -> pd.Series:
    return df[mask].groupby("symbol")["position"].size().reindex(all_syms, fill_value=0).gt(0)


def classify_portfolio(positions: pd.DataFrame) -> pd.DataFrame:
    """Return positions with an added pf_state column.

    States: zen / exposed / unprotected / uncovered / straddled /
            covering / protecting / sowed / orphaned.
    """
    if positions.empty:
        return positions.assign(pf_state=pd.Series(dtype="string"))

    df = positions.copy()
    df["pf_state"] = "unknown"

    is_stk = df.secType == "STK"
    is_opt = df.secType == "OPT"
    syms = df.symbol.unique()

    has_stk = _flag_by_symbol(df, is_stk & (df.position != 0), syms)
    has_short = _flag_by_symbol(df, is_opt & (df.position < 0), syms)
    has_long = _flag_by_symbol(df, is_opt & (df.position > 0), syms)
    has_call_long = _flag_by_symbol(df, is_opt & (df.position > 0) & (df.right == "C"), syms)
    has_put_long = _flag_by_symbol(df, is_opt & (df.position > 0) & (df.right == "P"), syms)

    def _stk_state(sym: str) -> str:
        if not has_stk.get(sym, False):
            if has_call_long.get(sym, False) and has_put_long.get(sym, False):
                return "straddled"
            return "exposed"
        if has_short.get(sym, False) and has_long.get(sym, False):
            return "zen"
        if has_short.get(sym, False):
            return "unprotected"
        if has_long.get(sym, False):
            return "uncovered"
        return "exposed"

    sym_state = {s: _stk_state(s) for s in syms}
    df.loc[is_stk, "pf_state"] = df.loc[is_stk, "symbol"].map(sym_state)

    has_stk_map = df.symbol.map(has_stk.to_dict()).fillna(False)
    short_mask = is_opt & (df.position < 0)
    long_mask = is_opt & (df.position > 0)
    df.loc[short_mask & has_stk_map, "pf_state"] = "covering"
    df.loc[short_mask & ~has_stk_map, "pf_state"] = "sowed"
    df.loc[long_mask & has_stk_map, "pf_state"] = "protecting"
    df.loc[long_mask & ~has_stk_map, "pf_state"] = "orphaned"
    return df.reset_index(drop=True)


def state_counts(positions: pd.DataFrame) -> pd.DataFrame:
    """Aggregate notional + count by pf_state."""
    if positions.empty or "pf_state" not in positions:
        return pd.DataFrame(columns=["pf_state", "count", "notional"])
    return (
        positions.assign(notional=positions.marketValue.abs())
        .groupby("pf_state")
        .agg(count=("symbol", "count"), notional=("notional", "sum"))
        .reset_index()
        .sort_values("notional", ascending=False)
    )
