"""Thin Zerodha Kite Connect wrapper with an OFFLINE mock mode.

In OFFLINE mode (``config OFFLINE: true`` or no credentials) every method returns
mock-data fixtures, so the full pipeline runs in this sandbox without network/credentials.
In live mode it delegates to ``kiteconnect.KiteConnect`` (imported lazily so the offline
path has no hard dependency on the SDK being configured).

Replaces ibd's ``ib_client.py`` — but synchronous and far simpler: the MVP polls quotes
rather than streaming, so there is no asyncio loop / circuit-breaker machinery.
"""

from __future__ import annotations

from functools import lru_cache

import pandas as pd
from loguru import logger

from ..config import load_config
from ..settings import get_settings
from . import mock_data


class KiteClient:
    def __init__(self, offline: bool | None = None) -> None:
        cfg = load_config()
        self.offline = cfg.get("OFFLINE", True) if offline is None else offline
        self.exchange = cfg.get("EXCHANGE", "NFO")
        self.product = cfg.get("PRODUCT", "NRML")
        self._span_pct = float(cfg.get("SPAN_MARGIN_PCT", 0.15))
        self._kite = None
        if not self.offline:
            self._connect()

    # ── connection ────────────────────────────────────────────────────────────
    def _connect(self) -> None:
        from kiteconnect import KiteConnect  # lazy import; only needed live

        s = get_settings()
        self._kite = KiteConnect(api_key=s.kite_api_key())
        token = s.kite_access_token()
        if not token:
            raise RuntimeError(
                "OFFLINE is false but KITE_ACCESS_TOKEN is empty. "
                "Generate a daily access token via the login flow (see README)."
            )
        self._kite.set_access_token(token)
        logger.info("Connected to Kite Connect (live).")

    # ── reference data ──────────────────────────────────────────────────────────
    def instruments(self, exchange: str = "NFO") -> pd.DataFrame:
        if self.offline:
            return mock_data.build_instruments_df()
        return pd.DataFrame(self._kite.instruments(exchange))

    def quote(self, instrument_tokens: list[int] | list[str]) -> dict:
        if self.offline:
            allq = mock_data.build_quotes(mock_data.build_instruments_df())
            return {str(t): allq.get(str(t), {"last_price": 0.0}) for t in instrument_tokens}
        return self._kite.quote(instrument_tokens)

    def spots(self, names: list[str]) -> dict[str, float]:
        """Underlying spot prices keyed by F&O name.

        OFFLINE: mock spot map. LIVE: ``kite.ltp`` against the NSE cash symbol (indices use
        their index symbol, e.g. ``NSE:NIFTY 50`` — adjust the mapping when wiring live).
        """
        if self.offline:
            sm = mock_data.spot_map()
            return {n: sm.get(n.upper(), float("nan")) for n in names}
        keys = [f"NSE:{n}" for n in names]
        ltp = self._kite.ltp(keys)
        return {n: float(ltp.get(f"NSE:{n}", {}).get("last_price", float("nan"))) for n in names}

    # ── account ──────────────────────────────────────────────────────────────────
    def positions(self) -> dict:
        if self.offline:
            return {"net": mock_data.mock_positions(), "day": []}
        return self._kite.positions()

    def holdings(self) -> list[dict]:
        if self.offline:
            return []
        return self._kite.holdings()

    def orders(self) -> list[dict]:
        if self.offline:
            return mock_data.mock_orders()
        return self._kite.orders()

    def margins(self) -> dict:
        if self.offline:
            return mock_data.mock_margins()
        return self._kite.margins()

    def nav(self) -> float:
        """Net liquidation value (equity segment net)."""
        m = self.margins()
        return float(m.get("equity", {}).get("net", 0.0))

    def cushion(self) -> float:
        """available live balance / NAV — a rough analogue of IBKR's cushion."""
        m = self.margins().get("equity", {})
        net = float(m.get("net", 0.0)) or 1.0
        avail = float(m.get("available", {}).get("live_balance", 0.0))
        return avail / net

    # ── margins ──────────────────────────────────────────────────────────────────
    def order_margins(self, orders: list[dict]) -> list[dict]:
        """Per-order SPAN+exposure margin.

        Live: ``kite.order_margins(orders)``. OFFLINE: approximate as
        ``notional * SPAN_MARGIN_PCT`` where notional = price * quantity.
        """
        if not self.offline:
            return self._kite.order_margins(orders)
        out = []
        for o in orders:
            notional = abs(float(o.get("price", 0.0)) * float(o.get("quantity", 0)))
            out.append({"total": round(notional * self._span_pct, 2)})
        return out

    # ── execution ────────────────────────────────────────────────────────────────
    def place_order(self, **kwargs) -> str:
        """Place a single order. OFFLINE dry-runs (logs and returns a fake id)."""
        if self.offline:
            logger.info("[DRY-RUN] place_order {}", kwargs)
            return f"MOCK-{kwargs.get('tradingsymbol', '?')}"
        return self._kite.place_order(**kwargs)


@lru_cache(maxsize=1)
def get_client() -> KiteClient:
    return KiteClient()
