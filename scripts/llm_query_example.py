#!/usr/bin/env python
"""Example: Query pickled data using the LLM.

Run:
    uv run python scripts/llm_query_example.py
"""

from __future__ import annotations

import pickle
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.dashboard.llm_query import query_data


def load_ohlc_sample() -> dict:
    """Load a few symbols from ohlc.pkl."""
    ohlc_path = ROOT / "data" / "master" / "ohlc.pkl"
    if not ohlc_path.exists():
        return {}

    with open(ohlc_path, "rb") as f:
        all_ohlc = pickle.load(f)

    return {k: all_ohlc[k].tail(5) for k in ["AAPL", "TSLA", "SPY"] if k in all_ohlc}


def main():
    print("LLM Query Example\n" + "=" * 50)

    # Load sample data
    ohlc = load_ohlc_sample()
    if not ohlc:
        print("No OHLC data found. Run fetch_ohlc.py first.")
        return

    # Mock portfolio context (in app.py this would come from live state)
    context = {
        "positions": "Portfolio: 100 AAPL @ $230, 50 TSLA @ $245, 10 SPY calls 600C",
        "greeks": {
            "total_delta": 145.5,
            "total_theta": -32.0,
            "total_vega": 28.5,
        },
        "metrics": {
            "NLV": "$125,000",
            "Excess Liquidity": "$23,500",
            "Maint Margin": "$8,200",
        },
        "ohlc_sample": "\n".join(
            [
                f"{symbol}:\n{df.to_string()}" for symbol, df in ohlc.items()
            ]
        ),
    }

    # Query 1: Risk analysis
    print("\n1. Query: 'What's my total delta exposure and is it balanced?'")
    print("-" * 50)
    try:
        response = query_data(
            "What's my total delta exposure and is it balanced?",
            context=context,
        )
        print(response)
    except ValueError as e:
        print(f"Error: {e}")

    # Query 2: Price analysis
    print("\n2. Query: 'Which stock looks strongest based on recent price action?'")
    print("-" * 50)
    try:
        response = query_data(
            "Which stock looks strongest based on recent price action?",
            context=context,
        )
        print(response)
    except ValueError as e:
        print(f"Error: {e}")


if __name__ == "__main__":
    main()
