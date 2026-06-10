"""Refresh flex_trades.pkl, flex_cash.pkl, and flex_nav.pkl.

Usage:
    uv run python scripts/update_trades.py               # API download + parse XMLs (current year)
    uv run python scripts/update_trades.py --year 2024   # API download for a specific year + parse
    uv run python scripts/update_trades.py --xml-only    # parse existing XMLs only (no API call)

API mode calls download_flex_xml() which downloads the Flex statement for the
given year and saves it as data/master/{year}.xml. One API call covers all
sections (Trades, CashTransactions, EquitySummaryByReportDateInBase).  All
*.xml files in data/master/ are then parsed and merged into the three pickles.

For historical years: set the portal query period to 'Custom Date Range'
(Jan 1 – Dec 31 of that year) before running with --year <year>.

Run weekly (or use the dashboard's Refresh Flex button which auto-triggers this).
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

logger.remove()
_SCRIPT_LOG = _HERE / "log" / "update_trades.log"
_SCRIPT_LOG.parent.mkdir(exist_ok=True)
logger.add(str(_SCRIPT_LOG), level="DEBUG", encoding="utf-8", rotation="10 MB", retention=5)
sys.path.insert(0, str(_HERE))

from src.dashboard.settings import get_settings
from src.flex.fetch import (
    download_flex_xml,
    load_cash_xml,
    load_nav_xml,
    load_xml,
    merge_cash_into_pickle,
    merge_into_pickle,
    merge_nav_into_pickle,
)
from src.flex.parse import mask_accounts, normalize, normalize_cash

_MASTER   = _HERE / "data" / "master"
_PKL      = _MASTER / "flex_trades.pkl"
_CASH_PKL = _MASTER / "flex_cash.pkl"
_NAV_PKL  = _MASTER / "flex_nav.pkl"


def _acct_map(settings) -> dict[str, str]:
    return {
        a: lbl
        for lbl, a in (
            ("US", settings.us_account.get_secret_value()),
            ("SG", settings.sg_account.get_secret_value()),
        )
        if a
    }


def run(api: bool = True, year: int | None = None) -> None:
    s     = get_settings()
    amap  = _acct_map(s)
    token = s.token.get_secret_value()
    qid   = s.trades_flexid.get_secret_value()
    sources: list[pd.DataFrame] = []

    before = len(pd.read_pickle(_PKL)) if _PKL.exists() else 0

    # ── API: download once, save as {year}.xml ────────────────────────────────
    # download_flex_xml() saves a single file covering all sections so the XML
    # path below handles trades, cash, and NAV in one pass.
    if api:
        if not (token and qid):
            logger.warning("TOKEN / TRADES_FLEXID not set in .env — skipping API download")
        else:
            try:
                download_flex_xml(token, qid, _MASTER, year=year)
            except Exception as e:
                logger.error("API download failed: {}", e)

    # ── XML: parse all *.xml files (includes freshly saved API file if any) ──
    try:
        raw   = load_xml(_MASTER)
        df_xml = mask_accounts(normalize(raw), amap)
        n_xml  = len(sorted(_MASTER.glob("*.xml")))
        sources.append(df_xml)
        logger.info("Trades XML: {} rows from {} file(s)", len(df_xml), n_xml)
    except FileNotFoundError:
        logger.info("XML: no *.xml files in {}", _MASTER)
    except Exception as e:
        logger.error("XML load failed: {}", e)

    try:
        df_cash = normalize_cash(load_cash_xml(_MASTER))
        if not df_cash.empty:
            merge_cash_into_pickle(df_cash, _CASH_PKL)
            logger.info("Cash XML: {} rows merged", len(df_cash))
    except Exception as e:
        logger.error("Cash XML load failed: {}", e)

    try:
        df_nav = load_nav_xml(_MASTER)
        if not df_nav.empty:
            merge_nav_into_pickle(df_nav, _NAV_PKL)
            logger.info("NAV XML: {} rows merged", len(df_nav))
    except Exception as e:
        logger.error("NAV XML load failed: {}", e)

    # ── Merge trades ──────────────────────────────────────────────────────────
    if not sources:
        print("ERROR: no XML data found — pickles unchanged. See log/update_trades.log")
        sys.exit(1)

    df_combined = pd.concat(sources, ignore_index=True)
    df_merged   = merge_into_pickle(df_combined, _PKL)
    if amap:
        df_merged = mask_accounts(df_merged, amap)
        df_merged.to_pickle(_PKL)

    added = len(df_merged) - before
    print(f"Done — {len(df_merged):,} rows total ({added:+,} new). Log: {_SCRIPT_LOG}")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Refresh flex_trades.pkl, flex_cash.pkl, flex_nav.pkl")
    p.add_argument("--xml-only", action="store_true", help="Parse existing XMLs only (no API call)")
    p.add_argument("--year", type=int, default=None,
                   help="Year for the downloaded XML filename (default: current year). "
                        "For historical years set the portal query to the matching date range first.")
    args = p.parse_args()

    run(
        api=not args.xml_only,
        year=args.year,
    )
