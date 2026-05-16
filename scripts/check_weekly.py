import pandas as pd

chains = pd.read_pickle("data/df_chains.pkl")
unds   = pd.read_pickle("data/df_unds.pkl")

chains["exp_date"] = pd.to_datetime(chains["expiry"], format="%Y%m%d")

def is_monthly(dt):
    return dt.weekday() == 4 and 15 <= dt.day <= 21

chains["is_monthly"] = chains["exp_date"].apply(is_monthly)

stats = chains.groupby("symbol").apply(lambda g: pd.Series({
    "monthly":     g[g["is_monthly"]]["expiry"].nunique(),
    "non_monthly": g[~g["is_monthly"]]["expiry"].nunique(),
})).reset_index()

not_weekly = set(stats[stats["non_monthly"] == 0]["symbol"])
result = sorted(s for s in unds["symbol"] if s in not_weekly)

print(f"Count: {len(result)}")
for s in result:
    m = int(stats.loc[stats.symbol == s, "monthly"].iloc[0])
    print(f"  {s:<8}  monthly_expiries={m}")
