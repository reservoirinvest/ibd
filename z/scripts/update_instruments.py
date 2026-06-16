"""Refresh the NFO instruments dump (lot sizes, contracts) to data/master/instruments_nfo.csv.

OFFLINE writes the mock dump; live downloads from Kite. Run before build when contracts roll.

    uv run python scripts/update_instruments.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.nsewheel import instruments as instr  # noqa: E402
from src.nsewheel.broker import get_client  # noqa: E402
from src.nsewheel.config import load_config  # noqa: E402
from src.nsewheel.paths import ensure_dirs  # noqa: E402


def main() -> None:
    ensure_dirs()
    client = get_client()
    raw = client.instruments(load_config().get("EXCHANGE", "NFO"))
    raw.to_csv(instr.INSTRUMENTS_PATH, index=False)
    df = instr.load_instruments()
    print(f"Wrote {len(raw)} rows -> {instr.INSTRUMENTS_PATH}")
    print(f"Underlyings: {df['name'].nunique()} | option contracts: {len(df)}")


if __name__ == "__main__":
    main()
