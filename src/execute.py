# %%
# @@@ CAUTION: This script places orders @@@

import os
import pandas as pd
import numpy as np
from rich.progress import track
from dotenv import find_dotenv, load_dotenv
from typing import List
from ib_async import LimitOrder

# Import from provided modules
# pyrefly: ignore [missing-import]
from src.build import (
    ROOT,
    load_config,
    pickle_me,
    get_pickle,
    delete_pkl_files,
    get_ib_connection,
    get_prec,
)

# pyrefly: ignore [missing-import]
from src.classify import get_financials

# Load environment variables
dotenv_path = find_dotenv()
load_dotenv(dotenv_path=dotenv_path)

# Use US_ACCOUNT for all operations
ACCOUNT_NO = os.getenv("US_ACCOUNT", "")

config = load_config("SNP")

MINCUSHION = config.get("MINCUSHION")
COVER_ME = config.get("COVER_ME")
PROTECT_ME = config.get("PROTECT_ME")
REAP_ME = config.get("REAP_ME")
SOW_NAKEDS = config.get("SOW_NAKEDS")

cov_path         = ROOT / "data" / "df_cov.pkl"
monthly_cov_path = ROOT / "data" / "df_monthly_cov.pkl"
nkd_path         = ROOT / "data" / "df_nkd.pkl"
reap_path        = ROOT / "data" / "df_reap.pkl"
protect_path     = ROOT / "data" / "df_protect.pkl"
deorph_path      = ROOT / "data" / "df_deorph.pkl"


def make_ib_orders(df: pd.DataFrame, action: str, account_no: str) -> tuple:
    """Make (contract, order) tuples with account number assigned to orders"""
    contracts = df.contract.to_list()
    orders = [
        LimitOrder(
            action=action,
            totalQuantity=abs(int(q)),
            lmtPrice=get_prec(p, 0.01),
            tif="DAY",
            account=account_no,  # Set the account number for the order
        )
        for q, p in zip(df.qty, df.xPrice)
    ]

    cos = tuple((c, o) for c, o in zip(contracts, orders))

    return cos


def place_orders(cos: tuple, account_no: str = "", blk_size: int = 25) -> List:
    """CAUTION: This places trades in the system !!!"""

    trades = []

    cobs = {cos[i : i + blk_size] for i in range(0, len(cos), blk_size)}

    with get_ib_connection("SNP", account_no=account_no) as ib:
        for b in track(cobs, description="Executing orders"):
            for c, o in b:
                td = ib.placeOrder(c, o)
                trades.append(td)
            ib.sleep(0.75)

    return trades


# %%
# ORDER COVER OPTIONS (weekly + monthly — both honour COVER_ME, COVXPMULT; monthly ignores COVER_MIN_DTE)

if COVER_ME:
    if cov_path.exists():
        df_cov = get_pickle(cov_path)
        cos = make_ib_orders(df_cov, action="SELL", account_no=ACCOUNT_NO)
        cov_trades = place_orders(cos, account_no=ACCOUNT_NO)
        pickle_me(cov_trades, ROOT / "data" / "traded_covers.pkl")
        print(f"\nPlaced {len(df_cov)} weekly cover orders")
        delete_pkl_files(["df_cov.pkl"])
    else:
        print("\nThere are no weekly covers\n")

    if monthly_cov_path.exists():
        df_monthly_cov = get_pickle(monthly_cov_path)
        monthly_cos = make_ib_orders(df_monthly_cov, action="SELL", account_no=ACCOUNT_NO)
        monthly_trades = place_orders(monthly_cos, account_no=ACCOUNT_NO)
        pickle_me(monthly_trades, ROOT / "data" / "traded_monthly_covers.pkl")
        print(f"\nPlaced {len(df_monthly_cov)} monthly cover orders")
        delete_pkl_files(["df_monthly_cov.pkl"])
    else:
        print("\nThere are no monthly covers\n")
else:
    print("\nCOVER_ME is disabled (false) in configuration\n")

# %%
# ORDER REAP OPTIONS
if REAP_ME:
    if (df_reap_path := reap_path).exists():
        df_reap = get_pickle(df_reap_path)
        reap_cos = make_ib_orders(df_reap, action="BUY", account_no=ACCOUNT_NO)
        reap_trades = place_orders(reap_cos, account_no=ACCOUNT_NO)
        print(f"\nPlaced {len(df_reap)} reaped options")
        pickle_me(reap_trades, ROOT / "data" / "traded_reaps.pkl")
        delete_pkl_files(["df_reap.pkl"])
    else:
        print("\nREAP_ME is disabled (false) in configuration\n")
else:
    print("\nREAP_ME is disabled (false) in configuration\n")

# %%
# ORDER NAKEDS BASED ON CUSHION
if SOW_NAKEDS:
    if (df_nkd_path := nkd_path).exists():
        fin = get_financials(account=ACCOUNT_NO)
        cushion = fin.get("cushion", np.nan)
        if cushion < MINCUSHION:
            print(
                f"Cushion: {cushion:.2f} < MINCUSHION: {MINCUSHION:.2f}, not placing naked orders"
            )
        else:
            df_nkd = get_pickle(df_nkd_path)
            nkd_cos = make_ib_orders(df_nkd, action="SELL", account_no=ACCOUNT_NO)
            nkd_trades = place_orders(nkd_cos, account_no=ACCOUNT_NO)
            print(f"\nPlaced {len(df_nkd)} naked options")
            pickle_me(nkd_trades, ROOT / "data" / "traded_nakeds.pkl")
            delete_pkl_files(["df_nkd.pkl"])
    else:
        print("\nThere are no nakeds\n")
else:
    print("\nSOW_NAKEDS is disabled (false) in configuration\n")

# %%
# ORDER PROTECT OPTIONS
if PROTECT_ME:
    if (df_protect_path := protect_path).exists():
        df_protect = get_pickle(df_protect_path)
        protect_cos = make_ib_orders(df_protect, action="BUY", account_no=ACCOUNT_NO)
        protect_trades = place_orders(protect_cos, account_no=ACCOUNT_NO)
        print(f"\nPlaced {len(df_protect)} protect options")
        pickle_me(protect_trades, ROOT / "data" / "traded_protects.pkl")
        delete_pkl_files(["df_protect.pkl"])
    else:
        print("\nThere are no protect options\n")
else:
    print("\nPROTECT_ME is disabled (false) in configuration\n")

# %%
# ORDER ORPHANED OPTIONS
if (df_deorph_path := deorph_path).exists():
    df_deorph = get_pickle(df_deorph_path)
    deorph_cos = make_ib_orders(df_deorph, action="SELL", account_no=ACCOUNT_NO)
    deorph_trades = place_orders(deorph_cos, account_no=ACCOUNT_NO)
    print(f"\nPlaced {len(df_deorph)} orphaned options")
    pickle_me(deorph_trades, ROOT / "data" / "traded_deorphs.pkl")
    delete_pkl_files(["df_deorph.pkl"])
else:
    print("\nThere are no orphaned options\n")
# %%
