#%%
# IMPORTS

import asyncio
import json
import logging
import math
import os
import pickle
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from itertools import product
from pathlib import Path
from typing import Dict, List, Optional

import nest_asyncio
import numpy as np
import pandas as pd
# pyrefly: ignore [untyped-import]
import yaml
from dotenv import find_dotenv, load_dotenv
from ib_async import IB, Contract, Stock
from pyprojroot import here
# pyrefly: ignore [untyped-import]
from src.dashboard.progress import progress_bar

logger = logging.getLogger(__name__)

# Apply nest_asyncio to allow nested event loops
nest_asyncio.apply()


#%% 
# Configuration functions

class Timediff:
    def __init__(
        self, td: timedelta, days: int, hours: int, minutes: int, seconds: float
    ):
        self.td = td
        self.days = days
        self.hours = hours
        self.minutes = minutes
        self.seconds = seconds

def load_config(market: str) -> dict:
    """
    Load configuration for a specific market from YAML and environment variables.
    
    Args:
        market: Market name (e.g., 'SNP', 'NSE')
    
    Returns:
        Dictionary with configuration values
    """
    dotenv_path = find_dotenv()
    load_dotenv(dotenv_path=dotenv_path)

    config_path = ROOT / "config" / f"{market.lower()}_config.yml"
    with open(config_path, "r") as f:
        config = yaml.safe_load(f)

    # Override config with environment variables if they exist
    for key, value in os.environ.items():
        if key in config:
            config[key] = value
    
    return config

ROOT = here()
config = load_config("SNP")
MAX_DTE = config.get("MAX_DTE")
PORT = config.get("PORT", 1300)
# Market-data type for bulk fetches: primary (frozen) works live + off-hours for
# subscribed symbols; fallback (delayed-frozen) fills the rest with free delayed data.
MKT_DATA_TYPE = int(config.get("MKT_DATA_TYPE", 2))
MKT_DATA_FALLBACK = int(config.get("MKT_DATA_FALLBACK", 4))

def get_ib_connection(market: str = "SNP", account_no: str = '', msg: bool=False) -> IB:
    """
    Create and return an IB connection using config settings.
    
    Args:
        market: Market name to load config for (default: 'SNP')
        account: Account code to receive updates for (default: '')
    
    Returns:
        Connected IB instance
    """
    config = load_config(market)
    client_id = config.get("CID", 10)
    
    ib = IB()
    
    max_retries = 5
    for attempt in range(max_retries):
        try:
            ib.connect('127.0.0.1', PORT, clientId=client_id, account=account_no)
            if msg:
                logger.info(
                    "Connected to IB on port %s with clientId %s (market: %s, account: %s)",
                    PORT, client_id, market, account_no or "default",
                )
            return ib
        except Exception as e:
            if attempt < max_retries - 1:
                logger.warning("Connection attempt %s/%s failed (%s), retrying in 2 s", attempt + 1, max_retries, e)
                time.sleep(2)
            else:
                raise

def get_safe_ib_connection(client_id, max_retries=3):
    """Create a new IB connection with retry logic."""
    for attempt in range(max_retries):
        try:
            ib = IB()
            ib.connect('127.0.0.1', PORT, clientId=client_id)  # Use TWS paper trading port
            if ib.isConnected():
                return ib
            logger.warning("Connection attempt %s/%s failed: Not connected", attempt + 1, max_retries)
        except Exception as e:
            logger.warning("Connection attempt %s/%s failed: %s", attempt + 1, max_retries, e)
            time.sleep(2)
    raise ConnectionError(f"Failed to connect to IB after {max_retries} attempts")

#%%
# CONSTANTS

USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
SNP_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
WEEKLYS_URL = "http://www.cboe.com/products/weeklys-options/available-weeklys"
ADDITIONAL_SYMBOLS = ["QQQ", "SPY"]

#%%
# Utility functions

def delete_pkl_files(files_to_delete):
    for filename in files_to_delete:
        if not filename.endswith('.pkl'):
            filename += '.pkl'
        file_path = ROOT / "data" / filename
        if file_path.exists():
            file_path.unlink()
            logger.debug("Deleted %s", file_path)

def pickle_me(obj, file_path: Path):
    # Atomic write: dump to a sibling temp file then os.replace, so a crash
    # mid-write can never leave a half-written / corrupt pickle behind.
    file_path = Path(file_path)
    tmp_path = file_path.with_suffix(file_path.suffix + ".tmp")
    with open(str(tmp_path), "wb") as handle:
        pickle.dump(obj, handle, protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(str(tmp_path), str(file_path))

def get_pickle(path: Path, print_msg: bool = True):
    try:
        with open(path, "rb") as f:
            output = pickle.load(f)
            logger.debug("Loaded %s", path)
            return output
    except FileNotFoundError:
        if print_msg:
            logger.warning("File not found: %s", path)
        return None

def do_i_refresh(my_path: Path, max_days: float) -> bool:
    """
    Decides whether to refresh the unds data or not based on how many days old it is.
    """
    days_old = how_many_days_old(my_path)

    return days_old is None or days_old > max_days

def how_many_days_old(file_path: Path) -> float:
    file_age = get_file_age(file_path=file_path)

    seconds_in_a_day = 86400
    file_age_in_days = (
        file_age.td.total_seconds() / seconds_in_a_day if file_age else None
    )

    # pyrefly: ignore [bad-return]
    return file_age_in_days

def get_file_age(file_path: Path) -> Optional[Timediff]:
    if not file_path.exists():
        logger.info(f"{file_path} file is not found")
        return None

    file_time = datetime.fromtimestamp(file_path.stat().st_mtime)
    time_now = datetime.now()
    td = time_now - file_time

    return split_time_difference(td)

def split_time_difference(diff: timedelta) -> Timediff:
    days = diff.days
    hours, remainder = divmod(diff.seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    seconds += diff.microseconds / 1e6

    return Timediff(diff, days, hours, minutes, seconds)

# pyrefly: ignore [non-convergent-recursion]
def get_dte(date_input):
    """
    Calculate days to expiration from a date string or pandas Series of date strings.
    
    Args:
        date_input (str or pd.Series): Date string(s) in 'YYYYMMDD' format
    
    Returns:
        float, pd.Series, or None: Number of days from option closing time to current time in UTC,
                                  or None if input is not a string or is null
    """
    # Handle None or non-string, non-Series input
    if date_input is None or (not isinstance(date_input, (str, pd.Series))):
        return None
        
    # If input is a pandas Series, apply the function to each element
    if isinstance(date_input, pd.Series):
        return date_input.apply(get_dte)
    
    # Take first 8 characters if string is longer
    # pyrefly: ignore [unnecessary-type-conversion]
    date_str = str(date_input)[:8]
    
    # Parse the date
    try:
        year = int(date_str[:4])
        month = int(date_str[4:6])
        day = int(date_str[6:8])
    except (ValueError, IndexError):
        return None
    
    # Create datetime object at option closing time (4 PM market close)
    try:
        expiry_datetime = datetime(year, month, day, 16, 0, 0, tzinfo=timezone.utc)
    except (ValueError, OverflowError):
        return None
    
    # Get current time in UTC
    current_time = datetime.now(timezone.utc)
    
    # Calculate time difference and convert to days
    time_diff = expiry_datetime - current_time
    days_to_expiry = time_diff.total_seconds() / (24 * 3600)
    
    return days_to_expiry

def get_prec(v: float, base: float) -> float:
    try:
        # pyrefly: ignore [unnecessary-type-conversion]
        output = round(round((v) / base) * base, -int(math.floor(math.log10(base))))
    except Exception:
        output = None

    # pyrefly: ignore [bad-return]
    return output


def get_prec_safe(v: Optional[float], base: float) -> Optional[float]:
    """Graceful-degradation wrapper for get_prec: returns None for None/NaN/±inf/bad base."""
    if v is None:
        return None
    try:
        if math.isnan(v) or math.isinf(v):
            return None
    except (TypeError, ValueError):
        return None
    try:
        return round(round(v / base) * base, -int(math.floor(math.log10(base))))
    except Exception:
        return None


def _is_valid_price(price) -> bool:
    """Return True when price is a finite non-sentinel float (not None, NaN, ±inf, or -1.0)."""
    if price is None:
        return False
    try:
        return price != -1.0 and not math.isnan(price) and not math.isinf(price)
    except (TypeError, ValueError):
        return False

def atm_margin(strike, undPrice, dte, vy):
    """
    Calculates the margin for an at-the-money put sale.

    Parameters:
    strike (float): The strike price of the put option.
    undPrice (float): The underlying asset price.
    dte (int): The number of days to expiration.
    vy (float): The volatility of the underlying asset.

    Returns:
    float: The margin for the put sale.
    """
    from scipy.stats import norm  # deferred: scipy import only needed here

    # Calculate the time to expiration in years
    t = dte / 365

    # Calculate the delta of the put option
    d1 = (np.log(undPrice / strike) + (vy**2 / 2) * t) / (vy * np.sqrt(t))
    delta = -norm.cdf(d1)
    
    # Calculate the margin
    margin = strike * 100 * abs(delta)
    
    return margin

#%%
# Cached helper functions
@lru_cache(maxsize=1)
def _fetch_snp_symbols() -> pd.Series:
    """Fetch S&P 500 symbols from Wikipedia (cached)."""
    import io
    import requests
    import urllib3
    headers = {'User-Agent': USER_AGENT}
    # Try with SSL verification first; fall back to verify=False if cert store is stale.
    for verify in (True, False):
        try:
            if not verify:
                urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
            resp = requests.get(SNP_URL, headers=headers, verify=verify, timeout=15)
            resp.raise_for_status()
            snp_table = pd.read_html(
                io.StringIO(resp.text),
                header=0,
                attrs={"id": "constituents"},
                flavor='lxml',
            )[0]
            return snp_table["Symbol"]
        except Exception as e:
            if verify:
                logger.warning(f"S&P 500 fetch with SSL verification failed ({e}); retrying without verification")
            else:
                logger.error(f"Failed to retrieve S&P 500 symbols: {e}")
    return pd.Series(dtype=str)

@lru_cache(maxsize=1)
def _fetch_weeklys() -> pd.Series:
    """Fetch weekly options symbols from CBOE (cached). Secondary fallback only."""
    try:
        return pd.read_html(WEEKLYS_URL)[0].iloc[:, 1]
    except Exception as e:
        logger.warning(f"CBOE weeklys fetch failed (URL may have moved): {e}")
        return pd.Series(dtype=str)
    

async def _async_fetch_weeklies_yf(symbols, look_ahead_days=MAX_DTE):
    """Filter S&P 500 symbols for those with weekly options (non-third-Friday expirations).

    Args:
        symbols (pd.Series): S&P 500 ticker symbols (Name: Symbol, dtype: object).
        look_ahead_days (int): Days to check for expirations (default: 45).

    Returns:
        pd.Series: Symbols with weekly options or error messages.
    """
    import yfinance as yf  # deferred: heavy import, only needed in this function
    today = datetime.now().date()
    # pyrefly: ignore [bad-argument-type]
    cutoff = today + timedelta(days=look_ahead_days)
    weekly_symbols = []

    def check_symbol(symbol):
        """Synchronous helper to check one symbol's options."""
        try:
            ticker = yf.Ticker(str(symbol))
            exps = ticker.options
            if exps:
                for exp in exps:
                    exp_date = datetime.strptime(exp, "%Y-%m-%d").date()
                    if today < exp_date <= cutoff and not (15 <= exp_date.day <= 21 and exp_date.weekday() == 4):
                        return symbol
            return None
        except Exception as e:
            return f"Error for {symbol}: {str(e)}"

    async def process_symbol(symbol, executor):
        """Run synchronous check_symbol in thread pool."""
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(executor, check_symbol, symbol)
        return result

    async def main():
        with ThreadPoolExecutor(max_workers=10) as executor:  # Limit concurrency to avoid rate limits
            tasks = [process_symbol(symbol, executor) for symbol in symbols.values]
            for result in await asyncio.gather(*tasks):
                if result:  # Skip None results
                    weekly_symbols.append(result)

    await main()
    return pd.Series(weekly_symbols, name="Symbol").sort_values()

#%%
# Main functions
def get_option_symbols(weeklies: bool = True) -> pd.Series:
    """
    Get S&P 500 symbols, optionally filtered for weekly options.
    
    Args:
        weeklies: If True, return only symbols with weekly options (plus QQQ/SPY).
                  If False, return all S&P 500 symbols.
    
    Returns:
        Series of stock symbols
    """
    try:
        snp_symbols = _fetch_snp_symbols()  # Cached network call
        
        # Return all S&P symbols if not filtering for weeklies
        if not weeklies:
            return snp_symbols
        
        # Fetch weekly options and filter
        weeklys_data = asyncio.run(_async_fetch_weeklies_yf(snp_symbols))  # Async call
        if weeklys_data.empty:
            weeklys_data = _fetch_weeklys()  # Cached network call as backup
        filtered = weeklys_data[weeklys_data.isin(snp_symbols) & weeklys_data.str.isalpha()]
        
        # Add additional symbols and return
        return pd.concat([filtered, pd.Series(ADDITIONAL_SYMBOLS)], ignore_index=True)
        
    except Exception as e:
        logger.error(f"Failed to retrieve option symbols: {e}")
        return pd.Series(ADDITIONAL_SYMBOLS if weeklies else [], dtype=str)

async def _qualify_batch(ib: IB, contracts: list, advance_fn=None) -> tuple:
    """Async helper to qualify contracts in batch."""
    qualified = []
    failed = []

    tasks = [ib.qualifyContractsAsync(contract) for contract in contracts]

    for i, task in enumerate(asyncio.as_completed(tasks)):
        try:
            result = await task
            if result:
                qualified.extend(result)
            else:
                failed.append(contracts[i].symbol)
        except Exception:
            failed.append(contracts[i].symbol)

        if advance_fn:
            advance_fn()

    return qualified, failed

def qualify_stock_contracts(symbols: pd.Series, market: str = "SNP") -> list:
    """
    Qualify stock contracts with Interactive Brokers using async batch processing.
    
    Args:
        symbols: Series of stock symbols to qualify
        market: Market name for config loading (default: 'SNP')
    
    Returns:
        List of qualified Stock contracts
    """
    ib = None
    
    try:
        # Connect to IB using config
        ib = get_ib_connection(market)
        
        # Create stock contracts
        contracts = [Stock(symbol, 'SMART', 'USD') for symbol in symbols]
        
        logger.info("Qualifying %s contracts via IBKR…", len(contracts))
        with progress_bar(len(contracts), "Qualifying symbols", unit="sym", file=sys.stderr) as pbar:
            # pyrefly: ignore [not-iterable]
            qualified, failed = ib.run(_qualify_batch(ib, contracts, lambda: pbar.update(1)))

        logger.info("Qualified %s/%s contracts", len(qualified), len(contracts))
        if failed:
            extra = f" + {len(failed) - 10} more" if len(failed) > 10 else ""
            logger.warning("Failed to qualify %s symbols: %s%s", len(failed), ", ".join(failed[:10]), extra)

        return qualified

    except Exception as e:
        logger.error("Failed to qualify contracts: %s", e)
        return []

    finally:
        if ib and ib.isConnected():
            ib.disconnect()
            logger.debug("Disconnected from IB")

def qualify_me(
    ib: IB, 
    data: list, 
    desc: str = "Qualifying contracts",
    batch_size: int = 50
) -> list:
    """
    Qualify a list of contracts with Interactive Brokers using async batch processing.
    Processes in smaller batches to prevent event loop errors and rate limiting.
    Removed progress bar to avoid widget CDN issues in restricted environments.
    """
    try:
        # Ensure data is a list
        contracts = list(data) if isinstance(data, (list, pd.Series)) else data

        qualified = []
        failed_total = []

        total_contracts = len(contracts)
        if total_contracts == 0:
            logger.warning("No contracts to qualify")  # no format args needed
            return []

        # Calculate number of batches
        num_batches = (total_contracts + batch_size - 1) // batch_size

        with progress_bar(total_contracts, desc, unit="sym", file=sys.stderr) as pbar:
            for batch_num in range(num_batches):
                start_idx = batch_num * batch_size
                end_idx = min(start_idx + batch_size, total_contracts)
                batch_contracts = contracts[start_idx:end_idx]

                # Per-batch try with one automatic retry on transient socket errors.
                _batch_ok = False
                for _attempt in range(2):
                    try:
                        # pyrefly: ignore [not-iterable]
                        q_batch, f_batch = ib.run(_qualify_batch(ib, batch_contracts, None))
                        qualified.extend(q_batch)
                        failed_total.extend(f_batch)
                        _batch_ok = True
                        break
                    except Exception as _be:
                        if _attempt == 0:
                            logger.warning(
                                "Batch %s/%s error (%s) — retrying in 3 s…",
                                batch_num + 1, num_batches, _be,
                            )
                            time.sleep(3)
                        else:
                            logger.warning(
                                "Batch %s/%s failed (%s), skipping",
                                batch_num + 1, num_batches, _be,
                            )
                            failed_total.extend(
                                getattr(c, "symbol", repr(c)) for c in batch_contracts
                            )

                # If socket is gone after a failed batch, stop early rather than
                # hammering through the remaining batches with guaranteed failures.
                if not _batch_ok and not ib.isConnected():
                    logger.warning("IB socket disconnected — stopping qualification early")  # no args
                    remaining = contracts[end_idx:]
                    failed_total.extend(getattr(c, "symbol", repr(c)) for c in remaining)
                    pbar.update(len(batch_contracts))
                    break

                if batch_num < num_batches - 1:
                    ib.sleep(1)  # 1-second pause to avoid rate limiting
                pbar.update(len(batch_contracts))

        if failed_total:
            extra = f" + {len(failed_total) - 10} more" if len(failed_total) > 10 else ""
            logger.warning(
                "Failed to qualify %s symbols: %s%s",
                len(failed_total), ", ".join(str(s) for s in failed_total[:10]), extra,
            )

        return qualified

    except Exception as e:
        logger.error("Failed to qualify contracts: %s", e)
        return []

def get_qualified_symbols(weeklies: bool = True, market: str = "SNP", save: bool = True) -> list:
    """
    Get option symbols and qualify them as stock contracts with IB.
    
    Args:
        weeklies: If True, get weekly options symbols; if False, all S&P 500
        market: Market name for config loading (default: 'SNP')
        save: If True, save the symbols to a pickle file at ROOT/'data'/df_symbols
    
    Returns:
        List of qualified Stock contracts
    """
    symbols = get_option_symbols(weeklies=weeklies)
    logger.info("Retrieved %s symbols (weeklies=%s)", len(symbols), weeklies)

    contracts = qualify_stock_contracts(symbols, market=market)

    # Normalize tradingClass for contracts with 'NMS'
    contracts = normalize_trading_class(contracts)
    contracts = [c for c in contracts if c is not None]
    
    if save:
        pickle_me(contracts, file_path=ROOT/'data'/'symbols.pkl')
    
    return contracts

#%%
# Contract Prices — Dual-Source Hybrid Pipeline helpers


async def _fetch_prices_bulk_yf(symbols: List[str]) -> Dict[str, float]:
    """Primary source: bulk last-close prices via yfinance (free, unauthenticated).

    Uses asyncio.to_thread so the blocking yf.download call does not block the
    event loop.  Returns a {symbol: close_price} dict; missing / errored symbols
    are simply absent (routed to the IBKR fallback by the caller).
    """
    if not symbols:
        return {}

    def _download() -> Dict[str, float]:
        import yfinance as yf  # deferred: heavy import, only needed in this function
        raw = yf.download(symbols, period="2d", progress=False, auto_adjust=True)
        if raw.empty:
            return {}
        # yf.download with a list always returns MultiIndex columns (field, symbol)
        if isinstance(raw.columns, pd.MultiIndex):
            close_row = raw["Close"].iloc[-1]          # Series: symbol → close
        else:
            # Single string passed — regular columns; wrap into Series
            val = raw["Close"].iloc[-1]
            close_row = pd.Series({symbols[0]: val})
        return {
            sym: float(close_row[sym])
            for sym in symbols
            if sym in close_row.index and _is_valid_price(close_row[sym])
        }

    try:
        return await asyncio.to_thread(_download)
    except Exception as exc:
        logger.warning("Bulk yfinance price fetch failed: %s", exc)
        return {}


async def _fetch_prices_fallback_ib(
    ib: IB,
    contracts: List[Contract],
) -> Dict[str, float]:
    """Fallback source: precision prices via semaphore-throttled ib_async requests.

    Enforces asyncio.Semaphore(40) — a safe buffer below IBKR's 50 req/s pacing
    limit.  Each slot is released after a 25 ms controlled pause.  Failures for
    individual contracts are swallowed and logged at DEBUG level so one bad tick
    never aborts the batch.
    """
    if not contracts:
        return {}

    sem = asyncio.Semaphore(40)  # pacing guard: max 40 concurrent IBKR requests

    async def _fetch_one(contract: Contract) -> tuple[str, Optional[float]]:
        async with sem:
            try:
                ticker = await ib.reqMktDataAsync(contract, "", snapshot=True)
                await asyncio.sleep(0.025)              # controlled release pacing
                price = (
                    ticker.last if _is_valid_price(ticker.last)
                    else ticker.close if _is_valid_price(ticker.close)
                    else None
                )
                return contract.symbol, price
            except Exception as exc:
                logger.debug("IBKR fallback failed for %s: %s", contract.symbol, exc)
                return contract.symbol, None

    completed = await asyncio.gather(*[_fetch_one(c) for c in contracts])
    return {sym: price for sym, price in completed if price is not None}


def get_prices(
    contracts: List[Contract],
    market: str = "SNP",
    max_wait_time: int = 10,
    snapshot: bool = True,
    batch_size: int = 50,
    ib: IB = None,
) -> pd.DataFrame:
    """Get market prices via the Dual-Source Hybrid Pipeline (audit Issue 2.1).

    Strategy
    --------
    1. **Primary** – yfinance bulk download in a thread executor.
       Fast, free, zero pacing cost.  Populates the ``close`` column.
    2. **Fallback** – ib_async ``reqMktDataAsync`` with ``Semaphore(40)``
       for every symbol yfinance could not price.  Stays safely below
       IBKR's 50 req/s pacing ceiling.  Populates ``last`` (and ``close``
       when ``last`` is absent).

    The ``bid`` / ``ask`` / ``volume`` / ``high`` / ``low`` / ``open``
    columns are retained for API compatibility but will be ``None`` for
    yfinance-sourced rows; only IBKR-sourced rows may populate them in a
    future enhancement.

    Parameters
    ----------
    contracts:     Qualified ``Contract`` objects.
    market:        Config key (default ``'SNP'``).
    max_wait_time: Kept for API compatibility; not used in the async path.
    snapshot:      Kept for API compatibility; fallback always uses snapshot mode.
    batch_size:    Kept for API compatibility; concurrency is managed by the semaphore.
    ib:            Optional pre-connected ``IB`` instance.  Created on demand
                   (and disconnected in ``finally``) only when the fallback runs.
    """
    if not contracts:
        return pd.DataFrame()

    sym_to_contract: Dict[str, Contract] = {c.symbol: c for c in contracts}
    symbols: List[str] = list(sym_to_contract)
    _created_ib = False

    try:
        # ── Stage 1: yfinance bulk fetch (primary source) ─────────────────
        yf_prices: Dict[str, float] = asyncio.run(
            _fetch_prices_bulk_yf(symbols)
        )
        logger.info(
            "yfinance priced %s/%s symbols",
            len(yf_prices), len(symbols),
        )

        # ── Stage 2: IBKR fallback for symbols yfinance missed ────────────
        missed = [s for s in symbols if s not in yf_prices]
        ib_prices: Dict[str, float] = {}

        if missed:
            if ib is None:
                ib = get_ib_connection(market)
                _created_ib = True
            missed_contracts = [sym_to_contract[s] for s in missed]
            ib_prices = asyncio.run(
                _fetch_prices_fallback_ib(ib, missed_contracts)
            )
            logger.info(
                "IBKR fallback priced %s/%s missed symbols",
                len(ib_prices), len(missed),
            )

        # ── Stage 3: assemble output DataFrame ───────────────────────────
        _COLS = [
            "symbol", "conId", "bid", "ask", "last", "close",
            "volume", "high", "low", "open",
            "bidSize", "askSize", "lastSize", "halted", "time",
        ]
        rows = []
        for contract in contracts:
            sym = contract.symbol
            yf_close = yf_prices.get(sym)
            ib_last = ib_prices.get(sym)
            rows.append({
                "symbol":   sym,
                "conId":    contract.conId,
                "bid":      None,
                "ask":      None,
                "last":     ib_last,
                "close":    yf_close if yf_close is not None else ib_last,
                "volume":   None,
                "high":     None,
                "low":      None,
                "open":     None,
                "bidSize":  None,
                "askSize":  None,
                "lastSize": None,
                "halted":   None,
                "time":     None,
            })

        df = pd.DataFrame(rows, columns=_COLS)

        valid = int((df["last"].notna() | df["close"].notna()).sum())
        total = len(df)
        logger.info(
            "Prices: %s/%s valid (%s%%)",
            valid, total,
            f"{100 * valid / total if total else 0.0:.1f}",
        )
        return df

    except Exception as exc:
        logger.error("get_prices failed: %s", exc)
        return pd.DataFrame()

    finally:
        if _created_ib and ib is not None and ib.isConnected():
            ib.disconnect()

def get_prices_snapshot(
    contracts: List[Contract], 
    market: str = "SNP", 
    batch_size: int = 50,
    max_wait_time: int = 10
) -> pd.DataFrame:
    """
    Get a one-time snapshot of prices for qualified contracts.
    
    Args:
        contracts: List of qualified Contract objects
        market: Market name for config loading (default: 'SNP')
        batch_size: Number of contracts per batch (default: 50)
        max_wait_time: Maximum time to wait for prices (default: 10)
    
    Returns:
        DataFrame with price data
    """
    df = get_prices(
        contracts, 
        market=market, 
        max_wait_time=max_wait_time, 
        snapshot=True,
        batch_size=batch_size
    )
    
    # pyrefly: ignore [no-matching-overload]
    df['price'] = df.apply(
        lambda row: 
            (row['bid'] + row['ask']) / 2 if row['bid'] is not None and row['ask'] is not None and row['bid'] > 0 and row['ask'] > 0 
            else row['last'] if row['last'] is not None 
            else row['close'] if row['close'] is not None 
            else None,
        axis=1
    )
    df['price'] = df['price'].apply(lambda x: get_prec_safe(x, 0.01))
    
    return df
    
# Volatility and Prices

def get_volatilities_snapshot(
    contracts: List[Contract],
    market: str = "SNP",
    batch_size: int = 50,
    max_wait_time: int = 10,  # seconds to wait for each ticker (IV can be slow)
    ib: IB = None,
    desc: str = "Fetching volatilities",
) -> pd.DataFrame:
    """
    Get a one-time snapshot of price, implied volatility (IV), and historical volatility (HV)
    for qualified contracts using the asynchronous 'volatilities' function.

    Args:
        contracts: List of qualified Contract objects (can be Stock or other)
        market: Market name for config loading (default: 'SNP')
        batch_size: Number of contracts per batch (default: 50)
        max_wait_time: Max seconds to wait for each ticker in the async call.
                       This is passed as 'sleep_time' (default: 10)

    Returns:
        DataFrame with symbol, price, implied volatility (iv), and historical volatility (hv).
    """
    # Keyed by conId (falls back to symbol) so the delayed fallback pass can
    # overwrite the rows whose price never arrived on the primary pass.
    rows: dict = {}

    def _has_price(d: dict) -> bool:
        p = d.get("price")
        return p is not None and not pd.isna(p) and p > 0

    def _keep(new, old):
        """Prefer a fresh non-null value, else retain the prior pass's value."""
        return new if (new is not None and not pd.isna(new)) else old

    def _set_mkt_data_type(t: int) -> None:
        try:
            ib.reqMarketDataType(t)
        except Exception as _e:  # noqa: BLE001
            logger.debug("reqMarketDataType(%s) failed: %s", t, _e)

    def _run_pass(pass_contracts: list, label: str) -> None:
        """Fetch one market-data-type pass over the given contracts into `rows`."""
        total_batches = (len(pass_contracts) + batch_size - 1) // batch_size
        with progress_bar(len(pass_contracts), label, unit="sym", file=sys.stderr) as pbar:
            for batch_num in range(total_batches):
                start_idx = batch_num * batch_size
                end_idx = min(start_idx + batch_size, len(pass_contracts))
                batch_contracts = pass_contracts[start_idx:end_idx]

                # pyrefly: ignore [missing-attribute]
                batch_results = ib.run(
                    volatilities(
                        contracts=batch_contracts,
                        ib=ib,
                        sleep_time=max_wait_time,
                        gentick="106, 104",
                    )
                )

                for contract_key, data in batch_results.items():
                    if isinstance(contract_key, Contract):
                        symbol = contract_key.symbol
                        conId = contract_key.conId
                    else:
                        symbol = contract_key
                        conId = None
                    key = conId if conId else symbol
                    prev = rows.get(key, {})
                    _np, _ni, _nh = data.get("price"), data.get("iv"), data.get("hv")
                    rows[key] = {
                        "symbol": symbol,
                        "conId": conId,
                        "price": _keep(_np, prev.get("price")),
                        "iv": _keep(_ni, prev.get("iv")),
                        "hv": _keep(_nh, prev.get("hv")),
                    }
                pbar.update(len(batch_contracts))

    try:
        # Connect to IB
        disconnect = False
        if ib is None:
            ib = get_ib_connection(market)
            disconnect = True

        # Primary pass — frozen (2) returns live data in market hours and the last
        # recorded close off-hours, so subscribed symbols fill in both regimes.
        _set_mkt_data_type(MKT_DATA_TYPE)
        _run_pass(list(contracts), desc)

        # Fallback pass — re-fetch only the symbols still missing a price using the
        # delayed-frozen (4) feed, which needs no real-time subscription and works
        # off-hours. This closes the coverage gap that left ~40% of symbols NaN.
        if MKT_DATA_FALLBACK and MKT_DATA_FALLBACK != MKT_DATA_TYPE:
            missing = [
                c for c in contracts
                if not _has_price(rows.get(getattr(c, "conId", None) or getattr(c, "symbol", c), {}))
            ]
            if missing:
                logger.info(
                    "Price fallback: %d/%d symbols missing price on type %d — retrying on delayed-frozen type %d",
                    len(missing), len(contracts), MKT_DATA_TYPE, MKT_DATA_FALLBACK,
                )
                _set_mkt_data_type(MKT_DATA_FALLBACK)
                _run_pass(missing, f"{desc} (delayed fallback)")
                _set_mkt_data_type(MKT_DATA_TYPE)  # restore primary for later calls

        df = pd.DataFrame(list(rows.values()))
        if df.empty:
            return df
        valid_ivs = df[df["iv"].notna()]
        valid_px = df[df["price"].apply(lambda p: p is not None and not pd.isna(p) and p > 0)]
        logger.info(
            "Volatility snapshot: %s contracts, %s/%s valid prices (%s%%), %s/%s valid IVs (%s%%)",
            len(df),
            len(valid_px), len(df), f"{100 * len(valid_px) / len(df) if len(df) else 0.0:.1f}",
            len(valid_ivs), len(df), f"{100 * len(valid_ivs) / len(df) if len(df) else 0.0:.1f}",
        )
        return df

    except Exception as e:
        logger.error("Failed to get volatilities: %s", e)
        return pd.DataFrame()

    finally:
        if disconnect and ib and ib.isConnected():
            ib.disconnect()
            logger.debug("Disconnected from IB")

async def volatilities(
    contracts: list,
    ib: IB,
    sleep_time: int = 8,
    gentick: str = "106, 104",
    max_concurrent: int = 40,
) -> dict:
    # Bound concurrent market-data lines so a 50-wide batch does not burst past
    # IBKR's ~50 req/s ceiling. Each contract resolves the moment IV arrives, so
    # the semaphore frees up well before `sleep_time`.
    sem = asyncio.Semaphore(max_concurrent)
    tasks = [
        get_an_iv(item=c, ib=ib, sleep_time=sleep_time, gentick=gentick, sem=sem)
        for c in contracts
    ]

    results = await asyncio.gather(*tasks)

    return {
        k: v for d in results for k, v in d.items()
    }  # Combine results into a single dictionary

async def get_an_iv(
    ib: IB,
    item: str,
    sleep_time: int = 8,
    gentick: str = "106, 104",
    sem: "asyncio.Semaphore | None" = None,
) -> dict:
    """Snapshot price/IV/HV for one contract, resolving the instant IV arrives.

    `sleep_time` is now an upper *cap*, not a fixed wait: we subscribe to ticks
    106 (IV) / 104 (HV) and await the ticker's updateEvent until implied
    volatility is populated, falling back to the cap only for symbols that never
    return a value. Typical resolution is sub-second vs the old fixed 10 s+2 s.
    """
    stock_contract = Stock(item, "SMART", "USD") if isinstance(item, str) else item
    key = item if isinstance(item, str) else stock_contract

    async def _do() -> dict:
        ticker = ib.reqMktData(stock_contract, genericTickList=gentick)
        try:
            # Wait (event-driven) for the slow IV tick, capped at sleep_time.
            if pd.isna(ticker.impliedVolatility):
                loop = asyncio.get_event_loop()
                fut: asyncio.Future = loop.create_future()

                def _on_update(t=ticker) -> None:
                    if not fut.done() and not pd.isna(t.impliedVolatility):
                        fut.set_result(True)

                ticker.updateEvent += _on_update
                try:
                    await asyncio.wait_for(fut, timeout=sleep_time)
                except asyncio.TimeoutError:
                    pass
                finally:
                    ticker.updateEvent -= _on_update
        finally:
            ib.cancelMktData(stock_contract)

        # Price priority: last trade → bid/ask midpoint → previous close
        if not pd.isna(ticker.last) and ticker.last > 0:
            price = ticker.last
        elif (
            not pd.isna(ticker.bid)
            and not pd.isna(ticker.ask)
            and ticker.bid > 0
            and ticker.ask > 0
        ):
            price = (ticker.bid + ticker.ask) / 2
        else:
            price = ticker.close

        return {key: {"price": price, "iv": ticker.impliedVolatility, "hv": ticker.histVolatility}}

    if sem is not None:
        async with sem:
            return await _do()
    return await _do()

#%%
# Option Chains

async def get_an_option_chain(item: Contract, ib: IB, sleep_time: int = 2):
    try:
        chain = await asyncio.wait_for(
            ib.reqSecDefOptParamsAsync(
                underlyingSymbol=item.symbol,
                futFopExchange="",
                underlyingSecType=item.secType,
                underlyingConId=item.conId,
            ),
            timeout=sleep_time,
        )

        if not chain:
            return None
        return chain[-1] if isinstance(chain, list) else chain

    except asyncio.TimeoutError:
        logger.error(f"Timeout occurred while getting option chain for {item.symbol}")
        return None

async def chains(contracts: list, ib: IB, sleep_time: int = 2) -> dict:
    tasks = [
        asyncio.create_task(
            get_an_option_chain(item=c, ib=ib, sleep_time=sleep_time), name=c.symbol
        )
        for c in contracts
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    out = {task.get_name(): result for task, result in zip(tasks, results) if not isinstance(result, Exception)}
    return out

def get_option_chains(
    contracts: List[Contract],
    market: str = "SNP",
    batch_size: int = 50,
    max_wait_time: int = 10,
    inter_batch_delay: float = 0.5,
    ib: IB = None
) -> pd.DataFrame:
    """
    Get option chain parameters (expiries, strikes) for a list of underlying contracts.
    Processes contracts in batches with a single IB connection, retrying failed symbols once.
    Returns a DataFrame with all expiry and strike combinations.

    Args:
        contracts: List of qualified Contract objects
        market: Market name for config loading (default: 'SNP')
        batch_size: Number of contracts per batch (default: 50)
        max_wait_time: Maximum seconds to wait for each chain request (default: 10)
        inter_batch_delay: Seconds to wait between batches to avoid rate limits (default: 0.5)

    Returns:
        DataFrame with columns: symbol, conId, tradingClass, expiry, strike, dte
    """
    all_chain_data = []
    failed_symbols = []

    try:
        # Connect to IB
        disconnect = False
        if ib is None:
            ib = get_ib_connection(market)
            disconnect = True

        # Process contracts in batches
        total_batches = (len(contracts) + batch_size - 1) // batch_size

        with progress_bar(len(contracts), "Fetching option chains", unit="sym", file=sys.stderr) as pbar:
            for batch_num in range(total_batches):
                start_idx = batch_num * batch_size
                end_idx = min(start_idx + batch_size, len(contracts))
                batch_contracts = contracts[start_idx:end_idx]

                # pyrefly: ignore [missing-attribute]
                batch_results = ib.run(
                    chains(contracts=batch_contracts, ib=ib, sleep_time=max_wait_time)
                )

                for contract in batch_contracts:
                    symbol = contract.symbol
                    chain = batch_results.get(symbol)

                    if chain is None:
                        failed_symbols.append(contract)
                        all_chain_data.append({
                            "symbol": symbol, "conId": contract.conId,
                            "tradingClass": None, "expiries": None, "strikes": None,
                        })
                    else:
                        all_chain_data.append({
                            "symbol": symbol, "conId": chain.underlyingConId,
                            "tradingClass": chain.tradingClass,
                            "expiries": chain.expirations, "strikes": chain.strikes,
                        })

                if batch_num < total_batches - 1:
                    ib.sleep(inter_batch_delay)
                pbar.update(len(batch_contracts))

        if failed_symbols:
            logger.info("Retrying %s failed symbols…", len(failed_symbols))
            # pyrefly: ignore [missing-attribute]
            retry_results = ib.run(
                chains(contracts=failed_symbols, ib=ib, sleep_time=max_wait_time)
            )
            retry_data = []
            for contract in failed_symbols:
                symbol = contract.symbol
                chain = retry_results.get(symbol)
                if chain is None:
                    retry_data.append({
                        "symbol": symbol, "conId": contract.conId,
                        "tradingClass": None, "expiries": None, "strikes": None,
                    })
                else:
                    retry_data.append({
                        "symbol": symbol, "conId": chain.underlyingConId,
                        "tradingClass": chain.tradingClass,
                        "expiries": chain.expirations, "strikes": chain.strikes,
                    })
            all_chain_data = [
                d for d in all_chain_data
                if d["symbol"] not in {c.symbol for c in failed_symbols}
            ]
            all_chain_data.extend(retry_data)

        df = pd.DataFrame(all_chain_data)
        valid_chains = df[df["expiries"].notna()]
        logger.info(
            "Option chains: %s contracts, %s/%s valid (%s%%)",
            len(df), len(valid_chains), len(df),
            f"{100 * len(valid_chains) / len(df) if len(df) else 0.0:.1f}",
        )

        if df.empty:
            return pd.DataFrame()

        expanded_rows = []
        for _, row in df.iterrows():
            if row["expiries"] is None or row["strikes"] is None:
                continue
            for expiry, strike in product(row["expiries"], row["strikes"]):
                expanded_rows.append({"symbol": row["symbol"], "expiry": expiry, "strike": strike})

        df_out = pd.DataFrame(expanded_rows)
        if df_out.empty:
            return df_out
        df_out["dte"] = get_dte(df_out["expiry"])
        # Drop already-expired expiries (negative DTE) so stale contracts never
        # leak into df_chains and downstream DTE windows.
        df_out = df_out[df_out["dte"] >= 0].reset_index(drop=True)
        return df_out

    except Exception as e:
        logger.error("Failed to get option chains: %s", e)
        return pd.DataFrame()

    finally:
        if disconnect and ib and ib.isConnected():
            ib.disconnect()
            logger.debug("Disconnected from IB")

# Change tradingClass for contracts that have 'NMS' in them.
# This is due to some error in NYSE that puts tradingClass as 'NMS'.

def normalize_trading_class(contracts: list) -> list:
    for contract in contracts:
        if getattr(contract, "tradingClass", None) == "NMS" and contract.symbol != "NMS":
            contract.tradingClass = None
    return contracts

# Function to calculate ATM margin for each row
def calculate_atm_margin(row, chains_df, target_dte):
    symbol = row['symbol']
    und_price = row['price']  # Use price from vols_df
    iv = row['iv']
    
    if pd.isna(und_price) or pd.isna(iv):
        return None
    
    if chains_df.empty or 'symbol' not in chains_df.columns:
        return None

    # Filter chains_df for the current symbol
    symbol_chains = chains_df[chains_df['symbol'] == symbol]
    
    if symbol_chains.empty:
        return None
    
    # Find the DTE closest to target_dte
    symbol_chains['dte_diff'] = abs(symbol_chains['dte'] - target_dte)
    min_dte_diff = symbol_chains['dte_diff'].min()
    closest_dte_rows = symbol_chains[symbol_chains['dte_diff'] == min_dte_diff]
    
    if closest_dte_rows.empty:
        return None
    
    # Select the first row with the closest DTE to ensure a single value
    closest_dte_row = closest_dte_rows.iloc[0]
    dte = closest_dte_row['dte']
    
    # Find the strike closest to the underlying price among the closest DTE rows
    closest_dte_rows['strike_diff'] = abs(closest_dte_rows['strike'] - und_price)
    closest_strike_row = closest_dte_rows.loc[closest_dte_rows['strike_diff'].idxmin()]
    
    strike = closest_strike_row['strike']
    
    # Calculate ATM margin
    margin = atm_margin(strike=strike, undPrice=und_price, dte=dte, vy=iv)
    return margin

BUILD_SUMMARY_PATH = ROOT / "data" / "build_summary.json"
_COVERAGE_WARN_PCT = 90.0  # below this, surface a WHAT-TO-DO-NEXT message


def _coverage(numerator: int, denominator: int) -> float:
    return round(100.0 * numerator / denominator, 1) if denominator else 0.0


def write_build_summary(
    n_symbols: int,
    df_chains: pd.DataFrame,
    df_unds: pd.DataFrame,
    path: Path = BUILD_SUMMARY_PATH,
    hv_fallback_syms: list[str] | None = None,
) -> dict:
    """Compute per-stage coverage, emit clear next-step guidance, and persist a
    machine-readable summary the dashboard can surface.

    Returns the summary dict (also written to `path` as JSON).
    """
    # Chains coverage
    n_chains = (
        int(df_chains["symbol"].nunique())
        if (isinstance(df_chains, pd.DataFrame) and not df_chains.empty and "symbol" in df_chains.columns)
        else 0
    )

    # Price / IV coverage from df_unds
    if isinstance(df_unds, pd.DataFrame) and not df_unds.empty and "symbol" in df_unds.columns:
        n_unds = int(len(df_unds))
        valid_price = int((pd.to_numeric(df_unds.get("price"), errors="coerce") > 0).sum())
        valid_iv = int((pd.to_numeric(df_unds.get("iv"), errors="coerce") > 0).sum())
        missing_iv_syms = sorted(
            df_unds.loc[~(pd.to_numeric(df_unds["iv"], errors="coerce") > 0), "symbol"].astype(str).tolist()
        )
    else:
        n_unds = valid_price = valid_iv = 0
        missing_iv_syms = []

    summary = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "symbols_qualified": int(n_symbols),
        "chains": {"symbols_with_chains": n_chains, "coverage_pct": _coverage(n_chains, n_symbols)},
        "prices": {"rows": n_unds, "valid": valid_price, "coverage_pct": _coverage(valid_price, n_unds)},
        "iv": {"rows": n_unds, "valid": valid_iv, "coverage_pct": _coverage(valid_iv, n_unds)},
        "warnings": [],
        "next_steps": [],
    }

    # Clear, actionable next-step messages when coverage is materially low.
    def _flag(label: str, pct: float, missing: int) -> None:
        if pct < _COVERAGE_WARN_PCT:
            msg = (
                f"{label} coverage {pct:.0f}% — {missing} symbols missing; "
                f"derive.py will SKIP these. Re-run during market hours: "
                f"uv run python src/build.py"
            )
            summary["warnings"].append(msg)
            summary["next_steps"].append(msg)
            logger.warning("WHAT TO DO NEXT: %s", msg)

    _flag("Chains", summary["chains"]["coverage_pct"], max(n_symbols - n_chains, 0))
    _flag("Price", summary["prices"]["coverage_pct"], max(n_unds - valid_price, 0))
    _flag("IV", summary["iv"]["coverage_pct"], len(missing_iv_syms))

    if missing_iv_syms:
        summary["missing_iv_symbols"] = missing_iv_syms[:50]  # cap list size

    if hv_fallback_syms:
        summary["hv_fallback"] = {
            "count": len(hv_fallback_syms),
            "symbols": hv_fallback_syms,
        }
        hv_note = (
            f"HV used as IV fallback for {len(hv_fallback_syms)} symbols "
            "(market closed / IV unavailable) — re-run during market hours for live IV."
        )
        summary["warnings"].append(hv_note)
        summary["next_steps"].append(hv_note)
        logger.info("build_summary: HV fallback recorded for %d symbols", len(hv_fallback_syms))

    try:
        path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        logger.info(
            "build_summary.json — chains %.0f%%, price %.0f%%, iv %.0f%%",
            summary["chains"]["coverage_pct"],
            summary["prices"]["coverage_pct"],
            summary["iv"]["coverage_pct"],
        )
    except Exception as exc:
        logger.warning("Could not write build_summary.json: %s", exc)

    return summary


def chains_n_unds(msg: bool = False):
    """
    Processes qualified contracts, option chains, and calculate margins.

    Returns:
        Tuple of DataFrames: (df_chains, df_unds)
    """
    sym_path = ROOT / 'data' / 'symbols.pkl'

    # Get qualified contracts
    if do_i_refresh(my_path=sym_path, max_days=1):
        logger.info("symbols.pkl missing/stale — rebuilding from web + IB (takes ~2-3 min)…")  # no args
        qualified_contracts = get_qualified_symbols(weeklies=True, market="SNP", save=True)
        pickle_me(qualified_contracts, file_path=sym_path)
    else:
        qualified_contracts = get_pickle(path=sym_path, print_msg=msg)

    # Get option chains for qualified contracts
    chain_path = ROOT / 'data' / 'df_chains.pkl'
    df_chains_check = get_pickle(chain_path)
    _chains_check_syms = (
        df_chains_check["symbol"].nunique()
        if (isinstance(df_chains_check, pd.DataFrame) and not df_chains_check.empty and "symbol" in df_chains_check.columns)
        else 0
    )
    _chains_incomplete = _chains_check_syms < len(qualified_contracts)
    if do_i_refresh(my_path=chain_path, max_days=1) or df_chains_check is None or (isinstance(df_chains_check, pd.DataFrame) and df_chains_check.empty) or _chains_incomplete:
        if _chains_incomplete and _chains_check_syms > 0:
            logger.warning(
                "df_chains incomplete (%d/%d symbols) — re-fetching",
                _chains_check_syms, len(qualified_contracts),
            )
        # pyrefly: ignore [bad-argument-type]
        df_chains = get_option_chains(qualified_contracts, market="SNP", batch_size=50)
        if not df_chains.empty and 'symbol' in df_chains.columns:
            pickle_me(df_chains, file_path=chain_path)
        else:
            logger.warning("get_option_chains returned no usable data — keeping existing pickle")
            _existing = get_pickle(path=chain_path, print_msg=msg)
            df_chains = _existing if (_existing is not None and not _existing.empty) else pd.DataFrame()
    else:
        df_chains = get_pickle(path=chain_path, print_msg=msg)

    # Integrity check: warn if chains coverage is materially below symbols list
    _n_syms = len(qualified_contracts)
    _n_chains = df_chains["symbol"].nunique() if (not df_chains.empty and "symbol" in df_chains.columns) else 0
    if _n_chains == 0:
        logger.error("df_chains has NO symbols — downstream covered-call generation will be skipped")
    elif _n_chains < _n_syms:
        logger.warning(
            "df_chains coverage %d/%d symbols (%.0f%%) — some covered-call candidates may be missed",
            _n_chains, _n_syms, 100 * _n_chains / _n_syms,
        )
    else:
        logger.info("df_chains coverage %d/%d symbols — OK", _n_chains, _n_syms)

    # Get price with volatilities and margins for qualified contracts
    # pyrefly: ignore [bad-argument-type]
    df_unds = get_volatilities_snapshot(qualified_contracts, market="SNP", batch_size=50)

    # OHLC-HV fallback: compute 30-day realized vol from price history for symbols
    # where IBKR didn't return HV (tick 104 is also absent outside market hours).
    # This fills df_unds["hv"] so the HV→IV block below can substitute it for iv.
    _ohlc_path = ROOT / "data" / "master" / "ohlc.pkl"
    if not df_unds.empty and "symbol" in df_unds.columns and _ohlc_path.exists():
        try:
            _ohlc_data: dict = pd.read_pickle(_ohlc_path)
            _hv_from_ohlc: dict[str, float] = {}
            for _sym, _sym_df in _ohlc_data.items():
                if "Close" in _sym_df.columns:
                    _closes = _sym_df["Close"].dropna()
                    if len(_closes) >= 31:
                        _hv30 = _closes.pct_change().rolling(30).std().iloc[-1] * (252 ** 0.5)
                        if pd.notna(_hv30) and _hv30 > 0:
                            _hv_from_ohlc[_sym] = float(_hv30)
            if _hv_from_ohlc:
                _hv_num_pre = pd.to_numeric(df_unds.get("hv", pd.Series(dtype=float)), errors="coerce")
                _ohlc_hv_full = df_unds["symbol"].map(_hv_from_ohlc)
                _fill_hv_mask = (~(_hv_num_pre > 0)) & _ohlc_hv_full.notna()
                if _fill_hv_mask.any():
                    df_unds.loc[_fill_hv_mask, "hv"] = _ohlc_hv_full[_fill_hv_mask]
                    logger.info(
                        "OHLC-HV: filled 30-day realized vol for %d symbols (IBKR HV unavailable)",
                        int(_fill_hv_mask.sum()),
                    )
        except Exception as _ohlc_exc:
            logger.warning("OHLC-HV computation failed: %s", _ohlc_exc)

    # HV fallback: where IV is unavailable (off-hours), substitute HV so derive.py
    # can still generate orders instead of skipping every symbol.
    _hv_fallback_syms: list[str] = []
    if not df_unds.empty and "symbol" in df_unds.columns:
        _iv_num = pd.to_numeric(df_unds.get("iv", pd.Series(dtype=float)), errors="coerce")
        _hv_num = pd.to_numeric(df_unds.get("hv", pd.Series(dtype=float)), errors="coerce")
        _use_hv_mask = (~(_iv_num > 0)) & (_hv_num > 0)
        if _use_hv_mask.any():
            _hv_fallback_syms = sorted(df_unds.loc[_use_hv_mask, "symbol"].astype(str).tolist())
            df_unds.loc[_use_hv_mask, "iv"] = _hv_num[_use_hv_mask]
            logger.info(
                "HV fallback: substituted HV for IV on %d symbols (market closed / IV unavailable)",
                len(_hv_fallback_syms),
            )

    # Load configuration to get VIRGIN_DTE
    config = load_config('SNP')
    virgin_dte = float(config.get('VIRGIN_DTE', 30))  # Default to 30 if not specified

    # Apply the ATM margin calculation.
    # Guard: get_volatilities_snapshot returns pd.DataFrame() (no columns) on
    # connection failure; apply(axis=1) on a column-less frame returns a
    # DataFrame rather than a Series, breaking the column assignment.
    if not df_unds.empty and 'symbol' in df_unds.columns:
        # pyrefly: ignore [no-matching-overload]
        df_unds['margin'] = df_unds.apply(
            lambda row: calculate_atm_margin(row, df_chains, virgin_dte),
            axis=1
        )
    else:
        df_unds['margin'] = pd.Series(dtype=float)

    if df_unds.empty or 'symbol' not in df_unds.columns:
        logger.warning("df_unds is empty — no volatility data retrieved")
    pickle_me(df_unds, file_path=ROOT / 'data' / 'df_unds.pkl')

    # Per-stage coverage + machine-readable summary + next-step guidance.
    write_build_summary(_n_syms, df_chains, df_unds, hv_fallback_syms=_hv_fallback_syms)

    return df_chains, df_unds

#%% 
# Test functions - Make symbols
if __name__ == "__main__":
    import argparse
    from src.log_utils import setup_logging

    _p = argparse.ArgumentParser()
    _p.add_argument("--debug", action="store_true", help="Show DEBUG output in terminal")
    setup_logging("build", debug=_p.parse_args().debug)

    sym_path = ROOT/'data'/'symbols.pkl'

    # Get qualified contracts
    if do_i_refresh(my_path=sym_path, max_days=1):
        qualified_contracts = get_qualified_symbols(weeklies=True, market="SNP", save=True)
        # qualified_contracts = normalize_trading_class(qualified_contracts)
        pickle_me(qualified_contracts, file_path=sym_path)
    else:
        qualified_contracts = get_pickle(path=sym_path)

    # Get option chains for qualified contracts
    # Note: chains should be run before unds, as unds uses chains for margin calculation
    chain_path = ROOT / 'data' / 'df_chains.pkl'
    df_chains_check = get_pickle(chain_path)
    if do_i_refresh(my_path=chain_path, max_days=1) or df_chains_check is None or (isinstance(df_chains_check, pd.DataFrame) and df_chains_check.empty):
        # pyrefly: ignore [bad-argument-type]
        df_chains = get_option_chains(qualified_contracts, market="SNP", batch_size=50)
        pickle_me(df_chains, file_path=chain_path)
    else:
        df_chains = get_pickle(path=chain_path)

    # Get price with volatilities and margins for qualified contracts
    # pyrefly: ignore [bad-argument-type]
    df_unds = get_volatilities_snapshot(qualified_contracts, market="SNP", batch_size=50)

    # Load configuration to get VIRGIN_DTE
    config = load_config('SNP')
    virgin_dte = float(config.get('VIRGIN_DTE', 30))  # Default to 30 if not specified

    # Apply the ATM margin calculation (same guard as chains_n_unds above).
    if not df_unds.empty and 'symbol' in df_unds.columns:
        # pyrefly: ignore [no-matching-overload]
        df_unds['margin'] = df_unds.apply(
            lambda row: calculate_atm_margin(row, df_chains, virgin_dte),
            axis=1
        )
    else:
        df_unds['margin'] = pd.Series(dtype=float)

    pickle_me(df_unds, file_path=ROOT/'data'/'df_unds.pkl')
