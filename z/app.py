"""NSE Wheel — Streamlit dashboard (Zerodha Kite Connect).

A single-page monitor adapted from the ibd dashboard: account header, action pipeline
(build / derive / execute), live positions, the F&O universe with lot sizes & settlement,
and suggested order tables (Cover / Sow / Reap). Runs in OFFLINE mode against mock data so it
boots with no Kite credentials.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from src.nsewheel import build as build_mod
from src.nsewheel import derive as derive_mod
from src.nsewheel import execute as execute_mod
from src.nsewheel import instruments as instr
from src.nsewheel.broker import get_client
from src.nsewheel.classify import classified_results
from src.nsewheel.config import load_config
from src.nsewheel.formatting import rupees
from src.nsewheel.util import load_pickle

st.set_page_config(page_title="NSE Wheel", page_icon="🛞", layout="wide")

cfg = load_config()
client = get_client()

# ── header ─────────────────────────────────────────────────────────────────────
mode = "🟢 LIVE" if not client.offline else "🟡 OFFLINE (mock data)"
st.title("🛞 NSE Wheel — Zerodha")
c1, c2, c3, c4 = st.columns(4)
c1.metric("Mode", mode)
c2.metric("NAV", rupees(client.nav()))
c3.metric("Cushion", f"{client.cushion() * 100:.1f}%")
c4.metric("Min cushion", f"{float(cfg.get('MINCUSHION', 0.18)) * 100:.0f}%")

st.caption(
    "Wheel runs on **single-stock F&O** (physically settled → assignment delivers "
    "lot-size shares). Index options (NIFTY/BANKNIFTY…) are **cash-settled → income-only**. "
    "All order quantities are whole lots."
)

# ── action pipeline ────────────────────────────────────────────────────────────
with st.expander("⚙️ Pipeline", expanded=True):
    a, b, c = st.columns(3)
    if a.button("1 — Build chains", use_container_width=True):
        with st.spinner("Building option chains + underlyings…"):
            build_mod.run(client)
        st.success("Built df_chains.pkl + df_unds.pkl")
    if b.button("2 — Derive orders", use_container_width=True):
        with st.spinner("Deriving cover / sow / reap…"):
            try:
                derive_mod.run(client)
                st.success("Derived suggested orders")
            except RuntimeError as exc:
                st.warning(str(exc))
    if c.button("3 — Execute (dry-run)" if client.offline else "3 — Execute", use_container_width=True):
        with st.spinner("Placing orders…"):
            placed = execute_mod.run(client)
        counts = {k: len(v) for k, v in placed.items()}
        st.success(f"Placed: {counts}")


# ── positions ──────────────────────────────────────────────────────────────────
df_instr = instr.load_instruments() if instr.INSTRUMENTS_PATH.exists() else None
res = classified_results(client, df_instr)
df_pf = res["df_pf"]

st.subheader("📒 Positions")
if df_pf is not None and not df_pf.empty:
    show = df_pf.copy()
    show["lots"] = (show["position"].abs() / show["lot_size"].replace(0, pd.NA)).round(1)
    st.dataframe(
        show[["symbol", "secType", "right", "strike", "position", "lots", "avgCost",
              "mktPrice", "settlement", "state"]],
        use_container_width=True, hide_index=True,
    )
else:
    st.info("No open positions.")


# ── universe ───────────────────────────────────────────────────────────────────
st.subheader("🌐 F&O universe (lot sizes & settlement)")
if df_instr is not None:
    uni = instr.universe(df_instr)
    st.dataframe(uni, use_container_width=True, hide_index=True)
else:
    st.info("Run **Build chains** first to populate the instruments dump.")


# ── suggested orders ───────────────────────────────────────────────────────────
st.subheader("🎯 Suggested orders")
tabs = st.tabs(["Cover", "Sow", "Reap"])
for tab, name in zip(tabs, ["df_cov.pkl", "df_nkd.pkl", "df_reap.pkl"]):
    with tab:
        df = load_pickle(name)
        if df is not None and not df.empty:
            st.dataframe(df, use_container_width=True, hide_index=True)
        else:
            st.caption("None — derive orders to populate.")


with st.expander("🔧 Config"):
    st.json(cfg)
