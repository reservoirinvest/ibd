"""
Classify S&P 500 symbols as weekly or monthly based on option chain expiry gaps.
Saves data/master/symbol_categories.pkl with columns:
    symbol, is_weekly, n_expiries, updated.

A symbol is *weekly* if any two consecutive expiries in df_chains.pkl are <20 days
apart. A symbol is *monthly-only* if it has enough expiries to judge
(>= MIN_EXPIRIES_FOR_MONTHLY) and none of them are close together.

Hardening: a genuinely monthly-only symbol still carries its full chain (10+
monthly expiries spanning a year), so FEW total expiries is the signature of an
incomplete build fetch, not a monthly symbol. We therefore never demote a symbol
to monthly on thin data — if there are too few expiries to be sure, we carry
forward the previous classification (and default new/unknown symbols to weekly,
the less-harmful default: a stray weekly sow attempt simply finds no weekly
expiry, whereas a false 'monthly' silently suppresses a real wheel candidate and
feeds Ask AI wrong facts).

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

# Minimum number of distinct expiries before a "monthly-only" verdict is trusted.
# Healthy chains carry 10-33 expiries; an incomplete fetch yields 1-3. A real
# monthly symbol still has many (monthly) expiries, so this cleanly separates
# "no weeklies found" from "not enough data fetched to look".
MIN_EXPIRIES_FOR_MONTHLY = 6


def classify_weeklies(
    chains: pd.DataFrame, prior: pd.DataFrame | None = None
) -> pd.DataFrame:
    """Return DataFrame(symbol, is_weekly, n_expiries, updated) from expiry gaps.

    `prior` is the existing symbol_categories.pkl (loaded from OUT_PATH when not
    supplied); its classifications are carried forward for any symbol whose chain
    is too thin to classify confidently this run.
    """
    if prior is None and OUT_PATH.exists():
        try:
            prior = pd.read_pickle(OUT_PATH)
        except Exception:  # corrupt/unreadable prior — proceed without it
            prior = None
    prior_is_weekly: dict[str, bool] = {}
    if prior is not None and not prior.empty and {"symbol", "is_weekly"} <= set(prior.columns):
        prior_is_weekly = dict(zip(prior["symbol"], prior["is_weekly"]))

    def _classify(expiries: list[str]) -> tuple[bool | None, int]:
        """Return (verdict, n_expiries). verdict None = too thin to judge."""
        dates = sorted(datetime.strptime(str(e), "%Y%m%d") for e in expiries)
        n = len(dates)
        # Positive weekly detection is trustworthy at any count.
        if n >= 2 and any((dates[i] - dates[i - 1]).days < 20 for i in range(1, n)):
            return True, n
        # No close expiries found — only assert monthly with enough data.
        if n >= MIN_EXPIRIES_FOR_MONTHLY:
            return False, n
        return None, n

    sym_expiries = chains.groupby("symbol")["expiry"].unique()
    now = datetime.now(tz=timezone.utc)
    rows = []
    carried: list[str] = []
    for sym, exps in sym_expiries.items():
        verdict, n = _classify(exps)
        if verdict is None:
            # Too few expiries to judge — keep prior verdict, default to weekly.
            verdict = bool(prior_is_weekly.get(sym, True))
            carried.append(sym)
        rows.append(
            {"symbol": sym, "is_weekly": verdict, "n_expiries": n, "updated": now}
        )
    df = pd.DataFrame(rows).sort_values("symbol").reset_index(drop=True)
    df.attrs["carried_forward"] = carried
    return df


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

    carried = df.attrs.get("carried_forward", [])
    if carried:
        print(
            f"WARNING: {len(carried)} symbol(s) had too few expiries "
            f"(< {MIN_EXPIRIES_FOR_MONTHLY}) to classify — kept prior/default verdict: "
            f"{sorted(carried)}.  Re-run build.py to refetch full chains."
        )

    df.to_pickle(OUT_PATH)
    print(f"Saved to {OUT_PATH}")

    monthly = sorted(df.loc[~df["is_weekly"], "symbol"].tolist())
    if monthly:
        print(f"Monthly-only symbols: {monthly}")


if __name__ == "__main__":
    main()
