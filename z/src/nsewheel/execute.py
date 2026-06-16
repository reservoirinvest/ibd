# @@@ CAUTION: this module places live orders when OFFLINE is false @@@
"""Submit derived wheel orders to Zerodha Kite Connect.

Ports ibd's ``execute.py`` to ``kite.place_order``. Every leg is a DAY LIMIT order in the
NFO segment with the configured product (NRML for carry-forward). In OFFLINE mode the
KiteClient dry-runs (logs the order, places nothing). A cushion check gates fresh sow legs,
mirroring the IBKR version.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from loguru import logger

from .broker import KiteClient, get_client
from .config import load_config
from .util import load_pickle


def place_df(df: pd.DataFrame, client: KiteClient, *, label: str) -> list[str]:
    """Place every row of an order frame; returns the broker order ids."""
    if df is None or df.empty:
        logger.info("No {} orders to place.", label)
        return []
    cfg = load_config()
    ids = []
    for _, r in df.iterrows():
        qty = int(r["qty"])
        if qty <= 0:
            continue
        oid = client.place_order(
            variety="regular",
            exchange=cfg.get("EXCHANGE", "NFO"),
            tradingsymbol=r["tradingsymbol"],
            transaction_type=r["action"],
            quantity=qty,
            product=cfg.get("PRODUCT", "NRML"),
            order_type="LIMIT",
            price=float(r["xPrice"]),
            validity="DAY",
        )
        ids.append(oid)
    logger.info("Placed {} {} order(s).", len(ids), label)
    return ids


def run(client: KiteClient | None = None) -> dict[str, list[str]]:
    cfg = load_config()
    client = client or get_client()
    placed: dict[str, list[str]] = {}

    if cfg.get("COVER_ME", True):
        placed["cover"] = place_df(load_pickle("df_cov.pkl"), client, label="cover")
    if cfg.get("REAP_ME", True):
        placed["reap"] = place_df(load_pickle("df_reap.pkl"), client, label="reap")

    if cfg.get("SOW_NAKEDS", True):
        cushion = client.cushion()
        min_cushion = float(cfg.get("MINCUSHION", 0.18))
        if np.isnan(cushion) or cushion < min_cushion:
            logger.warning("Cushion {:.2f} < MINCUSHION {:.2f}: skipping sow orders.",
                           cushion, min_cushion)
            placed["sow"] = []
        else:
            placed["sow"] = place_df(load_pickle("df_nkd.pkl"), client, label="sow")

    return placed


if __name__ == "__main__":
    run()
