"""One-shot IBKR connectivity probe — runs outside Streamlit, exits cleanly.

Usage:
    uv run python scripts/probe_cid10.py

Tests CID=10 on port 1300 (live settings from snp_config.yml).
Prints managed accounts, position count, and open order count then exits.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.dashboard.settings import get_settings


async def main() -> None:
    from ib_async import IB

    s = get_settings()
    print(f"Connecting to {s.ib_host}:{s.ib_port} with clientId={s.ib_client_id} ...")

    ib = IB()
    try:
        await ib.connectAsync(s.ib_host, s.ib_port, clientId=s.ib_client_id, timeout=10)
    except Exception as e:
        print(f"CONNECT FAILED: {e}")
        return

    accounts = ib.managedAccounts()
    print(f"managedAccounts: {accounts}")

    positions = await ib.reqPositionsAsync()
    print(f"positions count: {len(positions)}")
    for p in positions[:5]:
        print(f"  {p.account}  {p.contract.symbol:6s}  {p.contract.secType}  qty={p.position}")

    await asyncio.sleep(1)

    orders = await ib.reqAllOpenOrdersAsync()
    print(f"open orders count: {len(orders)}")

    ib.disconnect()
    print("Disconnected. Done.")


if __name__ == "__main__":
    asyncio.run(main())
