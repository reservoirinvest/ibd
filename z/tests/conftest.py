"""Shared fixtures: offline KiteClient + normalized mock instruments."""

import pandas as pd
import pytest

from src.nsewheel import instruments as instr
from src.nsewheel.broker import KiteClient
from src.nsewheel.broker import mock_data


@pytest.fixture
def client() -> KiteClient:
    return KiteClient(offline=True)


@pytest.fixture
def df_instr() -> pd.DataFrame:
    """Normalized (options-only) instruments frame from mock data."""
    raw = mock_data.build_instruments_df()
    raw = raw[raw["instrument_type"].isin(instr._RIGHT_MAP)].copy()
    raw["name"] = raw["name"].str.upper()
    raw["right"] = raw["instrument_type"].map(instr._RIGHT_MAP)
    raw["expiry"] = pd.to_datetime(raw["expiry"])
    for col in ("strike", "tick_size", "lot_size"):
        raw[col] = pd.to_numeric(raw[col])
    raw["lot_size"] = raw["lot_size"].astype(int)
    raw["settlement"] = raw["name"].map(instr.settlement_of)
    return raw.reset_index(drop=True)
