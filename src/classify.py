# %%
# Classify with status - load imports

import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Union

import pandas as pd
from ib_async import Contract, Order, util

# pyrefly: ignore [missing-import]
from src.build import (
    chains_n_unds,
    do_i_refresh,
    get_dte,
    get_ib_connection,
    get_pickle,
    ROOT,
)

logger = logging.getLogger(__name__)

# %%
# Config and constants

ACTIVESTATUS = os.getenv("ACTIVESTATUS", "").split(",")

pd.set_option("display.max_columns", None)


@dataclass
class OpenOrder:
    conId: int = 0
    symbol: str = "Dummy"
    secType: str = "STK"
    expiry: datetime = datetime.now()
    strike: float = 0.0
    # pyrefly: ignore [bad-assignment]
    right: str = None
    orderId: int = 0
    # pyrefly: ignore [bad-assignment]
    contract: Contract = None
    # pyrefly: ignore [bad-assignment]
    order: Order = None
    permId: int = 0
    action: str = "SELL"
    qty: float = 0.0
    lmtPrice: float = 0.0
    # pyrefly: ignore [bad-assignment]
    status: str = None

    def empty(self):
        return pd.DataFrame([self.__dict__]).iloc[0:0]


# %%
# Utility functions
def clean_ib_util_df(
    contracts: Union[list, pd.Series],
    eod=True,
    ist=False,
) -> Union[pd.DataFrame, None]:
    """Cleans ib_async's util.df to keep only relevant columns"""
    if isinstance(contracts, pd.Series):
        ct = contracts.to_list()
    elif not isinstance(contracts, list):
        logger.error(f"Invalid type for contracts: {type(contracts)}. Must be list or pd.Series.")  # f-string ok at error level
        return None
    else:
        ct = contracts

    try:
        udf = util.df(ct)
    except (AttributeError, ValueError) as e:
        logger.error(f"Error creating DataFrame from contracts: {e}")  # f-string ok at error level
        return None

    if udf is None or udf.empty:
        return None

    udf = udf[
        [
            "symbol",
            "conId",
            "secType",
            "lastTradeDateOrContractMonth",
            "strike",
            "right",
        ]
    ]
    udf.rename(columns={"lastTradeDateOrContractMonth": "expiry"}, inplace=True)

    if len(udf.expiry.iloc[0]) != 0:
        udf["expiry"] = udf["expiry"].apply(util.formatIBDatetime)
    else:
        udf["expiry"] = pd.NaT

    udf["contract"] = ct
    return udf


# %%
# Functions to get financials, portforlio and orders
def get_ib_portfolio(account: str, msg: bool = False) -> pd.DataFrame:
    """
    Get the IB portfolio for the specified account and return it as a DataFrame.

    Args:
        account: The account code (e.g., from .env US_ACCOUNT or SG_ACCOUNT)

    Returns:
        DataFrame with portfolio items
    """
    if not account:
        raise ValueError(f"Account '{account}' not found")

    ib = get_ib_connection("SNP", account_no=account)
    try:
        portfolio_items = ib.portfolio()
        upf = util.df(portfolio_items)
        # pyrefly: ignore [missing-attribute]
        contract_df = util.df(list(upf.contract)).iloc[:, :6]
        # upf = contract_df.join(upf.drop(columns=["account", "contract"]))
        upf = contract_df.join(upf)

        upf = upf.rename(
            columns={
                "lastTradeDateOrContractMonth": "expiry",
                "marketPrice": "mktPrice",
                "marketValue": "mktVal",
                "averageCost": "avgCost",
                "unrealizedPNL": "unPnL",
                "realizedPNL": "rePnL",
            }
        )

        df_pf = upf.drop_duplicates(keep="last")
        return df_pf

    finally:
        if ib and ib.isConnected():
            ib.disconnect()
            logger.debug("Disconnected from IB")


def get_financials(account: str = "", msg: bool = False) -> dict:
    """
    Get account financial values for the specified account or all consolidated accounts.

    Args:
        account: The account code (e.g., from .env US_ACCOUNT or SG_ACCOUNT).
                 If empty or None, returns aggregated values for all accounts.

    Returns:
        dict: Current net liquidation value, cash, margins, and excess liquidity with values rounded to 2 decimals
    """
    ib = get_ib_connection("SNP", account_no=account)
    try:
        if account:
            # Fetch values for specific account
            df_acc = util.df(ib.accountValues(account=account))
        else:
            # Fetch and aggregate values for all accounts
            df_acc = util.df(ib.accountValues())  # No account specified
            # pyrefly: ignore [missing-attribute]
            if not df_acc.empty:
                # Aggregate numeric values by tag, assuming sum for financial metrics
                # pyrefly: ignore [missing-attribute]
                df_acc = (
                    df_acc.groupby("tag")
                    .agg(
                        {
                            "value": lambda x: pd.to_numeric(x, errors="coerce").sum(),
                            "currency": "first",  # Keep first currency (assumes same currency)
                        }
                    )
                    .reset_index()
                )

        d_map = {
            "NetLiquidation": "net liquidation value",
            "StockMarketValue": "stocks",
            "TotalCashBalance": "cash",
            "Cushion": "cushion",
            "InitMarginReq": "initial margin",
            "MaintMarginReq": "maintenance margin",
            "UnrealizedPnL": "unrealized pnl",
            "RealizedPnL": "realized pnl",
            "LookAheadAvailableFunds": "funds available to trade",
            "ExcessLiquidity": "excess liquidity",
        }

        # Filter and set tag as categorical to match d_map order
        # pyrefly: ignore [unsupported-operation]
        df_out = df_acc[df_acc["tag"].isin(d_map.keys())].copy()

        acc = df_out.set_index("tag")["value"].apply(float).to_dict()

        # Calculate Cushion as ExcessLiquidity / NetLiquidation
        net_liquidation = acc.get("NetLiquidation", 0)
        excess_liquidity = acc.get("ExcessLiquidity", 0)
        acc["Cushion"] = (excess_liquidity / net_liquidation) if net_liquidation != 0 else 0

        acc = {d_map.get(k): round(v, 2) for k, v in acc.items()}

        return acc

    finally:
        if ib and ib.isConnected():
            ib.disconnect()
            logger.debug("Disconnected from IB")


def get_open_orders(account_no: str, is_active: bool = False, msg: bool = False) -> pd.DataFrame:
    """
    Get open orders for the specified account.

    Args:
        account: The account code (e.g., from .env US_ACCOUNT or SG_ACCOUNT).
                 If empty or None, returns orders labeled with 'ALL'.
        is_active: If True, only return orders with active status

    Returns:
        DataFrame with open order details, including account and order columns
    """
    ib = get_ib_connection("SNP", account_no=account_no or None)
    try:
        trades = ib.reqAllOpenOrders()  # Fetch all open orders
        dfo = OpenOrder().empty()

        if trades:
            all_trades_df = (
                # pyrefly: ignore [missing-attribute]
                clean_ib_util_df([t.contract for t in trades])
                .join(util.df(t.orderStatus for t in trades))
                .join(util.df(t.order for t in trades), lsuffix="_")
            )

            # Filter by account if provided
            if account_no:
                all_trades_df["account"] = pd.Series([t.order.account for t in trades])
                all_trades_df = all_trades_df[all_trades_df["account"] == account_no]

            # Add account and order columns just before creating dfo
            account_name = "ALL" if not account_no else account_no
            all_trades_df["account"] = account_name  # Assign account or 'ALL'
            order = pd.Series([t.order for t in trades], name="order")[all_trades_df.index]
            all_trades_df = all_trades_df.assign(order=order)

            all_trades_df.rename(
                {"lastTradeDateOrContractMonth": "expiry", "totalQuantity": "qty"},
                axis="columns",
                inplace=True,
            )

            if "symbol" not in all_trades_df.columns:
                if "contract" in all_trades_df.columns:
                    all_trades_df["symbol"] = all_trades_df["contract"].apply(lambda x: x.symbol)
                else:
                    raise ValueError(
                        "Neither 'symbol' nor 'contract' column found in the DataFrame"
                    )

            # Move account column to first position
            cols = ["account"] + [col for col in all_trades_df.columns if col != "account"]
            all_trades_df = all_trades_df[cols]

            dfo = all_trades_df[dfo.columns]

            if is_active:
                dfo = dfo[dfo.status.isin(ACTIVESTATUS)]

        if "state" not in dfo.columns:
            dfo = dfo.assign(state="unknown")

        return dfo

    except Exception as e:
        logger.error("Error fetching open orders for %s: %s", account_no or "ALL", e)
        return OpenOrder().empty()

    finally:
        if ib and ib.isConnected():
            ib.disconnect()
            logger.debug("Disconnected from IB")


# %%
# Functions to classify portfolios, open orders and update unds


def classify_pf(pf):
    """
    Classifies trading strategies in a portfolio based on option and stock positions.

    Parameters:
    pf (pd.DataFrame): Portfolio DataFrame containing columns:
        - symbol: Ticker symbol
        - secType: Security type ('STK' or 'OPT')
        - right: Option right ('C', 'P', or '0' for stocks)
        - expiry: Option expiration date
        - strike: Option strike price
        - position: Position size (positive or negative)

    Returns:
    pd.DataFrame: Original DataFrame with added 'state' column containing classifications
    """
    # Create a copy to avoid modifying the original DataFrame
    pf = pf.copy()

    # Add dte column for options
    if "expiry" in pf.columns and "dte" not in pf.columns:
        pf["dte"] = pf.expiry.apply(lambda x: get_dte(x) if pd.notnull(x) else None)

    pf["state"] = "tbd"

    # First, classify all options
    option_mask = pf.secType == "OPT"

    # Classify protecting options (long calls or long puts)
    protecting_mask = option_mask & (
        ((pf.right == "C") & (pf.position > 0))  # Long call
        | ((pf.right == "P") & (pf.position > 0))  # Long put
    )
    pf.loc[protecting_mask, "state"] = "protecting"

    # Classify sowed options (short options that are not part of a spread)
    sowed_mask = option_mask & (pf.position < 0)  # All short options
    pf.loc[sowed_mask, "state"] = "sowed"

    # Now classify covering options (short calls that are part of a spread)
    # These will override the 'sowed' classification
    covering_mask = (
        option_mask
        & (pf.position < 0)
        & ((pf.right == "C") | (pf.right == "P"))  # Short call or put
    )
    # Only mark as covering if there's a corresponding long position
    has_long = pf[pf.position > 0].groupby("symbol").size()
    covering_mask = covering_mask & pf.symbol.isin(has_long.index)
    pf.loc[covering_mask, "state"] = "covering"

    # Now classify stocks based on their options
    stock_mask = pf.secType == "STK"

    # Get symbols with protecting and covering options
    symbols_with_protecting = set(pf[pf.state == "protecting"].symbol.unique())
    symbols_with_covering = set(pf[pf.state == "covering"].symbol.unique())

    # Classify stocks
    pf.loc[
        stock_mask
        & pf.symbol.isin(symbols_with_protecting)
        & ~pf.symbol.isin(symbols_with_covering),
        "state",
    ] = "uncovered"

    pf.loc[
        stock_mask
        & ~pf.symbol.isin(symbols_with_protecting)
        & pf.symbol.isin(symbols_with_covering),
        "state",
    ] = "unprotected"

    pf.loc[
        stock_mask
        & pf.symbol.isin(symbols_with_protecting)
        & pf.symbol.isin(symbols_with_covering),
        "state",
    ] = "zen"

    pf.loc[stock_mask & (pf.state == "tbd") & (pf.position != 0), "state"] = "exposed"

    # Classify orphaned options (long options without corresponding stock)
    # Get symbols that have stock positions using the existing stock_mask
    has_stock = set(pf[stock_mask].symbol.unique())

    # Mark as orphaned if:
    # 1. It's an option
    # 2. It's a long position
    # 3. The symbol doesn't have any stock position
    pf.loc[option_mask & (pf.position > 0) & ~pf.symbol.isin(has_stock), "state"] = "orphaned"

    # For any remaining unclassified positions
    pf.loc[pf.state == "tbd", "state"] = "unclassified"

    return pf


def classify_open_orders(df_openords, pf):
    """
    Classify open orders based on their characteristics and portfolio context.

    Parameters:
    df_openords (pd.DataFrame): DataFrame of open orders
    pf (pd.DataFrame): Portfolio DataFrame

    Returns:
    pd.DataFrame: Open orders DataFrame with added 'state' column
    """
    if df_openords is None or df_openords.empty:
        return df_openords

    # Create a copy to avoid modifying the original DataFrame
    df = df_openords.copy()

    # Initialize status column
    df["state"] = "unclassified"

    # Identify option orders
    opt_orders = df[df.secType == "OPT"]

    # 'covering' - option SELL order with underlying stock position
    covering_mask = (opt_orders.action == "SELL") & (
        # Call option with positive stock position
        (
            (opt_orders.right == "C")
            & (opt_orders.symbol.isin(pf[(pf.secType == "STK") & (pf.position > 0)].symbol))
        )
        |
        # Put option with negative stock position
        (
            (opt_orders.right == "P")
            & (opt_orders.symbol.isin(pf[(pf.secType == "STK") & (pf.position < 0)].symbol))
        )
    )

    df.loc[covering_mask[covering_mask].index, "state"] = "covering"

    # 'protecting' - option BUY order with underlying stock position
    protecting_mask = (opt_orders.action == "BUY") & (
        # Put option protecting long stock position
        (
            (opt_orders.right == "P")
            & (opt_orders.symbol.isin(pf[(pf.secType == "STK") & (pf.position > 0)].symbol))
        )
        |
        # Call option protecting short stock position
        (
            (opt_orders.right == "C")
            & (opt_orders.symbol.isin(pf[(pf.secType == "STK") & (pf.position < 0)].symbol))
        )
    )
    df.loc[protecting_mask[protecting_mask].index, "state"] = "protecting"

    # 'sowing' - option SELL order without underlying stock position
    sowing_mask = (opt_orders.action == "SELL") & (
        ~opt_orders.symbol.isin(pf[(pf.secType == "STK")].symbol)
    )
    df.loc[sowing_mask[sowing_mask].index, "state"] = "sowing"

    # 'reaping' - option BUY order with matching existing option position
    reaping_mask = opt_orders.apply(
        lambda row: (
            row.action == "BUY"
            and not pf[
                (pf.secType == "OPT")
                & (pf.symbol == row.symbol)
                & (pf.right == row.right)
                & (pf.strike == row.strike)
            ].empty
        ),
        axis=1,
    )
    df.loc[reaping_mask[reaping_mask].index, "state"] = "reaping"

    # 'de-orphaning' - option SELL order with matching existing option position
    de_orphaning_mask = opt_orders.apply(
        lambda row: (
            row.action == "SELL"
            and not pf[
                (pf.secType == "OPT")
                & (pf.symbol == row.symbol)
                & (pf.right == row.right)
                & (pf.strike == row.strike)
            ].empty
        ),
        axis=1,
    )
    df.loc[de_orphaning_mask[de_orphaning_mask].index, "state"] = "de-orphaning"

    # 'straddling' - two option BUY orders for same symbol not in portfolio
    # Group by symbol and count BUY actions
    straddle_symbols = (
        opt_orders[(opt_orders.action == "BUY")]
        .groupby("symbol")
        .filter(lambda x: len(x) >= 2)["symbol"]
        .unique()
    )

    straddle_mask = (
        (opt_orders.action == "BUY")
        & (opt_orders.symbol.isin(straddle_symbols))
        & (~opt_orders.symbol.isin(pf.symbol))
    )
    df.loc[straddle_mask[straddle_mask].index, "state"] = "straddling"

    return df


def update_unds_status(
    df_unds: pd.DataFrame, df_pf: pd.DataFrame, df_openords: pd.DataFrame
) -> pd.DataFrame:
    """
    Update underlying symbols status based on portfolio and open orders.

    Parameters:
    df_unds (pd.DataFrame): Underlying symbols DataFrame
    df_pf (pd.DataFrame): Portfolio DataFrame

    Returns:
    pd.DataFrame: Updated underlying symbols DataFrame with 'state' column
    """
    df_unds = df_unds.drop(
        columns=[
            "mktPrice",
            "state",
        ],
        errors="ignore",
    ).merge(
        df_pf[df_pf["secType"] == "STK"][["symbol", "mktPrice", "state"]],
        on="symbol",
        how="left",
        suffixes=("", "_new"),
    )

    # update status from df_pf for stock symbols
    stk_symbols = df_pf[df_pf.secType == "STK"].symbol
    stk_state_dict = dict(
        zip(
            df_pf.loc[df_pf.secType == "STK", "symbol"],
            df_pf.loc[df_pf.secType == "STK", "state"],
        )
    )

    df_unds.loc[df_unds.symbol.isin(stk_symbols), "state"] = df_unds.loc[
        df_unds.symbol.isin(stk_symbols)
    ].symbol.map(stk_state_dict)

    # ..update status for symbols not in df_pf
    df_unds.loc[~df_unds.symbol.isin(df_pf.symbol.unique()), "state"] = "virgin"

    # Zen conditions
    zen_symbols = set()

    # 1. Symbols with both covering and protecting positions are zen
    for symbol, group in df_openords.groupby("symbol"):
        if len(group) == 2 and {"covering", "protecting"}.issubset(set(group.state)):
            zen_symbols.add(symbol)
        else:
            group = df_pf[df_pf.symbol == symbol]
            if len(group) == 2 and {"covering", "protecting"}.issubset(set(group.state)):
                zen_symbols.add(symbol)

    # 2. Symbols with 'straddled' portfolio state
    straddled_symbols = df_pf[df_pf.state == "straddled"].symbol
    zen_symbols.update(straddled_symbols)

    # 3. Symbols with short 'sowing' order
    sowing_symbols = df_openords[df_openords.state == "sowing"].symbol
    zen_symbols.update(sowing_symbols)

    # 4. Unprotected with protecting order
    unprotected_with_protect = df_pf[
        (df_pf.state == "unprotected")
        & df_pf.symbol.isin(df_openords[df_openords.state == "protecting"].symbol)
    ].symbol
    zen_symbols.update(unprotected_with_protect)

    # 5. Uncovered with covering order
    uncovered_with_cover = df_pf[
        (df_pf.state == "uncovered")
        & df_pf.symbol.isin(df_openords[df_openords.state == "covering"].symbol)
    ].symbol
    zen_symbols.update(uncovered_with_cover)

    # 6. Long 'orphaned' position with 'de-orphaning' order
    orphaned_with_deorphan = df_pf[
        (df_pf.state == "orphaned")
        & df_pf.symbol.isin(df_openords[df_openords.state == "de-orphaning"].symbol)
    ].symbol
    zen_symbols.update(orphaned_with_deorphan)

    # 7. Short 'sowed' position with 'reaping' order
    sowed_with_reap = df_pf[
        (df_pf.state == "sowed")
        & df_pf.symbol.isin(df_openords[df_openords.state == "reaping"].symbol)
    ].symbol
    zen_symbols.update(sowed_with_reap)

    # 8. Short 'orphaned' position with 'virgin' order
    orphaned_with_virgin = df_pf[
        (df_pf.state == "orphaned")
        & ~df_pf.symbol.isin(df_openords[df_openords.state == "virgin"].symbol)
    ].symbol
    zen_symbols.update(orphaned_with_virgin)

    # Update status for zen symbols
    df_unds.loc[df_unds.symbol.isin(zen_symbols), "state"] = "zen"

    # Unreaped: Symbol has a short option position with no open 'reaping' order
    unreaped_symbols = df_pf[
        (df_pf.state == "sowed")
        & ~df_pf.symbol.isin(df_openords[df_openords.state == "reaping"].symbol)
    ].symbol

    # Update status for unreaped symbols
    df_unds.loc[df_unds.symbol.isin(unreaped_symbols), "state"] = "unreaped"

    # Unprotected: Symbol has an exposed state with only one 'covering' order
    unprotected_symbols = []
    for symbol in df_pf[df_pf.state == "exposed"].symbol:
        openord_group = df_openords[df_openords.symbol == symbol]
        if len(openord_group) == 1 and openord_group.iloc[0].state == "covering":
            unprotected_symbols.append(symbol)

    # Update status for unprotected symbols
    df_unds.loc[df_unds.symbol.isin(unprotected_symbols), "state"] = "unprotected"

    # Uncovered: Symbol has an exposed state with only one 'protecting' order
    uncovered_symbols = []
    for symbol in df_unds[df_unds.state == "exposed"].symbol:
        openord_group = df_openords[df_openords.symbol == symbol]
        if len(openord_group) == 1 and openord_group.iloc[0].state == "protecting":
            uncovered_symbols.append(symbol)

    # Update status for uncovered symbols
    df_unds.loc[df_unds.symbol.isin(uncovered_symbols), "state"] = "uncovered"

    # Orphaned: Symbol has an 'orphaned' state with no open orders
    orphaned_symbols = df_pf[
        (df_pf.state == "orphaned") & ~df_pf.symbol.isin(df_openords.symbol)
    ].symbol

    # Update status for orphaned symbols
    df_unds.loc[df_unds.symbol.isin(orphaned_symbols), "state"] = "orphaned"

    # Classify short stock positions without covering/protecting options as 'exposed'
    # Get all short stock positions from portfolio
    short_stocks = df_pf[(df_pf.secType == "STK") & (df_pf.position < 0)]["symbol"]

    # Find short stocks that don't have covering or protecting options
    exposed_short_stocks = []
    for symbol in short_stocks:
        # Check if there are any covering or protecting options in portfolio or open orders
        has_covering = (df_pf.symbol == symbol) & (df_pf.state == "covering")
        has_protecting = (df_pf.symbol == symbol) & (df_pf.state == "protecting")
        has_covering_orders = (df_openords.symbol == symbol) & (df_openords.state == "covering")
        has_protecting_orders = (df_openords.symbol == symbol) & (df_openords.state == "protecting")

        if not (
            has_covering.any()
            or has_protecting.any()
            or has_covering_orders.any()
            or has_protecting_orders.any()
        ):
            exposed_short_stocks.append(symbol)

    # Update status for exposed short stocks
    df_unds.loc[df_unds.symbol.isin(exposed_short_stocks), "state"] = "exposed"

    return df_unds


def classifed_results(account_no: str, max_days: int = 1, msg: bool = False) -> dict:
    """
    Retrieve and process portfolio data, financials, and orders for specified account.

    Args:
        account_no (str): IB Account number
        max_days (int): Maximum days for data refresh check

    Returns:
        dict: Dictionary containing DataFrames and financial data
    """
    root_path = ROOT
    result = {}

    # Check if data needs refresh and load chains and underlying data
    chain_path = ROOT / "data" / "df_chains.pkl"
    df_chains_check = get_pickle(chain_path, print_msg=msg)
    if (
        do_i_refresh(my_path=chain_path, max_days=max_days)
        or df_chains_check is None
        or (isinstance(df_chains_check, pd.DataFrame) and df_chains_check.empty)
    ):
        result["df_chains"], result["df_unds"] = chains_n_unds()
    else:
        result["df_chains"] = get_pickle(path=root_path / "data" / "df_chains.pkl", print_msg=msg)
        result["df_unds"] = get_pickle(path=root_path / "data" / "df_unds.pkl", print_msg=msg)

    logger.info("Getting portfolio for account: %s", account_no)

    result["df_pf"] = get_ib_portfolio(account=account_no)
    result["df_pf"] = classify_pf(result["df_pf"])

    result["df_openords"] = get_open_orders(account_no=account_no, is_active=True)
    result["df_openords"] = classify_open_orders(result["df_openords"], result["df_pf"])

    # pyrefly: ignore [bad-argument-type]
    result["df_unds"] = update_unds_status(
        result["df_unds"], result["df_pf"], result["df_openords"]
    )

    logger.debug(
        "%d stocks, %d options in df_pf",
        len(result["df_pf"][result["df_pf"].secType == "STK"]),
        len(result["df_pf"][result["df_pf"].secType == "OPT"]),
    )
    logger.debug("%d open orders", len(result["df_openords"]))

    return result


# %%
# Test Functions
if __name__ == "__main__":
    import argparse
    from src.log_utils import setup_logging

    _p = argparse.ArgumentParser()
    _p.add_argument("--debug", action="store_true", help="Show DEBUG output in terminal")
    setup_logging("classify", debug=_p.parse_args().debug)

    ACCOUNT = "US_ACCOUNT"
    ACCOUNT_NO = os.getenv(ACCOUNT, "")

    if do_i_refresh(my_path=ROOT / "data" / "df_unds.pkl", max_days=1):
        df_chains, df_unds = chains_n_unds()
    else:
        df_chains = get_pickle(path=ROOT / "data" / "df_chains.pkl")
        df_unds = get_pickle(path=ROOT / "data" / "df_unds.pkl")

    # Get financials for US, SG and consolidated accounts
    us_fin = get_financials(account=os.getenv("US_ACCOUNT", ""))
    sg_fin = get_financials(account=os.getenv("SG_ACCOUNT", ""))
    fin = get_financials()

    logger.info("Consolidated Financials: %s", fin)
    logger.info("US Account Financials: %s", us_fin)
    logger.info("SG Account Financials: %s", sg_fin)

    logger.info("Using account: %s", ACCOUNT)

    df_pf = get_ib_portfolio(account=ACCOUNT_NO)
    df_pf = classify_pf(df_pf)
    logger.info(
        "%d stocks, %d options in df_pf",
        len(df_pf[df_pf.secType == "STK"]),
        len(df_pf[df_pf.secType == "OPT"]),
    )

    df_openords = get_open_orders(account_no=ACCOUNT_NO, is_active=True)
    df_openords = classify_open_orders(df_openords, df_pf)

    logger.info("%d open orders", len(df_openords))

    # pyrefly: ignore [bad-argument-type]
    df_unds = update_unds_status(df_unds, df_pf, df_openords)
    logger.info("%d underlyings after status update", len(df_unds))

# %%
