"""OHLC management — fetch, store and incrementally update daily price history.

Storage : data/master/ohlc.pkl
            Type  : dict[str, pd.DataFrame]
            Key   : IBKR-format symbol  (e.g. 'AAPL', 'BRK B', 'VWRL')
            Value : DataFrame with DatetimeIndex, columns [Open, High, Low, Close, Volume]

Symbol sources (combined at button-click time, written to ohlc_symbols.json):
    • data/symbols.pkl   — 501 S&P500 weekly-option underlyings (ib_async Stock objects)
    • snap.positions     — live portfolio (may include non-US ETFs, etc.)

Fetch strategy:
    1. yfinance  — batch parallel (asyncio + ThreadPoolExecutor, concurrency=20)
    2. IBKR      — reqHistoricalDataAsync fallback for symbols yfinance can't serve,
                   using CID=12 so it never conflicts with the dashboard (CID=10)

Incremental logic:
    • New symbol        → fetch MIN_DAYS (≥1.5 yr) from today
    • Existing symbol   → fetch from (last_date + 1) to today
    • Already current   → skip entirely
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import TextIO

from ib_async import IB, Stock

import pandas as pd
from loguru import logger
from pyprojroot import here
from .progress import progress_bar

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MASTER_DIR = here() / "data" / "master"
OHLC_PATH = MASTER_DIR / "ohlc.pkl"
SYMS_PATH = here() / "data" / "ohlc_symbols.json"  # written by button, read by subprocess
LOG_PATH = here() / "log" / "ohlc_progress.log"
# Persists learned IBKR-symbol → yfinance-ticker overrides (e.g. CSPX → CSPX.L).
# Auto-updated whenever a .L retry succeeds, so subsequent runs fetch directly.
_YF_OVERRIDES_PATH = here() / "data" / "yf_ticker_overrides.json"

MIN_DAYS = 548  # 1.5 years ≈ 548 calendar days
_OHLC_COLS = ["Open", "High", "Low", "Close", "Volume"]
_CONCURRENCY = 20  # parallel yfinance coroutines
_IB_HIST_CONCURRENCY = 5  # parallel IBKR historical-data requests (pacing-safe)
_IB_HIST_TIMEOUT = 30.0  # seconds per historical-data request before retry
_IB_PACING_BACKOFF = 2.0  # seconds to wait after an error-162 pacing violation

# Benchmark symbols always backfilled to this date for full-history performance comparison
_BENCHMARK_START = date(2019, 12, 31)
_BENCHMARK_SYMS: set[str] = {"SPY", "QQQ"}
_BENCHMARK_SPECS: list[SymbolSpec] = [
    {"symbol": s, "exchange": "SMART", "currency": "USD"}
    for s in sorted(_BENCHMARK_SYMS)
]

# ---------------------------------------------------------------------------
# Symbol / ticker utilities
# ---------------------------------------------------------------------------

# Hard overrides: IBKR symbol → yfinance ticker
_IB_TO_YF: dict[str, str] = {
    "BRK B": "BRK-B",
    "BF B": "BF-B",
}

# Primary-exchange → yfinance suffix
_EXCH_SUFFIX: dict[str, str] = {
    "LSE": ".L",
    "TSX": ".TO",
    "TSXV": ".V",
    "ASX": ".AX",
    "HKEX": ".HK",
    "FWB": ".DE",
    "IBIS": ".DE",
    "SBF": ".PA",
    "AEB": ".AS",
    "VIRTX": ".SW",
    "SWX": ".SW",
}

# Currency fallback when exchange is SMART / unknown
_CCY_SUFFIX: dict[str, str] = {
    "GBP": ".L",
    "CAD": ".TO",
    "AUD": ".AX",
    "HKD": ".HK",
    "CHF": ".SW",
    "EUR": ".DE",  # best-effort: most EUR instruments are Xetra
}


SymbolSpec = dict[str, str]  # {"symbol": ..., "exchange": ..., "currency": ...}


def ib_to_yf(symbol: str, exchange: str = "SMART", currency: str = "USD") -> str:
    """Convert an IBKR contract symbol + exchange/currency to a yfinance ticker."""
    if symbol in _IB_TO_YF:
        return _IB_TO_YF[symbol]
    # Non-US suffix from primary exchange or currency
    suffix = _EXCH_SUFFIX.get(exchange, "") or _CCY_SUFFIX.get(currency, "")
    return f"{symbol.replace(' ', '-')}{suffix}"


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def load_ohlc() -> dict[str, pd.DataFrame]:
    """Load OHLC store; return {} if not yet created or unreadable."""
    if not OHLC_PATH.exists():
        return {}
    try:
        data = pd.read_pickle(OHLC_PATH)
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.warning("ohlc.pkl unreadable ({}) — starting fresh", exc)
        return {}


def save_ohlc(data: dict[str, pd.DataFrame]) -> None:
    MASTER_DIR.mkdir(parents=True, exist_ok=True)
    pd.to_pickle(data, OHLC_PATH)


# ---------------------------------------------------------------------------
# Symbol discovery
# ---------------------------------------------------------------------------


def get_sp500_symbols() -> list[SymbolSpec]:
    """Load S&P500 symbol specs from data/symbols.pkl (list of ib_async Stock objects)."""
    path = here() / "data" / "symbols.pkl"
    if not path.exists():
        return []
    try:
        stocks = pd.read_pickle(path)  # list[ib_async.Stock]
        return [
            {
                "symbol": s.symbol,
                "exchange": getattr(s, "primaryExchange", "SMART"),
                "currency": getattr(s, "currency", "USD"),
            }
            for s in stocks
        ]
    except Exception as exc:
        logger.warning("symbols.pkl read error: {}", exc)
        return []


def write_symbol_list(specs: list[SymbolSpec]) -> None:
    """Serialise symbol list for the subprocess to read."""
    SYMS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SYMS_PATH.write_text(json.dumps(specs, ensure_ascii=False), encoding="utf-8")


def load_symbol_list() -> list[SymbolSpec]:
    """Load the combined symbol list written by the dashboard button handler."""
    if SYMS_PATH.exists():
        try:
            return json.loads(SYMS_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    # Fallback: S&P500 only (allows running fetch_ohlc.py standalone)
    return get_sp500_symbols()


def _load_yf_overrides() -> dict[str, str]:
    """Load persisted IBKR-symbol → yfinance-ticker overrides (e.g. CSPX → CSPX.L)."""
    if _YF_OVERRIDES_PATH.exists():
        try:
            return json.loads(_YF_OVERRIDES_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_yf_overrides(overrides: dict[str, str]) -> None:
    _YF_OVERRIDES_PATH.parent.mkdir(parents=True, exist_ok=True)
    _YF_OVERRIDES_PATH.write_text(
        json.dumps(overrides, indent=2, sort_keys=True, ensure_ascii=False),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# yfinance fetch  (async, bounded concurrency)
# ---------------------------------------------------------------------------


def _clean_df(df: pd.DataFrame) -> pd.DataFrame:
    """Normalise a yfinance DataFrame to our canonical OHLCV shape.

    Newer yfinance versions return a tz-aware DatetimeIndex for non-US exchanges
    (e.g. CSPX.L comes back with Europe/London).  Calling tz_localize(None) on a
    tz-aware index raises TypeError; we must use tz_convert(None) instead.
    """
    # yfinance ≥0.2.18 may return a MultiIndex for single-ticker downloads
    if isinstance(df.columns, pd.MultiIndex):
        df = df.droplevel(1, axis=1)
    cols = [c for c in _OHLC_COLS if c in df.columns]
    df = df[cols].copy()
    idx = pd.to_datetime(df.index)
    if idx.tz is not None:
        idx = idx.tz_convert(None)  # tz-aware → naive UTC equivalent
    df.index = idx.normalize()
    return df.sort_index().dropna(subset=["Close"])


async def _fetch_one_yf(
    spec: SymbolSpec,
    start: date,
    end: date,
) -> tuple[str, pd.DataFrame | None]:
    """Fetch one symbol from yfinance in a thread pool worker."""
    import yfinance as yf  # deferred — not always installed

    ib_sym = spec["symbol"]
    # "yf_ticker" key allows callers to override the computed ticker (e.g. for .L retry)
    yf_tick = spec.get("yf_ticker") or ib_to_yf(
        ib_sym, spec.get("exchange", "SMART"), spec.get("currency", "USD")
    )

    def _dl() -> pd.DataFrame:
        return yf.Ticker(yf_tick).history(
            start=start.isoformat(),
            end=(end + timedelta(days=1)).isoformat(),
            auto_adjust=True,
            actions=False,
        )

    try:
        df = await asyncio.to_thread(_dl)
        if df is None or df.empty:
            return ib_sym, None
        cleaned = _clean_df(df)
        return ib_sym, (cleaned if not cleaned.empty else None)
    except Exception as exc:
        logger.debug("yf {}: {}", yf_tick, exc)
        return ib_sym, None


async def _fetch_yf_all(
    specs: list[SymbolSpec],
    start: date,
    end: date,
    log_fh: TextIO,
) -> dict[str, pd.DataFrame]:
    """Bounded-concurrency yfinance fetch with inline rich progress."""
    sem = asyncio.Semaphore(_CONCURRENCY)
    results: dict[str, pd.DataFrame] = {}

    with progress_bar(len(specs), "OHLC yfinance", unit="sym", file=log_fh) as pbar:

        async def _bounded(spec: SymbolSpec) -> None:
            async with sem:
                ib_sym, df = await _fetch_one_yf(spec, start, end)
            if df is not None:
                results[ib_sym] = df
            pbar.set_postfix_str(spec["symbol"], refresh=False)
            pbar.update(1)

        await asyncio.gather(*[_bounded(s) for s in specs])

    return results


# ---------------------------------------------------------------------------
# IBKR fallback  (separate CID — never conflicts with dashboard CID=10)
# ---------------------------------------------------------------------------


async def _fetch_ib_all(
    specs: list[SymbolSpec],
    start: date,
    end: date,
    log_fh: TextIO,
    host: str = "127.0.0.1",
    port: int = 1300,
    client_id: int = 12,
) -> dict[str, pd.DataFrame]:
    """Fetch daily bars via IBKR reqHistoricalDataAsync for symbols yfinance missed.

    Runs requests concurrently behind a small semaphore: IBKR historical-data
    pacing is stricter than market data (≤ ~6 simultaneous; ~60 reqs / 10 min per
    contract), so concurrency is capped at _IB_HIST_CONCURRENCY and pacing
    violations (error 162) are retried once after a short backoff.
    """
    if not specs:
        return {}

    results: dict[str, pd.DataFrame] = {}
    duration_days = (end - start).days + 5
    duration_str = f"{max(duration_days, 30)} D"

    ib = IB()
    try:
        await ib.connectAsync(host, port, clientId=client_id)
    except Exception as exc:
        log_fh.write(f"IBKR fallback: could not connect ({exc})\n")
        log_fh.flush()
        return {}

    sem = asyncio.Semaphore(_IB_HIST_CONCURRENCY)

    async def _one_bars(contract):
        """Single historical-data request with timeout + one pacing retry."""
        for attempt in (1, 2):
            try:
                return await asyncio.wait_for(
                    ib.reqHistoricalDataAsync(
                        contract,
                        endDateTime="",
                        durationStr=duration_str,
                        barSizeSetting="1 day",
                        whatToShow="ADJUSTED_LAST",
                        useRTH=True,
                        formatDate=1,
                    ),
                    timeout=_IB_HIST_TIMEOUT,
                )
            except asyncio.TimeoutError:
                if attempt == 1:
                    continue  # one retry on a slow/dropped response
                raise
            except Exception as exc:
                # Error 162 == historical-data pacing violation → brief backoff + retry.
                if attempt == 1 and "162" in str(exc):
                    await asyncio.sleep(_IB_PACING_BACKOFF)
                    continue
                raise

    try:
        with progress_bar(len(specs), "OHLC IBKR fallback", unit="sym", file=log_fh) as pbar:

            async def _bounded(spec: SymbolSpec) -> None:
                sym = spec["symbol"]
                exchange = spec.get("exchange", "SMART")
                currency = spec.get("currency", "USD")
                contract = Stock(
                    sym, exchange if exchange not in ("SMART", "") else "SMART", currency
                )
                async with sem:
                    try:
                        bars = await _one_bars(contract)
                    except Exception as exc:
                        logger.warning("IBKR history {}: {}", sym, exc)
                        bars = None
                if bars:
                    rows = [
                        {
                            "date": b.date,
                            "Open": b.open,
                            "High": b.high,
                            "Low": b.low,
                            "Close": b.close,
                            "Volume": b.volume,
                        }
                        for b in bars
                    ]
                    df = pd.DataFrame(rows).set_index("date")
                    df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
                    df = df.sort_index().dropna(subset=["Close"])
                    df = df[df.index.date >= start]
                    if not df.empty:
                        results[sym] = df
                pbar.set_postfix_str(sym, refresh=False)
                pbar.update(1)

            await asyncio.gather(*[_bounded(s) for s in specs])
    finally:
        ib.disconnect()

    return results


# ---------------------------------------------------------------------------
# Incremental merge
# ---------------------------------------------------------------------------


def _merge(
    existing: dict[str, pd.DataFrame],
    new_results: dict[str, pd.DataFrame],
    fetch_plan: dict[str, tuple[SymbolSpec, date]],
    cutoff: date,
) -> dict[str, int]:
    """
    Merge freshly-fetched DataFrames into `existing` (mutated in place).
    Returns dict[symbol → new_row_count].
    """
    added: dict[str, int] = {}
    for sym, df_new in new_results.items():
        fetch_start = fetch_plan[sym][1]
        df_new = df_new[df_new.index.date >= fetch_start]
        if df_new.empty:
            continue
        if sym in existing and not existing[sym].empty:
            new_rows = df_new[df_new.index > existing[sym].index.max()]
            if new_rows.empty:
                continue
            existing[sym] = pd.concat([existing[sym], new_rows]).sort_index()
            added[sym] = len(new_rows)
        else:
            # Use fetch_start (not cutoff) so benchmark symbols store their full history
            df_trimmed = df_new[df_new.index.date >= fetch_start]
            existing[sym] = df_trimmed
            added[sym] = len(df_trimmed)
    return added


# ---------------------------------------------------------------------------
# Main async driver
# ---------------------------------------------------------------------------


async def _run_async(specs: list[SymbolSpec], log_fh: TextIO) -> None:
    existing = load_ohlc()
    today = date.today()
    cutoff = today - timedelta(days=MIN_DAYS)

    # Benchmark backfill: remove SPY/QQQ from existing if they don't reach BENCHMARK_START
    # so the fetch plan treats them as new symbols and re-fetches their full history.
    for _bsym in _BENCHMARK_SYMS:
        if _bsym in existing and not existing[_bsym].empty:
            if existing[_bsym].index.min().date() > _BENCHMARK_START:
                del existing[_bsym]

    # Build per-symbol fetch plan: symbol → (spec, start_date)
    fetch_plan: dict[str, tuple[SymbolSpec, date]] = {}
    up_to_date = 0
    for spec in specs:
        sym = spec["symbol"]
        if sym in existing and not existing[sym].empty:
            last = existing[sym].index.max().date()
            if last >= today - timedelta(days=1):
                up_to_date += 1
                continue
            fetch_start = last + timedelta(days=1)
        else:
            fetch_start = _BENCHMARK_START if sym in _BENCHMARK_SYMS else cutoff
        fetch_plan[sym] = (spec, fetch_start)

    # Inject benchmark symbols not covered by specs (e.g. QQQ not in S&P 500 list)
    _spec_syms = {s["symbol"] for s in specs}
    for _bspec in _BENCHMARK_SPECS:
        _bsym = _bspec["symbol"]
        if _bsym in fetch_plan or _bsym in _spec_syms:
            continue
        if _bsym in existing and not existing[_bsym].empty:
            _last = existing[_bsym].index.max().date()
            if _last >= today - timedelta(days=1):
                up_to_date += 1
                continue
            fetch_plan[_bsym] = (_bspec, _last + timedelta(days=1))
        else:
            fetch_plan[_bsym] = (_bspec, _BENCHMARK_START)

    log_fh.write(
        f"Symbols : {len(specs)} total  |  "
        f"{up_to_date} already current  |  "
        f"{len(fetch_plan)} to fetch\n"
    )
    log_fh.flush()

    if not fetch_plan:
        log_fh.write("All symbols up to date — nothing to fetch\n")
        log_fh.flush()
        return

    earliest = min(d for _, d in fetch_plan.values())
    all_specs = [spec for spec, _ in fetch_plan.values()]

    log_fh.write(f"Date range : {earliest} → {today}\n\n")
    log_fh.flush()

    # Apply persisted yfinance ticker overrides (e.g. LSE ETFs: CSPX → CSPX.L).
    # Overrides are learned automatically from successful .L retries below.
    _ov = _load_yf_overrides()
    _ov_applied = 0
    for spec in all_specs:
        if spec["symbol"] in _ov and "yf_ticker" not in spec:
            spec["yf_ticker"] = _ov[spec["symbol"]]
            _ov_applied += 1
    if _ov_applied:
        log_fh.write(f"Applied {_ov_applied} persisted ticker override(s)\n")
        log_fh.flush()

    # ── yfinance pass ────────────────────────────────────────────────────────
    yf_results = await _fetch_yf_all(all_specs, earliest, today, log_fh)

    yf_failed = [spec for spec, _ in fetch_plan.values() if spec["symbol"] not in yf_results]
    log_fh.write(f"\nyfinance: {len(yf_results)} OK, {len(yf_failed)} failed\n")
    log_fh.flush()

    # ── Pre-IBKR retry: try .L suffix for bare symbols (e.g. CSPX → CSPX.L) ─
    # When IBKR reports primaryExch=SMART for LSE ETFs, ib_to_yf produces a bare
    # ticker with no suffix.  Try appending .L before falling back to the slow
    # IBKR historical-data path.
    if yf_failed:
        _bare_failed = [
            spec
            for spec in yf_failed
            if "."
            not in ib_to_yf(
                spec["symbol"], spec.get("exchange", "SMART"), spec.get("currency", "USD")
            )
        ]
        if _bare_failed:
            _lse_specs = [
                {**s, "yf_ticker": s["symbol"].replace(" ", "-") + ".L"} for s in _bare_failed
            ]
            log_fh.write(
                f"  Retrying {len(_lse_specs)} as .L: "
                f"{', '.join(s['symbol'] for s in _lse_specs[:10])}"
                f"{'…' if len(_lse_specs) > 10 else ''}\n"
            )
            log_fh.flush()
            _lse_results = await _fetch_yf_all(_lse_specs, earliest, today, log_fh)
            yf_results.update(_lse_results)
            yf_failed = [spec for spec in yf_failed if spec["symbol"] not in yf_results]
            log_fh.write(
                f"  After .L retry: {len(_lse_results)} recovered, "
                f"{len(yf_failed)} still need IBKR fallback\n"
            )
            log_fh.flush()
            # Persist successful .L mappings so next run fetches them directly.
            if _lse_results:
                _ov = _load_yf_overrides()
                for s in _lse_specs:
                    if s["symbol"] in _lse_results:
                        _ov[s["symbol"]] = s["yf_ticker"]
                _save_yf_overrides(_ov)
                log_fh.write(
                    f"  Saved {len(_lse_results)} ticker override(s) → "
                    f"{_YF_OVERRIDES_PATH.name}\n"
                )
                log_fh.flush()

    if yf_failed:
        log_fh.write(
            f"  IBKR fallback needed: "
            f"{', '.join(s['symbol'] for s in yf_failed[:15])}"
            f"{'…' if len(yf_failed) > 15 else ''}\n"
        )
        log_fh.flush()

    # ── IBKR fallback ────────────────────────────────────────────────────────
    ib_results: dict[str, pd.DataFrame] = {}
    if yf_failed:
        try:
            ib_results = await _fetch_ib_all(yf_failed, earliest, today, log_fh)
        except Exception as exc:
            log_fh.write(f"IBKR fallback error: {exc}\n")
            log_fh.flush()

    # ── Merge & save ──────────────────────────────────────────────────────────
    all_results = {**yf_results, **ib_results}
    added = _merge(existing, all_results, fetch_plan, cutoff)
    save_ohlc(existing)

    still_missing = [s["symbol"] for s in yf_failed if s["symbol"] not in ib_results]
    log_fh.write(
        f"\nOHLC UPDATE COMPLETE\n"
        f"  Updated : {len(added)} symbols, {sum(added.values())} rows added\n"
        f"  Store   : {len(existing)} symbols, "
        f"oldest bar {min((df.index.min().date() for df in existing.values() if not df.empty), default='?')}\n"
    )
    if still_missing:
        log_fh.write(
            f"  Still missing ({len(still_missing)}): "
            f"{', '.join(still_missing[:20])}{'…' if len(still_missing) > 20 else ''}\n"
        )
    log_fh.flush()


# ---------------------------------------------------------------------------
# Public entry point  (called by fetch_ohlc.py subprocess)
# ---------------------------------------------------------------------------


def run_update(log_path: Path | None = None) -> None:
    """Incremental OHLC update.  Called by the fetch_ohlc.py runner."""
    specs = load_symbol_list()
    if not specs:
        print(
            "No symbols found. Run build.py first to generate data/symbols.pkl, "
            "or click Generate OHLCs from the dashboard (it writes ohlc_symbols.json).",
            flush=True,
        )
        sys.exit(1)

    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_fh: TextIO = open(log_path, "w", encoding="utf-8")  # noqa: SIM115
    else:
        log_fh = sys.stdout  # type: ignore[assignment]

    try:
        log_fh.write(f"OHLC update starting — {len(specs)} symbols\n")
        log_fh.flush()
        asyncio.run(_run_async(specs, log_fh))
    except Exception as exc:
        log_fh.write(f"\nFATAL: {exc}\n")
        log_fh.flush()
        raise
    finally:
        if log_path:
            log_fh.close()
