"""Instrument master: lot sizes, settlement classification, contract resolution."""

import pandas as pd

from src.nsewheel import instruments as instr


def test_settlement_index_vs_stock():
    assert instr.settlement_of("NIFTY") == "cash"
    assert instr.settlement_of("BANKNIFTY") == "cash"
    assert instr.settlement_of("RELIANCE") == "physical"
    assert instr.is_wheelable("INFY")
    assert not instr.is_wheelable("NIFTY")


def test_lot_size_map_positive(df_instr):
    lots = instr.lot_size_map(df_instr)
    assert lots["RELIANCE"] == 250
    assert lots["NIFTY"] == 75
    assert all(v > 0 for v in lots.values())


def test_universe_columns(df_instr):
    uni = instr.universe(df_instr)
    assert {"symbol", "settlement", "lot_size", "is_weekly"} <= set(uni.columns)
    assert (uni["lot_size"] > 0).all()


def test_resolve_roundtrip(df_instr):
    row = df_instr.iloc[0]
    res = instr.resolve(df_instr, row["name"], row["expiry"], row["strike"], row["right"])
    assert res is not None
    assert res["instrument_token"] == int(row["instrument_token"])
    assert res["lot_size"] == int(row["lot_size"])
    assert res["tradingsymbol"] == row["tradingsymbol"]


def test_resolve_missing(df_instr):
    assert instr.resolve(df_instr, "RELIANCE", pd.Timestamp("2000-01-01"), 1.0, "C") is None
