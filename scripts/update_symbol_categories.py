"""
Classify S&P 500 symbols as weekly or monthly based on option chain expiry gaps.
Saves data/master/symbol_categories.pkl with columns: symbol, is_weekly, updated.

A symbol is weekly if any two consecutive expiries in df_chains.pkl are <20 days apart.
Monthly-only symbols have only monthly/quarterly expiries (gaps ≥20 days).

Run:  uv run python scripts/update_symbol_categories.py
      (or via 'Identify Weeklies' button in the History tab)
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
CHAINS_PATH = ROOT / "data" / "df_chains.pkl"
OUT_PATH = ROOT / "data" / "master" / "symbol_categories.pkl"


def classify_weeklies(chains: pd.DataFrame) -> pd.DataFrame:
    """Return DataFrame(symbol, is_weekly, updated) from chain expiry gap analysis."""

    def _has_weekly(expiries: list[str]) -> bool:
        dates = sorted(datetime.strptime(str(e), "%Y%m%d") for e in expiries)
        return len(dates) >= 2 and any(
            (dates[i] - dates[i - 1]).days < 20 for i in range(1, len(dates))
        )

    sym_expiries = chains.groupby("symbol")["expiry"].unique()
    now = datetime.now(tz=timezone.utc)
    rows = [
        {"symbol": sym, "is_weekly": _has_weekly(exps), "updated": now}
        for sym, exps in sym_expiries.items()
    ]
    return pd.DataFrame(rows).sort_values("symbol").reset_index(drop=True)


def main() -> None:
    if not CHAINS_PATH.exists():
        print(f"ERROR: {CHAINS_PATH} not found — run build.py first.")
        raise SystemExit(1)

    chains = pd.read_pickle(CHAINS_PATH)
    n_sym = chains["symbol"].nunique()
    print(f"Loaded {len(chains):,} chain rows for {n_sym} symbols")

    df = classify_weeklies(chains)
    weekly_count = int(df["is_weekly"].sum())
    monthly_count = int((~df["is_weekly"]).sum())
    print(f"Weekly: {weekly_count}  |  Monthly-only: {monthly_count}")

    df.to_pickle(OUT_PATH)
    print(f"Saved to {OUT_PATH}")

    monthly = sorted(df.loc[~df["is_weekly"], "symbol"].tolist())
    if monthly:
        print(f"Monthly-only symbols: {monthly}")


if __name__ == "__main__":
    main()
