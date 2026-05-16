"""Download IBKR Flex trade history via ib_async.FlexReport.

Two load paths:
  1. Manual XML  — portal: run query → Custom Date Range → download XML
                   save to data/master/flex_trades.xml
                   dashboard: "Load from XML" button calls load_xml()

  2. API refresh — portal query must have a period (e.g. Last 365 Calendar Days)
                   TRADES_FLEXID in .env (single ID or comma-separated for multiple)
                   dashboard: "Refresh via API" button calls download_trades()
                   Use for incremental top-ups after the initial XML load.
"""
from __future__ import annotations

import time
from pathlib import Path

import pandas as pd
from ib_async.flexreport import FlexReport
from loguru import logger


def _load_one_xml(xml_path: Path) -> pd.DataFrame:
    """Load a single Flex XML file. Tries 'Trade' then 'TradeConfirm'."""
    logger.info("Loading XML path={}", xml_path.name)
    report = FlexReport(path=str(xml_path))
    topics = report.topics()
    topic = next((t for t in ("Trade", "TradeConfirm") if t in topics), None)
    if topic is None:
        raise ValueError(f"No trade topic in {xml_path.name}. Found: {topics}")
    df = report.df(topic)
    if df is None or df.empty:
        logger.warning("No rows in {} for topic={}", xml_path.name, topic)
        return pd.DataFrame()
    logger.info("Loaded {} rows from {}", len(df), xml_path.name)
    return df


def load_xml(xml_dir: Path) -> pd.DataFrame:
    """Load all flex_*.xml files from a directory and return a merged DataFrame.

    IBKR caps each query run at 365 days, so split a 5-year history into
    5 files: flex_1.xml … flex_5.xml (or any flex_*.xml names) in xml_dir.
    Files are merged and deduplicated on trade ID.
    """
    xml_files = sorted(xml_dir.glob("flex_*.xml"))
    if not xml_files:
        raise FileNotFoundError(
            f"No flex_*.xml files found in {xml_dir}. "
            "Download from portal and save as flex_1.xml, flex_2.xml, etc."
        )
    logger.info("Found {} XML files to load", len(xml_files))

    frames = []
    for f in xml_files:
        try:
            df = _load_one_xml(f)
            if not df.empty:
                frames.append(df)
        except Exception as e:
            logger.warning("Skipping {}: {}", f.name, e)

    if not frames:
        return pd.DataFrame()

    combined = pd.concat(frames, ignore_index=True)
    key_cols = [c for c in ("tradeID", "ibExecID", "ibOrderID") if c in combined.columns]
    if key_cols:
        before = len(combined)
        combined = combined.drop_duplicates(subset=key_cols)
        if before > len(combined):
            logger.info("Removed {} duplicate rows across files", before - len(combined))

    logger.info("Total rows after merge={}", len(combined))
    return combined


def _download_one(token: str, query_id: str) -> pd.DataFrame:
    logger.info("API download query_id={}", query_id)
    report = FlexReport(token=token, queryId=query_id)
    topics = report.topics()
    topic = next((t for t in ("Trade", "TradeConfirm") if t in topics), None)
    if topic is None:
        raise ValueError(f"No trade topic in query {query_id}. Topics: {topics}")
    df = report.df(topic)
    count = len(df) if df is not None else 0
    logger.info("query_id={} rows={}", query_id, count)
    return df if (df is not None and not df.empty) else pd.DataFrame()


def download_trades(
    token: str,
    query_ids: str,
    inter_query_delay: float = 2.0,
) -> pd.DataFrame:
    """Download via Flex Web Service API (uses the period saved in the query).

    Args:
        token:      Flex web service token (TOKEN in .env).
        query_ids:  Single query ID or comma-separated list (TRADES_FLEXID in .env).
    """
    ids = [q.strip() for q in query_ids.split(",") if q.strip()]
    if not ids:
        raise ValueError("query_ids is empty")

    frames: list[pd.DataFrame] = []
    errors: list[str] = []
    for i, qid in enumerate(ids):
        if i > 0:
            time.sleep(inter_query_delay)
        try:
            df = _download_one(token, qid)
            if not df.empty:
                frames.append(df)
            else:
                errors.append(f"query {qid}: downloaded OK but 0 rows returned")
        except Exception as e:
            errors.append(f"query {qid}: {e}")
            logger.warning("query_id={} failed: {}", qid, e)

    if not frames:
        detail = "\n".join(errors) if errors else "unknown reason"
        raise RuntimeError(
            f"All Flex API queries returned no data.\n{detail}\n\n"
            "Check: (1) TOKEN and TRADES_FLEXID are correct; "
            "(2) the query's saved period (edit query → Period) is set to "
            "'Last 365 Calendar Days' — the Custom Date Range used for manual "
            "XML downloads does not apply to API calls."
        )

    combined = pd.concat(frames, ignore_index=True)

    # Deduplicate — same trade can appear in overlapping query periods
    key_cols = [c for c in ("tradeID", "ibExecID", "ibOrderID") if c in combined.columns]
    if key_cols:
        before = len(combined)
        combined = combined.drop_duplicates(subset=key_cols)
        if before > len(combined):
            logger.info("Removed {} duplicate rows", before - len(combined))

    logger.info("API download total rows={}", len(combined))
    return combined


def merge_into_pickle(new_df: pd.DataFrame, pkl_path: Path) -> pd.DataFrame:
    """Merge new trades into an existing pickle, deduplicating on trade ID.

    Creates the pickle if it doesn't exist. Returns the merged DataFrame.
    """
    if pkl_path.exists():
        existing = pd.read_pickle(pkl_path)
        combined = pd.concat([existing, new_df], ignore_index=True)
    else:
        combined = new_df.copy()

    key_cols = [c for c in ("tradeID", "ibExecID", "ibOrderID") if c in combined.columns]
    if key_cols:
        combined = combined.drop_duplicates(subset=key_cols)

    pkl_path.parent.mkdir(parents=True, exist_ok=True)
    combined.to_pickle(pkl_path)
    logger.info("Saved merged trades rows={} path={}", len(combined), pkl_path)
    return combined
