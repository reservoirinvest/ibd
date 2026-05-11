#%%
# IMPORTS
import os
import time

import numpy as np
import pandas as pd
from ib_async import Option, util
from loguru import logger
# pyrefly: ignore [missing-import]
from build import (
    ROOT,
    atm_margin,
    delete_pkl_files,
    get_dte,
    get_ib_connection,
    get_prec,
    get_volatilities_snapshot,
    load_config,
    pickle_me,
    qualify_me,
)

# pyrefly: ignore [missing-import]
from classify import classifed_results, clean_ib_util_df, get_financials, get_open_orders, classify_open_orders

# Start timing the script execution
start_time = time.time()

def filter_closest_dates(chains, protect_dte, num_dates=2):
    """
    Filter rows from chains DataFrame to get the closest dates to protect_dte for each symbol.

    Args:
        chains (pd.DataFrame): DataFrame containing 'symbol' and 'dte' columns
        protect_dte (datetime): The target date to find closest dates to
        num_dates (int): Number of closest dates to return per symbol (default: 2)

    Returns:
        pd.DataFrame: Filtered DataFrame containing only the rows with the closest dates for each symbol
    """
    result = []

    for symbol, group in chains.groupby('symbol'):
        group = group.copy()
        group['date_diff'] = (group['dte'] - protect_dte).abs()
        unique_dates = group[['dte', 'date_diff']].drop_duplicates(subset=['dte'])
        closest_dates = unique_dates.nsmallest(num_dates, 'date_diff')['dte']
        filtered_group = group[group['dte'].isin(closest_dates)].drop(columns=['date_diff'])
        result.append(filtered_group)

    return pd.concat(result, ignore_index=True) if result else pd.DataFrame()

def filter_closest_strikes(chains, n=-2):
    """
    Filter rows to get the closest strikes to undPrice for each symbol and expiry.

    Args:
        chains (pd.DataFrame): DataFrame containing 'symbol', 'dte', 'strike', 'undPrice' columns
        n (int): Number of strikes to return.
                 If positive (calls for shorts): returns n closest strikes >= undPrice, sorted by strike ascending
                 If negative (puts for longs): returns |n| closest strikes <= undPrice, sorted by strike descending

    Returns:
        pd.DataFrame: Filtered DataFrame with closest strikes
    """
    if n == 0:
        return pd.DataFrame()

    result = []
    abs_n = abs(n)

    for (symbol, expiry), group in chains.groupby(['symbol', 'dte']):
        group = group.copy()
        filtered = group.copy()

        if n > 0:
            filtered = group[group['strike'] >= group['undPrice']]
            filtered = filtered.sort_values('strike', ascending=True)
        else:
            filtered = group[group['strike'] <= group['undPrice']]
            filtered = filtered.sort_values('strike', ascending=False)

        if not filtered.empty:
            result.append(filtered.head(abs_n))

    return pd.concat(result, ignore_index=True) if result else pd.DataFrame()

#%% CONFIG AND SETUP
ACCOUNT = 'US_ACCOUNT'
ACCOUNT_NO = os.getenv(ACCOUNT, "")

log_file_path = ROOT / "log" / "derive.log"
logger.add(log_file_path, rotation="2 days", encoding="utf-8")
util.logToFile(log_file_path, level=40) # IB ERRORs only logged.

config = load_config("SNP")
PROTECT_ME = config.get("PROTECT_ME", True)
PROTECT_DTE = config.get("PROTECT_DTE", 30)
PROTECTION_STRIP = config.get("PROTECTION_STRIP", 3)
COVER_ME = config.get("COVER_ME")
REAP_ME = config.get("REAP_ME")
COVER_MIN_DTE = config.get("COVER_MIN_DTE")
VIRGIN_DTE = config.get("VIRGIN_DTE")
MAX_FILE_AGE = config.get("MAX_FILE_AGE")
VIRGIN_QTY_MULT = config.get("VIRGIN_QTY_MULT")
COVXPMULT = config.get("COVXPMULT")
COV_STD_MULT = config.get('COVER_STD_MULT')
MINNAKEDOPTPRICE = config.get("MINNAKEDOPTPRICE")
NAKEDXPMULT = config.get("NAKEDXPMULT")
REAPRATIO = config.get("REAPRATIO")
MINREAPDTE = config.get("MINREAPDTE")
SOW_NAKEDS = config.get("SOW_NAKEDS")
VIRGIN_CALL_STD_MULT = config.get("VIRGIN_CALL_STD_MULT")
VIRGIN_PUT_STD_MULT = config.get("VIRGIN_PUT_STD_MULT")

print('Getting financials...')
fin = get_financials(ACCOUNT_NO)

#%% GET CLASSIFIED DATA
print("\n=== GETTING CLASSIFIED PORTFOLIO DATA ===")
data = classifed_results(account_no=ACCOUNT_NO)
df_unds = data["df_unds"]
df_pf = data["df_pf"]
chains = data["df_chains"]

df_unds = df_unds.merge(
    df_pf[df_pf.secType == "STK"][["symbol", "position", "avgCost"]], on="symbol", how="left"
).fillna({"position": 0, "avgCost": 0})

print(f"Loaded {len(df_unds)} underlyings")
print(f"Loaded {len(df_pf)} portfolio positions")
print(f"Loaded {len(chains)} chain entries")

#%% MAKE COVERS FOR EXPOSED AND UNCOVERED STOCK POSITIONS
print("\n=== MAKE COVERS FOR EXPOSED AND UNCOVERED STOCK POSITIONS ===")

delete_pkl_files(['df_cov.pkl'])

df_cov = pd.DataFrame()

uncov = df_unds.state.isin(["exposed", "uncovered"])
uncov_long = df_unds[uncov & (df_unds.position > 0)].reset_index(drop=True)

if not uncov_long.empty:
    print(f"Processing {len(uncov_long)} long uncovered/exposed positions...")

    df_cc = (
        chains[chains.symbol.isin(uncov_long.symbol.unique())]
        .loc[(chains.dte.between(COVER_MIN_DTE, COVER_MIN_DTE + 7))][
            ["symbol", "expiry", "strike", "dte"]
        ]
        .sort_values(["symbol", "dte"])
        .reset_index(drop=True)
    )

    df_cc = df_cc.merge(df_unds[["symbol", "price", "iv", "avgCost"]], on="symbol", how="left")
    df_cc.rename(columns={"price": "undPrice", "iv": "vy"}, inplace=True)

    long_put_cost = (
        df_pf[(df_pf.right == "P") & (df_pf.position > 0)]
        .assign(avgCostPerShare=lambda x: x.avgCost / 100)
        .groupby("symbol", as_index=False)["avgCostPerShare"].sum()
        .rename(columns={"avgCostPerShare": "longPutCost"})
    )

    df_cc = df_cc.merge(long_put_cost, on="symbol", how="left")
    df_cc["longPutCost"] = df_cc["longPutCost"].fillna(0)

    df_cc["sdev"] = df_cc.undPrice * df_cc.vy * (df_cc.dte / 365) ** 0.5

    vol_based_price = df_cc.undPrice + config.get("COVER_STD_MULT") * df_cc.sdev
    df_cc["covPrice"] = np.maximum(df_cc.avgCost + df_cc.longPutCost, vol_based_price)

    no_of_options = 3

    cc_long = (
        df_cc.groupby(["symbol", "expiry"])[["symbol", "expiry", "strike", "undPrice", "sdev", "covPrice"]]
        .apply(
            lambda x: x[x["strike"] > x["covPrice"]]
            .assign(diff=x["strike"] - x["covPrice"])
            .sort_values("diff")
            .head(no_of_options)
        )
        .drop(columns=["level_2", "diff"], errors="ignore")
    )

    cov_calls = [
        Option(s, e, k, "C", "SMART")
        for s, e, k in zip(cc_long.symbol, cc_long.expiry, cc_long.strike)
    ]

    print("Qualifying covered call contracts...")
    with get_ib_connection("SNP", account_no=ACCOUNT_NO) as ib:
        valid_contracts = qualify_me(ib, cov_calls, desc="Qualifying covered call contracts")
    valid_contracts = [v for v in valid_contracts if v is not None]

    if valid_contracts:
        df_cc1 = clean_ib_util_df(valid_contracts)

        df_ccf = df_cc1.loc[df_cc1.groupby("symbol")["strike"].idxmin()]

        df_ccf = df_ccf.reset_index(drop=True)

        df_ccf = df_ccf.merge(
            df_unds[["symbol", "price", "iv"]], on="symbol", how="left"
        )
        df_ccf.rename(columns={"price": "undPrice", "iv": "vy"}, inplace=True)

        df_ccf = df_ccf.merge(
            df_pf[df_pf.state.isin(["uncovered", "exposed"]) & (df_pf.secType == "STK")][["symbol", "position", "avgCost"]],
            on="symbol", how="left"
        )

        df_ccf["action"] = "SELL"
        df_ccf["qty"] = df_ccf["position"] / 100
        df_ccf = df_ccf.drop(columns=["position"])

        print("Getting covered call prices...")
        df_iv_cc = get_volatilities_snapshot(df_ccf["contract"].tolist(), market="SNP")

        if not df_iv_cc.empty:
            df_ccf = df_ccf.merge(df_iv_cc[["symbol", "price"]], on="symbol", how="left")

            df_ccf["margin"] = df_ccf.apply(
                lambda x: atm_margin(x.strike, x.undPrice, get_dte(x.expiry), x.vy), axis=1
            )
        else:
            print("No option price data available for covered calls")
    else:
        print("No valid contracts after qualification")
        df_ccf = pd.DataFrame()
else:
    df_ccf = pd.DataFrame()
    print("No long uncovered/exposed positions")

uncov_short = df_unds[uncov & (df_unds.position < 0)].reset_index(drop=True)

if not uncov_short.empty:
    print(f"Processing {len(uncov_short)} short uncovered/exposed positions...")

    df_cp = (
        chains[chains.symbol.isin(uncov_short.symbol.unique())]
        .loc[(chains.dte.between(COVER_MIN_DTE, COVER_MIN_DTE + 7))][
            ["symbol", "expiry", "strike", "dte"]
        ]
        .sort_values(["symbol", "dte"])
        .reset_index(drop=True)
    )

    df_cp = df_cp.merge(df_unds[["symbol", "price", "iv", "avgCost"]], on="symbol", how="left")
    df_cp.rename(columns={"price": "undPrice", "iv": "vy"}, inplace=True)

    df_cp["sdev"] = df_cp.undPrice * df_cp.vy * (df_cp.dte / 365) ** 0.5

    vol_based_price = df_cp.undPrice - config.get("COVER_STD_MULT") * df_cp.sdev
    df_cp["covPrice"] = np.minimum(df_cp.avgCost, vol_based_price)

    no_of_options = 3

    cp_short = (
        df_cp.groupby(["symbol", "expiry"])[["symbol", "expiry", "strike", "undPrice", "sdev", "covPrice"]]
        .apply(
            lambda x: x[x["strike"] < x["covPrice"]]
            .assign(diff=x["covPrice"] - x["strike"])
            .sort_values("diff")
            .head(no_of_options)
        )
        .drop(columns=["level_2", "diff"], errors="ignore")
    )

    cov_puts = [
        Option(s, e, k, "P", "SMART")
        for s, e, k in zip(cp_short.symbol, cp_short.expiry, cp_short.strike)
    ]

    print("Qualifying covered put contracts...")
    with get_ib_connection("SNP") as ib:
        valid_contracts = qualify_me(ib, cov_puts, desc="Qualifying covered put contracts")
    valid_contracts = [v for v in valid_contracts if v is not None]

    if valid_contracts:
        df_cp1 = clean_ib_util_df(valid_contracts)

        df_cpf = df_cp1.loc[df_cp1.groupby("symbol")["strike"].idxmax()]

        df_cpf = df_cpf.reset_index(drop=True)

        df_cpf = df_cpf.merge(
            df_unds[["symbol", "price", "iv"]], on="symbol", how="left"
        )
        df_cpf.rename(columns={"price": "undPrice", "iv": "vy"}, inplace=True)

        df_cpf = df_cpf.merge(
            df_pf[df_pf.state.isin(["uncovered", "exposed"]) & (df_pf.secType == "STK")][["symbol", "position", "avgCost"]],
            on="symbol", how="left"
        )

        df_cpf["action"] = "SELL"
        df_cpf["qty"] = abs(df_cpf["position"]) / 100
        df_cpf = df_cpf.drop(columns=["position"])

        print("Getting covered put prices...")
        df_iv_cp = get_volatilities_snapshot(df_cpf["contract"].tolist(), market="SNP")

        if not df_iv_cp.empty:
            df_cpf = df_cpf.merge(df_iv_cp[["symbol", "price"]], on="symbol", how="left")

            df_cpf["margin"] = df_cpf.apply(
                lambda x: atm_margin(x.strike, x.undPrice, get_dte(x.expiry), x.vy), axis=1
            )
        else:
            print("No option price data available for covered puts")

    else:
        print("No valid contracts after qualification")
        df_cpf = pd.DataFrame()
else:
    df_cpf = pd.DataFrame()
    print("No short uncovered/exposed positions")

df_cov = pd.concat([df_ccf, df_cpf], ignore_index=True)

cov_path = ROOT / "data" / "df_cov.pkl"

if not df_cov.empty:
    df_cov.insert(4, "dte", df_cov.expiry.apply(get_dte))

    df_cov = df_cov.dropna(subset=["price"])

    df_cov["xPrice"] = df_cov.apply(
        lambda x: max(get_prec(x.price*COVXPMULT, 0.01), 0.05)
        if x.qty != 0 else 0,
        axis=1,
    )

    pickle_me(df_cov, cov_path)

    cost = (df_cov.avgCost * df_cov.qty * 100).sum()
    premium = (df_cov.xPrice * df_cov.qty * 100).sum()
    maxProfit = (
        np.where(
            df_cov.right == "C",
            (df_cov.strike - df_cov.undPrice) * df_cov.qty * 100,
            (df_cov.undPrice - df_cov.strike) * df_cov.qty * 100,
        ).sum()
        + premium
    )

    print(f"Position Cost: $ {cost:,.2f}")
    print(f"Cover Premium: $ {premium:,.2f}")
    print(f"Max Profit: $ {maxProfit:,.2f}\n")
else:
    print("No covers available!\n")

#%% MAKE SOWING CONTRACTS FOR VIRGIN AND ORPHANED SYMBOLS
print("\n=== MAKE SOWING CONTRACTS FOR VIRGIN AND ORPHANED SYMBOLS ===")

delete_pkl_files(['df_nkd.pkl'])

df_v = df_unds[(df_unds.state == "virgin") | (df_unds.state == "orphaned")].reset_index(drop=True)

df_virg = chains.loc[
    chains[chains.symbol.isin(df_v.symbol.to_list())]
    .groupby(["symbol", "strike"])["dte"]
    .apply(lambda x: x.sub(VIRGIN_DTE).abs().idxmin())
]

df_virg = df_virg.merge(df_unds[["symbol", "price", "iv"]],
            on="symbol", how="left")
df_virg.rename(columns={"price": "undPrice", "iv": "vy"}, inplace=True)

df_virg["sdev"] = df_virg.undPrice * df_virg.vy * (df_virg.dte / 365) ** 0.5

v_std = config.get("VIRGIN_PUT_STD_MULT", 3)
no_of_options = 4

df_virg = df_virg.sort_values(["symbol", "expiry", "strike"], ascending=[True, True, False])

virg_short = (
    df_virg.groupby(["symbol", "expiry"])[["symbol", "expiry", "strike", "undPrice", "sdev"]]
    .apply(
        lambda x: x[x["strike"] < x["undPrice"] - v_std * x["sdev"]]
        .assign(diff=abs(x["strike"] - (x["undPrice"] - v_std * x["sdev"])))
        .sort_values("diff")
        .head(no_of_options)
    )
    .drop(columns=["level_2", "diff"], errors="ignore")
)

virg_puts = [
    Option(s, e, k, "P", "SMART")
    for s, e, k in zip(virg_short.symbol, virg_short.expiry, virg_short.strike)
    if not pd.isna(k)
]

df_nkd = pd.DataFrame()

if virg_puts:
    print("Qualifying virgin put contracts...")
    with get_ib_connection("SNP") as ib:
        valid_contracts = qualify_me(ib, virg_puts, desc="Qualifying virgin put contracts", batch_size=150)
    valid_contracts = [v for v in valid_contracts if v is not None]

    if valid_contracts:
        df_virg1 = clean_ib_util_df(valid_contracts)

        df_virg1["dte"] = df_virg1.expiry.apply(lambda x: get_dte(x))

        nakeds = df_virg1.loc[df_virg1.groupby("symbol")["strike"].idxmax()]

        nakeds = nakeds.reset_index(drop=True)

        nakeds = nakeds.merge(
            df_unds[["symbol", "price", "iv"]], on="symbol", how="left"
        )
        nakeds.rename(columns={"price": "undPrice", "iv": "vy"}, inplace=True)

        print("Getting naked put prices...")
        df_iv_n = get_volatilities_snapshot(nakeds["contract"].tolist(), market="SNP")

        if not df_iv_n.empty:
            df_nkd = nakeds.merge(
                df_iv_n[["symbol", "price"]],
                on="symbol",
                how="left",
            )

            df_nkd["margin"] = df_nkd.apply(
                lambda x: atm_margin(x.strike, x.undPrice, get_dte(x.expiry), x.vy), axis=1
            )

            max_fund_per_symbol = VIRGIN_QTY_MULT * fin.get("net liquidation value", 0)
            df_nkd["qty"] = df_nkd.margin.apply(
                lambda x: max(1, int(max_fund_per_symbol / x)) if x > 0 else 1
            )
            df_nkd['xPrice'] = df_nkd.apply(
                lambda x: get_prec(max(x.price*NAKEDXPMULT, MINNAKEDOPTPRICE / x.qty), 0.01), axis=1)

            nkd_path = ROOT / "data" / "df_nkd.pkl"
            pickle_me(df_nkd, nkd_path)

            premium = (df_nkd.xPrice * 100 * df_nkd.qty).sum()
            print(f"Naked Premiums: $ {premium:,.2f}\n")
        else:
            print("No option price data available for naked puts")
    else:
        print("No valid contracts after qualification")
else:
    print("No suitable put chains found for virgin/orphaned")

#%% MAKE REAPS
print("\n=== MAKE REAPS ===")

df_sowed = df_unds[df_unds.state == "unreaped"].reset_index(drop=True)

df_reap = df_pf[df_pf.symbol.isin(df_sowed.symbol)
            & (df_pf.secType == "OPT")].reset_index(drop=True)

df_reap = df_reap[df_reap.expiry.apply(get_dte) > MINREAPDTE].reset_index(drop=True)

df_reap = df_reap.merge(
    df_unds[["symbol", "iv", "price"]], on="symbol", how="left"
)
df_reap.rename(columns={"iv": "vy", "price": "undPrice"}, inplace=True)

if not df_reap.empty:
    print(f"Processing {len(df_reap)} unreaped positions...")

    print("Qualifying reap contracts...")
    with get_ib_connection("SNP") as ib:
        valid_contracts = qualify_me(ib, df_reap["contract"].tolist(), desc="Qualifying reap contracts")
    valid_contracts = [v for v in valid_contracts if v is not None]

    if valid_contracts:
        print("Calculating reap option prices...")
        reap_prices = {}
        df_reap_prices = get_volatilities_snapshot(valid_contracts, market='SNP')
        df_reap['optPrice'] = df_reap.merge(df_reap_prices, on="symbol")["price"]

        df_reap["xPrice"] = df_reap["optPrice"].apply(
            lambda x: get_prec(max(0.01, x), 0.01) if pd.notna(x) else 0.01
        )

        df_reap['xPrice'] = df_reap.apply(lambda x: min(x.xPrice, get_prec(abs(x.avgCost*REAPRATIO/100), 0.01)), axis=1)
        df_reap['qty'] = df_reap.position.abs().astype(int)

        reaps = (abs(df_reap.mktPrice - df_reap.xPrice)*df_reap.qty*100).sum()

        reap_path = ROOT / "data" / "df_reap.pkl"
        pickle_me(df_reap, reap_path)
        print(f'Have {len(df_reap)} reaping options unlocking US$ {reaps:,.0f}\n')
    else:
        print("No valid contracts after qualification")
else:
    print("No unreaped positions")

#%% EXTRACT ORPHANED CONTRACTS FROM df_pf
print("\n=== EXTRACT ORPHANED CONTRACTS FROM df_pf ===")

delete_pkl_files(['df_deorph.pkl'])

df_deorph = df_pf[(df_pf.state == "orphaned") & (df_pf.secType == "OPT")].copy()

df_openords = get_open_orders(account_no=ACCOUNT_NO)
df_openords = classify_open_orders(df_openords, df_pf)


df_deorph = df_deorph[~df_deorph.symbol.isin(
    df_openords.loc[df_openords.state == 'de-orphaning', 'symbol']
)]

if not df_deorph.empty:
    print(f"Processing {len(df_deorph)} orphaned positions...")

    print("Qualifying orphaned contracts...")
    with get_ib_connection("SNP") as ib:
        valid_contracts = qualify_me(ib, df_deorph["contract"].tolist(), desc="Qualifying orphaned contracts")
    valid_contracts = [v for v in valid_contracts if v is not None]

    if valid_contracts:
        df_deorph["qty"] = df_deorph.position.abs().astype(int)
        df_deorph["xPrice"] = df_deorph["mktPrice"].apply(lambda x: max(0.09, get_prec(x, 0.1)))

        deorph_total = (df_deorph.mktPrice * df_deorph.qty * 100).sum()

        deorph_path = ROOT / 'data' / 'df_deorph.pkl'
        pickle_me(df_deorph, deorph_path)
        print(f'Have {len(df_deorph)} orphaned options with total value US$ {deorph_total:,.0f}\n')
    else:
        print("No valid contracts after qualification")
else:
    print("There are no orphaned options to process\n")

#%% IDENTIFY UNPROTECTED POSITIONS
print("\n=== IDENTIFYING UNPROTECTED POSITIONS ===")

delete_pkl_files(['df_protect.pkl'])

if not PROTECT_ME:
    print("PROTECT_ME is False. No protection recommendations will be generated.")
    df_ulong = pd.DataFrame()
    df_ushort= pd.DataFrame()

else:
    df_unprot = df_unds[df_unds.state.isin(["unprotected", "exposed"])].reset_index(
        drop=True
    )
    print(f"Found {len(df_unprot)} unprotected/exposed positions")

    # Separate long and short positions
    df_ulong = df_unprot[df_unprot.position > 0].copy()
    df_ushort = df_unprot[df_unprot.position < 0].copy()

    print(f"Long unprotected: {len(df_ulong)}")
    print(f"Short unprotected: {len(df_ushort)}")

#%% BUILD LONG PROTECTION (PUTS FOR LONG STOCK)
print("\n=== BUILDING LONG PROTECTION RECOMMENDATIONS ===")

df_lprot = pd.DataFrame()

if not df_ulong.empty:
    print(f"Processing {len(df_ulong)} long positions...")

    # Get chains nearest to PROTECT_DTE
    mask = chains.symbol.isin(df_ulong.symbol)
    df_uch = (
        chains[mask]
        .groupby(["symbol", "strike"])["dte"]
        .apply(lambda x: x.sub(PROTECT_DTE).abs().idxmin())
    )
    # Use .values to get integer indices from idxmin()
    df_uch = chains.loc[df_uch.values].reset_index(drop=True)

    # Get PROTECTION_STRIP OTM puts (strikes below undPrice)
    df_ul = df_uch[df_uch.symbol.isin(df_ulong.symbol)].copy()
    df_ul = df_ul.sort_values(
        ["symbol", "expiry", "strike"], ascending=[True, True, False]
    )
    df_ul = df_ul.merge(df_unds[["symbol", "price"]], on="symbol", how="left")
    df_ul.rename(columns={"price": "undPrice"}, inplace=True)

    # Filter for puts below underlying price
    def get_otm_puts(group):
        und_price = group["undPrice"].iloc[0]
        otm_puts = group[group["strike"] <= und_price].head(PROTECTION_STRIP)
        return otm_puts

    df_ul = df_ul.groupby("symbol", group_keys=True).apply(get_otm_puts, include_groups=False).reset_index()

    if not df_ul.empty:
        df_ul["right"] = "P"
        df_ul["contract"] = df_ul.apply(
            lambda x: Option(x.symbol, x.expiry, x.strike, x.right, "SMART"), axis=1
        )

        # Qualify contracts with a fresh connection
        print("Qualifying long protection contracts...")
        with get_ib_connection("SNP") as ib:
            valid_contracts = qualify_me(ib, df_ul["contract"].tolist(), desc="Qualifying long protection contracts")
        valid_contracts = [v for v in valid_contracts if v is not None]

        # Get option market data using built-in connection in get_volatilities_snapshot
        if valid_contracts:
            print("Getting option prices...")
            df_iv_p = get_volatilities_snapshot(valid_contracts, market="SNP")

            if not df_iv_p.empty:
                df_u = clean_ib_util_df(valid_contracts)
                dfu = df_unds[["symbol", "iv", "price", "position"]].copy()
                dfu.rename(columns={"iv": "vy", "price": "undPrice"}, inplace=True)

                df_ivp = df_u.merge(dfu, on="symbol", how="left")
                df_ivp = df_ivp.merge(df_iv_p.drop(columns='conId'), on="symbol", how="left")
                df_ivp["qty"] = (df_ivp.position.abs() / 100).astype("int")
                df_ivp["dte"] = df_ivp.expiry.apply(get_dte)
                df_ivp["protection"] = (
                    (df_ivp["undPrice"] - df_ivp["strike"]) * 100 * df_ivp.qty
                )

                # Select closest (cheapest) protection per symbol
                df_lprot = df_ivp.loc[df_ivp.groupby("symbol")["protection"].idxmin()]
                print(f"Generated {len(df_lprot)} long protection recommendations")
            else:
                print("No option price data available for long protection")
        else:
            print("No valid contracts after qualification")
    else:
        print("No suitable put chains found for long protection")
else:
    print("No long unprotected positions")

#%% BUILD SHORT PROTECTION (CALLS FOR SHORT STOCK)
print("\n=== BUILDING SHORT PROTECTION RECOMMENDATIONS ===")

df_sprot = pd.DataFrame()

if not df_ushort.empty:
    print(f"Processing {len(df_ushort)} short positions...")

    # Get chains nearest to PROTECT_DTE
    mask = chains.symbol.isin(df_ushort.symbol)
    df_sch = (
        chains[mask]
        .groupby(["symbol", "strike"])["dte"]
        .apply(lambda x: x.sub(PROTECT_DTE).abs().idxmin())
    )
    # Use .values to get integer indices from idxmin()
    df_sch = chains.loc[df_sch.values].reset_index(drop=True)

    # Get PROTECTION_STRIP OTM calls (strikes above undPrice)
    df_us = df_sch[df_sch.symbol.isin(df_ushort.symbol)].copy()
    df_us = df_us.sort_values(
        ["symbol", "expiry", "strike"], ascending=[True, True, True]
    )
    df_us = df_us.merge(df_unds[["symbol", "price"]], on="symbol", how="left")
    df_us.rename(columns={"price": "undPrice"}, inplace=True)

    # Filter for calls above underlying price
    def get_otm_calls(group):
        und_price = group["undPrice"].iloc[0]
        otm_calls = group[group["strike"] >= und_price].head(PROTECTION_STRIP)
        return otm_calls

    df_us = df_us.groupby("symbol", group_keys=True).apply(get_otm_calls, include_groups=False).reset_index()

    if not df_us.empty:
        df_us["right"] = "C"
        df_us["contract"] = df_us.apply(
            lambda x: Option(x.symbol, x.expiry, x.strike, x.right, "SMART"), axis=1
        )

        # Qualify contracts with a fresh connection
        print("Qualifying short protection contracts...")
        with get_ib_connection("SNP") as ib:
            valid_contracts = qualify_me(ib, df_us["contract"].tolist(), desc="Qualifying short protection contracts")
        valid_contracts = [v for v in valid_contracts if v is not None]

        # Get option market data using built-in connection in get_volatilities_snapshot
        if valid_contracts:
            print("Getting option prices...")
            df_iv_s = get_volatilities_snapshot(valid_contracts, market="SNP")

            if not df_iv_s.empty:
                df_u = clean_ib_util_df(valid_contracts)
                dfu = df_unds[["symbol", "iv", "price", "position"]].copy()
                dfu.rename(columns={"iv": "vy", "price": "undPrice"}, inplace=True)

                df_ivs = df_u.merge(dfu, on="symbol", how="left")
                df_ivs = df_ivs.merge(df_iv_s.drop(columns='conId'), on="symbol", how="left")
                df_ivs["qty"] = (df_ivs.position.abs() / 100).astype("int")
                df_ivs["dte"] = df_ivs.expiry.apply(get_dte)
                df_ivs["protection"] = (
                    (df_ivs["strike"] - df_ivs["undPrice"]) * 100 * df_ivs.qty
                )

                # Select closest (cheapest) protection per symbol
                df_sprot = df_ivs.loc[df_ivs.groupby("symbol")["protection"].idxmin()]
                print(f"Generated {len(df_sprot)} short protection recommendations")
            else:
                print("No option price data available for short protection")
        else:
            print("No valid contracts after qualification")
    else:
        print("No suitable call chains found for short protection")
else:
    print("No short unprotected positions")

#%% CALCULATE FINAL PROTECTION PRICES
print("\n=== CALCULATING FINAL PROTECTION PRICES ===")

df_protect = pd.concat([df_lprot, df_sprot], ignore_index=True)

if df_protect.empty:
    print("No protection recommendations generated!")
else:
    print(f"Combined {len(df_protect)} protection recommendations")

    # Replace 'vy' with 'iv' in 'df_protect' where 'iv' is not NaN
    mask = df_protect['iv'].notna()
    df_protect.loc[mask, 'vy'] = df_protect.loc[mask, 'iv']

    df_protect["xPrice"] = df_protect["price"].apply(
        lambda x: get_prec(max(0.01, x), 0.01) if pd.notna(x) else 0.01
    )

    # Calculate costs
    df_protect["cost"] = df_protect["xPrice"] * df_protect["qty"] * 100
    df_protect["puc"] = df_protect["protection"] / df_protect["cost"]

    # Clean up
    df_protect.drop(columns=["iv", "hv"], inplace=True, errors="ignore")

    # Summary
    total_protection = df_protect["protection"].sum()
    total_cost = df_protect["cost"].sum()
    avg_dte = df_protect["dte"].mean()

    print(f"\n{'=' * 60}")
    print("PROTECTION SUMMARY")
    print(f"{'=' * 60}")
    print(f"Total potential damage prevented:  ${total_protection:,.0f}")
    print(f"Total protection cost:            ${total_cost:,.0f}")
    print(f"Number of symbols:               {len(df_protect.symbol.unique())}")
    print(f"Average DTE:                      {avg_dte:.1f} days")
    print(f"{'=' * 60}")

    # Save results
    protect_path = ROOT / "data" / "df_protect.pkl"
    pickle_me(df_protect, protect_path)
    print(f"\nSaved protection recommendations to {protect_path}")

    # Show top recommendations
    print("\nTop 10 Protection Recommendations:")
    display_cols = [
        "symbol",
        "right",
        "strike",
        "undPrice",
        "qty",
        "protection",
        "xPrice",
        "cost",
        "puc",
        "dte",
    ]
    print(df_protect[display_cols].head(10).round(2).to_string())

#%% FINAL OUTPUT
print("\n=== PROTECTION RECOMMENDATIONS COMPLETE ===")
if not df_protect.empty:
    print(f"✅ Successfully generated {len(df_protect)} protection recommendations")
    print(f"📁 Saved to: {ROOT / 'data' / 'df_protect.pkl'}")
else:
    print("ℹ️  No protection recommendations needed or generated!")

#%% ROLLS FOR PROTECTING PUTS
print("\n=== ROLLS FOR PROTECTING PUTS ===")

delete_pkl_files(['protect_rolls.pkl'])

df_pfu = df_pf.merge(df_unds[['symbol', 'price']], on='symbol', how='left')
df_pfu.rename(columns={'price': 'undPrice'}, inplace=True)

df_rolls = (
    df_pfu[(df_pfu['state'] == 'protecting') & (df_pfu.right == 'P')]
    .assign(
        odiff=lambda x: (x['undPrice'] - x['strike']),
        ostrike=df_pfu.strike,
        odte=df_pfu.dte,
        pct_diff=lambda x: (abs(x['strike'] - x['undPrice']) / x['undPrice'] * 100)
    )
    .sort_values('pct_diff', ascending=False)
    .reset_index(drop=True)
)

short_itm_calls = (
    df_pfu[
        (df_pfu.secType == 'OPT')
        & (df_pfu.right == 'C')
        & (df_pfu.position < 0)
        & df_pfu['undPrice'].notna()
    ]
    .loc[lambda x: x['strike'] < x['undPrice'], 'symbol']
    .unique()
)

if short_itm_calls.size:
    df_rolls = df_rolls[~df_rolls.symbol.isin(short_itm_calls)].reset_index(drop=True)
    print(
        "Skipping protecting-put rolls for symbols with ITM short calls: "
        + ", ".join(sorted(short_itm_calls))
    )

if not df_rolls.symbol.isnull().all().all():

    rol_chains = chains[chains.symbol.isin(set(df_rolls.symbol))]

    rol_chains = rol_chains.set_index('symbol').join(df_unds.set_index('symbol')[['price']]).reset_index()
    rol_chains.rename(columns={'price': 'undPrice'}, inplace=True)

    df_cd = filter_closest_dates(rol_chains, PROTECT_DTE, num_dates=1)
    p = filter_closest_strikes(df_cd, n=-4)

else:
    print("No protecting puts found in portfolio for rolling")
    p = pd.DataFrame()

df_purl = pd.DataFrame()

if not p.empty:
    p["right"] = "P"
    # pyrefly: ignore [no-matching-overload]
    p["contract"] = p.apply(
        lambda x: Option(x.symbol, x.expiry, x.strike, x.right, "SMART"), axis=1
    )

    print("Qualifying protecting put roll contracts...")
    with get_ib_connection("SNP") as ib:
        valid_contracts = qualify_me(ib, p["contract"].tolist(), desc= 'Qualifying protecting put roll contracts')
    valid_contracts = [v for v in valid_contracts if v is not None]

    if valid_contracts:
        df_u = clean_ib_util_df(valid_contracts)
        df_purl = df_u.groupby("symbol").first().reset_index()

        print("Getting put roll prices...")
        df_iv_purl = get_volatilities_snapshot(df_purl["contract"].tolist(), market="SNP")

        if not df_iv_purl.empty:
            df_up = df_unds.assign(undPrice=lambda x: x.price)

            purls = df_iv_purl.merge(df_up[['symbol', 'undPrice']], on='symbol')
            purls = purls.merge(df_u[['conId', 'secType', 'right', 'strike', 'expiry']], on='conId')
            purls = purls.merge(df_rolls[['symbol', 'odiff']], on='symbol')
            purls = purls.merge(df_rolls[['symbol', 'ostrike']], on='symbol')
            purls = purls.merge(df_rolls[['symbol', 'odte']], on='symbol')

            purls['diff'] = purls['strike'] / purls['undPrice'] - 1
            purls = purls.sort_values('diff', key=lambda x: x - purls['odiff'])

            cols = ['symbol', 'secType', 'expiry', 'strike', 'ostrike', 'odte', 'undPrice', 'right',
                   'price', 'odiff', 'diff']

            if (purls['diff'] < -0.05).any():
                print("\nWARNING: There are some put rolls whose strike-undPrice is larger than 5%. "
                      "These will be taken out from auto-roll suggestion.")
                print(purls[purls['diff'] < -0.05][cols])

            purls1 = purls[purls['diff'] >= -0.05]
            purls1 = purls1[purls1['strike'] != purls1['ostrike']]

            purls1 = purls1.copy()
            purls1['qty'] = purls1['symbol'].map(df_unds.set_index('symbol')['position'] / 100)

            purls1 = purls1.merge(df_pf[(df_pf.secType == 'OPT') & (df_pf.right == 'P') & (df_pf.position > 0)][['symbol', 'mktPrice']], on='symbol', how='left')
            purls1.rename(columns={'mktPrice': 'cost'}, inplace=True)
            purls1['rollcost'] = (purls1.price - purls1.cost +
                                  purls1['strike'] - purls1['ostrike']) * purls1.qty * 100

            purls1 = purls1.sort_values(['odte', 'rollcost'], ascending=[True, False])
            rol_cols = ['symbol', 'conId', 'expiry', 'undPrice', 'strike', 'ostrike', 'odte', 'right',
                        'qty', 'price', 'cost', 'rollcost']

            rollover_cost = (purls1.price - purls1.cost + purls1['strike'] - purls1['ostrike']) * purls1.qty * 100
            print(f"\nThe rollover cost of {purls1.symbol.unique().shape[0]} symbols for {purls1.expiry.apply(get_dte).max():.0f} days would be ${rollover_cost.sum():,.0f}.\n")
            purls1 = purls1[rol_cols].sort_values('rollcost', ascending=False)
            purls_path = ROOT / 'data' / 'df_prot_rolls.pkl'
            pickle_me(purls1[rol_cols], purls_path)
        else:
            print("No option price data available for protecting put rolls")
    else:
        print("No valid contracts after qualification")
else:
    print("No suitable put chains found for protecting rolls")

#%% FINAL OUTPUT
end_time = time.time()
execution_time = end_time - start_time
minutes = int(execution_time // 60)
seconds = int(execution_time % 60)
print(f"\n{'='*50}")
print(f"Total execution time: {minutes} minutes and {seconds} seconds")
print(f"{'='*50}")
print("\n=== RECOMMENDATIONS COMPLETE ===")
