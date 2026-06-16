"""Score per-symbol wheel performance from data/master/trades.pkl.

    uv run python scripts/run_backtest.py [--since YYYY-MM-DD]
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.nsewheel.backtest import score_from_trades  # noqa: E402
from src.nsewheel.util import load_pickle  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=None, help="only score closing trades on/after this date")
    args = ap.parse_args()

    df = load_pickle("trades.pkl")
    if df is None or df.empty:
        print("No trades.pkl — run scripts/update_trades.py first.")
        return

    und = "underlyingSymbol" if "underlyingSymbol" in df.columns else "symbol"
    rows = []
    for sym in sorted(df[und].dropna().unique()):
        s = score_from_trades(df, sym, strategy="wheel", since=args.since)
        rows.append((sym, s.total_trades, s.win_rate, s.profit_factor, s.composite, s.verdict))

    print(f"{'symbol':<14}{'trades':>7}{'win%':>7}{'PF':>7}{'score':>7}  verdict")
    for sym, n, wr, pf, comp, verdict in sorted(rows, key=lambda r: -r[4]):
        print(f"{sym:<14}{n:>7}{wr * 100:>6.0f}%{pf:>7.2f}{comp:>7.0f}  {verdict}")


if __name__ == "__main__":
    main()
