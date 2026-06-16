"""Configuration loading: YAML config + environment overrides.

Mirrors the ibd `load_config` pattern but for NSE. Strategy parameters live in
``config/nse_config.yml``; any key may be overridden by an environment variable of the
same name (useful for OFFLINE toggles and CI).
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

import yaml

from .paths import CONFIG_DIR

_CONFIG_PATH = CONFIG_DIR / "nse_config.yml"

# Keys that should be coerced from string env-vars back to their native type.
_BOOL_KEYS = {"OFFLINE", "INDEX_WHEEL", "COVER_ME", "SOW_NAKEDS", "REAP_ME"}
_INT_KEYS = {"STOCK_EXPIRY_WEEKDAY", "MAX_DTE", "COVER_MIN_DTE", "COV_AGED_DTE",
             "VIRGIN_DTE", "MINREAPDTE"}
_FLOAT_KEYS = {"MINCUSHION", "FUND_PER_SYMBOL_PCT", "SPAN_MARGIN_PCT", "COVER_STD_MULT",
               "COVXPMULT", "VIRGIN_PUT_STD_MULT", "VIRGIN_CALL_STD_MULT", "NAKEDXPMULT",
               "MINNAKEDOPTPRICE", "REAPRATIO"}


def _coerce(key: str, value: Any) -> Any:
    if not isinstance(value, str):
        return value
    if key in _BOOL_KEYS:
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if key in _INT_KEYS:
        return int(value)
    if key in _FLOAT_KEYS:
        return float(value)
    return value


@lru_cache(maxsize=1)
def load_config() -> dict[str, Any]:
    """Load nse_config.yml and overlay matching environment variables."""
    with open(_CONFIG_PATH, "r") as fh:
        config: dict[str, Any] = yaml.safe_load(fh) or {}

    for key in list(config.keys()):
        if key in os.environ:
            config[key] = _coerce(key, os.environ[key])

    return config


def get(key: str, default: Any = None) -> Any:
    """Convenience accessor for a single config value."""
    return load_config().get(key, default)
