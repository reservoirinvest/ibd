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

# Fallback dedup key when IBKR ID columns are absent (e.g. older Activity XML exports)
_NAT_KEY = ["accountId", "dateTime", "symbol", "putCall", "strike", "expiry", "quantity", "tradePrice"]


def _dedup(df: pd.DataFrame, context: str = "") -> pd.DataFrame:
    """Deduplicate on IBKR trade ID columns, falling back to composite natural key.

    ID columns are only used when ≥80% of rows have a non-null value. This prevents
    pandas from treating all NaN rows as identical (which collapses historical data
    built before tradeID was in the query into a single row when merged with newer data).
    """
    key_cols = [
        c for c in ("tradeID", "ibExecID", "ibOrderID")
        if c in df.columns and df[c].notna().mean() >= 0.8
    ]
    if not key_cols:
        key_cols = [c for c in _NAT_KEY if c in df.columns]
    if key_cols:
        before = len(df)
        df = df.drop_duplicates(subset=key_cols)
        dropped = before - len(df)
        if dropped:
            logger.info("Removed {} duplicate rows{} (key={})", dropped, f" [{context}]" if context else "", key_cols)
    return df


def _load_one_xml(xml_path: Path) -> pd.DataFrame:
    """Load a single Flex XML file. Tries 'Trade' then 'TradeConfirm'."""
    logger.info("Loading XML path={}", xml_path.name)
    report = FlexReport(path=str(xml_path))
    topics = report.topics()
    topic = next((t for t in ("Trade", "TradeConfirm") if t in topics), None)
    if topic is None:
        raise ValueError(
            f"No trade data in {xml_path.name} (topics: {topics}). "
            "Check that the XML was exported from an Activity Flex Query with the Trades section enabled."
        )
    df = report.df(topic)
    if df is None or df.empty:
        logger.warning("No rows in {} for topic={}", xml_path.name, topic)
        return pd.DataFrame()
    logger.info("Loaded {} rows from {}", len(df), xml_path.name)
    return df


def load_xml(xml_dir: Path) -> pd.DataFrame:
    """Load all *.xml files from a directory and return a merged DataFrame.

    IBKR caps each query run at 365 days, so download one file per year
    (e.g. 2021.xml, 2022.xml … 2026.xml) and place them all in xml_dir.
    Files are merged and deduplicated on trade ID.
    """
    xml_files = sorted(xml_dir.glob("*.xml"))
    if not xml_files:
        raise FileNotFoundError(
            f"No *.xml files found in {xml_dir}. "
            "Download from portal (one file per year) and save as 2021.xml, 2022.xml, etc."
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

    combined = _dedup(pd.concat(frames, ignore_index=True), context="load_xml")
    logger.info("Total rows after merge={}", len(combined))
    return combined


def download_flex_xml(
    token: str,
    query_id: str,
    out_dir: Path,
    year: int | None = None,
) -> Path:
    """Download a Flex statement via the API and save it as {year}.xml in out_dir.

    The IBKR Flex API returns whatever period the portal query is configured for.
    For the current year use the query's default 'Last 365 Calendar Days' setting.
    For a historical year, temporarily set the portal query to a custom date range
    (Jan 1 – Dec 31 of that year), then call this function with year=<year>.

    One API call covers all sections (Trades, CashTransactions,
    EquitySummaryByReportDateInBase) so the saved file is the single source of
    truth for all three pickles.

    Args:
        token:    Flex web service token (TOKEN in .env).
        query_id: Single Flex Query ID (TRADES_FLEXID in .env).
        out_dir:  Directory where {year}.xml will be written (data/master/).
        year:     Year label for the output filename. Defaults to the current year.

    Returns:
        Path to the saved XML file.
    """
    if year is None:
        year = pd.Timestamp.today().year
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{year}.xml"
    logger.info("Downloading Flex statement for {} via query_id={}", year, query_id)
    report = FlexReport(token=token, queryId=query_id)
    report.save(str(out_path))
    logger.info("Saved Flex XML {} ({:,} bytes) → {}", year, out_path.stat().st_size, out_path.name)
    return out_path


def _download_one(token: str, query_id: str) -> pd.DataFrame:
    logger.info("API download query_id={}", query_id)
    report = FlexReport(token=token, queryId=query_id)
    topics = report.topics()
    topic = next((t for t in ("Trade", "TradeConfirm") if t in topics), None)
    if topic is None:
        raise ValueError(
            f"No trade data in query {query_id} (topics found: {topics}).\n"
            "Fix in IBKR portal → Reports → Flex Queries → edit your TradeHistory query:\n"
            "  1. Under 'Sections' enable 'Trades' and save all required fields.\n"
            "  2. Under 'General' set Period to 'Last 365 Calendar Days' (not Custom Date Range).\n"
            "Run scripts/diagnose_flex_api.py to inspect the raw XML returned by the API."
        )
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

    combined = _dedup(pd.concat(frames, ignore_index=True), context="download_trades")
    logger.info("API download total rows={}", len(combined))
    return combined


def _load_cash_from_report(report: FlexReport) -> pd.DataFrame:
    """Extract CashTransaction topic from a FlexReport. Returns empty DataFrame if absent."""
    if "CashTransaction" not in report.topics():
        return pd.DataFrame()
    df = report.df("CashTransaction")
    return df if (df is not None and not df.empty) else pd.DataFrame()


def load_cash_xml(xml_dir: Path) -> pd.DataFrame:
    """Load CashTransaction rows from all *.xml files in xml_dir."""
    frames: list[pd.DataFrame] = []
    for f in sorted(xml_dir.glob("*.xml")):
        try:
            df = _load_cash_from_report(FlexReport(path=str(f)))
            if not df.empty:
                frames.append(df)
        except Exception as e:
            logger.warning("Cash XML {}: {}", f.name, e)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def download_cash_transactions(
    token: str,
    query_ids: str,
    inter_query_delay: float = 2.0,
) -> pd.DataFrame:
    """Download CashTransaction rows via Flex Web Service API (same query IDs as trades).

    Returns empty DataFrame if the Flex Query has no CashTransaction section.
    """
    ids = [q.strip() for q in query_ids.split(",") if q.strip()]
    frames: list[pd.DataFrame] = []
    for i, qid in enumerate(ids):
        if i > 0:
            time.sleep(inter_query_delay)
        try:
            df = _load_cash_from_report(FlexReport(token=token, queryId=qid))
            if not df.empty:
                frames.append(df)
            else:
                logger.info("query_id={} has no CashTransaction section or returned 0 rows", qid)
        except Exception as e:
            logger.warning("Cash API query_id={} failed: {}", qid, e)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def merge_cash_into_pickle(new_df: pd.DataFrame, pkl_path: Path) -> pd.DataFrame:
    """Merge new cash transactions into pkl, deduplicating on natural key."""
    if pkl_path.exists():
        existing = pd.read_pickle(pkl_path)
        combined = pd.concat([existing, new_df], ignore_index=True)
    else:
        combined = new_df.copy()

    _key = [c for c in ("accountId", "date", "amount", "currency", "description") if c in combined.columns]
    if _key:
        before = len(combined)
        combined = combined.drop_duplicates(subset=_key)
        if (dropped := before - len(combined)):
            logger.info("Cash dedup removed {} duplicate rows", dropped)

    # Second-pass: same (accountId, amount, currency, description) within the same Mon-Sun week
    # catches IBKR reporting the same deposit on transaction date AND settlement date.
    _fuzzy_key = [c for c in ("accountId", "amount", "currency", "description") if c in combined.columns]
    if _fuzzy_key and "date" in combined.columns:
        _dates = pd.to_datetime(combined["date"], errors="coerce")
        combined["_week"] = _dates - pd.to_timedelta(_dates.dt.dayofweek, unit="D")
        before = len(combined)
        combined = (
            combined.sort_values("date")
            .drop_duplicates(subset=_fuzzy_key + ["_week"], keep="last")
            .drop(columns=["_week"])
        )
        if (dropped := before - len(combined)):
            logger.info("Cash fuzzy-dedup removed {} near-duplicate rows (same week)", dropped)

    pkl_path.parent.mkdir(parents=True, exist_ok=True)
    combined.to_pickle(pkl_path)
    logger.info("Saved cash transactions rows={} path={}", len(combined), pkl_path)
    return combined


def _load_nav_from_report(report: FlexReport) -> pd.DataFrame:
    """Extract EquitySummaryByReportDateInBase from a FlexReport. Returns empty DataFrame if absent."""
    if "EquitySummaryByReportDateInBase" not in report.topics():
        return pd.DataFrame()
    df = report.df("EquitySummaryByReportDateInBase")
    return df if (df is not None and not df.empty) else pd.DataFrame()


def _normalize_nav_report(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize a raw EquitySummaryByReportDateInBase DataFrame → [reportDate, total]."""
    df = df[["reportDate", "total"]].copy()
    df["reportDate"] = pd.to_datetime(
        df["reportDate"].astype(str), format="%Y%m%d", errors="coerce"
    )
    df["total"] = pd.to_numeric(df["total"], errors="coerce")
    df = df[df["total"].notna() & (df["total"] != 0)]
    if df.empty:
        return df
    return df.groupby("reportDate")["total"].sum().reset_index()


def load_nav_xml(xml_dir: Path) -> pd.DataFrame:
    """Load daily consolidated NAV from all *.xml files in xml_dir.

    Two-step aggregation:
      1. Within each file: sum per-account rows → single daily total per file.
      2. Across files: dedup by date (keep last) — adjacent year XMLs share boundary
         dates with identical values; summing across files would double-count them.

    Returns DataFrame with columns [reportDate (Timestamp), total (float)].
    """
    file_navs: list[pd.DataFrame] = []
    for f in sorted(xml_dir.glob("*.xml")):
        try:
            raw = _load_nav_from_report(FlexReport(path=str(f)))
            if raw.empty:
                continue
            daily = _normalize_nav_report(raw)
            if not daily.empty:
                file_navs.append(daily)
        except Exception as e:
            logger.warning("NAV XML {}: {}", f.name, e)

    if not file_navs:
        return pd.DataFrame()

    nav_daily = (
        pd.concat(file_navs, ignore_index=True)
        .sort_values("reportDate")
        .drop_duplicates(subset=["reportDate"], keep="last")
        .reset_index(drop=True)
    )
    logger.info("NAV: {} daily rows from {} file(s)", len(nav_daily), len(file_navs))
    return nav_daily


def download_nav(
    token: str,
    query_ids: str,
    inter_query_delay: float = 2.0,
) -> pd.DataFrame:
    """Download daily consolidated NAV via Flex Web Service API (same query IDs as trades).

    Returns DataFrame with columns [reportDate (Timestamp), total (float)].
    Returns empty DataFrame if the Flex Query has no EquitySummaryByReportDateInBase section.
    """
    ids = [q.strip() for q in query_ids.split(",") if q.strip()]
    frames: list[pd.DataFrame] = []
    for i, qid in enumerate(ids):
        if i > 0:
            time.sleep(inter_query_delay)
        try:
            raw = _load_nav_from_report(FlexReport(token=token, queryId=qid))
            if raw.empty:
                logger.info("query_id={} has no EquitySummaryByReportDateInBase section or returned 0 rows", qid)
                continue
            daily = _normalize_nav_report(raw)
            if not daily.empty:
                frames.append(daily)
                logger.info("NAV API query_id={} rows={}", qid, len(daily))
        except Exception as e:
            logger.warning("NAV API query_id={} failed: {}", qid, e)

    if not frames:
        return pd.DataFrame()

    return (
        pd.concat(frames, ignore_index=True)
        .sort_values("reportDate")
        .drop_duplicates(subset=["reportDate"], keep="last")
        .reset_index(drop=True)
    )


def merge_nav_into_pickle(new_df: pd.DataFrame, pkl_path: Path) -> pd.DataFrame:
    """Merge new daily NAV rows into pkl, deduplicating by reportDate (keep last)."""
    if pkl_path.exists():
        existing = pd.read_pickle(pkl_path)
        combined = pd.concat([existing, new_df], ignore_index=True)
    else:
        combined = new_df.copy()
    if "reportDate" in combined.columns:
        combined = (
            combined.sort_values("reportDate")
            .drop_duplicates(subset=["reportDate"], keep="last")
            .reset_index(drop=True)
        )
    pkl_path.parent.mkdir(parents=True, exist_ok=True)
    combined.to_pickle(pkl_path)
    logger.info("Saved NAV rows={} path={}", len(combined), pkl_path)
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

    combined = _dedup(combined, context="merge_into_pickle")

    pkl_path.parent.mkdir(parents=True, exist_ok=True)
    combined.to_pickle(pkl_path)
    logger.info("Saved merged trades rows={} path={}", len(combined), pkl_path)
    return combined
