"""Pickle I/O and small shared utilities."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from .paths import MASTER_DIR, ensure_dirs


def pickle_path(name: str) -> Path:
    return MASTER_DIR / name


def save_pickle(obj: Any, name: str) -> Path:
    """Atomic pickle write into data/master/."""
    ensure_dirs()
    path = pickle_path(name)
    tmp = path.with_suffix(path.suffix + ".tmp")
    pd.to_pickle(obj, tmp)
    tmp.replace(path)
    return path


def load_pickle(name: str, default: Any = None) -> Any:
    path = pickle_path(name)
    if not path.exists():
        return default
    try:
        return pd.read_pickle(path)
    except Exception:
        return default


def dte(expiry, today: pd.Timestamp | None = None) -> pd.Series | float:
    """Days-to-expiry for a Series or scalar of dates."""
    today = today or pd.Timestamp.today().normalize()
    exp = pd.to_datetime(expiry)
    if isinstance(exp, pd.Series):
        return (exp - today).dt.days
    return float((exp - today).days)
