# %%
# IMPORTS
import argparse as _ap
import json
import logging
import os
import time

import pandas as pd
from ib_async import Option
from src.log_utils import setup_ib_logging, setup_logging as _setup_logging

# pyrefly: ignore [missing-import]
from src.build import (
    ROOT,
    atm_margin,
    delete_pkl_files,
    get_dte,
    get_ib_connection,
    get_pickle,
    get_prec,
    get_volatilities_snapshot,
    load_config,
    pickle_me,
    qualify_me,
)

# pyrefly: ignore [missing-import]
from src.classify import (
    classifed_results,
    clean_ib_util_df,
    get_financials,
    get_open_orders,
    classify_open_orders,
)

logger = logging.getLogger(__name__)

_p = _ap.ArgumentParser(add_help=False)
_p.add_argument("--debug", action="store_true")
_setup_logging("derive", debug=_p.parse_known_args()[0].debug)
setup_ib_logging(ROOT / "log" / "ib_async.log", level=logging.ERROR)
del _ap, _p, _setup_logging

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

    for symbol, group in chains.groupby("symbol"):
        group = group.copy()
        group["date_diff"] = (group["dte"] - protect_dte).abs()
        unique_dates = group[["dte", "date_diff"]].drop_duplicates(subset=["dte"])
        closest_dates = unique_dates.nsmallest(num_dates, "date_diff")["dte"]
        filtered_group = group[group["dte"].isin(closest_dates)].drop(columns=["date_diff"])
        result.append(filtered_group)

    return pd.concat(result, ignore_index=True) if result else pd.DataFrame()


def _is_weekly_expiry(expiry: str) -> bool:
    """True for non-3rd-Friday expiries. 3rd Friday = day 15-21, weekday 4 (Friday)."""
    dt = pd.Timestamp(expiry)
    return not (dt.weekday() == 4 and 15 <= dt.day <= 21)


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

    for (symbol, expiry), group in chains.groupby(["symbol", "dte"]):
        group = group.copy()
        filtered = group.copy()

        if n > 0:
            filtered = group[group["strike"] >= group["undPrice"]]
            filtered = filtered.sort_values("strike", ascending=True)
        else:
            filtered = group[group["strike"] <= group["undPrice"]]
            filtered = filtered.sort_values("strike", ascending=False)

        if not filtered.empty:
            result.append(filtered.head(abs_n))

    return pd.concat(result, ignore_index=True) if result else pd.DataFrame()


# %% CONFIG AND SETUP
ACCOUNT = "US_ACCOUNT"
ACCOUNT_NO = os.getenv(ACCOUNT, "")

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
COV_STD_MULT = config.get("COVER_STD_MULT")
MINNAKEDOPTPRICE = config.get("MINNAKEDOPTPRICE")
NAKEDXPMULT = config.get("NAKEDXPMULT")
REAPRATIO = config.get("REAPRATIO")
MINREAPDTE = config.get("MINREAPDTE")
SOW_NAKEDS = config.get("SOW_NAKEDS")
VIRGIN_CALL_STD_MULT = config.get("VIRGIN_CALL_STD_MULT")
VIRGIN_PUT_STD_MULT = config.get("VIRGIN_PUT_STD_MULT")
MINCUSHION = config.get("MINCUSHION", 0.20)
COV_AGED_DTE = config.get("COV_AGED_DTE", 180)

logger.info("Getting financials...")
fin = get_financials(ACCOUNT_NO)

# %% GET CLASSIFIED DATA
# classifed_results → chains_n_unds opens/closes its own CID=10 connections internally.
# Open derive.py's long-lived connection AFTER all internal connections have closed.
logger.info("=== GETTING CLASSIFIED PORTFOLIO DATA ===")
data = classifed_results(account_no=ACCOUNT_NO)
df_unds = data["df_unds"]
df_pf = data["df_pf"]
chains = data["df_chains"]

df_unds = df_unds.merge(
    df_pf[df_pf.secType == "STK"][["symbol", "position", "avgCost"]], on="symbol", how="left"
).fillna({"position": 0, "avgCost": 0})

if "mktPrice" in df_unds.columns and "price" in df_unds.columns:
    df_unds["price"] = df_unds["price"].fillna(df_unds["mktPrice"])


logger.info("Loaded %s underlyings", len(df_unds))
logger.info("Loaded %s portfolio positions", len(df_pf))
logger.info("Loaded %s chain entries", len(chains))

# Load weekly/monthly classification (built by scripts/update_symbol_categories.py)
_sym_cat_path = ROOT / "data" / "master" / "symbol_categories.pkl"
if _sym_cat_path.exists():
    _sym_cat = pd.read_pickle(_sym_cat_path)
    _monthly_syms = set(_sym_cat.loc[~_sym_cat.is_weekly, "symbol"])
    logger.info("Loaded symbol categories: %s monthly-only symbols", len(_monthly_syms))
else:
    _monthly_syms = set()
    logger.info("Warning: symbol_categories.pkl missing — run 'Identify Weeklies' in History tab; monthly sow exclusion skipped")

# Prefetch active open orders before opening our own connection — get_open_orders opens CID=10 itself.
# is_active=True excludes cancelled/expired orders so they don't block new suggestion generation.
df_openords = get_open_orders(account_no=ACCOUNT_NO, is_active=True)
df_openords = classify_open_orders(df_openords, df_pf)

# Symbols already covered by an active open order — skip suggestion generation for these.
# This prevents duplicate orders when the user has manually entered orders in IBKR.
_oo_covering   = set(df_openords.loc[df_openords.state == "covering",   "symbol"])
_oo_sowing     = set(df_openords.loc[df_openords.state == "sowing",     "symbol"])
_oo_protecting = set(df_openords.loc[df_openords.state == "protecting", "symbol"])
# Reap exclusion is contract-level (symbol+right+strike) — multiple short options on the same
# symbol at different strikes must each be checked independently.
_oo_reaping = set(
    df_openords.loc[df_openords.state == "reaping", ["symbol", "right", "strike"]]
    .itertuples(index=False, name=None)
)

if _oo_covering:
    logger.info("Skipping cover for %d symbols with active orders", len(_oo_covering))
if _oo_sowing:
    logger.info("Skipping sow for %d symbols with active orders", len(_oo_sowing))
if _oo_protecting:
    logger.info("Skipping protect for %d symbols with active orders", len(_oo_protecting))
if _oo_reaping:
    logger.info("Skipping reap for %d contracts with active orders", len(_oo_reaping))

logger.info("Connecting to IB...")
ib = get_ib_connection("SNP", account_no=ACCOUNT_NO)

# Refresh prices/IV for any symbol whose snapshot is missing (NaN or ≤0) — not
# only the all-NaN pre-market case. Covers and sow both need valid undPrice/iv to
# compute sdev and thresholds. After refreshing, symbols still missing price/iv
# are EXPLICITLY excluded and logged, so they are never silently dropped by the
# downstream how="left" NaN merges.
def _invalid_price_iv(_df: pd.DataFrame) -> pd.Series:
    _p = pd.to_numeric(_df.get("price"), errors="coerce")
    _v = pd.to_numeric(_df.get("iv"), errors="coerce")
    return ~((_p > 0) & (_v > 0))

if not df_unds.empty and "price" in df_unds.columns:
    _need = df_unds[_invalid_price_iv(df_unds)]
    if not _need.empty:
        logger.info(
            "df_unds missing price/iv for %d/%d symbols — refreshing with live IBKR snapshot...",
            len(_need), len(df_unds),
        )
        _sym_contracts = get_pickle(ROOT / "data" / "symbols.pkl")
        if _sym_contracts:
            _need_syms = set(_need["symbol"])
            _subset = [c for c in _sym_contracts if getattr(c, "symbol", None) in _need_syms]
            if _subset:
                _snap = get_volatilities_snapshot(
                    _subset, market="SNP", batch_size=50, ib=ib, desc="Refreshing prices"
                )
                if not _snap.empty and _snap["price"].notna().any():
                    _refresh_cols = [c for c in ["price", "iv", "hv"] if c in _snap.columns]
                    # update() fills only the refreshed symbols with non-NaN values,
                    # preserving any good existing data.
                    df_unds = df_unds.set_index("symbol")
                    df_unds.update(_snap.set_index("symbol")[_refresh_cols])
                    df_unds = df_unds.reset_index()
                    # HV→IV fallback: IBKR may return HV (tick 104) but not IV (tick 106);
                    # substitute HV for IV so these symbols are not dropped downstream.
                    _iv_r = pd.to_numeric(df_unds.get("iv", pd.Series(dtype=float)), errors="coerce")
                    _hv_r = pd.to_numeric(df_unds.get("hv", pd.Series(dtype=float)), errors="coerce")
                    _use_hv_r = (~(_iv_r > 0)) & (_hv_r > 0)
                    if _use_hv_r.any():
                        df_unds.loc[_use_hv_r, "iv"] = _hv_r[_use_hv_r]
                        logger.info("HV→IV fallback: %d symbols after price refresh", int(_use_hv_r.sum()))
                    logger.info(
                        "Price refresh: %d/%d symbols now have valid price+iv",
                        int((~_invalid_price_iv(df_unds)).sum()), len(df_unds),
                    )
                else:
                    logger.warning("Price refresh returned no valid data — cover/sow orders may be incomplete")
        else:
            logger.warning("symbols.pkl not found — cannot refresh prices")

    # Explicitly drop + log symbols that still lack valid price/iv.
    _still_bad = df_unds[_invalid_price_iv(df_unds)]
    if not _still_bad.empty:
        _bad_syms = sorted(_still_bad["symbol"].astype(str).tolist())
        logger.warning(
            "Excluded %d symbols missing price/iv (NO orders generated for them): %s",
            len(_bad_syms),
            ", ".join(_bad_syms[:30]) + (" …" if len(_bad_syms) > 30 else ""),
        )
        df_unds = df_unds[~_invalid_price_iv(df_unds)].reset_index(drop=True)

# %% MAKE COVERS FOR EXPOSED AND UNCOVERED STOCK POSITIONS
logger.info("=== MAKE COVERS FOR EXPOSED AND UNCOVERED STOCK POSITIONS ===")

delete_pkl_files(["df_cov.pkl"])

df_cov = pd.DataFrame()

if not COVER_ME:
    logger.info("COVER_ME=False — skipping cover generation")
else:
    uncov = df_unds.state.isin(["exposed", "uncovered"])
    uncov_long = df_unds[
        uncov & (df_unds.position > 0) & ~df_unds.symbol.isin(_oo_covering)
    ].reset_index(drop=True)

    if not uncov_long.empty:
        logger.info("Processing %s long uncovered/exposed positions...", len(uncov_long))

        df_cc = (
            chains[chains.symbol.isin(uncov_long.symbol.unique())]
            .loc[((chains.dte // 1).between(COVER_MIN_DTE, COVER_MIN_DTE + 7))][
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
            .groupby("symbol", as_index=False)["avgCostPerShare"]
            .sum()
            .rename(columns={"avgCostPerShare": "longPutCost"})
        )

        df_cc = df_cc.merge(long_put_cost, on="symbol", how="left")
        df_cc["longPutCost"] = df_cc["longPutCost"].fillna(0)

        df_cc["sdev"] = df_cc.undPrice * df_cc.vy * (df_cc.dte / 365) ** 0.5

        vol_based_price = df_cc.undPrice + config.get("COVER_STD_MULT") * df_cc.sdev

        # Load assignment dates from flex_trades.pkl — most recent STK BUY per symbol.
        # Stocks held longer than COV_AGED_DTE days use vol_based_price only (earn
        # income rather than waiting to recover cost basis). Newer assignments use
        # max(avgCost + longPutCost, vol_based_price) to prevent selling calls below cost.
        _assignment_dates: dict = {}
        _flex_trades_path = ROOT / "data" / "master" / "flex_trades.pkl"
        if _flex_trades_path.exists():
            try:
                _ft = pd.read_pickle(_flex_trades_path)
                _stk_buys = _ft[(_ft["assetCategory"] == "STK") & (_ft["buySell"] == "BUY")]
                _assignment_dates = (
                    _stk_buys.groupby("symbol")["tradeDate"].max().to_dict()
                )
            except Exception as _e:
                logger.warning(f"Could not load assignment dates from flex_trades.pkl: {_e}")

        _today_ts = pd.Timestamp.now(tz=None).normalize()

        def _covprice_for_row(row):
            sym = row["symbol"]
            default = max(row["avgCost"] + row["longPutCost"], row["vol_based_price"])
            if sym not in _assignment_dates:
                return default
            assign_ts = pd.Timestamp(_assignment_dates[sym])
            if pd.isna(assign_ts):
                return default
            days_held = (_today_ts - assign_ts.normalize()).days
            if days_held > COV_AGED_DTE:
                return row["vol_based_price"]
            return default

        df_cc["vol_based_price"] = vol_based_price
        df_cc["covPrice"] = df_cc.apply(_covprice_for_row, axis=1)

        _aged_syms = []
        if _assignment_dates:
            for _sym in df_cc["symbol"].unique():
                if _sym in _assignment_dates:
                    _ats = pd.Timestamp(_assignment_dates[_sym])
                    if not pd.isna(_ats):
                        _dh = (_today_ts - _ats.normalize()).days
                        if _dh > COV_AGED_DTE:
                            _aged_syms.append(f"{_sym}({_dh}d)")
        if _aged_syms:
            logger.info(f"Aged stocks using vol-only covPrice (>{COV_AGED_DTE}d): {', '.join(_aged_syms)}")

        no_of_options = 3

        _cov_parts: list[pd.DataFrame] = []
        for (_, _exp_unused), _cg in df_cc.groupby(["symbol", "expiry"]):
            _cp = (
                _cg[_cg["strike"] > _cg["covPrice"]]
                .assign(diff=_cg["strike"] - _cg["covPrice"])
                .sort_values("diff")
                .head(no_of_options)
                .drop(columns=["diff"], errors="ignore")
            )
            if not _cp.empty:
                _cov_parts.append(_cp)
        cc_long = pd.concat(_cov_parts) if _cov_parts else pd.DataFrame(
            columns=["symbol", "expiry", "strike", "undPrice", "sdev", "covPrice"]
        )

        cov_calls = [
            Option(s, e, k, "C", "SMART")
            for s, e, k in zip(cc_long.symbol, cc_long.expiry, cc_long.strike)
            if pd.notna(s) and pd.notna(e) and pd.notna(k)
        ]


        logger.info("Qualifying covered call contracts...")
        valid_contracts = qualify_me(ib, cov_calls, desc="Qualifying covered call contracts")
        valid_contracts = [v for v in valid_contracts if v is not None]

        if valid_contracts:
            df_cc1 = clean_ib_util_df(valid_contracts)

            df_ccf = df_cc1.loc[df_cc1.groupby("symbol")["strike"].idxmin()]

            df_ccf = df_ccf.reset_index(drop=True)

            df_ccf = df_ccf.merge(df_unds[["symbol", "price", "iv"]], on="symbol", how="left")
            df_ccf.rename(columns={"price": "undPrice", "iv": "vy"}, inplace=True)

            df_ccf = df_ccf.merge(
                df_pf[df_pf.state.isin(["uncovered", "exposed"]) & (df_pf.secType == "STK")][
                    ["symbol", "position", "avgCost"]
                ],
                on="symbol",
                how="left",
            )

            df_ccf["action"] = "SELL"
            df_ccf["qty"] = df_ccf["position"] / 100
            df_ccf = df_ccf.drop(columns=["position"])

            logger.info("Getting covered call prices...")
            df_iv_cc = get_volatilities_snapshot(df_ccf["contract"].tolist(), market="SNP", ib=ib, desc="Fetching covered call volatilities")

            if not df_iv_cc.empty:
                df_ccf = df_ccf.merge(df_iv_cc[["symbol", "price"]], on="symbol", how="left")
            else:
                df_ccf["price"] = float("nan")
            _nan_cc = df_ccf["price"].isna()
            if _nan_cc.any():
                df_ccf.loc[_nan_cc, "price"] = df_ccf.loc[_nan_cc].apply(
                    lambda x: max(
                        x.undPrice * x.vy * (max(get_dte(x.expiry), 1) / 365) ** 0.5 * 0.4,
                        0.01,
                    ),
                    axis=1,
                )
                logger.info(
                    "Estimated theoretical price for %d covered call(s) (no live market data)",
                    int(_nan_cc.sum()),
                )
            _cc_estimated = int(_nan_cc.sum())
            df_ccf["margin"] = df_ccf.apply(
                lambda x: atm_margin(x.strike, x.undPrice, get_dte(x.expiry), x.vy), axis=1
            )
        else:
            logger.info("No valid contracts after qualification")
            df_ccf = pd.DataFrame()
    else:
        df_ccf = pd.DataFrame()
        logger.info("No long uncovered/exposed positions")

    uncov_short = df_unds[
        uncov & (df_unds.position < 0) & ~df_unds.symbol.isin(_oo_covering)
    ].reset_index(drop=True)

    if not uncov_short.empty:
        logger.info("Processing %s short uncovered/exposed positions...", len(uncov_short))

        df_cp = (
            chains[chains.symbol.isin(uncov_short.symbol.unique())]
            .loc[((chains.dte // 1).between(COVER_MIN_DTE, COVER_MIN_DTE + 7))][
                ["symbol", "expiry", "strike", "dte"]
            ]
            .sort_values(["symbol", "dte"])
            .reset_index(drop=True)
        )

        df_cp = df_cp.merge(df_unds[["symbol", "price", "iv", "avgCost"]], on="symbol", how="left")
        df_cp.rename(columns={"price": "undPrice", "iv": "vy"}, inplace=True)

        df_cp["sdev"] = df_cp.undPrice * df_cp.vy * (df_cp.dte / 365) ** 0.5

        vol_based_price = df_cp.undPrice - config.get("COVER_STD_MULT") * df_cp.sdev

        # Mirror of the long-stock aged logic: short positions held > COV_AGED_DTE days
        # use vol_based_price only; newer assignments use min(avgCost, vol_based_price)
        # to prevent selling puts above cost basis while in the wheel cycle.
        _assignment_dates_cp: dict = {}
        _flex_trades_path_cp = ROOT / "data" / "master" / "flex_trades.pkl"
        if _flex_trades_path_cp.exists():
            try:
                _ft_cp = pd.read_pickle(_flex_trades_path_cp)
                _stk_sells = _ft_cp[(_ft_cp["assetCategory"] == "STK") & (_ft_cp["buySell"] == "SELL")]
                _assignment_dates_cp = (
                    _stk_sells.groupby("symbol")["tradeDate"].max().to_dict()
                )
            except Exception as _e:
                logger.warning(f"Could not load assignment dates (short) from flex_trades.pkl: {_e}")

        _today_ts_cp = pd.Timestamp.now(tz=None).normalize()

        def _covprice_for_row_cp(row):
            sym = row["symbol"]
            default = min(row["avgCost"], row["vol_based_price"])
            if sym not in _assignment_dates_cp:
                return default
            assign_ts = pd.Timestamp(_assignment_dates_cp[sym])
            if pd.isna(assign_ts):
                return default
            days_held = (_today_ts_cp - assign_ts.normalize()).days
            if days_held > COV_AGED_DTE:
                return row["vol_based_price"]
            return default

        df_cp["vol_based_price"] = vol_based_price
        df_cp["covPrice"] = df_cp.apply(_covprice_for_row_cp, axis=1)

        _aged_syms_cp = []
        if _assignment_dates_cp:
            for _sym in df_cp["symbol"].unique():
                if _sym in _assignment_dates_cp:
                    _ats = pd.Timestamp(_assignment_dates_cp[_sym])
                    if not pd.isna(_ats):
                        _dh = (_today_ts_cp - _ats.normalize()).days
                        if _dh > COV_AGED_DTE:
                            _aged_syms_cp.append(f"{_sym}({_dh}d)")
        if _aged_syms_cp:
            logger.info(f"Aged short stocks using vol-only covPrice (>{COV_AGED_DTE}d): {', '.join(_aged_syms_cp)}")

        no_of_options = 3

        cp_short = (
            df_cp.groupby(["symbol", "expiry"])[
                ["symbol", "expiry", "strike", "undPrice", "sdev", "covPrice"]
            ]
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
            if pd.notna(s) and pd.notna(e) and pd.notna(k)
        ]


        logger.info("Qualifying covered put contracts...")
        valid_contracts = qualify_me(ib, cov_puts, desc="Qualifying covered put contracts")
        valid_contracts = [v for v in valid_contracts if v is not None]

        if valid_contracts:
            df_cp1 = clean_ib_util_df(valid_contracts)

            df_cpf = df_cp1.loc[df_cp1.groupby("symbol")["strike"].idxmax()]

            df_cpf = df_cpf.reset_index(drop=True)

            df_cpf = df_cpf.merge(df_unds[["symbol", "price", "iv"]], on="symbol", how="left")
            df_cpf.rename(columns={"price": "undPrice", "iv": "vy"}, inplace=True)

            df_cpf = df_cpf.merge(
                df_pf[df_pf.state.isin(["uncovered", "exposed"]) & (df_pf.secType == "STK")][
                    ["symbol", "position", "avgCost"]
                ],
                on="symbol",
                how="left",
            )

            df_cpf["action"] = "SELL"
            df_cpf["qty"] = abs(df_cpf["position"]) / 100
            df_cpf = df_cpf.drop(columns=["position"])

            logger.info("Getting covered put prices...")
            df_iv_cp = get_volatilities_snapshot(df_cpf["contract"].tolist(), market="SNP", ib=ib, desc="Fetching covered put volatilities")

            if not df_iv_cp.empty:
                df_cpf = df_cpf.merge(df_iv_cp[["symbol", "price"]], on="symbol", how="left")
            else:
                df_cpf["price"] = float("nan")
            _nan_cp = df_cpf["price"].isna()
            if _nan_cp.any():
                df_cpf.loc[_nan_cp, "price"] = df_cpf.loc[_nan_cp].apply(
                    lambda x: max(
                        x.undPrice * x.vy * (max(get_dte(x.expiry), 1) / 365) ** 0.5 * 0.4,
                        0.01,
                    ),
                    axis=1,
                )
                logger.info(
                    "Estimated theoretical price for %d covered put(s) (no live market data)",
                    int(_nan_cp.sum()),
                )
            _cp_estimated = int(_nan_cp.sum())
            df_cpf["margin"] = df_cpf.apply(
                lambda x: atm_margin(x.strike, x.undPrice, get_dte(x.expiry), x.vy), axis=1
            )

        else:
            logger.info("No valid contracts after qualification")
            df_cpf = pd.DataFrame()
    else:
        df_cpf = pd.DataFrame()
        logger.info("No short uncovered/exposed positions")

    df_cov = pd.concat([df_ccf, df_cpf], ignore_index=True)

    cov_path = ROOT / "data" / "df_cov.pkl"

    if not df_cov.empty:
        df_cov.insert(4, "dte", df_cov.expiry.apply(get_dte))

        df_cov = df_cov.dropna(subset=["price"])

        _xp = df_cov["price"].apply(
            lambda p: max(get_prec(p * COVXPMULT, 0.01) or 0.05, 0.05)
        )
        df_cov["xPrice"] = _xp.where(df_cov["qty"] != 0, 0.0)

        pickle_me(df_cov, cov_path)
    else:
        logger.info("No covers generated")

    # --- Per-symbol cover diagnostic ---
    _covered_syms = set(df_cov["symbol"]) if not df_cov.empty else set()
    _missing_cov: list[tuple] = []
    if not uncov_long.empty:
        for _, _r in uncov_long.iterrows():
            if _r.symbol not in _covered_syms:
                _missing_cov.append((_r.symbol, "CALL", _r))
    if not uncov_short.empty:
        for _, _r in uncov_short.iterrows():
            if _r.symbol not in _covered_syms:
                _missing_cov.append((_r.symbol, "PUT", _r))
    if _missing_cov:
        logger.info(
            "=== COVER DIAGNOSTIC: %d of %d processed positions have no cover order ===",
            len(_missing_cov), len(uncov_long) + len(uncov_short),
        )
    _cov_reasons: dict[str, list[str]] = {}
    for _sym, _side, _row in sorted(_missing_cov, key=lambda x: x[0]):
        _sym_chains_dte = chains[
            (chains.symbol == _sym) & (chains.dte // 1).between(COVER_MIN_DTE, COVER_MIN_DTE + 7)
        ]
        if _sym_chains_dte.empty:
            _avail = sorted(chains.loc[chains.symbol == _sym, "dte"].dropna().unique())
            logger.info(
                "NO COVER %s (%s): no chain in DTE window %d–%d; nearest available DTEs: %s",
                _sym, _side, COVER_MIN_DTE, COVER_MIN_DTE + 7,
                [f"{d:.0f}d" for d in _avail[:5]] or ["none in chains"],
            )
            _cov_reasons.setdefault("no chain in DTE window", []).append(_sym)
            continue
        _und = getattr(_row, "price", float("nan"))
        _iv = getattr(_row, "iv", float("nan"))
        _avg = getattr(_row, "avgCost", float("nan"))
        _dt = _sym_chains_dte["dte"].min()
        _sdev = (
            _und * _iv * (_dt / 365) ** 0.5
            if pd.notna(_und) and pd.notna(_iv) and pd.notna(_dt)
            else float("nan")
        )
        _cov_mult = config.get("COVER_STD_MULT", 0.5)
        _vol_price = (
            (_und + _cov_mult * _sdev) if _side == "CALL" else (_und - _cov_mult * _sdev)
        ) if pd.notna(_sdev) else float("nan")
        # Approximate: does not include longPutCost or aged-stock override
        _approx_cov = (
            max(_avg, _vol_price) if _side == "CALL" else min(_avg, _vol_price)
        ) if pd.notna(_avg) and pd.notna(_vol_price) else float("nan")
        _max_k = _sym_chains_dte["strike"].max()
        _min_k = _sym_chains_dte["strike"].min()
        if pd.isna(_approx_cov):
            logger.info(
                "NO COVER %s (%s): price or IV is NaN (und=%s, iv=%s) — cannot compute covPrice",
                _sym, _side, _und, _iv,
            )
            _cov_reasons.setdefault("price or IV missing", []).append(_sym)
        elif _side == "CALL" and _approx_cov > _max_k:
            logger.info(
                "NO COVER %s (CALL): covPrice≈%.2f (avgCost=%.2f, vol_based=%.2f) > max strike %.2f"
                " — cost basis above DTE %d–%d chain ceiling",
                _sym, _approx_cov, _avg, _vol_price, _max_k, COVER_MIN_DTE, COVER_MIN_DTE + 7,
            )
            _cov_reasons.setdefault("cost basis above chain ceiling", []).append(_sym)
        elif _side == "PUT" and _approx_cov < _min_k:
            logger.info(
                "NO COVER %s (PUT): covPrice≈%.2f (avgCost=%.2f, vol_based=%.2f) < min strike %.2f"
                " — cost basis below DTE %d–%d chain floor",
                _sym, _approx_cov, _avg, _vol_price, _min_k, COVER_MIN_DTE, COVER_MIN_DTE + 7,
            )
            _cov_reasons.setdefault("cost basis below chain floor", []).append(_sym)
        else:
            logger.info(
                "NO COVER %s (%s): strikes %.2f–%.2f exist (covPrice≈%.2f)"
                " — contract qualification or price fetch failed",
                _sym, _side, _min_k, _max_k, _approx_cov,
            )
            _cov_reasons.setdefault("qualification failed", []).append(_sym)

    try:
        _cov_n_estimated = _cc_estimated + _cp_estimated
    except NameError:
        _cov_n_estimated = 0
    (ROOT / "data" / "cover_summary.json").write_text(
        json.dumps({
            "processed": len(uncov_long) + len(uncov_short),
            "generated": len(df_cov),
            "estimated_prices": _cov_n_estimated,
        }),
        encoding="utf-8",
    )

# %% MAKE MONTHLY COVERED CALLS FOR MONTHLY-ONLY HELD STOCKS
logger.info("=== MAKE MONTHLY COVERED CALLS FOR MONTHLY-ONLY HELD STOCKS ===")

delete_pkl_files(["df_monthly_cov.pkl"])
df_monthly_cov = pd.DataFrame()

if not COVER_ME:
    logger.info("COVER_ME=False — skipping monthly CC generation")
elif not _monthly_syms:
    logger.info("No symbol_categories data — skipping monthly CC generation")
else:
    _monthly_uncov = df_unds[
        df_unds.state.isin(["exposed", "uncovered"])
        & (df_unds.position > 0)
        & df_unds.symbol.isin(_monthly_syms)
        & ~df_unds.symbol.isin(_oo_covering)
    ].reset_index(drop=True)

    if _monthly_uncov.empty:
        logger.info("No monthly-only uncovered positions")
    else:
        logger.info("Processing %s monthly-only uncovered positions...", len(_monthly_uncov))

        _df_mc = chains[chains.symbol.isin(_monthly_uncov.symbol.unique())].copy()
        _df_mc = _df_mc.merge(df_unds[["symbol", "price", "iv", "avgCost"]], on="symbol", how="left")
        _df_mc.rename(columns={"price": "undPrice", "iv": "vy"}, inplace=True)
        _df_mc["sdev"] = _df_mc.undPrice * _df_mc.vy * (_df_mc.dte / 365) ** 0.5

        # For each symbol, pick the nearest expiry that has a call at/above avgCost
        _mc_best: list[pd.Series] = []
        for _sym, _grp in _df_mc.groupby("symbol"):
            _avg = _grp["avgCost"].iloc[0]
            for _exp in sorted(_grp["expiry"].unique()):
                _viable = _grp[(_grp.expiry == _exp) & (_grp.strike >= _avg)].sort_values("strike")
                if not _viable.empty:
                    _mc_best.append(_viable.iloc[0])
                    break

        if not _mc_best:
            logger.info("No viable monthly CC strikes at/above avgCost")
        else:
            _mc_calls_df = pd.DataFrame(_mc_best).reset_index(drop=True)
            _monthly_opts = [
                Option(s, e, k, "C", "SMART")
                for s, e, k in zip(_mc_calls_df.symbol, _mc_calls_df.expiry, _mc_calls_df.strike)
                if pd.notna(s) and pd.notna(e) and pd.notna(k)
            ]


            logger.info("Qualifying monthly CC contracts...")
            _valid_mc = qualify_me(ib, _monthly_opts, desc="Qualifying monthly CC contracts")
            _valid_mc = [v for v in _valid_mc if v is not None]

            if not _valid_mc:
                logger.info("No valid monthly CC contracts after qualification")
            else:
                _df_mc1 = clean_ib_util_df(_valid_mc)
                _df_mcf = _df_mc1.loc[_df_mc1.groupby("symbol")["strike"].idxmin()].reset_index(drop=True)

                _df_mcf = _df_mcf.merge(df_unds[["symbol", "price", "iv"]], on="symbol", how="left")
                _df_mcf.rename(columns={"price": "undPrice", "iv": "vy"}, inplace=True)

                _df_mcf = _df_mcf.merge(
                    df_pf[df_pf.state.isin(["uncovered", "exposed"]) & (df_pf.secType == "STK")][
                        ["symbol", "position", "avgCost"]
                    ],
                    on="symbol",
                    how="left",
                )

                _df_mcf["action"] = "SELL"
                _df_mcf["qty"] = _df_mcf["position"] / 100
                _df_mcf = _df_mcf.drop(columns=["position"])

                logger.info("Getting monthly CC prices...")
                _df_iv_mc = get_volatilities_snapshot(
                    _df_mcf["contract"].tolist(), market="SNP", ib=ib,
                    desc="Fetching monthly CC volatilities",
                )

                if _df_iv_mc.empty:
                    logger.info("No price data for monthly CCs")
                else:
                    _df_mcf = _df_mcf.merge(_df_iv_mc[["symbol", "price"]], on="symbol", how="left")
                    _df_mcf["dte"] = _df_mcf.expiry.apply(get_dte)
                    _df_mcf = _df_mcf.merge(
                        _mc_calls_df[["symbol", "sdev"]], on="symbol", how="left"
                    )
                    _df_mcf["margin"] = _df_mcf.apply(
                        lambda x: atm_margin(x.strike, x.undPrice, get_dte(x.expiry), x.vy), axis=1
                    )
                    _df_mcf["xPrice"] = _df_mcf.apply(
                        lambda x: max(get_prec(x.price * COVXPMULT, 0.01), 0.05)
                        if x.qty != 0 else 0,
                        axis=1,
                    )

                    df_monthly_cov = _df_mcf.dropna(subset=["price"]).reset_index(drop=True)
                    pickle_me(df_monthly_cov, ROOT / "data" / "df_monthly_cov.pkl")

# %% MAKE SOWING CONTRACTS FOR VIRGIN AND ORPHANED SYMBOLS
logger.info("=== MAKE SOWING CONTRACTS FOR VIRGIN AND ORPHANED SYMBOLS ===")

delete_pkl_files(["df_nkd.pkl"])

_SOW_SKIP_PATH = ROOT / "data" / "sow_skip.json"
_sow_reasons: dict[str, list[str]] = {}

if not SOW_NAKEDS:
    logger.info("SOW_NAKEDS=False — skipping sow generation")
    _SOW_SKIP_PATH.unlink(missing_ok=True)
elif fin.get("cushion", 0) < MINCUSHION:
    _cushion_actual = fin.get("cushion", 0)
    logger.info("Skipping sow: cushion %s < MINCUSHION %s", f"{_cushion_actual:.1%}", f"{MINCUSHION:.1%}")
    _SOW_SKIP_PATH.write_text(
        json.dumps({"reason": "cushion", "actual": _cushion_actual, "required": MINCUSHION}),
        encoding="utf-8",
    )
else:
    _SOW_SKIP_PATH.unlink(missing_ok=True)
    df_v = df_unds[
        ((df_unds.state == "virgin") | (df_unds.state == "orphaned"))
        & ~df_unds.symbol.isin(_oo_sowing)
        & ~df_unds.symbol.isin(_monthly_syms)
    ].reset_index(drop=True)
    logger.info("Sow candidates (virgin/orphaned, weekly-eligible): %d", len(df_v))
    if df_v.empty:
        logger.info("Sow: no virgin/orphaned symbols — skipping chain lookup")
    if _monthly_syms:
        _skipped_monthly = set(
            df_unds.loc[
                df_unds.state.isin(["virgin", "orphaned"]) & df_unds.symbol.isin(_monthly_syms),
                "symbol",
            ]
        )
        if _skipped_monthly:
            logger.info("Skipping sow for %d monthly-only symbols", len(_skipped_monthly))

    sow_chains = chains[chains["expiry"].apply(_is_weekly_expiry)]
    logger.info(
        "Weekly chain rows after 3rd-Friday filter: %d (expiries: %s)",
        len(sow_chains),
        sorted(sow_chains["expiry"].unique())[:5] if not sow_chains.empty else [],
    )
    _sow_sym_chains = sow_chains[sow_chains.symbol.isin(df_v.symbol.to_list())]
    if _sow_sym_chains.empty:
        logger.info("Sow: no weekly chains found for virgin/orphaned symbols")
    df_virg = sow_chains.loc[
        _sow_sym_chains
        .groupby(["symbol", "strike"])["dte"]
        .apply(lambda x: x.sub(VIRGIN_DTE).abs().idxmin())
    ]

    df_virg = df_virg.merge(df_unds[["symbol", "price", "iv"]], on="symbol", how="left")
    df_virg.rename(columns={"price": "undPrice", "iv": "vy"}, inplace=True)

    df_virg["sdev"] = df_virg.undPrice * df_virg.vy * (df_virg.dte / 365) ** 0.5

    v_std = config.get("VIRGIN_PUT_STD_MULT", 3)
    no_of_options = 4

    # Per-symbol VIRGIN_PUT_STD_MULT overrides (set via dashboard REFINE Overrides panel)
    _overrides_file = ROOT / "data" / "symbol_overrides.json"
    _sym_put_mult: dict[str, float] = {}
    if _overrides_file.exists():
        try:
            _sym_put_mult = json.loads(
                _overrides_file.read_text(encoding="utf-8")
            ).get("VIRGIN_PUT_STD_MULT", {})
            if _sym_put_mult:
                logger.info("Per-symbol put-mult overrides active: %s", list(_sym_put_mult.keys()))
        except Exception:
            pass

    def _closest_put(x: pd.DataFrame) -> pd.DataFrame:
        _v = _sym_put_mult.get(x["symbol"].iloc[0], v_std)
        return (
            x[x["strike"] < x["undPrice"] - _v * x["sdev"]]
            .assign(diff=abs(x["strike"] - (x["undPrice"] - _v * x["sdev"])))
            .sort_values("diff")
            .head(no_of_options)
        )

    df_virg = df_virg.sort_values(["symbol", "expiry", "strike"], ascending=[True, True, False])

    _sow_parts: list[pd.DataFrame] = []
    for (_, _), _sg in df_virg.groupby(["symbol", "expiry"]):
        _sp = _closest_put(_sg)
        if not _sp.empty:
            _sow_parts.append(_sp.drop(columns=["diff"], errors="ignore"))
    virg_short = pd.concat(_sow_parts) if _sow_parts else pd.DataFrame(
        columns=["symbol", "expiry", "strike", "undPrice", "sdev"]
    )
    logger.info(
        "Sow: %d put candidates after strike filter (threshold: undPrice - %.1f×sdev)",
        len(virg_short), v_std,
    )

    virg_puts = [
        Option(s, e, k, "P", "SMART")
        for s, e, k in zip(virg_short.symbol, virg_short.expiry, virg_short.strike)
        if pd.notna(s) and pd.notna(e) and pd.notna(k)
    ]


    df_nkd = pd.DataFrame()

    if virg_puts:
        logger.info("Qualifying virgin put contracts...")
        valid_contracts = qualify_me(
            ib, virg_puts, desc="Qualifying virgin put contracts", batch_size=150
        )
        valid_contracts = [v for v in valid_contracts if v is not None]

        if valid_contracts:
            df_virg1 = clean_ib_util_df(valid_contracts)

            df_virg1["dte"] = df_virg1.expiry.apply(lambda x: get_dte(x))

            nakeds = df_virg1.loc[df_virg1.groupby("symbol")["strike"].idxmax()]

            nakeds = nakeds.reset_index(drop=True)

            nakeds = nakeds.merge(df_unds[["symbol", "price", "iv"]], on="symbol", how="left")
            nakeds.rename(columns={"price": "undPrice", "iv": "vy"}, inplace=True)

            logger.info("Getting naked put prices...")
            df_iv_n = get_volatilities_snapshot(nakeds["contract"].tolist(), market="SNP", ib=ib, desc="Fetching naked put volatilities")

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
                df_nkd["xPrice"] = df_nkd.apply(
                    lambda x: get_prec(max(x.price * NAKEDXPMULT, MINNAKEDOPTPRICE / x.qty), 0.01),
                    axis=1,
                )

                nkd_path = ROOT / "data" / "df_nkd.pkl"
                pickle_me(df_nkd, nkd_path)

                premium = (df_nkd.xPrice * 100 * df_nkd.qty).sum()
                logger.info("Naked Premiums: $ %s", f"{premium:,.2f}")
            else:
                logger.info("No option price data available for naked puts")
        else:
            logger.info("No valid contracts after qualification")
    else:
        logger.info("No suitable put chains found for virgin/orphaned")

    # --- Sow diagnostic ---
    _sow_generated = set(df_nkd["symbol"]) if not df_nkd.empty else set()
    _sow_chain_syms = set(_sow_sym_chains["symbol"]) if not _sow_sym_chains.empty else set()
    _virg_short_syms = set(virg_short["symbol"]) if not virg_short.empty else set()
    for _sym in sorted(df_v["symbol"]):
        if _sym in _sow_generated:
            continue
        if _sym not in _sow_chain_syms:
            _sow_reasons.setdefault("no weekly chain", []).append(_sym)
        elif _sym not in _virg_short_syms:
            _sow_reasons.setdefault("no puts below OTM threshold", []).append(_sym)
        else:
            _sow_reasons.setdefault("qualification or price fetch failed", []).append(_sym)

# %% MAKE REAPS
logger.info("=== MAKE REAPS ===")

delete_pkl_files(["df_reap.pkl"])

df_reap = pd.DataFrame()

if not REAP_ME:
    logger.info("REAP_ME=False — skipping reap generation")
else:
    df_sowed = df_unds[df_unds.state == "unreaped"].reset_index(drop=True)

    df_reap = df_pf[df_pf.symbol.isin(df_sowed.symbol) & (df_pf.secType == "OPT")].reset_index(
        drop=True
    )

    # Exclude contracts that already have an active BUY order (reaping order already submitted).
    if _oo_reaping and not df_reap.empty:
        _reap_key = list(zip(df_reap["symbol"], df_reap["right"], df_reap["strike"]))
        df_reap = df_reap[[k not in _oo_reaping for k in _reap_key]].reset_index(drop=True)

    df_reap = df_reap[df_reap.expiry.apply(get_dte) > MINREAPDTE].reset_index(drop=True)

    df_reap = df_reap.merge(df_unds[["symbol", "iv", "price"]], on="symbol", how="left")
    df_reap.rename(columns={"iv": "vy", "price": "undPrice"}, inplace=True)

    if not df_reap.empty:
        logger.info("Processing %s unreaped positions...", len(df_reap))

        logger.info("Qualifying reap contracts...")
        valid_contracts = qualify_me(ib, df_reap["contract"].tolist(), desc="Qualifying reap contracts")
        valid_contracts = [v for v in valid_contracts if v is not None]

        if valid_contracts:
            logger.info("Calculating reap option prices...")
            reap_prices = {}
            df_reap_prices = get_volatilities_snapshot(valid_contracts, market="SNP", ib=ib, desc="Fetching reap volatilities")
            df_reap["optPrice"] = df_reap.merge(df_reap_prices, on="symbol")["price"]

            df_reap["xPrice"] = df_reap["optPrice"].apply(
                lambda x: get_prec(max(0.01, x), 0.01) if pd.notna(x) else 0.01
            )

            df_reap["xPrice"] = df_reap.apply(
                lambda x: min(x.xPrice, get_prec(abs(x.avgCost * REAPRATIO / 100), 0.01)), axis=1
            )
            df_reap["qty"] = df_reap.position.abs().astype(int)

            reaps = (abs(df_reap.mktPrice - df_reap.xPrice) * df_reap.qty * 100).sum()

            reap_path = ROOT / "data" / "df_reap.pkl"
            pickle_me(df_reap, reap_path)
            logger.info("Have %s reaping options unlocking US$ %s", len(df_reap), f"{reaps:,.0f}")
        else:
            logger.info("No valid contracts after qualification")
    else:
        logger.info("No unreaped positions")

    # --- Per-option reap diagnostic ---
    _sow_candidates = df_pf[(df_pf.secType == "OPT") & (df_pf.state == "sowed")].copy()
    _reaped_keys = (
        set(zip(df_reap["symbol"], df_reap["right"], df_reap["strike"]))
        if not df_reap.empty else set()
    )
    logger.info(
        "=== REAP DIAGNOSTIC: %d sowed options, %d being reaped, %d not ===",
        len(_sow_candidates), len(_reaped_keys), len(_sow_candidates) - len(_reaped_keys),
    )
    _reap_reasons: dict[str, list[str]] = {}
    for _, _opt in _sow_candidates.iterrows():
        _key = (_opt.symbol, _opt.right, _opt.strike)
        if _key in _reaped_keys:
            continue
        _dte_val = get_dte(_opt.expiry)
        _label = f"{_opt.symbol} {_opt.right}@{_opt.strike:.0f} exp {_opt.expiry} DTE={_dte_val}"
        _short_label = f"{_opt.symbol} {_opt.right}@{_opt.strike:.0f}"
        if _key in _oo_reaping:
            logger.info(
                "NOT REAPED %s: active reaping BUY order already submitted",
                _label,
            )
            # Has an active open order — not truly uncreated; exclude from summary
        elif _dte_val <= MINREAPDTE:
            logger.info(
                "NOT REAPED %s: DTE=%d ≤ MINREAPDTE=%d — let expire worthless",
                _label, _dte_val, MINREAPDTE,
            )
            _reap_reasons.setdefault(f"DTE ≤ {MINREAPDTE} (let expire)", []).append(_short_label)
        else:
            _unds_rows = df_unds.loc[df_unds.symbol == _opt.symbol, "state"]
            _st = _unds_rows.iloc[0] if not _unds_rows.empty else "unknown"
            if _st != "unreaped":
                logger.info(
                    "NOT REAPED %s: symbol state='%s' — %s",
                    _label, _st,
                    "active sowing/reaping/covering order (zen)" if _st == "zen"
                    else f"in wheel stock cycle ({_st})",
                )
                _reap_reasons.setdefault("in wheel cycle / zen", []).append(_short_label)
            else:
                logger.info(
                    "NOT REAPED %s: was 'unreaped' but qualification or price fetch failed",
                    _label,
                )
                _reap_reasons.setdefault("qualification failed", []).append(_short_label)

# %% EXTRACT ORPHANED CONTRACTS FROM df_pf
logger.info("=== EXTRACT ORPHANED CONTRACTS FROM df_pf ===")

delete_pkl_files(["df_deorph.pkl"])

df_deorph = df_pf[(df_pf.state == "orphaned") & (df_pf.secType == "OPT")].copy()

df_deorph = df_deorph[
    ~df_deorph.symbol.isin(df_openords.loc[df_openords.state == "de-orphaning", "symbol"])
]

if not df_deorph.empty:
    logger.info("Processing %s orphaned positions...", len(df_deorph))

    logger.info("Qualifying orphaned contracts...")
    valid_contracts = qualify_me(
        ib, df_deorph["contract"].tolist(), desc="Qualifying orphaned contracts"
    )
    valid_contracts = [v for v in valid_contracts if v is not None]

    if valid_contracts:
        df_deorph["qty"] = df_deorph.position.abs().astype(int)
        df_deorph["xPrice"] = df_deorph["mktPrice"].apply(lambda x: max(0.09, get_prec(x, 0.1)))

        deorph_total = (df_deorph.mktPrice * df_deorph.qty * 100).sum()

        deorph_path = ROOT / "data" / "df_deorph.pkl"
        pickle_me(df_deorph, deorph_path)
        logger.info("Have %s orphaned options with total value US$ %s", len(df_deorph), f"{deorph_total:,.0f}")
    else:
        logger.info("No valid contracts after qualification")
else:
    logger.info("There are no orphaned options to process")

# %% IDENTIFY UNPROTECTED POSITIONS
logger.info("=== IDENTIFYING UNPROTECTED POSITIONS ===")

delete_pkl_files(["df_protect.pkl"])

df_protect = pd.DataFrame()

if not PROTECT_ME:
    logger.info("PROTECT_ME=False — skipping protection generation")
else:
    df_unprot = df_unds[
        df_unds.state.isin(["unprotected", "exposed"]) & ~df_unds.symbol.isin(_oo_protecting)
    ].reset_index(drop=True)
    logger.info("Found %s unprotected/exposed positions", len(df_unprot))

    # Separate long and short positions
    df_ulong = df_unprot[df_unprot.position > 0].copy()
    df_ushort = df_unprot[df_unprot.position < 0].copy()

    logger.info("Long unprotected: %s", len(df_ulong))
    logger.info("Short unprotected: %s", len(df_ushort))

    # %% BUILD LONG PROTECTION (PUTS FOR LONG STOCK)
    logger.info("=== BUILDING LONG PROTECTION RECOMMENDATIONS ===")

    df_lprot = pd.DataFrame()

    if not df_ulong.empty:
        logger.info("Processing %s long positions...", len(df_ulong))

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
        df_ul = df_ul.sort_values(["symbol", "expiry", "strike"], ascending=[True, True, False])
        df_ul = df_ul.merge(df_unds[["symbol", "price"]], on="symbol", how="left")
        df_ul.rename(columns={"price": "undPrice"}, inplace=True)

        # Filter for puts below underlying price
        def get_otm_puts(group):
            und_price = group["undPrice"].iloc[0]
            otm_puts = group[group["strike"] <= und_price].head(PROTECTION_STRIP)
            return otm_puts

        df_ul = (
            df_ul.groupby("symbol", group_keys=True)
            .apply(get_otm_puts, include_groups=False)
            .reset_index()
        )

        if not df_ul.empty:
            df_ul = df_ul.dropna(subset=["symbol", "expiry", "strike"])
            df_ul["right"] = "P"
            df_ul["contract"] = df_ul.apply(
                lambda x: Option(x.symbol, x.expiry, x.strike, x.right, "SMART"), axis=1
            )


            # Qualify contracts
            logger.info("Qualifying long protection contracts...")
            valid_contracts = qualify_me(
                ib, df_ul["contract"].tolist(), desc="Qualifying long protection contracts"
            )
            valid_contracts = [v for v in valid_contracts if v is not None]

            # Get option market data using built-in connection in get_volatilities_snapshot
            if valid_contracts:
                logger.info("Getting option prices...")
                df_iv_p = get_volatilities_snapshot(valid_contracts, market="SNP", ib=ib, desc="Fetching long protection volatilities")

                if not df_iv_p.empty:
                    df_u = clean_ib_util_df(valid_contracts)
                    dfu = df_unds[["symbol", "iv", "price", "position"]].copy()
                    dfu.rename(columns={"iv": "vy", "price": "undPrice"}, inplace=True)

                    df_ivp = df_u.merge(dfu, on="symbol", how="left")
                    df_ivp = df_ivp.merge(df_iv_p.drop(columns="conId"), on="symbol", how="left")
                    df_ivp["qty"] = (df_ivp.position.abs() / 100).astype("int")
                    df_ivp["dte"] = df_ivp.expiry.apply(get_dte)
                    df_ivp["protection"] = (df_ivp["undPrice"] - df_ivp["strike"]) * 100 * df_ivp.qty

                    # Select closest (cheapest) protection per symbol
                    df_lprot = df_ivp.loc[df_ivp.groupby("symbol")["protection"].idxmin()]
                    logger.info("Generated %s long protection recommendations", len(df_lprot))
                else:
                    logger.info("No option price data available for long protection")
            else:
                logger.info("No valid contracts after qualification")
        else:
            logger.info("No suitable put chains found for long protection")
    else:
        logger.info("No long unprotected positions")

    # %% BUILD SHORT PROTECTION (CALLS FOR SHORT STOCK)
    logger.info("=== BUILDING SHORT PROTECTION RECOMMENDATIONS ===")

    df_sprot = pd.DataFrame()

    if not df_ushort.empty:
        logger.info("Processing %s short positions...", len(df_ushort))

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
        df_us = df_us.sort_values(["symbol", "expiry", "strike"], ascending=[True, True, True])
        df_us = df_us.merge(df_unds[["symbol", "price"]], on="symbol", how="left")
        df_us.rename(columns={"price": "undPrice"}, inplace=True)

        # Filter for calls above underlying price
        def get_otm_calls(group):
            und_price = group["undPrice"].iloc[0]
            otm_calls = group[group["strike"] >= und_price].head(PROTECTION_STRIP)
            return otm_calls

        df_us = (
            df_us.groupby("symbol", group_keys=True)
            .apply(get_otm_calls, include_groups=False)
            .reset_index()
        )

        if not df_us.empty:
            df_us = df_us.dropna(subset=["symbol", "expiry", "strike"])
            df_us["right"] = "C"
            df_us["contract"] = df_us.apply(
                lambda x: Option(x.symbol, x.expiry, x.strike, x.right, "SMART"), axis=1
            )


            # Qualify contracts
            logger.info("Qualifying short protection contracts...")
            valid_contracts = qualify_me(
                ib, df_us["contract"].tolist(), desc="Qualifying short protection contracts"
            )
            valid_contracts = [v for v in valid_contracts if v is not None]

            # Get option market data using built-in connection in get_volatilities_snapshot
            if valid_contracts:
                logger.info("Getting option prices...")
                df_iv_s = get_volatilities_snapshot(valid_contracts, market="SNP", ib=ib, desc="Fetching short protection volatilities")

                if not df_iv_s.empty:
                    df_u = clean_ib_util_df(valid_contracts)
                    dfu = df_unds[["symbol", "iv", "price", "position"]].copy()
                    dfu.rename(columns={"iv": "vy", "price": "undPrice"}, inplace=True)

                    df_ivs = df_u.merge(dfu, on="symbol", how="left")
                    df_ivs = df_ivs.merge(df_iv_s.drop(columns="conId"), on="symbol", how="left")
                    df_ivs["qty"] = (df_ivs.position.abs() / 100).astype("int")
                    df_ivs["dte"] = df_ivs.expiry.apply(get_dte)
                    df_ivs["protection"] = (df_ivs["strike"] - df_ivs["undPrice"]) * 100 * df_ivs.qty

                    # Select closest (cheapest) protection per symbol
                    df_sprot = df_ivs.loc[df_ivs.groupby("symbol")["protection"].idxmin()]
                    logger.info("Generated %s short protection recommendations", len(df_sprot))
                else:
                    logger.info("No option price data available for short protection")
            else:
                logger.info("No valid contracts after qualification")
        else:
            logger.info("No suitable call chains found for short protection")
    else:
        logger.info("No short unprotected positions")

    # %% CALCULATE FINAL PROTECTION PRICES
    logger.info("=== CALCULATING FINAL PROTECTION PRICES ===")

    df_protect = pd.concat([df_lprot, df_sprot], ignore_index=True)

    if df_protect.empty:
        logger.info("No protection recommendations generated!")
    else:
        logger.info("Combined %s protection recommendations", len(df_protect))

        # Replace 'vy' with 'iv' in 'df_protect' where 'iv' is not NaN
        mask = df_protect["iv"].notna()
        df_protect.loc[mask, "vy"] = df_protect.loc[mask, "iv"]

        df_protect["xPrice"] = df_protect["price"].apply(
            lambda x: get_prec(max(0.01, x), 0.01) if pd.notna(x) else 0.01
        )

        # Calculate costs
        df_protect["cost"] = df_protect["xPrice"] * df_protect["qty"] * 100
        df_protect["puc"] = df_protect["protection"] / df_protect["cost"]

        # Clean up
        df_protect.drop(columns=["iv", "hv"], inplace=True, errors="ignore")

        # Save results
        protect_path = ROOT / "data" / "df_protect.pkl"
        pickle_me(df_protect, protect_path)
        logger.info("Saved %s protection recommendations to %s", len(df_protect), protect_path)

# %% FINAL OUTPUT (PROTECTION)
logger.info("=== PROTECTION RECOMMENDATIONS COMPLETE ===")
if not df_protect.empty:
    logger.info("Successfully generated %s protection recommendations", len(df_protect))
else:
    logger.info("No protection recommendations needed or generated!")

# %% ROLLS FOR PROTECTING PUTS
logger.info("=== ROLLS FOR PROTECTING PUTS ===")

delete_pkl_files(["protect_rolls.pkl"])

if not PROTECT_ME:
    logger.info("PROTECT_ME=False — skipping protection rolls")
else:
    df_pfu = df_pf.merge(df_unds[["symbol", "price"]], on="symbol", how="left")
    df_pfu.rename(columns={"price": "undPrice"}, inplace=True)

    df_rolls = (
        df_pfu[(df_pfu["state"] == "protecting") & (df_pfu.right == "P")]
        .assign(
            odiff=lambda x: (x["undPrice"] - x["strike"]),
            ostrike=df_pfu.strike,
            odte=df_pfu.dte,
            pct_diff=lambda x: (abs(x["strike"] - x["undPrice"]) / x["undPrice"] * 100),
        )
        .sort_values("pct_diff", ascending=False)
        .reset_index(drop=True)
    )

    short_itm_calls = (
        df_pfu[
            (df_pfu.secType == "OPT")
            & (df_pfu.right == "C")
            & (df_pfu.position < 0)
            & df_pfu["undPrice"].notna()
        ]
        .loc[lambda x: x["strike"] < x["undPrice"], "symbol"]
        .unique()
    )

    if short_itm_calls.size:
        df_rolls = df_rolls[~df_rolls.symbol.isin(short_itm_calls)].reset_index(drop=True)
        logger.info(
            "Skipping protecting-put rolls for symbols with ITM short calls: %s",
            ", ".join(sorted(short_itm_calls)),
        )

    if not df_rolls.symbol.isnull().all().all():
        rol_chains = chains[chains.symbol.isin(set(df_rolls.symbol))]

        rol_chains = (
            rol_chains.set_index("symbol").join(df_unds.set_index("symbol")[["price"]]).reset_index()
        )
        rol_chains.rename(columns={"price": "undPrice"}, inplace=True)

        df_cd = filter_closest_dates(rol_chains, PROTECT_DTE, num_dates=1)
        p = filter_closest_strikes(df_cd, n=-4)

    else:
        logger.info("No protecting puts found in portfolio for rolling")
        p = pd.DataFrame()

    df_purl = pd.DataFrame()

    if not p.empty:
        p = p.dropna(subset=["symbol", "expiry", "strike"])
        p["right"] = "P"
        # pyrefly: ignore [no-matching-overload]
        p["contract"] = p.apply(
            lambda x: Option(x.symbol, x.expiry, x.strike, x.right, "SMART"), axis=1
        )


        logger.info("Qualifying protecting put roll contracts...")
        valid_contracts = qualify_me(
            ib, p["contract"].tolist(), desc="Qualifying protecting put roll contracts"
        )
        valid_contracts = [v for v in valid_contracts if v is not None]

        if valid_contracts:
            df_u = clean_ib_util_df(valid_contracts)
            df_purl = df_u.groupby("symbol").first().reset_index()

            logger.info("Getting put roll prices...")
            df_iv_purl = get_volatilities_snapshot(df_purl["contract"].tolist(), market="SNP", ib=ib, desc="Fetching put roll volatilities")

            if not df_iv_purl.empty:
                df_up = df_unds.assign(undPrice=lambda x: x.price)

                purls = df_iv_purl.merge(df_up[["symbol", "undPrice"]], on="symbol")
                purls = purls.merge(df_u[["conId", "secType", "right", "strike", "expiry"]], on="conId")
                purls = purls.merge(df_rolls[["symbol", "odiff"]], on="symbol")
                purls = purls.merge(df_rolls[["symbol", "ostrike"]], on="symbol")
                purls = purls.merge(df_rolls[["symbol", "odte"]], on="symbol")

                purls["diff"] = purls["strike"] / purls["undPrice"] - 1
                purls = purls.sort_values("diff", key=lambda x: x - purls["odiff"])

                cols = [
                    "symbol",
                    "secType",
                    "expiry",
                    "strike",
                    "ostrike",
                    "odte",
                    "undPrice",
                    "right",
                    "price",
                    "odiff",
                    "diff",
                ]

                if (purls["diff"] < -0.05).any():
                    logger.info(
                        "WARNING: There are some put rolls whose strike-undPrice is larger than 5%. "
                        "These will be taken out from auto-roll suggestion."
                    )

                purls1 = purls[purls["diff"] >= -0.05]
                purls1 = purls1[purls1["strike"] != purls1["ostrike"]]

                purls1 = purls1.copy()
                purls1["qty"] = purls1["symbol"].map(df_unds.set_index("symbol")["position"] / 100)

                purls1 = purls1.merge(
                    df_pf[(df_pf.secType == "OPT") & (df_pf.right == "P") & (df_pf.position > 0)][
                        ["symbol", "mktPrice"]
                    ],
                    on="symbol",
                    how="left",
                )
                purls1.rename(columns={"mktPrice": "cost"}, inplace=True)
                purls1["rollcost"] = (
                    (purls1.price - purls1.cost + purls1["strike"] - purls1["ostrike"])
                    * purls1.qty
                    * 100
                )

                purls1 = purls1.sort_values(["odte", "rollcost"], ascending=[True, False])
                rol_cols = [
                    "symbol",
                    "conId",
                    "expiry",
                    "undPrice",
                    "strike",
                    "ostrike",
                    "odte",
                    "right",
                    "qty",
                    "price",
                    "cost",
                    "rollcost",
                ]

                rollover_cost = (
                    (purls1.price - purls1.cost + purls1["strike"] - purls1["ostrike"])
                    * purls1.qty
                    * 100
                )
                logger.info(
                    "The rollover cost of %s symbols for %s days would be $%s.",
                    purls1.symbol.unique().shape[0],
                    f"{purls1.expiry.apply(get_dte).max():.0f}",
                    f"{rollover_cost.sum():,.0f}",
                )
                purls1 = purls1[rol_cols].sort_values("rollcost", ascending=False)
                purls_path = ROOT / "data" / "df_prot_rolls.pkl"
                pickle_me(purls1[rol_cols], purls_path)
            else:
                logger.info("No option price data available for protecting put rolls")
        else:
            logger.info("No valid contracts after qualification")
    else:
        logger.info("No suitable put chains found for protecting rolls")

# %% UNCREATED ORDERS SUMMARY
_uncreated: dict = {}
if _missing_cov and _cov_reasons:
    _r = "; ".join(
        f"{k}: {', '.join(sorted(set(v)))}" for k, v in _cov_reasons.items() if v
    )
    logger.info("Symbols without Covered Calls due to — %s", _r)
    _uncreated["cover"] = {k: sorted(set(v)) for k, v in _cov_reasons.items() if v}
if _sow_reasons:
    _r = "; ".join(
        f"{k}: {', '.join(sorted(set(v)))}" for k, v in _sow_reasons.items() if v
    )
    logger.info("Symbols without Sow Orders due to — %s", _r)
    _uncreated["sow"] = {k: sorted(set(v)) for k, v in _sow_reasons.items() if v}
if _reap_reasons:
    _r = "; ".join(f"{k}: {', '.join(v)}" for k, v in _reap_reasons.items() if v)
    logger.info("Symbols without Reap Orders due to — %s", _r)
    _uncreated["reap"] = {k: list(v) for k, v in _reap_reasons.items() if v}
(ROOT / "data" / "derive_uncreated.json").write_text(
    json.dumps(_uncreated), encoding="utf-8"
)

# %% FINAL OUTPUT
end_time = time.time()
execution_time = end_time - start_time
minutes = int(execution_time // 60)
seconds = int(execution_time % 60)
logger.info("Total execution time: %s minutes and %s seconds", minutes, seconds)
logger.info("=== RECOMMENDATIONS COMPLETE ===")
