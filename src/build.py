#%% 
# IMPORTS

import asyncio
import math
import os
import pickle
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from itertools import product
from pathlib import Path
from typing import List, Optional

import nest_asyncio
import numpy as np
import pandas as pd
# pyrefly: ignore [untyped-import]
import yaml
import yfinance as yf
from dotenv import find_dotenv, load_dotenv
from ib_async import IB, Contract, Stock
from loguru import logger
from pyprojroot import here
from scipy.stats import norm
# pyrefly: ignore [untyped-import]
from tqdm import tqdm as _tqdm
from tqdm.asyncio import tqdm as async_tqdm

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
                print(f"Connected to IB on port {PORT} with client ID {client_id} (market: {market}, account: {account_no or 'default'})")
            return ib
        except Exception as e:
            if attempt < max_retries - 1:
                print(f"Connection attempt {attempt + 1} failed ({e}), retrying in 2s...")
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
            print(f"Connection attempt {attempt + 1} failed: Not connected")
        except Exception as e:
            print(f"Connection attempt {attempt + 1} failed: {str(e)}")
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
            print(f"Deleted {file_path}")

def pickle_me(obj, file_path: Path):
    with open(str(file_path), "wb") as handle:
        pickle.dump(obj, handle, protocol=pickle.HIGHEST_PROTOCOL)

def get_pickle(path: Path, print_msg: bool = True):
    try:
        with open(path, "rb") as f:
            output = pickle.load(f)
            print(f"Loaded {path}")
            return output
    except FileNotFoundError:
        if print_msg:
            print(f"File not found: {path}")
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
    try:
        headers = {'User-Agent': USER_AGENT}
        snp_table = pd.read_html(
            SNP_URL, 
            header=0, 
            attrs={"id": "constituents"}, 
            flavor='lxml', 
            storage_options=headers
        )[0]
        return snp_table["Symbol"]
    except Exception as e:
        logger.error(f"Failed to retrieve S&P 500 symbols: {e}")
        return pd.Series(dtype=str)

@lru_cache(maxsize=1)
def _fetch_weeklys() -> pd.Series:
    """Fetch weekly options symbols from CBOE (cached)."""
    try:
        return pd.read_html(WEEKLYS_URL)[0].iloc[:, 1]
    except Exception as e:
        logger.error(f"Failed to retrieve weekly options: {e}")
        return pd.Series(dtype=str)
    

async def _async_fetch_weeklies_yf(symbols, look_ahead_days=MAX_DTE):
    """Filter S&P 500 symbols for those with weekly options (non-third-Friday expirations).
    
    Args:
        symbols (pd.Series): S&P 500 ticker symbols (Name: Symbol, dtype: object).
        look_ahead_days (int): Days to check for expirations (default: 45).
    
    Returns:
        pd.Series: Symbols with weekly options or error messages.
    """
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
            for result in await async_tqdm.gather(*tasks, desc="Checking symbols"):
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

async def _qualify_batch(ib: IB, contracts: list, pbar=None) -> tuple:
    """Async helper to qualify contracts in batch."""
    qualified = []
    failed = []
    
    # Create async tasks for all contracts
    tasks = [ib.qualifyContractsAsync(contract) for contract in contracts]
    
    # Process tasks as they complete
    for i, task in enumerate(asyncio.as_completed(tasks)):
        try:
            result = await task
            contract = contracts[i]
            if result:
                qualified.extend(result)
                if pbar:
                    pbar.set_postfix_str(f"✓ {contract.symbol}")
            else:
                failed.append(contract.symbol)
                if pbar:
                    pbar.set_postfix_str(f"✗ {contract.symbol}")
        except Exception as e:
            contract = contracts[i]
            failed.append(contract.symbol)
            if pbar:
                pbar.set_postfix_str(f"✗ {contract.symbol}: {str(e)[:30]}")
        
        if pbar:
            pbar.update(1)
    
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
        
        print(f"Qualifying {len(contracts)} contracts asynchronously...")
        with _tqdm(total=len(contracts), desc="Qualifying symbols", unit="sym") as pbar:
            # pyrefly: ignore [not-iterable]
            qualified, failed = ib.run(_qualify_batch(ib, contracts, pbar))
        
        print(f"\n✓ Successfully qualified {len(qualified)}/{len(contracts)} contracts")
        if failed:
            print(f"✗ Failed to qualify {len(failed)} symbols: {', '.join(failed[:10])}")
            if len(failed) > 10:
                print(f"  ... and {len(failed) - 10} more")
        
        return qualified
        
    except Exception as e:
        logger.error(f"Failed to qualify contracts: {e}")
        return []
        
    finally:
        # Disconnect from IB
        if ib and ib.isConnected():
            ib.disconnect()
            print("Disconnected from IB\n")

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
            logger.warning("No contracts to qualify")
            return []

        # Calculate number of batches
        num_batches = (total_contracts + batch_size - 1) // batch_size

        # Since we use rich track, we don't need the context manager tqdm.
        for batch_num in _tqdm(range(num_batches), desc=desc):
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
                            f"Batch {batch_num + 1}/{num_batches} error ({_be}) — "
                            f"retrying in 3 s…"
                        )
                        time.sleep(3)
                    else:
                        logger.warning(
                            f"Batch {batch_num + 1}/{num_batches} failed ({_be}), skipping"
                        )
                        failed_total.extend(
                            getattr(c, "symbol", repr(c)) for c in batch_contracts
                        )

            # If socket is gone after a failed batch, stop early rather than
            # hammering through the remaining batches with guaranteed failures.
            if not _batch_ok and not ib.isConnected():
                logger.warning("IB socket disconnected — stopping qualification early")
                remaining = contracts[end_idx:]
                failed_total.extend(getattr(c, "symbol", repr(c)) for c in remaining)
                break

            if batch_num < num_batches - 1:
                ib.sleep(1)  # 1-second pause to avoid rate limiting

        if failed_total:
            print(f"  Failed to qualify {len(failed_total)} symbols: {', '.join(str(s) for s in failed_total[:10])}"
                  + (f" + {len(failed_total) - 10} more" if len(failed_total) > 10 else ""))

        return qualified

    except Exception as e:
        logger.error(f"Failed to qualify contracts: {e}")
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
    print(f"Retrieved {len(symbols)} symbols (weeklies={weeklies})")

    contracts = qualify_stock_contracts(symbols, market=market)

    # Normalize tradingClass for contracts with 'NMS'
    contracts = normalize_trading_class(contracts)
    contracts = [c for c in contracts if c != None]
    
    if save:
        pickle_me(contracts, file_path=ROOT/'data'/'symbols.pkl')
    
    return contracts

#%%
# Contract Prices

def get_prices(
    contracts: List[Contract], 
    market: str = "SNP",
    max_wait_time: int = 10,
    snapshot: bool = True,
    batch_size: int = 50,
    ib: IB = None
) -> pd.DataFrame:
    """
    Get market prices for a list of qualified contracts.
    
    Args:
        contracts: List of qualified Contract objects
        market: Market name for config loading (default: 'SNP')
        max_wait_time: Maximum seconds to wait for each ticker to populate (default: 10)
        snapshot: If True, request snapshot; if False, stream data (default: True)
        batch_size: Number of contracts to request at once (default: 50)
        ib: Optional existing IB connection
    
    Returns:
        DataFrame with symbol, bid, ask, last, close, volume, and other price data
    """
    try:
        # Connect to IB
        disconnect = False
        if ib is None:
            ib = get_ib_connection(market)
            disconnect = True
        
        # Process contracts in batches to avoid ticker limit
        all_price_data = []
        total_batches = (len(contracts) + batch_size - 1) // batch_size
        
        def is_valid_price(price):
            """Check if price is valid: not None, not -1.0, and not NaN"""
            if price is None:
                return False
            try:
                return price != -1.0 and not math.isnan(price)
            except (TypeError, ValueError):
                return False
        
        for batch_num in _tqdm(range(total_batches), desc="Fetching prices"):
            start_idx = batch_num * batch_size
            end_idx = min(start_idx + batch_size, len(contracts))
            batch_contracts = contracts[start_idx:end_idx]
            
            # Request market data for batch
            tickers = []
            for contract in batch_contracts:
                ticker = ib.reqMktData(contract, '', snapshot, False)
                tickers.append(ticker)
            
            # Wait for each ticker to get first price, with max timeout
            start_time = time.time()
            tickers_pending = set(range(len(tickers)))
            
            while tickers_pending and (time.time() - start_time) < max_wait_time:
                ib.sleep(0.1)  # Small sleep to allow data to come in
                
                # Check which tickers have received data
                for idx in list(tickers_pending):
                    ticker = tickers[idx]
                    # Consider ticker filled if it has last, close, bid, or ask
                    if (is_valid_price(ticker.last) or 
                        is_valid_price(ticker.close) or
                        is_valid_price(ticker.bid) or
                        is_valid_price(ticker.ask)):
                        tickers_pending.remove(idx)
            
            # Extract price data
            for ticker in tickers:
                # Check if data is valid (not -1, nan, or None)
                bid = ticker.bid if is_valid_price(ticker.bid) else None
                ask = ticker.ask if is_valid_price(ticker.ask) else None
                last = ticker.last if is_valid_price(ticker.last) else None
                close = ticker.close if is_valid_price(ticker.close) else None
                all_price_data.append({
                    'symbol': ticker.contract.symbol,
                    'conId': ticker.contract.conId,
                    'bid': bid,
                    'ask': ask,
                    'last': last,
                    'close': close,
                    'volume': ticker.volume if is_valid_price(ticker.volume) else None,
                    'high': ticker.high if is_valid_price(ticker.high) else None,
                    'low': ticker.low if is_valid_price(ticker.low) else None,
                    'open': ticker.open if is_valid_price(ticker.open) else None,
                    'bidSize': ticker.bidSize,
                    'askSize': ticker.askSize,
                    'lastSize': ticker.lastSize,
                    'halted': ticker.halted,
                    'time': ticker.time
                })
            
            # Cancel market data subscriptions if not snapshot
            if not snapshot:
                for contract in batch_contracts:
                    ib.cancelMktData(contract)
        
        # Convert to DataFrame
        df = pd.DataFrame(all_price_data)
        
        # Report data quality
        valid_prices = df[df['last'].notna() | df['close'].notna()]
        print(f"\n✓ Retrieved prices for {len(df)} contracts")
        print(f"✓ Valid prices: {len(valid_prices)}/{len(df)} ({100*len(valid_prices)/len(df):.1f}%)")
        
        return df
        
    except Exception as e:
        logger.error(f"Failed to get prices: {e}")
        return pd.DataFrame()
        
    finally:
        # Disconnect from IB
        if ib and ib.isConnected():
            ib.disconnect()
            print("Disconnected from IB\n")

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
    df['price'] = df['price'].apply(lambda x: get_prec(x, 0.01) if x is not None else None)
    
    return df
    
# Volatility and Prices

def get_volatilities_snapshot(
    contracts: List[Contract],
    market: str = "SNP",
    batch_size: int = 50,
    max_wait_time: int = 10,  # Note: max_wait_time is used as sleep_time in async call
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
    all_vol_data = []

    try:
        # Connect to IB
        disconnect = False
        if ib is None:
            ib = get_ib_connection(market)
            disconnect = True

        # Process contracts in batches
        total_batches = (len(contracts) + batch_size - 1) // batch_size

        for batch_num in _tqdm(range(total_batches), desc=desc):
            start_idx = batch_num * batch_size
            end_idx = min(start_idx + batch_size, len(contracts))
            batch_contracts = contracts[start_idx:end_idx]

            # Run the asynchronous volatility fetching function within the IB event loop
            # ib.run is the synchronous way to execute async code with ib_insync
            batch_results = ib.run(
                volatilities(
                    contracts=batch_contracts,
                    ib=ib,
                    sleep_time=max_wait_time, # Pass max_wait_time as sleep_time for async
                    gentick="106, 104" # Standard genticks for IV (106) and HV (104)
                )
            )
            
            # Process and flatten the batch results
            # pyrefly: ignore [missing-attribute]
            for contract_key, data in batch_results.items():
                if isinstance(contract_key, Contract):
                    symbol = contract_key.symbol
                    conId = contract_key.conId
                else: # Assuming contract_key is the symbol string
                    symbol = contract_key
                    conId = None # conId not readily available if key is string

                all_vol_data.append({
                    'symbol': symbol,
                    'conId': conId,
                    'price': data.get('price'),
                    'iv': data.get('iv'),
                    'hv': data.get('hv')
                })

        # Convert to DataFrame
        df = pd.DataFrame(all_vol_data)

        # Report data quality
        valid_ivs = df[df['iv'].notna()]
        print(f"\n✓ Retrieved data for {len(df)} contracts")
        print(f"✓ Valid Implied Volatilities: {len(valid_ivs)}/{len(df)} ({100*len(valid_ivs)/len(df):.1f}%)")
        
        return df

    except Exception as e:
        # Assuming logger is defined elsewhere
        # logger.error(f"Failed to get volatilities: {e}") 
        print(f"Failed to get volatilities: {e}")
        return pd.DataFrame()

    finally:
        # Disconnect from IB
        if disconnect and ib and ib.isConnected():
            ib.disconnect()
            print("Disconnected from IB\n")

async def volatilities(
    contracts: list, ib: IB, sleep_time: int = 3, gentick: str = "106, 104"
) -> dict:
    tasks = [
        get_an_iv(item=c, ib=ib, sleep_time=sleep_time, gentick=gentick)
        for c in contracts
    ]

    results = await asyncio.gather(*tasks)

    return {
        k: v for d in results for k, v in d.items()
    }  # Combine results into a single dictionary

async def get_an_iv(
    ib: IB, item: str, sleep_time: int = 3, gentick: str = "106, 104"
) -> dict:
    stock_contract = Stock(item, "SMART", "USD") if isinstance(item, str) else item

    ticker = ib.reqMktData(
        stock_contract, genericTickList=gentick
    )  # Request market data with gentick

    await asyncio.sleep(sleep_time)  # Use asyncio.sleep instead of ib.sleep

    # Check if ticker.impliedVolatility is NaN and wait if true

    if pd.isna(ticker.impliedVolatility):
        await asyncio.sleep(2)

    ib.cancelMktData(stock_contract)

    # Return a dictionary with the symbol, price, implied volatility, and historical volatility
    key = item if isinstance(item, str) else stock_contract

    price = ticker.last if not pd.isna(ticker.last) else ticker.close  # Get last price

    iv = ticker.impliedVolatility  # Get implied volatility from ticker

    hv = ticker.histVolatility  # Get historical volatility from ticker

    return {key: {"price": price, "iv": iv, "hv": hv}}  # Return structured data

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

        if chain:
            chain = chain[-1] if isinstance(chain, list) else chain

        return chain

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

        for batch_num in _tqdm(range(total_batches), desc="Fetching option chains"):
            start_idx = batch_num * batch_size
            end_idx = min(start_idx + batch_size, len(contracts))
            batch_contracts = contracts[start_idx:end_idx]

            # Run the asynchronous chain fetching function
            batch_results = ib.run(
                chains(
                    contracts=batch_contracts,
                    ib=ib,
                    sleep_time=max_wait_time
                )
            )

            # Process results and track failed symbols
            for contract in batch_contracts:
                symbol = contract.symbol
                # pyrefly: ignore [missing-attribute]
                chain = batch_results.get(symbol)

                if chain is None:
                    failed_symbols.append(contract)
                    all_chain_data.append({
                        'symbol': symbol,
                        'conId': contract.conId,
                        'tradingClass': None,
                        'expiries': None,
                        'strikes': None
                    })
                    continue

                all_chain_data.append({
                    'symbol': symbol,
                    'conId': chain.underlyingConId,
                    'tradingClass': chain.tradingClass,
                    'expiries': chain.expirations,
                    'strikes': chain.strikes
                })

            # Add inter-batch delay to avoid rate limiting
            if batch_num < total_batches - 1:
                ib.sleep(inter_batch_delay)

        # Retry failed symbols once
        if failed_symbols:
            print(f"Retrying {len(failed_symbols)} failed symbols...")
            retry_results = ib.run(
                chains(
                    contracts=failed_symbols,
                    ib=ib,
                    sleep_time=max_wait_time
                )
            )

            # Update all_chain_data with retry results
            retry_data = []
            for contract in failed_symbols:
                symbol = contract.symbol
                # pyrefly: ignore [missing-attribute]
                chain = retry_results.get(symbol)

                if chain is None:
                    retry_data.append({
                        'symbol': symbol,
                        'conId': contract.conId,
                        'tradingClass': None,
                        'expiries': None,
                        'strikes': None
                    })
                else:
                    retry_data.append({
                        'symbol': symbol,
                        'conId': chain.underlyingConId,
                        'tradingClass': chain.tradingClass,
                        'expiries': chain.expirations,
                        'strikes': chain.strikes
                    })

            # Replace failed entries with retry results
            all_chain_data = [d for d in all_chain_data if d['symbol'] not in [c.symbol for c in failed_symbols]]
            all_chain_data.extend(retry_data)

        # Convert to DataFrame
        df = pd.DataFrame(all_chain_data)

        # Report data quality
        valid_chains = df[df['expiries'].notna()]
        print(f"\n✓ Retrieved data for {len(df)} contracts")
        print(f"✓ Valid option chains: {len(valid_chains)}/{len(df)} ({100*len(valid_chains)/len(df):.1f}%)")

        if df.empty:
            return pd.DataFrame()

        # Expand rows for each expiry and strike combination
        expanded_rows = []
        for index, row in df.iterrows():
            if row['expiries'] is None or row['strikes'] is None:
                continue
            for expiry, strike in product(row['expiries'], row['strikes']):
                expanded_rows.append({
                    'symbol': row['symbol'],
                    'expiry': expiry,
                    'strike': strike
                })

        # Create final DataFrame
        df_out = pd.DataFrame(expanded_rows)
        df_out['dte'] = get_dte(df_out['expiry'])

        return df_out

    except Exception as e:
        logger.error(f"Failed to get option chains: {e}")
        return pd.DataFrame()

    finally:
        # Disconnect from IB
        if disconnect and ib and ib.isConnected():
            ib.disconnect()
            print("Disconnected from IB\n")

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

def chains_n_unds(msg: bool = False):
    """
    Processes qualified contracts, option chains, and calculate margins.
    
    Returns:
        Tuple of DataFrames: (df_chains, df_unds)
    """
    sym_path = ROOT / 'data' / 'symbols.pkl'

    # Get qualified contracts
    if do_i_refresh(my_path=sym_path, max_days=1):
        print("symbols.pkl missing/stale — rebuilding from web + IB (takes ~2-3 min)...")
        qualified_contracts = get_qualified_symbols(weeklies=True, market="SNP", save=True)
        pickle_me(qualified_contracts, file_path=sym_path)
    else:
        qualified_contracts = get_pickle(path=sym_path, print_msg=msg)

    # Get option chains for qualified contracts
    chain_path = ROOT / 'data' / 'df_chains.pkl'
    df_chains_check = get_pickle(chain_path)
    if do_i_refresh(my_path=chain_path, max_days=1) or df_chains_check is None or (isinstance(df_chains_check, pd.DataFrame) and df_chains_check.empty):
        # pyrefly: ignore [bad-argument-type]
        df_chains = get_option_chains(qualified_contracts, market="SNP", batch_size=50)
        pickle_me(df_chains, file_path=chain_path)
    else:
        df_chains = get_pickle(path=chain_path, print_msg=msg)

    # Get price with volatilities and margins for qualified contracts
    # pyrefly: ignore [bad-argument-type]
    df_unds = get_volatilities_snapshot(qualified_contracts, market="SNP", batch_size=50)

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
        print("Warning: df_unds is empty — no volatility data retrieved")
    pickle_me(df_unds, file_path=ROOT / 'data' / 'df_unds.pkl')

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

    # pyrefly: ignore [missing-attribute]
    print(df_chains.head(10))

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

    print(df_unds[['symbol', 'iv', 'hv', 'margin', 'price']].head(10))
    pickle_me(df_unds, file_path=ROOT/'data'/'df_unds.pkl')
