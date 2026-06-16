"""The decisive NSE invariant: every derived order quantity is a whole lot multiple."""

import pandas as pd

from src.nsewheel import build as build_mod
from src.nsewheel import derive as derive_mod
from src.nsewheel.broker import mock_data
from src.nsewheel.config import load_config


def _chains_and_unds(df_instr, client):
    quotes = mock_data.build_quotes(mock_data.build_instruments_df())
    df_chains = build_mod.build_chains(df_instr, quotes, max_dte=50)
    df_unds = build_mod.build_unds(df_chains, mock_data.spot_map(), {})
    df_unds = build_mod.add_margins(df_unds, client)
    return df_chains, df_unds


def _assert_lot_multiples(df):
    assert not df.empty
    assert (df["qty"] > 0).all()
    assert (df["lots"] >= 1).all()
    assert (df["qty"] % df["lot_size"] == 0).all()


def test_sow_orders_are_lot_multiples(client, df_instr):
    cfg = load_config()
    df_chains, df_unds = _chains_and_unds(df_instr, client)
    df_unds["state"] = "virgin"
    df_nkd = derive_mod.derive_sow(df_unds, df_chains, cfg, nav=2_000_000.0)
    _assert_lot_multiples(df_nkd)


def test_index_sow_is_income_only_strangle(client, df_instr):
    cfg = load_config()
    df_chains, df_unds = _chains_and_unds(df_instr, client)
    df_unds["state"] = "virgin"
    df_nkd = derive_mod.derive_sow(df_unds, df_chains, cfg, nav=2_000_000.0)
    nifty = df_nkd[df_nkd["symbol"] == "NIFTY"]
    # index gets both put + call legs (income strangle), all cash-settled
    assert set(nifty["right"]) == {"C", "P"}
    assert (nifty["settlement"] == "cash").all()


def test_cover_orders_are_lot_multiples(client, df_instr):
    cfg = load_config()
    df_chains, df_unds = _chains_and_unds(df_instr, client)
    lot = int(df_unds.loc[df_unds.symbol == "RELIANCE", "lot_size"].iloc[0])
    df_pf = pd.DataFrame([{
        "symbol": "RELIANCE", "secType": "STK", "right": "", "strike": float("nan"),
        "expiry": pd.NaT, "position": lot * 2, "avgCost": 2850.0, "mktPrice": 2900.0,
        "lot_size": 0, "settlement": "physical", "state": "exposed",
    }])
    df_cov = derive_mod.derive_cover(df_pf, df_chains, df_unds, cfg)
    _assert_lot_multiples(df_cov)
    assert (df_cov["right"] == "C").all()  # calls against long stock
