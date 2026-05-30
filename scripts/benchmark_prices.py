"""benchmark_prices.py — Compare IBKR vs yfinance price/volatility fetch speed.

Run:
    uv run python scripts/benchmark_prices.py [--port 1300] [--symbols AAPL MSFT ...]

Measures wall-clock time and data quality for each source across multiple batch sizes.
Requires a live IBKR connection (TWS or IB Gateway).
"""

from __future__ import annotations

import argparse
import asyncio
import time
from datetime import datetime

import pandas as pd
import yfinance as yf

# Default S&P 500 test pool — covers a mix of sectors and price ranges
_DEFAULT_SYMBOLS = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "META", "TSLA", "NVDA", "NFLX", "AMD", "INTC",
    "JPM",  "BAC",  "GS",   "MS",   "WFC",  "C",    "AXP",  "BLK",  "BX",  "SCHW",
    "JNJ",  "PFE",  "MRK",  "ABBV", "UNH",  "CVS",  "LLY",  "BMY",  "AMGN","GILD",
    "XOM",  "CVX",  "COP",  "EOG",  "SLB",  "PSX",  "MPC",  "VLO",  "OXY", "HAL",
    "AMZN", "WMT",  "HD",   "COST", "TGT",  "NKE",  "MCD",  "SBUX", "YUM", "CMG",
    "BA",   "LMT",  "RTX",  "NOC",  "GD",   "CAT",  "DE",   "MMM",  "HON", "GE",
    "AAPL", "MSFT", "GOOGL","AMZN", "META", "TSLA", "NVDA", "NFLX", "AMD", "INTC",
    "PYPL", "V",    "MA",   "SQ",   "ADBE", "CRM",  "NOW",  "SNOW", "PLTR","DDOG",
    "SPY",  "QQQ",  "IWM",  "GLD",  "TLT",  "SHY",  "HYG",  "LQD",  "VNQ", "XLF",
    "BRK-B","ORCL", "IBM",  "HPQ",  "CSCO", "QCOM", "TXN",  "AVGO", "MU",  "KLAC",
]


# ── yfinance ─────────────────────────────────────────────────────────────────

def _fetch_yf_prices(symbols: list[str]) -> dict[str, float | None]:
    """Bulk-fetch latest close prices via yfinance.download()."""
    try:
        raw = yf.download(symbols, period="2d", progress=False, auto_adjust=True)
        close = raw["Close"] if "Close" in raw.columns else raw
        last = close.dropna(how="all").iloc[-1]
        return {sym: (float(last[sym]) if sym in last and pd.notna(last[sym]) else None)
                for sym in symbols}
    except Exception as exc:
        print(f"  [yf] download error: {exc}")
        return {sym: None for sym in symbols}


def _fetch_yf_iv(symbols: list[str]) -> dict[str, float | None]:
    """Fetch front-month ATM IV from yfinance option chain (per-symbol, slower)."""
    result: dict[str, float | None] = {}
    for sym in symbols:
        try:
            t = yf.Ticker(sym)
            exps = t.options
            if not exps:
                result[sym] = None
                continue
            chain = t.option_chain(exps[0])
            spot_info = t.fast_info
            spot = getattr(spot_info, "last_price", None)
            if spot is None:
                result[sym] = None
                continue
            calls = chain.calls
            # find ATM call (closest strike to spot)
            atm_calls = calls.iloc[(calls["strike"] - spot).abs().argsort()[:1]]
            iv = float(atm_calls["impliedVolatility"].iloc[0]) if not atm_calls.empty else None
            result[sym] = iv
        except Exception:
            result[sym] = None
    return result


# ── IBKR ─────────────────────────────────────────────────────────────────────

async def _fetch_ib_prices_async(
    symbols: list[str], port: int, semaphore_n: int
) -> dict[str, float | None]:
    """Fetch last/close prices from IBKR using reqMktData snapshots."""
    from ib_async import IB, Stock

    ib = IB()
    try:
        await ib.connectAsync("127.0.0.1", port, clientId=91)
    except Exception as exc:
        print(f"  [ib] connect error: {exc}")
        return {sym: None for sym in symbols}

    contracts = [Stock(sym, "SMART", "USD") for sym in symbols]
    try:
        await ib.qualifyContractsAsync(*contracts)
    except Exception:
        pass

    sem = asyncio.Semaphore(semaphore_n)
    results: dict[str, float | None] = {}

    async def _one(contract: "Stock") -> None:
        async with sem:
            try:
                ticker = await ib.reqMktDataAsync(contract, "", snapshot=True)
                price = ticker.last or ticker.close or ticker.marketPrice()
                results[contract.symbol] = float(price) if price and price == price else None
            except Exception:
                results[contract.symbol] = None

    await asyncio.gather(*[_one(c) for c in contracts])
    ib.disconnect()
    return results


async def _fetch_ib_iv_async(
    symbols: list[str], port: int, semaphore_n: int
) -> dict[str, float | None]:
    """Fetch model IV from IBKR option ticker data."""
    from ib_async import IB, Stock

    ib = IB()
    try:
        await ib.connectAsync("127.0.0.1", port, clientId=92)
    except Exception as exc:
        print(f"  [ib iv] connect error: {exc}")
        return {sym: None for sym in symbols}

    contracts = [Stock(sym, "SMART", "USD") for sym in symbols]
    try:
        await ib.qualifyContractsAsync(*contracts)
    except Exception:
        pass

    sem = asyncio.Semaphore(semaphore_n)
    results: dict[str, float | None] = {}

    async def _one(contract: "Stock") -> None:
        async with sem:
            try:
                ticker = await ib.reqMktDataAsync(contract, "106", snapshot=True)  # 106 = IV
                iv = None
                if ticker.modelGreeks:
                    iv = ticker.modelGreeks.impliedVol
                results[contract.symbol] = float(iv) if iv else None
            except Exception:
                results[contract.symbol] = None

    await asyncio.gather(*[_one(c) for c in contracts])
    ib.disconnect()
    return results


# ── Benchmark runner ─────────────────────────────────────────────────────────

def _summarise(prices_a: dict, prices_b: dict, symbols: list[str]) -> dict:
    """Compute correlation and per-symbol delta between two price dicts."""
    pairs = [
        (prices_a.get(s), prices_b.get(s))
        for s in symbols
        if prices_a.get(s) is not None and prices_b.get(s) is not None
    ]
    if len(pairs) >= 2:
        import statistics
        deltas = [abs(a - b) / b for a, b in pairs if b > 0]
        corr_vals = [a for a, _ in pairs]
        corr_comp = [b for _, b in pairs]
        # Simple Pearson
        n = len(pairs)
        mx, my = sum(corr_vals)/n, sum(corr_comp)/n
        num = sum((a - mx)*(b - my) for a, b in pairs)
        den = (sum((a - mx)**2 for a in corr_vals) * sum((b - my)**2 for b in corr_comp)) ** 0.5
        corr = num / den if den else 0.0
        avg_delta_pct = statistics.mean(deltas) * 100 if deltas else 0.0
    else:
        corr, avg_delta_pct = float("nan"), float("nan")
    return {
        "n_both": len(pairs),
        "corr":   round(corr, 4),
        "avg_delta_pct": round(avg_delta_pct, 4),
    }


def benchmark(symbols_pool: list[str], batch_sizes: list[int], port: int) -> pd.DataFrame:
    rows: list[dict] = []

    for n in batch_sizes:
        batch = symbols_pool[:n]
        print(f"\n{'─'*60}")
        print(f" Batch size: {n}  ({len(batch)} symbols)")
        print(f"{'─'*60}")

        # ── yfinance price ────────────────────────────────────────────────
        print("  yfinance prices ...", end="", flush=True)
        t0 = time.perf_counter()
        yf_prices = _fetch_yf_prices(batch)
        yf_elapsed = time.perf_counter() - t0
        yf_ok  = sum(1 for v in yf_prices.values() if v is not None)
        yf_nan = n - yf_ok
        print(f" {yf_elapsed:.2f}s  ok={yf_ok}  nan={yf_nan}")

        # ── IBKR price ────────────────────────────────────────────────────
        print("  IBKR prices    ...", end="", flush=True)
        t0 = time.perf_counter()
        ib_prices = asyncio.run(_fetch_ib_prices_async(batch, port, min(n, 40)))
        ib_elapsed = time.perf_counter() - t0
        ib_ok  = sum(1 for v in ib_prices.values() if v is not None)
        ib_nan = n - ib_ok
        print(f" {ib_elapsed:.2f}s  ok={ib_ok}  nan={ib_nan}")

        price_stats = _summarise(yf_prices, ib_prices, batch)

        rows.append({
            "batch": n, "source": "yfinance",
            "elapsed_s": round(yf_elapsed, 2),
            "ok": yf_ok, "nan": yf_nan,
            "type": "price",
            "corr_vs_other": price_stats["corr"],
            "avg_delta_pct": price_stats["avg_delta_pct"],
        })
        rows.append({
            "batch": n, "source": "IBKR",
            "elapsed_s": round(ib_elapsed, 2),
            "ok": ib_ok, "nan": ib_nan,
            "type": "price",
            "corr_vs_other": price_stats["corr"],
            "avg_delta_pct": price_stats["avg_delta_pct"],
        })

        # ── yfinance IV (per-symbol, slow — only for small batches) ──────
        if n <= 20:
            print("  yfinance IV    ...", end="", flush=True)
            t0 = time.perf_counter()
            yf_iv = _fetch_yf_iv(batch)
            yf_iv_elapsed = time.perf_counter() - t0
            yf_iv_ok = sum(1 for v in yf_iv.values() if v is not None)
            print(f" {yf_iv_elapsed:.2f}s  ok={yf_iv_ok}/{n}")

            print("  IBKR IV        ...", end="", flush=True)
            t0 = time.perf_counter()
            ib_iv = asyncio.run(_fetch_ib_iv_async(batch, port, min(n, 40)))
            ib_iv_elapsed = time.perf_counter() - t0
            ib_iv_ok = sum(1 for v in ib_iv.values() if v is not None)
            print(f" {ib_iv_elapsed:.2f}s  ok={ib_iv_ok}/{n}")

            iv_stats = _summarise(yf_iv, ib_iv, batch)

            rows.append({
                "batch": n, "source": "yfinance",
                "elapsed_s": round(yf_iv_elapsed, 2),
                "ok": yf_iv_ok, "nan": n - yf_iv_ok,
                "type": "IV",
                "corr_vs_other": iv_stats["corr"],
                "avg_delta_pct": iv_stats["avg_delta_pct"],
            })
            rows.append({
                "batch": n, "source": "IBKR",
                "elapsed_s": round(ib_iv_elapsed, 2),
                "ok": ib_iv_ok, "nan": n - ib_iv_ok,
                "type": "IV",
                "corr_vs_other": iv_stats["corr"],
                "avg_delta_pct": iv_stats["avg_delta_pct"],
            })

    return pd.DataFrame(rows)


def _print_table(df: pd.DataFrame) -> None:
    pd.set_option("display.width", 120)
    pd.set_option("display.max_columns", 20)
    print("\n" + "=" * 90)
    print(" BENCHMARK RESULTS")
    print("=" * 90)
    for data_type in df["type"].unique():
        sub = df[df["type"] == data_type].copy()
        print(f"\n── {data_type.upper()} PRICES ──")
        print(sub.to_string(index=False))
    print("=" * 90)
    print("\nCorr = Pearson correlation of prices between sources (same batch).")
    print("AvgDelta% = mean absolute % price difference between yfinance and IBKR.")
    print("Elapsed_s = wall-clock seconds for that source/batch combination.")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Benchmark IBKR vs yfinance price/IV fetch.")
    p.add_argument("--port",    type=int,   default=1300,
                   help="IBKR TWS/Gateway port (default 1300)")
    p.add_argument("--batches", type=int,   nargs="+", default=[5, 10, 25, 50, 100],
                   help="Batch sizes to test (default: 5 10 25 50 100)")
    p.add_argument("--symbols", type=str,   nargs="+", default=_DEFAULT_SYMBOLS,
                   help="Symbol pool (first N are used per batch)")
    args = p.parse_args()

    print(f"Benchmark — {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"Port={args.port}  Batches={args.batches}  Pool={len(args.symbols)} symbols")

    results = benchmark(args.symbols, args.batches, args.port)
    _print_table(results)

    out_path = "data/benchmark_prices.csv"
    results.to_csv(out_path, index=False)
    print(f"\nSaved → {out_path}")
