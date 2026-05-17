"""Standalone script to refresh flex_trades.pkl from API and/or XML files.

Usage:
    uv run python scripts/update_trades.py            # API + any XML in data/master/
    uv run python scripts/update_trades.py --xml-only # XML only (no API call)
    uv run python scripts/update_trades.py --api-only # API only (ignore XML files)

The script mirrors the dashboard "Update Trades" button:
  1. Downloads via IBKR Flex API (TOKEN + TRADES_FLEXID in .env), trimming to
     3 days before the pkl's most-recent entry to skip old already-merged rows.
  2. Merges any flex_*.xml files found in data/master/.
  3. Combines both sources into flex_trades.pkl using merge_into_pickle() which
     deduplicates on trade ID (or a composite natural key for older exports).

Run this script quarterly (or after a manual portal XML download) to keep
flex_trades.pkl current.  The dashboard's History tab reads the same pkl.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from loguru import logger

load_dotenv()

_HERE = Path(__file__).resolve().parent.parent

# Redirect verbose INFO/DEBUG logs to file; suppress terminal output.
# The script prints one summary line to stdout when done.
logger.remove()
_SCRIPT_LOG = _HERE / "log" / "update_trades.log"
_SCRIPT_LOG.parent.mkdir(exist_ok=True)
logger.add(str(_SCRIPT_LOG), level="DEBUG", encoding="utf-8", rotation="10 MB", retention=5)
sys.path.insert(0, str(_HERE))

from src.dashboard.settings import get_settings
from src.flex.fetch import download_trades, load_xml, merge_into_pickle
from src.flex.parse import mask_accounts, normalize

_MASTER = _HERE / "data" / "master"
_PKL    = _MASTER / "flex_trades.pkl"


def _acct_map(settings) -> dict[str, str]:
    return {
        a: lbl
        for lbl, a in (
            ("US", settings.us_account.get_secret_value()),
            ("SG", settings.sg_account.get_secret_value()),
        )
        if a
    }


def run(api: bool = True, xml: bool = True) -> None:
    s = get_settings()
    amap = _acct_map(s)
    sources: list[pd.DataFrame] = []

    # Reference date: 3 days before the pkl's most-recent entry.
    pkl_max_dt: pd.Timestamp | None = None
    if _PKL.exists():
        df_ex = pd.read_pickle(_PKL)
        if not df_ex.empty and "dateTime" in df_ex.columns:
            t = df_ex["dateTime"].max()
            pkl_max_dt = t if pd.notna(t) else None
    before = len(pd.read_pickle(_PKL)) if _PKL.exists() else 0

    # ── API ───────────────────────────────────────────────────────────────────
    if api:
        token = s.token.get_secret_value()
        qid   = s.trades_flexid.get_secret_value()
        if not (token and qid):
            logger.warning("TOKEN / TRADES_FLEXID not set in .env — skipping API")
        else:
            logger.info("Downloading via Flex API query_id={}", qid)
            try:
                df_api = mask_accounts(normalize(download_trades(token, qid)), amap)
                if df_api.empty:
                    logger.warning(
                        "API returned 0 rows — ensure the IBKR portal query uses "
                        "'Last 365 Calendar Days' (not a Custom Date Range)."
                    )
                else:
                    if pkl_max_dt is not None and "dateTime" in df_api.columns:
                        cutoff = pkl_max_dt - pd.Timedelta(days=3)
                        df_api = df_api[df_api["dateTime"] >= cutoff]
                    sources.append(df_api)
                    logger.info("API: {} rows (trimmed to 3 days before pkl max)", len(df_api))
            except Exception as e:
                logger.error("API download failed: {}", e)

    # ── XML ───────────────────────────────────────────────────────────────────
    if xml:
        try:
            raw    = load_xml(_MASTER)
            df_xml = mask_accounts(normalize(raw), amap)
            n_xml  = len(sorted(_MASTER.glob("flex_*.xml")))
            sources.append(df_xml)
            logger.info("XML: {} rows from {} file(s)", len(df_xml), n_xml)
        except FileNotFoundError:
            logger.info("XML: no flex_*.xml files in {}", _MASTER)
        except Exception as e:
            logger.error("XML load failed: {}", e)

    # ── Merge ─────────────────────────────────────────────────────────────────
    if not sources:
        print("ERROR: No data from any source — pkl unchanged. See log/update_trades.log for details.")
        sys.exit(1)

    df_combined = pd.concat(sources, ignore_index=True)
    df_merged   = merge_into_pickle(df_combined, _PKL)
    if amap:
        df_merged = mask_accounts(df_merged, amap)
        df_merged.to_pickle(_PKL)

    added = len(df_merged) - before
    print(f"Done — {len(df_merged):,} rows total ({added:+,} new). Log: {_SCRIPT_LOG}")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Refresh flex_trades.pkl")
    p.add_argument("--api-only", action="store_true", help="API source only")
    p.add_argument("--xml-only", action="store_true", help="XML source only")
    args = p.parse_args()

    if args.api_only and args.xml_only:
        p.error("--api-only and --xml-only are mutually exclusive")

    run(
        api=not args.xml_only,
        xml=not args.api_only,
    )
