"""fetch_ohlc.py — Incremental OHLC update runner.

Called by the dashboard's 'Generate OHLCs' button via subprocess.Popen.
Reads the symbol list from data/ohlc_symbols.json (written by the button
handler), fetches missing bars from yfinance (primary) and IBKR (fallback),
and saves to data/master/ohlc.pkl.

Run standalone (S&P500 only, no portfolio extras):
    uv run python fetch_ohlc.py
"""

from __future__ import annotations

from src.dashboard.ohlc import LOG_PATH, run_update

if __name__ == "__main__":
    import argparse
    from src.log_utils import setup_logging

    _p = argparse.ArgumentParser(add_help=False)
    _p.add_argument("--debug", action="store_true")
    setup_logging("ohlc", debug=_p.parse_known_args()[0].debug)
    run_update(log_path=LOG_PATH)
