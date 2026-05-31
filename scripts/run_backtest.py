"""Standalone script: run synthetic wheel-strategy backtest and print config suggestions.

Usage:
    uv run python scripts/run_backtest.py
    uv run python scripts/run_backtest.py --refresh-ohlc   # re-fetch 5-year OHLC
    uv run python scripts/run_backtest.py --since 2025-08-08  # personal-history filter
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from loguru import logger

# Clean uncolored output to stdout so the dashboard log expander can read it.
logger.remove()
logger.add(sys.stdout, format="{time:HH:mm:ss} | {level:<7} | {message}",
           colorize=False, level="INFO")

from src.backtest.synthetic import run_backtest, BACKTEST_RESULTS_PATH


def main() -> None:
    parser = argparse.ArgumentParser(description="Synthetic wheel backtest")
    parser.add_argument("--refresh-ohlc", action="store_true",
                        help="Force re-fetch of 5-year OHLC data")
    parser.add_argument("--dte", type=int, default=35,
                        help="Target DTE for simulated option sells (default: 35)")
    args = parser.parse_args()

    logger.info("Starting synthetic backtest (DTE target={})", args.dte)
    results, suggested = run_backtest(
        force_refresh_ohlc=args.refresh_ohlc,
        dte_target=args.dte,
    )

    # ── Summary ───────────────────────────────────────────────────────────────
    meta = suggested.pop("_meta", {})
    print("\n" + "=" * 60)
    print("  SYNTHETIC BACKTEST — RESULTS SUMMARY")
    print("=" * 60)
    print(f"  Symbols tested  : {meta.get('n_symbols', len(results))}")
    print(f"  DEPLOY          : {meta.get('n_deploy', 0)}")
    print(f"  REFINE          : {meta.get('n_refine', 0)}")
    print(f"  ABANDON         : {meta.get('n_abandon', 0)}")
    print(f"  Deploy rate     : {meta.get('deploy_pct', 0):.1f}%")
    print()
    print("  SUGGESTED CONFIG VALUES (snp_config.yml)")
    print("-" * 60)
    for k, v in suggested.items():
        print(f"  {k:<28}: {v}")
    print()
    print(f"  NOTE: {meta.get('note', '')}")
    print("=" * 60)
    print(f"\n  Full results saved to: {BACKTEST_RESULTS_PATH}")

    # Top 10 DEPLOY by composite score
    if not results.empty and "composite" in results.columns:
        top = (results[results["verdict"] == "DEPLOY"]
               .nlargest(10, "composite")[
                   ["symbol", "composite", "cover_std_mult_opt",
                    "put_std_mult_opt", "cc_pf", "csp_pf"]
               ])
        if not top.empty:
            print("\n  TOP 10 DEPLOY SYMBOLS:")
            print(top.to_string(index=False))
    print()


if __name__ == "__main__":
    main()
