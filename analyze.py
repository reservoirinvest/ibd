# %%
# IMPORTS AND CONFIGS

import os

import numpy as np
import pandas as pd

# pyrefly: ignore [missing-import]
from build import (
    ROOT,
    chains_n_unds,
    do_i_refresh,
    get_dte,
    get_pickle,
    load_config,
)
# pyrefly: ignore [missing-import]
from classify import (
    classifed_results,
    classify_open_orders,
    get_financials,
    get_open_orders,
)

# Load config
config = load_config("SNP")
MAX_FILE_AGE = config.get("MAX_FILE_AGE")
REAPRATIO = config.get("REAPRATIO")
COVER_ME = config.get("COVER_ME")
PROTECT_ME = config.get("PROTECT_ME")
REAP_ME = config.get("REAP_ME")

# Paths
pf_path = ROOT / "data" / "df_pf.pkl"
cov_path = ROOT / "data" / "df_cov.pkl"
nkd_path = ROOT / "data" / "df_nkd.pkl"
unds_path = ROOT / "data" / "df_unds.pkl"
protect_path = ROOT / "data" / "df_protect.pkl"
reap_path = ROOT / "data" / "df_reap.pkl"
chains_path = ROOT / "data" / "df_chains.pkl"
purls_path = ROOT / "data" / "df_prot_rolls.pkl"
deorph_path = ROOT / "data" / "df_deorph.pkl"

# Load pickled dataframes, handle None gracefully
df_cov = get_pickle(cov_path, print_msg=False)
df_nkd = get_pickle(nkd_path, print_msg=False)
df_protect = get_pickle(protect_path, print_msg=False)
df_reap = get_pickle(reap_path, print_msg=False)
df_rolls = get_pickle(purls_path, print_msg=False)
df_deorph = get_pickle(deorph_path, print_msg=False)

# %%
ACCOUNT = 'US_ACCOUNT'
ACCOUNT_NO = os.getenv(ACCOUNT, "")

# Refresh base data if needed
if do_i_refresh(unds_path, max_days=MAX_FILE_AGE):
    chains, df_unds = chains_n_unds()
else:
    chains = get_pickle(chains_path, print_msg=False)
    df_unds = get_pickle(unds_path, print_msg=False)

#%%
# GET CLASSIFIED DATA
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

fin = get_financials(ACCOUNT_NO)  # Use classify's get_financials
fin["unique symbols"] = len(df_pf.symbol.unique())
fin[f"...{len(df_pf[df_pf.secType == 'STK'])} stocks"] = df_pf[df_pf.secType == 'STK'].mktVal.sum()
if 'stocks' in fin:
    del fin['stocks']
fin[f"...{len(df_pf[df_pf.secType == 'OPT'])} options"] = df_pf[df_pf.secType == 'OPT'].mktVal.sum()

print("\nFINANCIALS")
print('==========')
for k, v in fin.items():
    if v:
        print(f"{k}: {format(v, ',.2f') if abs(v) < 1 else format(v, ',.0f')}")

# %%
# COMPUTES RISK AND REWARD WITH COST OF MITIGATING RISK

df_openords = get_open_orders(account_no=ACCOUNT_NO)
df_openords = classify_open_orders(df_openords, df_pf)

df = pd.concat([
    df_pf.assign(source='pf'),
    df_openords.assign(source='oo')
], ignore_index=True)

df = df.assign(
    dte=df.expiry.apply(lambda x: get_dte(x) if pd.notna(x) and x else np.nan)
)

# Simplified sorting: by symbol, then right (C < 0 < P), then source
df['sort_key'] = df.apply(lambda x: (
    x['symbol'],
    {'C': 0, '0': 1, 'P': 2}.get(x['right'], 3),
    1 if x['source'] == 'und' else 0
), axis=1)
df = df.sort_values('sort_key').drop('sort_key', axis=1).reset_index(drop=True)

df = (pd.concat([df, df_unds.assign(source='und')], ignore_index=True)
      .assign(source_order=lambda x: x['right'].map({'C': 0, '0': 1, 'P': 2, np.nan: 3}))
      .sort_values(by=['symbol', 'source_order'])
      .drop(columns=['source_order'])
      .reset_index(drop=True))

und_price_dict = df_unds.set_index('symbol')['price'].to_dict() if not df_unds.empty else {}
df['undPrice'] = df['symbol'].map(und_price_dict)

# Sum unPnL for underlyings
sum_by_symbol = df.groupby('symbol')['unPnL'].transform('sum')
df.loc[df['source'] == 'und', 'unPnL'] = sum_by_symbol

# Fill mktVal for open orders from portfolio mean (if available)
df.loc[df.source == 'oo', 'mktVal'] = df.groupby('symbol')['mktVal'].transform('mean')

# Set avgCost and position for open orders
df.loc[df.source == 'oo', 'avgCost'] = df.loc[df.source == 'oo', 'lmtPrice'] * 100
df.loc[df.source == 'oo', 'position'] = df.loc[df.source == 'oo', 'qty']

# Set qty for portfolio
df.loc[(df.source == 'pf') & (df.secType == 'STK'), 'qty'] = df['position'] / 100
df.loc[(df.source == 'pf') & (df.secType == 'OPT'), 'qty'] = df['position']

cols = [
    'source', 'symbol', 'conId', 'secType', 'position', 'state', 'undPrice', 'strike',
    'avgCost', 'mktVal', 'right', 'expiry', 'dte', 'qty', 'lmtPrice', 'action', 'unPnL'
]
df = df[cols]

# Compute risks for protecting positions
df_risk = (
    df.query('state == "protecting"')
    .groupby('symbol')
    .agg({
        'source': 'first',
        'avgCost': lambda x: (x * df.loc[x.index, 'position']).sum(),
        'undPrice': 'first',
        'strike': 'first',
        'dte': 'first',
        'position': 'first',
        'qty': 'first',
        'mktVal': lambda x: (x * df.loc[x.index, 'qty']).sum()
    })
    .assign(
        cost=lambda x: x['avgCost'],
        unprot_val=lambda x: np.where(
            x['source'] == 'pf',
            abs(x['undPrice'] - x['strike']) * x['position'] * 100,
            abs((x['undPrice'] - x['strike']) * x['qty']) * 100
        )
    )
    .reset_index()
    [['symbol', 'source', 'cost', 'unprot_val', 'mktVal', 'dte']]
)

# Compute rewards for covering positions
df_reward = (
    df.query('state == "covering"')
    .groupby('symbol')
    .agg({
        'source': 'first',
        'avgCost': lambda x: (x * df.loc[x.index, 'position']).sum(),
        'undPrice': 'first',
        'strike': 'first',
        'dte': 'first',
        'position': 'first',
        'qty': 'first',
        'mktVal': lambda x: (x * df.loc[x.index, 'qty']).sum()
    })
    .assign(
        premium=lambda x: x['avgCost'],
        max_reward=lambda x: abs((x['strike'] - x['undPrice']) * x['qty'] * 100)
    )
    .reset_index()
    [['symbol', 'source', 'premium', 'max_reward', 'mktVal', 'dte']]
)

# Assignment risk (simplified version, as get_assignment_risk not provided)
df_assign = df[(df.state == 'sowed') & (
    ((df.right == 'C') & (df.undPrice > df.strike)) |
    ((df.right == 'P') & (df.strike > df.undPrice))
)].reset_index(drop=True)

df_assign.sort_values('unPnL', ascending=True, inplace=True)

# Covers about to get blown
df_cov_blow = df[(df.state == 'covering') & (df.source == 'pf') & (
    ((df.right == 'C') & (df.strike < df.undPrice)) |
    ((df.right == 'P') & (df.strike > df.undPrice))
)]

# Cover blown: STK or short OPT in covering symbols
cols = ['symbol', 'secType', 'position', 'right', 'dte', 'strike', 'undPrice', 'avgCost', 'mktVal', 'unPnL']
cover_condition = (
    (df.source == 'pf') &
    (df.symbol.isin(df_assign[df_assign.state == 'covering'].symbol)) &
    ((df.secType == 'STK') | ((df.secType == 'OPT') & (df.position < 0)))
)
cover_blown = df[cover_condition].sort_values(['symbol', 'right'], ascending=[True, False])[cols]

# Projections
df_sowed = df[df.state == 'sowed'].sort_values('unPnL')
cover_projection = (df_reward.dte.mean() / 7) * abs(df_reward.premium.sum()) if not df_reward.empty else 0
sowed_projection = df_sowed.avgCost.sum() * (1 - REAPRATIO) if not df_sowed.empty else 0
total_reward = cover_projection + abs(sowed_projection)

print('\nRISKS')
print('======')
if not PROTECT_ME:
    print('PROTECT_ME is disabled in configuration.')

pf_states = {state: df_pf[df_pf.state == state].symbol.nunique() for state in df_pf.state.unique()}
msg = ' '.join(f"{state}: {n}" for state, n in pf_states.items())
# print('\nPortfolio symbol states:\n' + msg)

stocks_val = df_pf[df_pf.symbol.isin(df_pf[df_pf.state == 'protecting'].symbol)].mktVal.sum()

risk_msg = []
if not df_risk.empty:
    risk_msg.append(f'Risk from {df_pf[df_pf.state == "protecting"].symbol.nunique()} protected stocks valued at ${stocks_val:,.0f} is ${df_risk.unprot_val.sum():,.0f} for {df_risk.dte.mean():.1f} days.')
    risk_msg.append(f'Risk premium paid: ${df_risk.cost.sum():,.0f}')

unprotected_stocks = df[(df.source == "und") & (df.state.isin(["unprotected", "exposed"]))].symbol.unique()

podf = df[(df.source == 'oo') & (df.state == 'protecting')].reset_index(drop=True)
oo_protect = sum(abs((podf.undPrice - podf.strike) * podf.qty) * 100) if not podf.empty else 0
podf_mkt = df_pf[df_pf.symbol.isin(podf.symbol.unique()) & (df_pf.secType == 'STK')].mktVal.sum() if not podf.empty else 0

if unprotected_stocks.size > 0:
    stocks_str = '\n\t'.join([', '.join(unprotected_stocks[i:i+5]) for i in range(0, len(unprotected_stocks), 5)])
    risk_msg.append(f'\n{len(unprotected_stocks)} stocks need protection:\n{stocks_str}')
    if df_protect is not None:
        dprot = df_protect[df_protect.symbol.isin(unprotected_stocks)]
        protection = dprot.protection.sum() if 'protection' in dprot.columns else 0
        protection_price = (dprot.xPrice * dprot.qty * 100).sum() if 'xPrice' in dprot.columns else 0
        dprot_val = sum(dprot.undPrice * dprot.qty * 100) if 'undPrice' in dprot.columns else 0
        risk_msg.append(f'\nFor {len(dprot)} stocks worth ${dprot_val:,.0f}, protection band of ${protection:,.0f} for {df_protect.dte.mean():.1f} days at cost ${protection_price:,.0f}')
        if not PROTECT_ME:
            risk_msg.append(' (PROTECT_ME disabled)')
elif podf_mkt > 0:
    risk_msg.append(f'\nRemaining positions worth ${podf_mkt:,.0f} protected with ${oo_protect:,.0f} from {len(podf)} open orders at cost ${sum(podf.avgCost * podf.qty):,.0f}')

if df_rolls is not None:
    risk_msg.append(f"\nRollover cost for {df_rolls.symbol.unique().shape[0]} symbols over {df_rolls.expiry.apply(get_dte).max():.0f} days: ${df_rolls.rollcost.sum():,.0f}")

if not df_assign.empty:
    risk_msg.append(f'\n{len(set(df_assign.symbol))} naked assignments in {df_assign.dte.mean():.1f} days:')
    risk_cols = ['symbol', 'right', 'undPrice', 'strike', 'dte', 'position', 'qty', 'avgCost', 'mktVal', 'unPnL']
    risk_msg.append(df_assign[risk_cols].to_string(index=False))

print('\n'.join(risk_msg))

print('\nREWARDS')
print('=======')

naked_premium = (df_openords.lmtPrice * df_openords.qty).sum() * 100 if not df_openords.empty else 0

if not COVER_ME:
    print('COVER_ME disabled. No cover premiums calculable.')

reward_msg = (
    f'Total weekly reward projection: ${total_reward:,.0f}\n'
    f'  Sowed reward over {df_sowed.dte.mean():.1f} days: ${sowed_projection:,.0f}\n'
    f'  Cover premiums over {df_reward.dte.mean():.1f} days: ${abs(df_reward.premium.sum()):,.0f}\n'
    f'  Max cover reward if all blown: ${df_reward.max_reward.sum():,.0f}'
)

if naked_premium > 0:
    reward_msg += f'\n  Naked premiums from open orders: ${naked_premium:,.0f}'

print(reward_msg)

if not cover_blown.empty:
    print(f'\n{len(set(cover_blown.symbol))} covers may blow, realizing ${cover_blown.unPnL.sum():,.0f} over {cover_blown.dte.mean():.1f} days:\n')
    print(cover_blown.to_string(index=False))

# %%
# GETS STATE DETAILS

if df_cov is not None:
    cov_premium = (df_cov.xPrice * df_cov.qty * 100).sum() if 'xPrice' in df_cov.columns else 0
    maxProfit = (
        np.where(
            df_cov.right == "C",
            (df_cov.strike - df_cov.undPrice) * df_cov.qty * 100,
            (df_cov.undPrice - df_cov.strike) * df_cov.qty * 100,
        ).sum()
        + cov_premium
    ) if 'undPrice' in df_cov.columns else 0
else:
    cov_premium = 0
    maxProfit = 0

if df_nkd is not None and not df_nkd.empty and 'xPrice' in df_nkd.columns:
    nkd_premium = (df_nkd.xPrice * 100 * df_nkd.qty).sum()
else:
    nkd_premium = 0

print('\nORDER PREMIUMS AND PROFITS')
print('==========================')
total_premium = cov_premium + nkd_premium
print(f"Total premium available: ${total_premium:,.0f}")
print(f"  Cover premium: ${cov_premium:,.0f}")
print(f"  Naked premium: ${nkd_premium:,.0f}")
if cov_premium > 0:
    print(f"Max profit if all covers blown: ${maxProfit:,.0f}")

print('\nSYMBOL COUNT BY STATE')
print('=====================')
state_counts = ', '.join(f"{state}: {len(df)}" for state, df in df_unds.groupby('state')) if not df_unds.empty else 'No states available'
print(state_counts)

print('\nDATAFRAME SYMBOL COUNTS')
print('=======================')
df_counts = {
    k: len(v) if isinstance(v, pd.DataFrame) else 0
    for k, v in {
        'df_deorph': df_deorph,
        'df_cov': df_cov,
        'df_protect': df_protect,
        'df_reap': df_reap,
        'df_nkd': df_nkd,
        'df_rolls': df_rolls,
    }.items()
}
print(', '.join(f"{k}: {v}" for k, v in df_counts.items()))

if df_unds is not None and not df_unds.empty:
    orphaned_symbols = set(df_unds[df_unds.state == 'orphaned'].symbol)
    de_orphed_symbols = set(df_deorph['symbol']) if df_deorph is not None and not df_deorph.empty else set()
    if len(orphaned_symbols - de_orphed_symbols) > 0:
        print(f"\nOrphaned symbols left out: {', '.join(orphaned_symbols - de_orphed_symbols)}")

    uncovered_symbols = set(df_unds[df_unds.state == 'uncovered'].symbol)
    covered_symbols = set(df_cov['symbol']) if df_cov is not None and not df_cov.empty else set()
    if len(uncovered_symbols - covered_symbols) > 0:
        print(f"\nUncovered symbols left out: {', '.join(uncovered_symbols - covered_symbols)}")

    unreaped_symbols = set(df_unds[df_unds.state == 'unreaped'].symbol)
    reaped_symbols = set(df_reap['symbol']) if df_reap is not None and not df_reap.empty else set()
    if len(unreaped_symbols - reaped_symbols) > 0:
        print(f"\nUnreaped symbols left out: {', '.join(unreaped_symbols - reaped_symbols)}")

    unprotected_symbols = set(df_unds[df_unds.state.isin(['exposed', 'unprotected'])].symbol)
    protected_symbols = set(df_unds[df_unds.state.isin(['zen', 'uncovered'])].symbol)
    if len(unprotected_symbols - protected_symbols) > 0:
        print(f"\nUnprotected symbols left out: {', '.join(unprotected_symbols - protected_symbols)}")



# %%
# ANALYZES THE BASE DATA QUALITY

unique_unds_symbols = df_unds['symbol'].nunique() if not df_unds.empty else 0
unique_chains_symbols = chains['symbol'].nunique() if not chains.empty else 0
unique_oo_symbols = df_openords['symbol'].nunique() if not df_openords.empty and 'symbol' in df_openords.columns else 0
unique_pf_symbols = df_pf['symbol'].nunique() if not df_pf.empty and 'symbol' in df_pf.columns else 0

print('\nBASE INTEGRITY CHECK')
print('====================')
unique_symbols_count = {
    'Und symbols': unique_unds_symbols,
    'Chain symbols': unique_chains_symbols,
    'Portfolio symbols': unique_pf_symbols,
    'Open order symbols': unique_oo_symbols,
}
for k, v in unique_symbols_count.items():
    print(f'{k}: {v}')

if not chains.empty and not df_unds.empty:
    missing_in_unds = chains[~chains['symbol'].isin(df_unds['symbol'])]['symbol'].unique()
    missing_in_chains = df_unds[~df_unds['symbol'].isin(chains['symbol'])]['symbol'].unique()
    missing_in_chains_from_pf = df_pf[~df_pf['symbol'].isin(chains['symbol'])]['symbol'].unique() if not df_pf.empty else []

    print("\nMissing symbols:")
    print("In unds from chains:", missing_in_unds)
    print("In chains from unds:", missing_in_chains)
    print("In chains from pf:", missing_in_chains_from_pf)
# %%
# OVERALL P&L CHECK
df_pnl = (
    df_pf.groupby('symbol', as_index=False)['unPnL']
         .sum()
         .rename(columns={'unPnL': 'symbolPnL'})
         .sort_values('symbolPnL', ascending=True)
         .reset_index(drop=True)
)

print('\nOVERALL PnL CHECK')
print('===================')
print(df_pnl.to_string(index=False))
# %%
