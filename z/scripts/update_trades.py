"""Parse a Zerodha Console tradebook CSV into data/master/trades.pkl.

    uv run python scripts/update_trades.py path/to/tradebook.csv [--account NSE]

Download the tradebook from Zerodha Console -> Reports -> Tradebook (CSV).
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.nsewheel import history  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("tradebook_csv", help="Console tradebook CSV export")
    ap.add_argument("--account", default="NSE")
    args = ap.parse_args()

    df = history.update_trades(args.tradebook_csv, account_id=args.account)
    print(f"Parsed {len(df)} trades -> data/master/{history.TRADES_PKL}")
    print(df.head(10).to_string(index=False))


if __name__ == "__main__":
    main()
