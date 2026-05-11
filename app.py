"""IBKR live risk dashboard - Streamlit entrypoint.

Run:
    uv run streamlit run app.py --server.address=127.0.0.1
"""

from __future__ import annotations

import os
import pickle
import re
import subprocess
import sys
from pathlib import Path

import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
import yaml
from plotly.subplots import make_subplots
from pyprojroot import here as _here

# pyrefly: ignore [missing-import]
from src.dashboard.formatting import money, pct, signed_money
# pyrefly: ignore [missing-import]
from src.dashboard.ib_client import get_client
# pyrefly: ignore [missing-import]
import pandas as pd
from src.dashboard.risk import (
    _dte_series,
    _join_tickers,
    _select_account_values,
    account_kpis,
    cover_protect_gaps,
    greek_dollar_sums,
    position_delta_dollars,
    position_margin_est,
    reap_candidates,
)
# pyrefly: ignore [missing-import]
from src.dashboard.settings import get_settings
# pyrefly: ignore [missing-import]
from src.dashboard.state import classify_portfolio


st.set_page_config(
    page_title="IB Monitor",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# Force dark Plotly hover tooltips via SVG CSS — the Python hoverlabel API is
# unreliable in Streamlit; targeting the SVG layer directly is the only sure fix.
st.markdown(
    """
    <style>
    /* Plotly hover tooltip: dark background */
    .js-plotly-plot .plotly .hoverlayer .hovertext path {
        fill: #1e2130 !important;
        stroke: #475569 !important;
    }
    /* Plotly hover tooltip: light text (OHLC values, etc.) */
    .js-plotly-plot .plotly .hoverlayer .hovertext text,
    .js-plotly-plot .plotly .hoverlayer .hovertext text tspan {
        fill: #f1f5f9 !important;
    }
    /* Trace-name badge: Plotly renders a white <rect> + <text class="name">.
       The global white-text rule above makes the name unreadable on white.
       Override it back to near-black so it reads clearly on the white badge. */
    .js-plotly-plot .plotly .hoverlayer .hovertext .name {
        fill: #1e2130 !important;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

settings = get_settings()
client = get_client()
client.start(settings)

# ---------------------------------------------------------------------------
# Account selector — build label → account-number mapping from settings
# ---------------------------------------------------------------------------
_US = settings.us_account.get_secret_value()
_SG = settings.sg_account.get_secret_value()

_DATA_DIR    = _here() / "data"
_MASTER_DIR  = _DATA_DIR / "master"
_CFG_PATH    = _here() / "config" / "snp_config.yml"
_DERIVE_LOG  = _here() / "log" / "derive_progress.log"
_OHLC_LOG    = _here() / "log" / "ohlc_progress.log"

# Known derive.py output markers → progress percentage
_DERIVE_PHASES: list[tuple[str, int]] = [
    ("Getting financials",                          5),
    ("GETTING CLASSIFIED PORTFOLIO DATA",          15),
    ("MAKE COVERS FOR EXPOSED",                    28),
    ("MAKE SOWING CONTRACTS",                      45),
    ("MAKE REAPS",                                 60),
    ("EXTRACT ORPHANED CONTRACTS",                 70),
    ("IDENTIFYING UNPROTECTED POSITIONS",          75),
    ("BUILDING LONG PROTECTION RECOMMENDATIONS",   80),
    ("BUILDING SHORT PROTECTION RECOMMENDATIONS",  87),
    ("CALCULATING FINAL PROTECTION PRICES",        93),
    ("ROLLS FOR PROTECTING PUTS",                  97),
    ("RECOMMENDATIONS COMPLETE",                  100),
]

# Build label → account-number map for whichever accounts are actually configured.
# "ALL" is only included when more than one real account is present.
_REAL_ACCOUNTS: dict[str, str] = {}   # label → account number (non-empty only)
if _US:
    _REAL_ACCOUNTS["US"] = _US
if _SG:
    _REAL_ACCOUNTS["SG"] = _SG

_ACCOUNT_OPTIONS: dict[str, str] = {}
if len(_REAL_ACCOUNTS) > 1:
    _ACCOUNT_OPTIONS["ALL"] = ""        # ALL only makes sense with 2+ accounts
_ACCOUNT_OPTIONS.update(_REAL_ACCOUNTS)

# Default: first real account, or ALL if none configured
if "acct_sel" not in st.session_state:
    st.session_state["acct_sel"] = next(iter(_REAL_ACCOUNTS), "ALL")


def _selected_account() -> str:
    """Return the raw account number for the current UI selection ('' = ALL)."""
    label = st.session_state.get("acct_sel", "ALL")
    return _ACCOUNT_OPTIONS.get(label, "")


def _filter_positions(positions, account: str):
    """Filter a positions DataFrame to the selected account (no-op for ALL)."""
    if not account or positions.empty or "account" not in positions.columns:
        return positions
    return positions[positions["account"] == account].reset_index(drop=True)


_EVEN_BG = "background-color: rgba(255, 255, 255, 0.08)"   # subtle white overlay for dark mode
_ODD_BG  = "background-color: rgba(255, 255, 255, 0.03)"   # lighter band for dark mode
_ITM_BG  = "background-color: rgba(251, 191, 36, 0.25)"    # amber tint, works in dark/light


def _banded(df: pd.DataFrame, itm_mask=None):
    """Return a Styler with alternating row shading; amber tint for ITM rows."""
    _mask = itm_mask  # close over the array

    def _row(row):
        i = int(row.name)
        if _mask is not None and i < len(_mask) and _mask[i]:
            return [_ITM_BG] * len(row)
        return [_EVEN_BG if i % 2 == 0 else _ODD_BG] * len(row)

    return df.style.apply(_row, axis=1)


# ---------------------------------------------------------------------------
# derive.py helpers
# ---------------------------------------------------------------------------

def _load_pkl(name: str) -> pd.DataFrame:
    """Load a pickle DataFrame from data/; return empty DataFrame on error/missing."""
    p = _DATA_DIR / name
    if not p.exists():
        return pd.DataFrame()
    try:
        with open(p, "rb") as fh:
            obj = pickle.load(fh)
        return obj if isinstance(obj, pd.DataFrame) else pd.DataFrame()
    except Exception:
        return pd.DataFrame()


def _pkl_age(name: str) -> str:
    """Human-readable age of a pickle file, e.g. '3 m ago'."""
    from datetime import datetime
    p = _DATA_DIR / name
    if not p.exists():
        return "never"
    secs = (datetime.now() - datetime.fromtimestamp(p.stat().st_mtime)).total_seconds()
    if secs < 120:
        return f"{int(secs)}s ago"
    if secs < 7200:
        return f"{int(secs/60)}m ago"
    return f"{secs/3600:.1f}h ago"


# ---------------------------------------------------------------------------
# snp_config.yml helpers
# ---------------------------------------------------------------------------

def _init_cfg_state() -> None:
    """Seed session_state from snp_config.yml exactly once per browser session."""
    if st.session_state.get("_cfg_inited"):
        return
    try:
        cfg = yaml.safe_load(_CFG_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        cfg = {}
    defaults: dict[str, object] = {
        "cfg_cover_me":          cfg.get("COVER_ME", True),
        "cfg_cover_min_dte":     cfg.get("COVER_MIN_DTE", 4),
        "cfg_cover_std_mult":    cfg.get("COVER_STD_MULT", 0.65),
        "cfg_covxpmult":         cfg.get("COVXPMULT", 1.2),
        "cfg_sow_nakeds":        cfg.get("SOW_NAKEDS", True),
        "cfg_virgin_dte":        cfg.get("VIRGIN_DTE", 5),
        "cfg_virgin_call_std":   cfg.get("VIRGIN_CALL_STD_MULT", 3.8),
        "cfg_virgin_put_std":    cfg.get("VIRGIN_PUT_STD_MULT", 1.2),
        "cfg_nakedxpmult":       cfg.get("NAKEDXPMULT", 4.95),
        "cfg_minnaked":          cfg.get("MINNAKEDOPTPRICE", 2.5),
        "cfg_virgin_qty_mult":   cfg.get("VIRGIN_QTY_MULT", 0.055),
        "cfg_protect_me":        cfg.get("PROTECT_ME", False),
        "cfg_protect_dte":       cfg.get("PROTECT_DTE", 35),
        "cfg_protection_strip":  cfg.get("PROTECTION_STRIP", 5),
        "cfg_reap_me":           cfg.get("REAP_ME", True),
        "cfg_reapratio":         cfg.get("REAPRATIO", 0.025),
        "cfg_minreapdte":        cfg.get("MINREAPDTE", 1),
        "cfg_max_dte":           cfg.get("MAX_DTE", 50),
        "cfg_mincushion":        cfg.get("MINCUSHION", 0.2),
    }
    for k, v in defaults.items():
        st.session_state.setdefault(k, v)
    st.session_state["_cfg_inited"] = True


def _save_cfg() -> None:
    """Write session_state config values back to snp_config.yml (comments not preserved)."""
    try:
        cfg = yaml.safe_load(_CFG_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        cfg = {}
    cfg.update({
        "COVER_ME":           bool(st.session_state["cfg_cover_me"]),
        "COVER_MIN_DTE":      int(st.session_state["cfg_cover_min_dte"]),
        "COVER_STD_MULT":     float(st.session_state["cfg_cover_std_mult"]),
        "COVXPMULT":          float(st.session_state["cfg_covxpmult"]),
        "SOW_NAKEDS":         bool(st.session_state["cfg_sow_nakeds"]),
        "VIRGIN_DTE":         int(st.session_state["cfg_virgin_dte"]),
        "VIRGIN_CALL_STD_MULT": float(st.session_state["cfg_virgin_call_std"]),
        "VIRGIN_PUT_STD_MULT":  float(st.session_state["cfg_virgin_put_std"]),
        "NAKEDXPMULT":        float(st.session_state["cfg_nakedxpmult"]),
        "MINNAKEDOPTPRICE":   float(st.session_state["cfg_minnaked"]),
        "VIRGIN_QTY_MULT":    float(st.session_state["cfg_virgin_qty_mult"]),
        "PROTECT_ME":         bool(st.session_state["cfg_protect_me"]),
        "PROTECT_DTE":        int(st.session_state["cfg_protect_dte"]),
        "PROTECTION_STRIP":   int(st.session_state["cfg_protection_strip"]),
        "REAP_ME":            bool(st.session_state["cfg_reap_me"]),
        "REAPRATIO":          float(st.session_state["cfg_reapratio"]),
        "MINREAPDTE":         int(st.session_state["cfg_minreapdte"]),
        "MAX_DTE":            int(st.session_state["cfg_max_dte"]),
        "MINCUSHION":         float(st.session_state["cfg_mincushion"]),
    })
    _CFG_PATH.write_text(
        yaml.dump(cfg, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )


def _cfg_dirty() -> bool:
    """Return True if any config session_state value differs from the current YAML file."""
    if not st.session_state.get("_cfg_inited"):
        return False
    try:
        cfg = yaml.safe_load(_CFG_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        return True
    pairs: list[tuple[object, object]] = [
        (bool(st.session_state.get("cfg_cover_me")),             bool(cfg.get("COVER_ME", True))),
        (int(st.session_state.get("cfg_cover_min_dte", 4)),      int(cfg.get("COVER_MIN_DTE", 4))),
        (float(st.session_state.get("cfg_cover_std_mult", 0.65)), float(cfg.get("COVER_STD_MULT", 0.65))),
        (float(st.session_state.get("cfg_covxpmult", 1.2)),      float(cfg.get("COVXPMULT", 1.2))),
        (bool(st.session_state.get("cfg_sow_nakeds")),           bool(cfg.get("SOW_NAKEDS", True))),
        (int(st.session_state.get("cfg_virgin_dte", 5)),         int(cfg.get("VIRGIN_DTE", 5))),
        (float(st.session_state.get("cfg_virgin_call_std", 3.8)), float(cfg.get("VIRGIN_CALL_STD_MULT", 3.8))),
        (float(st.session_state.get("cfg_virgin_put_std", 1.2)), float(cfg.get("VIRGIN_PUT_STD_MULT", 1.2))),
        (float(st.session_state.get("cfg_nakedxpmult", 4.95)),   float(cfg.get("NAKEDXPMULT", 4.95))),
        (float(st.session_state.get("cfg_minnaked", 2.5)),       float(cfg.get("MINNAKEDOPTPRICE", 2.5))),
        (float(st.session_state.get("cfg_virgin_qty_mult", 0.055)), float(cfg.get("VIRGIN_QTY_MULT", 0.055))),
        (bool(st.session_state.get("cfg_protect_me")),           bool(cfg.get("PROTECT_ME", False))),
        (int(st.session_state.get("cfg_protect_dte", 35)),       int(cfg.get("PROTECT_DTE", 35))),
        (int(st.session_state.get("cfg_protection_strip", 5)),   int(cfg.get("PROTECTION_STRIP", 5))),
        (bool(st.session_state.get("cfg_reap_me")),              bool(cfg.get("REAP_ME", True))),
        (float(st.session_state.get("cfg_reapratio", 0.025)),    float(cfg.get("REAPRATIO", 0.025))),
        (int(st.session_state.get("cfg_minreapdte", 1)),         int(cfg.get("MINREAPDTE", 1))),
        (int(st.session_state.get("cfg_max_dte", 50)),           int(cfg.get("MAX_DTE", 50))),
        (float(st.session_state.get("cfg_mincushion", 0.2)),     float(cfg.get("MINCUSHION", 0.2))),
    ]
    return any(a != b for a, b in pairs)


def _derive_progress() -> tuple[float, str, list[str]]:
    """Parse derive_progress.log → (progress 0–1, phase label, last 4 output lines)."""
    if not _DERIVE_LOG.exists():
        return 0.0, "Initialising…", []
    try:
        lines = _DERIVE_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return 0.0, "", []
    pct = 0
    label = "Initialising…"
    for marker, p in _DERIVE_PHASES:
        if any(marker in ln for ln in lines):
            pct = p
            label = marker.replace("===", "").strip().title()
    tail = [ln for ln in lines[-6:] if ln.strip() and "===" not in ln][-4:]
    return pct / 100.0, label, tail


_TQDM_BAR_RE = re.compile(r"^(.+?):\s+\d+%\|")


def _log_lines(log_path: Path, n: int = 35) -> list[str]:
    """Return the last *n* logical lines of a tqdm log file.

    tqdm emits one \\n-line per update when stdout is not a TTY, building a
    pyramid.  We collapse repeated progress-bar lines so each bar occupies
    exactly one slot showing its latest state.
    """
    if not log_path.exists():
        return []
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []
    # Within each \n segment take the last \r segment (handles TTY carriage-returns)
    raw = [seg.split("\r")[-1] for seg in text.split("\n") if seg]
    bar_slot: dict[str, int] = {}
    out: list[str] = []
    for line in raw:
        m = _TQDM_BAR_RE.match(line)
        if m:
            key = m.group(1).strip()
            if key in bar_slot:
                out[bar_slot[key]] = line
            else:
                bar_slot[key] = len(out)
                out.append(line)
        else:
            out.append(line)
    return out[-n:]


def _derive_log_lines(n: int = 35) -> list[str]:
    return _log_lines(_DERIVE_LOG, n)


def _ohlc_log_lines(n: int = 30) -> list[str]:
    return _log_lines(_OHLC_LOG, n)


# ---------------------------------------------------------------------------
# Technical-indicator helpers (used by Analysis tab)
# ---------------------------------------------------------------------------

def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder RSI — values 0–100; NaN for the first `period` bars."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0.0, float("nan"))
    return 100.0 - (100.0 / (1.0 + rs))


def _bollinger(
    close: pd.Series, window: int = 20, num_std: float = 2.0
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Return (upper, mid, lower) Bollinger Band series."""
    mid = close.rolling(window).mean()
    std = close.rolling(window).std()
    return mid + num_std * std, mid, mid - num_std * std


# ---------------------------------------------------------------------------
# Fragments
# ---------------------------------------------------------------------------


_DROP_HELP = (
    "**Market drop withstand** = Excess Liquidity ÷ |Portfolio Dollar Delta| × 100%.\n\n"
    "Estimates the % decline the broad market (or your underlying basket) can sustain "
    "before your excess liquidity reaches zero, assuming linear (delta-only) exposure. "
    "A well-hedged or net-short portfolio may show N/A — in that case a *rising* market "
    "is your primary risk, not a drop."
)


def _drop_withstand(excess: float, delta_abs: float) -> str:
    """Format a market-drop withstand % string."""
    if delta_abs < 1:
        return "N/A"
    v = excess / delta_abs * 100
    return f"{v:.1f}%" if v < 200 else ">200%"


@st.fragment(run_every=2.0)
def header() -> None:
    snap = client.snapshot()
    acct = _selected_account()
    positions_filt = _filter_positions(snap.positions, acct)
    cols = st.columns([3, 2, 2, 2, 2])
    with cols[0]:
        if client.is_frozen():
            status = "🧊 FROZEN"
        elif snap.connected:
            status = "🟢 LIVE"
        else:
            status = "🔴 DISCONNECTED"
        st.markdown(
            f"### IB Monitor &nbsp;&nbsp; {status} &nbsp;&nbsp;"
            f"<span style='font-size:1rem;color:#22c55e;font-weight:700;'>"
            f"{settings.currency}</span>",
            unsafe_allow_html=True,
        )
    cols[1].caption(
        f"as_of: {snap.as_of.strftime('%H:%M:%S UTC') if snap.as_of else '-'}"
    )
    cols[2].caption(
        f"port: {settings.ib_port}  •  cid: {settings.ib_client_id}"
    )
    cols[3].caption(f"positions: {len(positions_filt)}")
    with cols[4]:
        if len(_REAL_ACCOUNTS) > 1:
            # Multiple accounts — show ALL / US / SG selector
            st.selectbox(
                "Account",
                list(_ACCOUNT_OPTIONS.keys()),
                key="acct_sel",
                label_visibility="collapsed",
            )
        elif _REAL_ACCOUNTS:
            # Single account — label only, no interaction needed
            st.caption(next(iter(_REAL_ACCOUNTS)))

    # ── Market drop withstand row ─────────────────────────────────────────────
    if snap.account_values and not snap.positions.empty:
        k_sel = account_kpis(snap, min_cushion=settings.min_cushion, account=acct)
        g_sel = greek_dollar_sums(positions_filt, snap.tickers)
        delta_sel = g_sel["delta_$"]

        if len(_REAL_ACCOUNTS) > 1 and _US and not acct:
            # ALL view: show US-only AND combined (US+SG) columns side by side
            pos_us    = _filter_positions(snap.positions, _US)
            k_us      = account_kpis(snap, account=_US)
            g_us      = greek_dollar_sums(pos_us, snap.tickers)
            delta_us  = g_us["delta_$"]

            dr_us  = _drop_withstand(k_us["excess_liquidity"],  abs(delta_us))
            dr_all = _drop_withstand(k_sel["excess_liquidity"], abs(delta_sel))

            dr1, dr2, _spacer = st.columns([2, 2, 7])
            dr1.metric(
                "US drop withstand",
                "N/A (short)" if delta_us  <= 0 else dr_us,
                help=_DROP_HELP,
            )
            dr2.metric(
                "US+SG drop withstand",
                "N/A (short)" if delta_sel <= 0 else dr_all,
                help=_DROP_HELP,
            )
        else:
            # Single account — one metric
            dr_str = _drop_withstand(k_sel["excess_liquidity"], abs(delta_sel))
            dr1, _spacer = st.columns([2, 9])
            dr1.metric(
                "Drop withstand",
                "N/A (short)" if delta_sel <= 0 else dr_str,
                help=_DROP_HELP,
            )


@st.fragment(run_every=2.0)
def kpi_strip() -> None:
    snap = client.snapshot()
    acct = _selected_account()
    positions = _filter_positions(snap.positions, acct)
    k = account_kpis(snap, min_cushion=settings.min_cushion, account=acct)
    g = greek_dollar_sums(positions, snap.tickers)
    av = _select_account_values(snap, acct)
    from decimal import Decimal as _D
    cash = float(av.get("CashBalance", _D("0")) or 0)

    c1, c2, c3, c4, c5, c6, c7, c8, c9 = st.columns(9)
    c1.metric(
        "NLV", money(k["nlv"]),
        help="Net Liquidating Value: total portfolio value if all positions were closed "
             "at current market prices.",
    )
    c2.metric(
        f"Unreal P&L ({settings.currency})", signed_money(k["unrealized_pnl"]),
        help="Unrealized P&L: mark-to-market gain/loss on all open positions.",
    )
    c3.metric(
        "Cash", money(cash),
        help="Cash Balance: uninvested cash in the account.",
    )
    c4.metric(
        "Cushion",
        pct(k["cushion"]),
        delta=f"min {pct(settings.min_cushion)}",
        delta_color="inverse" if k["cushion_breach"] else "off",
        help="Excess Liquidity / NLV. Margin buffer remaining as a fraction of portfolio. "
             f"Red/alert when below the {pct(settings.min_cushion)} minimum.",
    )
    c5.metric(
        "Excess Liq", money(k["excess_liquidity"]),
        help="Funds available after satisfying all margin requirements. "
             "Dropping below zero triggers a margin call.",
    )
    c6.metric(
        "Maint Margin", money(k["maint_margin"]),
        help="Maintenance Margin Requirement: collateral required to hold current positions. "
             "Falling below this triggers a margin call. "
             "Sourced directly from IBKR account values.",
    )
    c7.metric(
        "Σ Δ ($)", signed_money(g["delta_$"]),
        help="Dollar delta: total directional exposure across the book. "
             "Each 1-point move in the market changes P&L by roughly this amount. "
             "Positive = long bias, negative = short bias.",
    )
    c8.metric(
        "Σ Θ ($/d)", signed_money(g["theta_$"]),
        help="Dollar theta: daily time-decay P&L across all options. "
             "Positive = book earns theta (net short options). "
             "Negative = book pays theta (net long options).",
    )
    c9.metric(
        "Σ ν ($)", signed_money(g["vega_$"]),
        help="Dollar vega: total sensitivity to a 1% rise in implied volatility. "
             "Positive = long vol (profits when IV spikes). "
             "Negative = short vol (profits when IV falls).",
    )


@st.fragment(run_every=5.0)
def render_positions() -> None:
    snap = client.snapshot()
    acct = _selected_account()
    if snap.positions.empty:
        st.info("Waiting for portfolio data…")
        return
    df = classify_portfolio(_filter_positions(snap.positions, acct))
    df = _join_tickers(df, snap.tickers)

    # ---- dollar delta per row (needed before ITM, which filters need) ------
    df["delta_$"] = position_delta_dollars(df)

    # ---- ITM flag (computed before filters so the ITM filter can use it) ---
    und_px = (
        df["underlying_px"] if "underlying_px" in df.columns
        else pd.Series(float("nan"), index=df.index)
    )
    strike_col = df["strike"] if "strike" in df.columns else pd.Series(float("nan"), index=df.index)
    right_col  = df["right"]  if "right"  in df.columns else pd.Series("",           index=df.index)
    _itm_call  = (df["secType"] == "OPT") & (right_col == "C") & (und_px > strike_col)
    _itm_put   = (df["secType"] == "OPT") & (right_col == "P") & (und_px < strike_col)
    df["_itm"] = _itm_call | _itm_put

    # ---- filter bar --------------------------------------------------------
    with st.expander("🔍 Filters", expanded=False):
        fc1, fc2, fc3, fc4, fc5 = st.columns(5)
        all_sectypes = sorted(df["secType"].dropna().unique().tolist())
        all_rights = sorted(df["right"].dropna().unique().tolist()) if "right" in df.columns else []
        all_states = (
            sorted(df["pf_state"].dropna().unique().tolist()) if "pf_state" in df.columns else []
        )
        sel_sec = fc1.multiselect(
            "secType", all_sectypes, placeholder="all", key="pos_f_sec",
            help="STK = stock position, OPT = option contract",
        )
        sel_right = fc2.multiselect(
            "right (C/P)", all_rights, placeholder="all", key="pos_f_right",
            help="C = call option, P = put option",
        )
        sel_state = fc3.multiselect(
            "pf_state", all_states, placeholder="all", key="pos_f_state",
            help="Portfolio state assigned to each row",
        )
        sym_filter = fc4.text_input(
            "symbol (prefix)", value="", key="pos_f_sym",
            help="Prefix match — 'A' shows AAPL, AMZN; not symbols containing A elsewhere",
        )
        itm_only = fc5.checkbox(
            "ITM options only", key="pos_f_itm",
            help="Show only options that are currently in-the-money (amber rows)",
        )

    # apply filters — all symbol text inputs use strict prefix matching
    if sel_sec:
        df = df[df["secType"].isin(sel_sec)]
    if sel_right and "right" in df.columns:
        df = df[df["right"].isin(sel_right)]
    if sel_state and "pf_state" in df.columns:
        df = df[df["pf_state"].isin(sel_state)]
    if sym_filter:
        df = df[df["symbol"].str.upper().str.startswith(sym_filter.strip().upper())]
    if itm_only:
        df = df[df["_itm"]]

    # Sort so ITM mask index aligns with view index
    df = df.sort_values(["pf_state", "symbol"]).reset_index(drop=True)

    # ---- margin column: prefer live what-if data, fall back to Reg T est --
    if "margin_init" in df.columns:
        margin_col = "margin_init"
        margin_label = "Margin"
        margin_help = (
            "Initial margin for this position per IBKR what-if calculation. "
            "Based on a closing what-if order; reflects Portfolio Margin netting "
            "at the time of the last refresh."
        )
    else:
        df["margin_est"] = position_margin_est(df)
        margin_col = "margin_est"
        margin_label = "Margin*"
        margin_help = (
            "Estimated Reg T initial margin (what-if data not yet available). "
            "STK long: 50%, STK short: 150%, OPT short: max(20%-OTM, 10%) x notional, "
            "OPT long: 0. Approximate only."
        )

    # ---- display -----------------------------------------------------------
    cols_show = [
        "symbol", "secType", "underlying_px", "right", "strike", "expiry",
        "position", "avgCost", "marketPrice", "marketValue",
        "unrealizedPNL", "delta_$", margin_col, "delta", "theta", "vega", "iv", "pf_state",
    ]
    # Build view — df is already sorted & reset; extract ITM mask then drop helper col
    view = df[[c for c in cols_show if c in df.columns] + ["_itm"]].copy()
    itm_mask = view.pop("_itm").to_numpy(dtype=bool)
    col_cfg: dict = {
        "symbol": st.column_config.TextColumn(
            "Symbol",
            help="Ticker symbol of the underlying or security.",
        ),
        "secType": st.column_config.TextColumn(
            "Type",
            help="Security type: STK = stock/ETF position, OPT = option contract.",
        ),
        "underlying_px": st.column_config.NumberColumn(
            "Underlying", format="$%.2f",
            help="Current price of the underlying stock (for options). "
                 "Used to determine ITM/OTM status (row shading).",
        ),
        "right": st.column_config.TextColumn(
            "C/P",
            help="Option right: C = call (right to buy), P = put (right to sell). "
                 "Blank for stocks.",
        ),
        "strike": st.column_config.NumberColumn(
            "Strike", format="%.1f",
            help="Option strike price: the price at which the option can be exercised.",
        ),
        "expiry": st.column_config.TextColumn(
            "Expiry",
            help="Option expiration date (YYYYMMDD). After this date the option expires "
                 "worthless if out-of-the-money.",
        ),
        "position": st.column_config.NumberColumn(
            "Qty",
            help="Number of shares (STK) or contracts (OPT). "
                 "Negative = short position (sold/written).",
        ),
        "avgCost": st.column_config.NumberColumn(
            "Avg Cost", format="%.2f",
            help="Average cost basis per share or per contract (in contract-price terms, "
                 "not multiplied by 100).",
        ),
        "marketPrice": st.column_config.NumberColumn(
            "Mkt Px", format="%.2f",
            help="Current market price (last trade or mid quote).",
        ),
        "marketValue": st.column_config.NumberColumn(
            "Mkt Val", format="$%,.0f",
            help="Market value = position x market price x multiplier (100 for options).",
        ),
        "unrealizedPNL": st.column_config.NumberColumn(
            "Unreal P&L", format="$%,.0f",
            help="Unrealized P&L: mark-to-market gain/loss vs average cost basis.",
        ),
        "delta_$": st.column_config.NumberColumn(
            "Delta $", format="$%,.0f",
            help="Dollar delta: P&L change for a 1-point move in the underlying. "
                 "STK: equals market value (delta=1). "
                 "OPT: position x delta x 100 x underlying price. "
                 "Sum of this column = header Sigma-Delta.",
        ),
        margin_col: st.column_config.NumberColumn(
            margin_label, format="$%,.0f",
            help=margin_help,
        ),
    }
    snap_for_margins = client.snapshot()
    margins_ts = snap_for_margins.margins_as_of
    margins_label = (
        f"Margins as of {margins_ts.strftime('%H:%M:%S')}" if margins_ts else "Margins: pending"
    )
    hdr_left, hdr_right = st.columns([6, 2])
    with hdr_right:
        if st.button("↻ Refresh Margins", help="Re-run what-if margin query for all positions"):
            client.schedule_margin_refresh()
            st.toast("Margin refresh scheduled — results in ~10 s")
        st.caption(margins_label)

    st.dataframe(
        _banded(view, itm_mask),
        width="stretch",
        hide_index=True,
        column_config={
            **col_cfg,
            "delta": st.column_config.NumberColumn(
                "Δ Delta", format="%.3f",
                help="Rate of change of option price per 1-point move in the underlying. "
                     "Range: 0 to +1 for calls, -1 to 0 for puts. "
                     "A delta of 0.30 means the option moves 0.30 per 1-point move in the stock.",
            ),
            "theta": st.column_config.NumberColumn(
                "Θ Theta", format="%.3f",
                help="Daily time decay of the option's value in dollars per contract per day. "
                     "Negative for long options (lose value daily); "
                     "positive for short options (collect decay).",
            ),
            "vega": st.column_config.NumberColumn(
                "ν Vega", format="%.3f",
                help="Sensitivity of the option price to a 1% change in implied volatility. "
                     "Positive for long options; negative for short options.",
            ),
            "iv": st.column_config.NumberColumn(
                "IV", format="%.3f",
                help="Implied Volatility: market-implied annualised volatility of the underlying. "
                     "Higher IV means more expensive options.",
            ),
            "pf_state": st.column_config.TextColumn(
                "State",
                help=(
                    "Portfolio state: zen (full hedge), covering (short option on stock), "
                    "protecting (long option on stock), unprotected (stock + cover, no protect), "
                    "uncovered (stock + protect, no cover), exposed (stock alone), "
                    "sowed (short option alone), orphaned (long option alone), "
                    "straddled (long call + put, no stock)"
                ),
            ),
        },
    )


@st.fragment(run_every=5.0)
def render_risk() -> None:
    snap = client.snapshot()
    acct = _selected_account()
    if snap.positions.empty:
        st.info("Waiting for portfolio data…")
        return
    df = classify_portfolio(_filter_positions(snap.positions, acct))
    df = _join_tickers(df, snap.tickers)
    df["delta_$"] = position_delta_dollars(df)
    k = account_kpis(snap, min_cushion=settings.min_cushion, account=acct)

    # ── Build "already addressed" sets from open orders + suggested orders ───
    _ord_syms: set[str] = set()
    if not snap.orders.empty and "symbol" in snap.orders.columns:
        _ord_syms.update(snap.orders["symbol"].dropna().unique())
    _cov_syms: set[str] = set()
    _df_cov_risk = _load_pkl("df_cov.pkl")
    if not _df_cov_risk.empty and "symbol" in _df_cov_risk.columns:
        _cov_syms.update(_df_cov_risk["symbol"].dropna().unique())
    _nkd_syms: set[str] = set()
    _df_nkd_risk = _load_pkl("df_nkd.pkl")
    if not _df_nkd_risk.empty and "symbol" in _df_nkd_risk.columns:
        _nkd_syms.update(_df_nkd_risk["symbol"].dropna().unique())
    _reap_syms: set[str] = set()
    _df_reap_risk = _load_pkl("df_reap.pkl")
    if not _df_reap_risk.empty and "symbol" in _df_reap_risk.columns:
        _reap_syms.update(_df_reap_risk["symbol"].dropna().unique())
    _addressed_cover = _ord_syms | _cov_syms
    _addressed_reap = _ord_syms | _reap_syms

    # ── Compute top risk-reduction actions ─────────────────────────────────
    actions: list[tuple[str, str]] = []

    # 1. Exposed stocks — exclude symbols already covered by open orders or suggested cover
    exposed_stk = df[(df["pf_state"] == "exposed") & (df["secType"] == "STK")]
    exposed_stk = exposed_stk[~exposed_stk["symbol"].isin(_addressed_cover)]
    if not exposed_stk.empty:
        top_exp = exposed_stk.sort_values("marketValue", ascending=False, key=abs)
        syms = top_exp["symbol"].tolist()
        sym_str = ", ".join(syms[:4]) + ("…" if len(syms) > 4 else "")
        notional = abs(float(exposed_stk["marketValue"].sum()))
        n = len(exposed_stk)
        actions.append((
            "error",
            f"{n} exposed position{'s' if n > 1 else ''} with no hedge and no pending order: "
            f"{sym_str} — ${notional:,.0f} at full market risk. "
            "Write covered calls or buy protective puts.",
        ))

    # 2. Margin cushion breach
    if k.get("cushion_breach"):
        cushion_pct = float(k.get("cushion", 0)) * 100
        min_pct = settings.min_cushion * 100
        actions.append((
            "error",
            f"Margin cushion breach: {cushion_pct:.1f}% (minimum {min_pct:.0f}%). "
            "Reduce delta exposure or add funds to avoid a margin call.",
        ))

    # 3. Short options ITM and near expiry (assignment risk)
    opt_df = df[df["secType"] == "OPT"].copy()
    if not opt_df.empty and "expiry" in opt_df.columns:
        opt_df["_dte"] = _dte_series(opt_df["expiry"])
        short_opts = opt_df[opt_df["position"] < 0].copy()
        if not short_opts.empty and "underlying_px" in short_opts.columns and "strike" in short_opts.columns:
            itm_calls = (short_opts["right"] == "C") & (short_opts["underlying_px"] > short_opts["strike"])
            itm_puts  = (short_opts["right"] == "P") & (short_opts["underlying_px"] < short_opts["strike"])
            near_assign = short_opts[(itm_calls | itm_puts) & (short_opts["_dte"] <= 14)].copy()
            near_assign = near_assign[~near_assign["symbol"].isin(_ord_syms)]
            if not near_assign.empty:
                # compute how far ITM: calls → (und - strike) / und, puts → (strike - und) / und
                und = near_assign["underlying_px"]
                sk  = near_assign["strike"]
                itm_pct = pd.Series(
                    ((und - sk) / und).where(near_assign["right"] == "C", (sk - und) / und),
                    index=near_assign.index,
                ).fillna(0.0)
                near_assign["_itm_pct"] = itm_pct
                near_assign = near_assign.sort_values("_itm_pct", ascending=False)
                rows_str = ", ".join(
                    f"{r['symbol']} {r['right']}{r['strike']:.0f} ({r['_dte']:.0f}d {r['_itm_pct']*100:.1f}% ITM)"
                    for _, r in near_assign.head(4).iterrows()
                )
                n = len(near_assign)
                actions.append((
                    "error",
                    f"{n} short option{'s' if n > 1 else ''} ITM with ≤14 DTE — assignment risk: "
                    f"{rows_str}. Roll or close immediately.",
                ))

    # 4. Delta concentration — single symbol > 15% of total abs(delta_$)
    sym_delta = df.groupby("symbol")["delta_$"].sum().dropna()
    total_abs_delta = float(sym_delta.abs().sum())
    if total_abs_delta > 0 and not sym_delta.empty:
        max_sym = str(sym_delta.abs().idxmax())
        conc = float(sym_delta.abs().max()) / total_abs_delta
        if conc > 0.15:
            delta_val = float(sym_delta[max_sym])
            actions.append((
                "warning",
                f"Delta concentration: {max_sym} is {conc * 100:.0f}% of total "
                f"directional exposure (${delta_val:+,.0f}). "
                "Consider writing a covered call or trimming the position.",
            ))

    # 5. Reap candidates — exclude symbols already in df_reap or open orders
    cands = reap_candidates(
        df, snap.tickers,
        reap_ratio=settings.reap_ratio,
        min_reap_dte=settings.min_reap_dte,
    )
    if not cands.empty:
        cands = cands[~cands["symbol"].isin(_addressed_reap)]
    if not cands.empty:
        total_pnl = float(cands["pnl_if_reaped"].sum())
        syms = cands["symbol"].unique().tolist()
        sym_str = ", ".join(syms[:4]) + ("…" if len(syms) > 4 else "")
        n = len(cands)
        actions.append((
            "info",
            f"{n} reap candidate{'s' if n > 1 else ''} (no pending order): {sym_str}. "
            f"Closing these short options locks in ${total_pnl:,.0f} profit.",
        ))

    # 6. Unprotected stocks (if PROTECT_ME enabled)
    if settings.protect_me:
        unprot = df[(df["pf_state"] == "unprotected") & (df["secType"] == "STK")]
        if not unprot.empty:
            syms = unprot["symbol"].tolist()
            sym_str = ", ".join(syms[:4]) + ("…" if len(syms) > 4 else "")
            notional = abs(float(unprot["marketValue"].sum()))
            n = len(unprot)
            actions.append((
                "warning",
                f"{n} unprotected position{'s' if n > 1 else ''} (covered but no put): "
                f"{sym_str} — ${notional:,.0f} exposed to downside. Buy protective puts.",
            ))

    # Sort errors first, then warnings, then info; cap at 5
    _rank = {"error": 0, "warning": 1, "info": 2}
    actions = sorted(actions, key=lambda x: _rank.get(x[0], 3))[:5]

    st.markdown("#### Top Risk-Reduction Actions")
    if not actions:
        st.success("No critical risks detected — portfolio appears well-hedged.")
    else:
        for i, (severity, text) in enumerate(actions, 1):
            getattr(st, severity)(f"**{i}.** {text}")

    # ── Delta concentration chart + Cover/Protect gaps ─────────────────────
    c1, c2 = st.columns([3, 2])
    with c1:
        _n_syms = int(sym_delta.notna().sum()) if not sym_delta.empty else 0
        _top_n = min(15, _n_syms)
        st.markdown(f"##### Delta concentration by symbol (top {_top_n} of {_n_syms})")
        if total_abs_delta > 0 and not sym_delta.empty:
            top15_idx = sym_delta.abs().nlargest(_top_n).index
            plot_df = (
                sym_delta.loc[top15_idx]
                .sort_values(ascending=True)
                .reset_index()
            )
            plot_df.columns = ["symbol", "delta_$"]
            plot_df["color"] = plot_df["delta_$"].apply(
                lambda v: "#22c55e" if v >= 0 else "#ef4444"
            )
            fig = px.bar(
                plot_df, x="delta_$", y="symbol", orientation="h",
                color="color", color_discrete_map="identity",
            )
            fig.update_layout(
                showlegend=False, height=460,
                margin=dict(l=10, r=10, t=10, b=10),
                xaxis_title="Dollar Delta ($)",
                yaxis_title=None,
            )
            st.plotly_chart(fig, width="stretch")
        else:
            st.info("No delta data yet.")

    with c2:
        st.markdown("##### Cover / Protect gaps")
        if not settings.protect_me:
            st.caption("PROTECT_ME=False — protection not requested, cover gaps only.")
        gaps = cover_protect_gaps(
            df, snap.tickers,
            protect_me=settings.protect_me,
            cover_std_mult=settings.cover_std_mult,
            max_dte=settings.max_dte,
        )
        if gaps.empty:
            success_msg = (
                "No cover gaps."
                if not settings.protect_me
                else "No gaps — all stocks covered and protected."
            )
            st.success(success_msg)
        else:
            gap_col_cfg: dict = {
                "mkt_px": st.column_config.NumberColumn("Mkt Px", format="$%.2f"),
                "avg_cost": st.column_config.NumberColumn("Avg Cost", format="$%.2f"),
                "cover_strike": st.column_config.NumberColumn(
                    "Cover Strike",
                    format="$%.1f",
                    help=(
                        f"Target call strike: max(avgCost, mkt_px + "
                        f"{settings.cover_std_mult}×IV×√(DTE/252)), "
                        f"DTE={settings.max_dte}. "
                        "IV sourced from existing option tickers, default 30%."
                    ),
                ),
                "gain_if_called": st.column_config.NumberColumn(
                    "Gain if Called",
                    format="$%,.0f",
                    help=(
                        "Capital gain vs avg cost if the stock is called away "
                        "at the cover strike."
                    ),
                ),
            }
            if settings.protect_me:
                gap_col_cfg["max_downside"] = st.column_config.NumberColumn(
                    "Max Downside",
                    format="$%,.0f",
                    help=(
                        "Total cost basis at risk (avg cost x shares) "
                        "if the position falls to zero."
                    ),
                )
            st.dataframe(
                _banded(gaps), hide_index=True, width="stretch", column_config=gap_col_cfg,
            )


@st.fragment(run_every=3.0)
def render_orders() -> None:
    snap = client.snapshot()
    acct = _selected_account()

    # ── Subprocess state ─────────────────────────────────────────────────────
    proc: subprocess.Popen | None       = st.session_state.get("derive_proc")
    ohlc_proc: subprocess.Popen | None  = st.session_state.get("ohlc_proc")
    frozen     = client.is_frozen()
    proc_done  = proc is not None and proc.poll() is not None
    ohlc_done  = ohlc_proc is not None and ohlc_proc.poll() is not None
    frozen_for = st.session_state.get("frozen_for", "")   # "derive" | "ohlc" | ""

    # Auto-unfreeze: derive finished
    if frozen and proc_done and frozen_for == "derive":
        client.unfreeze()
        st.session_state["derive_proc"] = None
        st.session_state.pop("_derive_exit", None)
        st.session_state.pop("frozen_for", None)
        st.rerun()

    # Auto-unfreeze: OHLC finished
    if frozen and ohlc_done and frozen_for == "ohlc":
        client.unfreeze()
        st.session_state["ohlc_proc"] = None
        st.session_state.pop("_ohlc_exit", None)
        st.session_state.pop("frozen_for", None)
        st.rerun()


    # Capture exit codes the moment each process ends
    if proc is not None and proc.poll() is not None and "_derive_exit" not in st.session_state:
        st.session_state["_derive_exit"] = proc.poll()
    if ohlc_proc is not None and ohlc_proc.poll() is not None and "_ohlc_exit" not in st.session_state:
        st.session_state["_ohlc_exit"] = ohlc_proc.poll()

    # ── Imports needed by Generate OHLCs ──────────────────────────────────────
    from src.dashboard.ohlc import (   # noqa: PLC0415  (local import inside fragment)
        OHLC_PATH,
        get_sp500_symbols,
        write_symbol_list,
    )

    # ── Action buttons — all three on one row ─────────────────────────────────
    gen_col, ohlc_col, clr_col, _btn_spacer = st.columns([2, 2, 2, 5])

    # ── Generate Orders ────────────────────────────────────────────────────────
    with gen_col:
        if st.button(
            "⚙️ Generate Orders",
            disabled=frozen,
            use_container_width=True,
            help="Freezes the dashboard (releases CID), runs derive.py, then reconnects. "
                 "Takes 2–5 min. Last-known data stays visible during the freeze.",
        ):
            _DERIVE_LOG.parent.mkdir(parents=True, exist_ok=True)
            log_fh = open(_DERIVE_LOG, "w", encoding="utf-8")  # noqa: SIM115
            _env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
            new_proc = subprocess.Popen(
                [sys.executable, str(_here() / "derive.py")],
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                env=_env,
            )
            st.session_state["derive_proc"] = new_proc
            st.session_state.pop("_derive_exit", None)
            st.session_state["frozen_for"] = "derive"
            client.freeze()          # release CID=10 for derive.py
            st.rerun()
        # Last-derive timestamp sits right under the button
        if "_derive_exit" not in st.session_state and not frozen:
            ages = [_pkl_age(n) for n in ["df_cov.pkl", "df_nkd.pkl", "df_reap.pkl"]]
            age_str = ages[0] if len(set(ages)) == 1 else " | ".join(ages)
            st.caption(f"Last: {age_str}")

    # ── Generate OHLCs ────────────────────────────────────────────────────────
    with ohlc_col:
        if st.button(
            "📊 Generate OHLCs",
            disabled=frozen,
            use_container_width=True,
            help=(
                "Fetch / update 1.5 yr daily OHLC for S&P500 weekly underlyings + "
                "portfolio positions. Freezes dashboard while running; reconnects after."
            ),
        ):
            # Build combined symbol list: S&P500 weekly underlyings + portfolio extras.
            # Use primaryExch / currency from the position row so non-US ETFs
            # (e.g. CSPX on LSE) get the correct yfinance ticker suffix.
            sp500_specs = get_sp500_symbols()
            seen: set[str] = {s["symbol"] for s in sp500_specs}
            port_specs: list[dict[str, str]] = []
            if not snap.positions.empty:
                for _, _pos in snap.positions.iterrows():
                    _sym = str(_pos.get("symbol", ""))
                    if not _sym or _sym in seen:
                        continue
                    port_specs.append({
                        "symbol":   _sym,
                        "exchange": str(_pos.get("primaryExch", "")) or "SMART",
                        "currency": str(_pos.get("currency", "")) or "USD",
                    })
                    seen.add(_sym)
            write_symbol_list(sp500_specs + port_specs)

            # Launch subprocess, then freeze so IBKR fallback inside it can connect.
            _OHLC_LOG.parent.mkdir(parents=True, exist_ok=True)
            _ohlc_log_fh = open(_OHLC_LOG, "w", encoding="utf-8")   # noqa: SIM115
            _env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
            _ohlc_new_proc = subprocess.Popen(
                [sys.executable, str(_here() / "fetch_ohlc.py")],
                stdout=_ohlc_log_fh,
                stderr=subprocess.STDOUT,
                env=_env,
            )
            st.session_state["ohlc_proc"] = _ohlc_new_proc
            st.session_state.pop("_ohlc_exit", None)
            st.session_state["frozen_for"] = "ohlc"
            client.freeze()   # release CID=10 — subprocess may need IBKR
            st.rerun()
        # Last-OHLC timestamp sits right under the button
        if "_ohlc_exit" not in st.session_state and not frozen:
            _ohlc_age = "never"
            if OHLC_PATH.exists():
                from datetime import datetime as _dt
                _secs = (_dt.now() - _dt.fromtimestamp(OHLC_PATH.stat().st_mtime)).total_seconds()
                _ohlc_age = (
                    f"{int(_secs)}s ago" if _secs < 120
                    else f"{int(_secs/60)}m ago" if _secs < 7200
                    else f"{_secs/3600:.1f}h ago"
                )
            st.caption(f"Last: {_ohlc_age}")

    # ── Clear Data ─────────────────────────────────────────────────────────────
    with clr_col:
        if st.button(
            "🗑️ Clear Data",
            use_container_width=True,
            help="Delete all top-level files in data/ (pickles, JSONs). "
                 "data/master/ (OHLC store) is never deleted.",
        ):
            _cleared, _locked = [], []
            for _p in sorted(_DATA_DIR.iterdir()):
                if not _p.is_file():
                    continue
                try:
                    _p.unlink()
                    _cleared.append(_p.name)
                except PermissionError:
                    _locked.append(_p.name)
            if _cleared:
                st.toast(f"Cleared {len(_cleared)} file(s): {', '.join(_cleared)}")
            if _locked:
                st.toast(
                    f"⚠️ {', '.join(_locked)} still in use — retry in a moment",
                    icon="⚠️",
                )
            # No st.rerun() here — fragment auto-refreshes every 3 s via run_every.
            # Calling st.rerun() from inside a fragment triggers a full-page rerun
            # which can race with the IBKR connection and produce error 326.
        st.caption("Keeps OHLC store.")

    # ── Status row (derive + OHLC) ────────────────────────────────────────────
    if frozen or "_derive_exit" in st.session_state or "_ohlc_exit" in st.session_state:
        gen_status_col, ohlc_status_col, _st_spacer = st.columns([3, 3, 5])
        with gen_status_col:
            if frozen and frozen_for == "derive":
                _pct, phase, _ = _derive_progress()
                st.progress(max(_pct, 0.01), text=f"⏳ {phase}")
            elif "_derive_exit" in st.session_state:
                rc = st.session_state["_derive_exit"]
                if rc == 0:
                    st.success("✅ Orders generated")
                else:
                    st.error(f"❌ Generate Orders failed (exit {rc})")
        with ohlc_status_col:
            if frozen and frozen_for == "ohlc":
                st.progress(0.5, text="⏳ Fetching OHLCs…")
            elif "_ohlc_exit" in st.session_state:
                rc = st.session_state["_ohlc_exit"]
                if rc == 0:
                    st.success("✅ OHLCs up to date")
                else:
                    st.error(f"❌ OHLC fetch failed (exit {rc})")

    # ── Scrollable log (live during freeze; collapsible after) ────────────────
    if frozen and frozen_for == "ohlc":
        _ohlc_live = _ohlc_log_lines(30)
        if _ohlc_live:
            st.code("\n".join(_ohlc_live), language=None)
    elif frozen:
        log_lines = _derive_log_lines(35)
        if log_lines:
            st.code("\n".join(log_lines), language=None)
    elif "_derive_exit" in st.session_state:
        rc = st.session_state["_derive_exit"]
        with st.expander("📋 derive.py log", expanded=rc != 0):
            if _DERIVE_LOG.exists():
                try:
                    st.code(
                        _DERIVE_LOG.read_text(encoding="utf-8", errors="replace"),
                        language=None,
                    )
                except Exception as _e:
                    st.warning(f"Could not read log: {_e}")

    # Post-run OHLC log — collapsible after freeze ends
    if "_ohlc_exit" in st.session_state:
        rc = st.session_state["_ohlc_exit"]
        with st.expander("📋 OHLC log", expanded=rc != 0):
            if _OHLC_LOG.exists():
                try:
                    st.code(
                        _OHLC_LOG.read_text(encoding="utf-8", errors="replace"),
                        language=None,
                    )
                except Exception as _e:
                    st.warning(f"Could not read OHLC log: {_e}")

    st.divider()

    # ── Load all suggested-order DataFrames upfront (needed before filter bar) ─
    def _exp_premium(df: pd.DataFrame) -> float:
        if df.empty or "xPrice" not in df.columns or "qty" not in df.columns:
            return 0.0
        return float((df["xPrice"] * df["qty"] * 100).sum())

    def _exp_cover_reward(df: pd.DataFrame) -> float:
        if df.empty or "xPrice" not in df.columns or "qty" not in df.columns:
            return 0.0
        base = df["xPrice"] * df["qty"] * 100
        if "strike" in df.columns and "avgCost" in df.columns:
            base = base + (df["strike"] - df["avgCost"]) * df["qty"] * 100
        return float(base.sum())

    _raw_cov  = _load_pkl("df_cov.pkl")
    _raw_nkd  = _load_pkl("df_nkd.pkl")
    _raw_reap = _load_pkl("df_reap.pkl")
    try:
        _yml_protect = (yaml.safe_load(_CFG_PATH.read_text(encoding="utf-8")) or {}).get(
            "PROTECT_ME", False
        )
    except Exception:
        _yml_protect = False
    _raw_prot = _load_pkl("df_protect.pkl") if _yml_protect else pd.DataFrame()

    # ── Filter bar (shared: applies to Open Orders + all Suggested tables) ──
    _f1, _f2, _f3 = st.columns([3, 1, 1])
    _sym_filt   = _f1.text_input(
        "🔍 Filter by symbol", key="ord_f_sym", placeholder="e.g. AAPL, SPY",
        label_visibility="collapsed",
    )
    _right_filt = _f2.multiselect(
        "C/P", ["C", "P"], key="ord_f_right", placeholder="C / P",
        label_visibility="collapsed",
    )
    if _f3.button("✕ Clear", key="ord_clear_filter", use_container_width=True,
                  help="Clear symbol and C/P filters"):
        st.session_state.pop("ord_f_sym", None)
        st.session_state.pop("ord_f_right", None)
        st.rerun()

    def _ord_filt(df: pd.DataFrame) -> pd.DataFrame:
        """Apply symbol prefix + C/P filter; returns a reset-index copy."""
        if df.empty:
            return df
        if _sym_filt and "symbol" in df.columns:
            # Strict prefix match — 'A' shows AAPL/AMZN, not symbols with A elsewhere
            df = df[df["symbol"].str.upper().str.startswith(_sym_filt.strip().upper())]
        if _right_filt and "right" in df.columns:
            df = df[df["right"].isin(_right_filt)]
        return df.reset_index(drop=True)

    df_cov      = _ord_filt(_raw_cov)
    df_nkd      = _ord_filt(_raw_nkd)
    df_reap_pkl = _ord_filt(_raw_reap)
    df_prot     = _ord_filt(_raw_prot)

    # ── Open Orders ─────────────────────────────────────────────────────────
    st.markdown("##### Open Orders")
    orders = snap.orders
    if acct and not orders.empty and "account" in orders.columns:
        orders = orders[orders["account"] == acct].reset_index(drop=True)
    orders = _ord_filt(orders)

    if orders.empty:
        st.info("No open orders." if not (_sym_filt or _right_filt) else "No open orders match filter.")
    else:
        cols_show = [
            "symbol", "secType", "right", "strike", "expiry",
            "action", "qty", "filled", "remaining",
            "orderType", "lmtPrice", "status",
        ]
        if len(_ACCOUNT_OPTIONS) > 2 or not acct:
            cols_show = ["account"] + cols_show
        view = orders[[c for c in cols_show if c in orders.columns]].copy()
        st.dataframe(
            _banded(view),
            hide_index=True,
            width="stretch",
            column_config={
                "lmtPrice": st.column_config.NumberColumn("Limit Px", format="%.2f"),
                "qty":       st.column_config.NumberColumn(format="%.0f"),
                "filled":    st.column_config.NumberColumn(format="%.0f"),
                "remaining": st.column_config.NumberColumn(format="%.0f"),
                "strike":    st.column_config.NumberColumn(format="%.1f"),
                "status":    st.column_config.TextColumn(),
            },
        )

    # ── Suggested Orders ─────────────────────────────────────────────────────
    st.markdown("##### Suggested Orders")
    st.caption("Generated by derive.py — click **Generate Orders** to refresh.")

    # Cover
    n_cov = len(df_cov)
    cov_reward = _exp_cover_reward(df_cov)
    cov_label = (
        f"📈 Cover — {n_cov} orders · ${cov_reward:,.0f} expected if called"
        if n_cov else "📈 Cover — 0 orders"
    )
    with st.expander(cov_label, expanded=True):
        if _raw_cov.empty:
            st.info("No cover suggestions — run Generate Orders or no exposed positions.")
        elif df_cov.empty:
            st.info("No cover orders match the current filter.")
        else:
            c_cols = ["symbol", "right", "strike", "expiry", "dte", "qty",
                      "undPrice", "sdev", "avgCost", "price", "xPrice", "margin"]
            st.dataframe(
                _banded(df_cov[[c for c in c_cols if c in df_cov.columns]].copy()),
                hide_index=True, width="stretch",
                column_config={
                    "strike":   st.column_config.NumberColumn("Strike",      format="%.1f"),
                    "undPrice": st.column_config.NumberColumn("Und Px",      format="$%,.2f"),
                    "sdev":     st.column_config.NumberColumn("1σ Move",     format="$%,.2f",
                        help="1-sigma expected underlying move = undPrice × IV × √(DTE/365)"),
                    "avgCost":  st.column_config.NumberColumn("Avg Cost",    format="$%,.2f"),
                    "price":    st.column_config.NumberColumn("Mkt Px",      format="$%,.2f"),
                    "xPrice":   st.column_config.NumberColumn("Expected Px", format="$%,.2f",
                        help="Target execution price = max(avgCost+putCost, mkt + COVER_STD_MULT×σ) × COVXPMULT"),
                    "margin":   st.column_config.NumberColumn("Margin",      format="$%,.0f",
                        help="Estimated margin per contract from atm_margin()"),
                },
            )

    # Sow (Nakeds)
    n_nkd = len(df_nkd)
    nkd_premium = _exp_premium(df_nkd)
    nkd_label = (
        f"🌱 Sow — {n_nkd} orders · ${nkd_premium:,.0f} expected premium"
        if n_nkd else "🌱 Sow — 0 orders"
    )
    with st.expander(nkd_label, expanded=True):
        if _raw_nkd.empty:
            st.info("No sow suggestions — run Generate Orders or no virgin/orphaned positions.")
        elif df_nkd.empty:
            st.info("No sow orders match the current filter.")
        else:
            n_cols = ["symbol", "right", "strike", "expiry", "dte", "qty",
                      "undPrice", "vy", "sdev", "price", "xPrice", "margin"]
            st.dataframe(
                _banded(df_nkd[[c for c in n_cols if c in df_nkd.columns]].copy()),
                hide_index=True, width="stretch",
                column_config={
                    "strike":   st.column_config.NumberColumn("Strike",      format="%.1f"),
                    "undPrice": st.column_config.NumberColumn("Und Px",      format="$%,.2f"),
                    "vy":       st.column_config.NumberColumn("IV",          format="%.3f",
                        help="Implied volatility of the underlying"),
                    "sdev":     st.column_config.NumberColumn("1σ Move",     format="$%,.2f",
                        help="1-sigma expected underlying move = undPrice × IV × √(DTE/365)"),
                    "price":    st.column_config.NumberColumn("Mkt Px",      format="$%,.2f"),
                    "xPrice":   st.column_config.NumberColumn("Expected Px", format="$%,.2f",
                        help="max(mkt × NAKEDXPMULT, MINNAKEDOPTPRICE / qty)"),
                    "margin":   st.column_config.NumberColumn("Margin",      format="$%,.0f"),
                },
            )

    # Reap
    n_reap = len(df_reap_pkl)
    reap_cost = _exp_premium(df_reap_pkl)
    reap_label = (
        f"🌾 Reap — {n_reap} orders · ${reap_cost:,.0f} to close"
        if n_reap else "🌾 Reap — 0 orders"
    )
    with st.expander(reap_label, expanded=True):
        if _raw_reap.empty:
            st.info("No reap suggestions — run Generate Orders or nothing cheap enough.")
        elif df_reap_pkl.empty:
            st.info("No reap orders match the current filter.")
        else:
            r_cols = ["symbol", "right", "strike", "expiry", "dte", "qty",
                      "undPrice", "avgCost", "optPrice", "xPrice"]
            st.dataframe(
                _banded(df_reap_pkl[[c for c in r_cols if c in df_reap_pkl.columns]].copy()),
                hide_index=True, width="stretch",
                column_config={
                    "strike":   st.column_config.NumberColumn("Strike",      format="%.1f"),
                    "undPrice": st.column_config.NumberColumn("Und Px",      format="$%,.2f"),
                    "avgCost":  st.column_config.NumberColumn("Avg Cost",    format="$%,.2f"),
                    "optPrice": st.column_config.NumberColumn("Opt Px",      format="$%.3f"),
                    "xPrice":   st.column_config.NumberColumn("Expected Px", format="$%.3f",
                        help="Target close price ≤ REAPRATIO × avgCost"),
                },
            )

    # Protect
    n_prot = len(df_prot)
    prot_label = f"🛡️ Protect — {n_prot} orders" if _yml_protect else "🛡️ Protect"
    with st.expander(prot_label, expanded=True):
        if not _yml_protect:
            st.info("PROTECT_ME=False — protection suggestions not shown. "
                    "Enable in the Config panel and save, then re-run Generate Orders.")
        elif _raw_prot.empty:
            st.info("No protect suggestions — run Generate Orders or all positions protected.")
        elif df_prot.empty:
            st.info("No protect orders match the current filter.")
        else:
            p_cols = ["symbol", "right", "strike", "expiry", "dte", "qty",
                      "undPrice", "xPrice", "cost", "protection", "puc"]
            st.dataframe(
                _banded(df_prot[[c for c in p_cols if c in df_prot.columns]].copy()),
                hide_index=True, width="stretch",
                column_config={
                    "strike":     st.column_config.NumberColumn("Strike",      format="%.1f"),
                    "undPrice":   st.column_config.NumberColumn("Und Px",      format="$%,.2f"),
                    "xPrice":     st.column_config.NumberColumn("Expected Px", format="$%,.2f"),
                    "cost":       st.column_config.NumberColumn("Total Cost",  format="$%,.0f",
                        help="xPrice × qty × 100"),
                    "protection": st.column_config.NumberColumn("Protection",  format="$%,.0f",
                        help="Downside covered by the put/call"),
                    "puc":        st.column_config.NumberColumn("PUC",         format="%.2f",
                        help="Protection per unit cost = protection / cost"),
                },
            )


@st.fragment
def render_config_panel() -> None:
    """Interactive editor for snp_config.yml.

    Fragment: toggle/number interactions only rerun this panel, not the full page
    or other fragments.  This prevents accidental triggering of the unfreeze logic
    in render_orders when the user edits config while derive is running.
    """
    _init_cfg_state()
    st.markdown("#### ⚙️ Config")
    st.caption("Changes apply to next derive run. Comments in YAML are not preserved on save.")

    # ── COVER ──────────────────────────────────────────────────────────────
    st.toggle("COVER_ME", key="cfg_cover_me")
    if st.session_state["cfg_cover_me"]:
        st.number_input("COVER_MIN_DTE", min_value=0, step=1,
                        key="cfg_cover_min_dte",
                        help="Minimum days to expiry for covered call/put candidates")
        st.number_input("COVER_STD_MULT", min_value=0.0, step=0.05, format="%.2f",
                        key="cfg_cover_std_mult",
                        help="Strike distance in units of 1σ above/below spot")
        st.number_input("COVXPMULT", min_value=0.0, step=0.05, format="%.2f",
                        key="cfg_covxpmult",
                        help="Multiplier on market price for execution limit")

    st.divider()

    # ── SOW ────────────────────────────────────────────────────────────────
    st.toggle("SOW_NAKEDS", key="cfg_sow_nakeds")
    if st.session_state["cfg_sow_nakeds"]:
        st.number_input("VIRGIN_DTE", min_value=0, step=1,
                        key="cfg_virgin_dte",
                        help="Target DTE for naked put entries")
        st.number_input("VIRGIN_CALL_STD_MULT", min_value=0.0, step=0.1, format="%.2f",
                        key="cfg_virgin_call_std",
                        help="σ OTM for virgin call strikes")
        st.number_input("VIRGIN_PUT_STD_MULT", min_value=0.0, step=0.1, format="%.2f",
                        key="cfg_virgin_put_std",
                        help="σ OTM for virgin put strikes")
        st.number_input("NAKEDXPMULT", min_value=0.0, step=0.05, format="%.2f",
                        key="cfg_nakedxpmult",
                        help="Multiplier on market price for naked execution limit")
        st.number_input("MINNAKEDOPTPRICE $", min_value=0.0, step=0.25, format="%.2f",
                        key="cfg_minnaked",
                        help="Minimum option price to write a naked put")
        st.number_input("VIRGIN_QTY_MULT", min_value=0.0, step=0.005, format="%.3f",
                        key="cfg_virgin_qty_mult",
                        help="Fraction of NLV per symbol allocated to naked puts")

    st.divider()

    # ── PROTECT ────────────────────────────────────────────────────────────
    st.toggle("PROTECT_ME", key="cfg_protect_me")
    if st.session_state["cfg_protect_me"]:
        st.number_input("PROTECT_DTE", min_value=0, step=1,
                        key="cfg_protect_dte",
                        help="Target DTE for protective put/call purchases")
        st.number_input("PROTECTION_STRIP", min_value=1, step=1,
                        key="cfg_protection_strip",
                        help="Number of OTM strikes to evaluate for protection")

    st.divider()

    # ── REAP ───────────────────────────────────────────────────────────────
    st.toggle("REAP_ME", key="cfg_reap_me")
    if st.session_state["cfg_reap_me"]:
        st.number_input("REAPRATIO", min_value=0.001, step=0.005, format="%.3f",
                        key="cfg_reapratio",
                        help="Close short option when price ≤ REAPRATIO × avgCost")
        st.number_input("MINREAPDTE", min_value=0, step=1,
                        key="cfg_minreapdte",
                        help="Do not reap at or below this DTE")

    st.divider()

    # ── GENERAL ────────────────────────────────────────────────────────────
    st.number_input("MAX_DTE", min_value=1, step=1,
                    key="cfg_max_dte",
                    help="Maximum days to expiry for new option entries")
    st.number_input("MINCUSHION", min_value=0.0, max_value=1.0, step=0.01, format="%.2f",
                    key="cfg_mincushion",
                    help="Minimum excess-liquidity / NLV cushion (triggers alert below this)")

    if st.button("💾 Save Config", type="primary", use_container_width=True,
                 disabled=not _cfg_dirty()):
        try:
            _save_cfg()
            st.success("Saved ✓")
        except Exception as e:
            st.error(f"Save failed: {e}")


@st.fragment(run_every=3.0)
def render_diagnostics() -> None:
    snap = client.snapshot()
    acct = _selected_account()

    # ── Key account values (de-duplicated / summed via _select_account_values) ──
    from decimal import Decimal as _D
    av = _select_account_values(snap, acct)

    # IBKR tag names: StockMarketValue (not StockValue); Leverage-S (short leverage, ~3)
    _KEY_TAGS: list[tuple[str, str, bool]] = [
        # (ibkr_tag,           display_label,   is_ratio)
        ("CashBalance",        "Cash Balance",   False),
        ("StockMarketValue",   "Stock Value",    False),
        ("AccruedDividend",    "Dividend",       False),
        ("InitMarginReq",      "Init Margin",    False),
        ("AvailableFunds",     "Avail Funds",    False),
        ("BuyingPower",        "Buying Power",   False),
        ("Leverage-S",         "Leverage-S",     True),   # ratio e.g. 3.0×
        ("UnrealizedPnL",      "Unreal P&L",     False),
    ]

    st.markdown("##### Key account values")
    kav_cols = st.columns(len(_KEY_TAGS))
    for col, (tag, label, is_ratio) in zip(kav_cols, _KEY_TAGS):
        raw = float(av.get(tag, _D("0")) or 0)
        display = f"{raw:.2f}×" if is_ratio else money(raw, dp=0 if abs(raw) >= 1 else 2)
        col.metric(label, display)

    # ── Raw account values in collapsible twistie ─────────────────────────────
    if snap.account_values:
        all_av = snap.account_values

        def _av_rows(a: str, vals: dict) -> list[dict]:
            return [
                {"account": a, "tag": k, "value": str(v)}
                for k, v in sorted(vals.items())
                if float(v) not in (0.0, -1.0)
            ]

        if acct and acct in all_av:
            raw_rows = _av_rows(acct, all_av[acct])
        else:
            raw_rows = [
                row
                for a, vals in sorted(all_av.items())
                for row in _av_rows(a, vals)
            ]
        with st.expander("📋 All account tags (raw)", expanded=False):
            st.dataframe(
                _banded(pd.DataFrame(raw_rows)), hide_index=True, width="stretch", height=300,
            )
    else:
        st.info("Waiting for account data…")

    st.divider()

    # ── Recent errors (IB + connection + log-file scan) ───────────────────────
    st.markdown("##### Recent errors")

    err_rows: list[dict] = []
    # IB errors from snap (includes 326 via _on_error + connect-failure path)
    for ts, code, msg in list(snap.errors)[-30:][::-1]:
        err_rows.append({
            "ts":     ts.strftime("%H:%M:%S"),
            "source": "IB" if code != -1 else "conn",
            "code":   code,
            "msg":    msg,
        })

    # Scan OHLC + derive logs for error-level lines (yfinance failures, etc.)
    _ERR_KW = ("error", "failed", "fatal", "exception", "traceback", "missing")
    for log_path, label in [(_OHLC_LOG, "ohlc"), (_DERIVE_LOG, "derive")]:
        if log_path.exists():
            try:
                lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
                for ln in lines[-200:]:
                    if any(kw in ln.lower() for kw in _ERR_KW):
                        err_rows.append({"ts": "log", "source": label, "code": 0, "msg": ln.strip()})
            except Exception:
                pass

    if err_rows:
        st.dataframe(
            _banded(pd.DataFrame(err_rows[:50])),
            hide_index=True,
            width="stretch",
            height=300,
            column_config={
                "code": st.column_config.NumberColumn("Code", format="%d"),
            },
        )
    else:
        st.success("No errors.")


# ---------------------------------------------------------------------------
# Analysis tab
# ---------------------------------------------------------------------------

@st.fragment(run_every=60.0)
def render_analysis() -> None:
    """OHLC browser, candlestick/BB/RSI/volume chart, and per-symbol position summary."""
    from src.dashboard.ohlc import load_ohlc  # noqa: PLC0415

    snap = client.snapshot()
    acct = _selected_account()

    # ── Load OHLC store ───────────────────────────────────────────────────────
    ohlc_store = load_ohlc()
    if not ohlc_store:
        st.info(
            "No OHLC data yet. Click **📊 Generate OHLCs** in the Orders tab "
            "to build the store (requires data/symbols.pkl from build.py first)."
        )
        return

    all_symbols = sorted(ohlc_store.keys())

    # ── Prefix filter ─────────────────────────────────────────────────────────
    flt_col, cnt_col, rst_col = st.columns([3, 1, 1])
    sym_prefix = flt_col.text_input(
        "🔍 Filter symbols (prefix)",
        key="analysis_sym_filter",
        placeholder="e.g. C  →  C, CSCO, CSPX…",
        help="Strict prefix match — type 'J' to see J only, 'JP' for JPM etc.",
    )
    if rst_col.button("↺ Reset filters", key="analysis_reset_btn", use_container_width=True):
        for _rk in ["analysis_sym_filter", "analysis_chart_sym"]:
            st.session_state.pop(_rk, None)
        st.rerun()
    filtered = (
        [s for s in all_symbols if s.upper().startswith(sym_prefix.strip().upper())]
        if sym_prefix.strip()
        else all_symbols
    )
    cnt_col.metric("Symbols", f"{len(filtered)}", f"of {len(all_symbols)}")

    # ── OHLC summary table ────────────────────────────────────────────────────
    tbl_rows: list[dict] = []
    for sym in filtered:
        df_s = ohlc_store.get(sym)
        if df_s is None or df_s.empty or "Close" not in df_s.columns:
            continue
        last_close = float(df_s["Close"].iloc[-1])
        prev_close = float(df_s["Close"].iloc[-2]) if len(df_s) > 1 else last_close
        chg_pct = (last_close - prev_close) / prev_close * 100 if prev_close else 0.0
        tbl_rows.append({
            "symbol":    sym,
            "bars":      len(df_s),
            "from":      str(df_s.index.min().date()),
            "to":        str(df_s.index.max().date()),
            "last_close": round(last_close, 2),
            "1d_chg_%":  round(chg_pct, 2),
        })

    if tbl_rows:
        st.dataframe(
            pd.DataFrame(tbl_rows),
            hide_index=True,
            width="stretch",
            height=min(320, len(tbl_rows) * 35 + 40),
            column_config={
                "symbol":     st.column_config.TextColumn("Symbol"),
                "bars":       st.column_config.NumberColumn("Bars",       format="%,d"),
                "from":       st.column_config.TextColumn("From"),
                "to":         st.column_config.TextColumn("To"),
                "last_close": st.column_config.NumberColumn("Last Close", format="$%,.2f"),
                "1d_chg_%":   st.column_config.NumberColumn("1d Chg %",  format="%.2f%%"),
            },
        )

    if not filtered:
        st.info("No symbols match — try a shorter prefix.")
        return

    # ── Chart symbol selector ─────────────────────────────────────────────────
    selected_sym: str | None = st.selectbox(
        "Select symbol for chart & position summary",
        filtered,
        key="analysis_chart_sym",
    )
    if not selected_sym or selected_sym not in ohlc_store:
        return

    df_chart = ohlc_store[selected_sym].copy()
    if df_chart.empty or "Close" not in df_chart.columns:
        st.warning(f"No price data stored for {selected_sym}.")
        return

    # Limit to last ~252 trading days (~1 year) for readability
    df_chart = df_chart.tail(252)

    # ── Bollinger Band multiplier from config ─────────────────────────────────
    try:
        _cfg_yml = yaml.safe_load(_CFG_PATH.read_text(encoding="utf-8")) or {}
        bb_mult = float(_cfg_yml.get("VIRGIN_PUT_STD_MULT", 1.2))
    except Exception:
        bb_mult = 1.2

    close = df_chart["Close"]
    bb_upper, bb_mid, bb_lower = _bollinger(close, window=20, num_std=bb_mult)
    rsi_vals = _rsi(close, period=14)

    # ── Current price & RSI condition for chart title ─────────────────────────
    _last_close_v = float(close.iloc[-1]) if not close.empty else 0.0
    _rsi_clean    = rsi_vals.dropna()
    _last_rsi_v   = float(_rsi_clean.iloc[-1]) if not _rsi_clean.empty else 50.0
    _condition_str = (
        "overbought" if _last_rsi_v >= 70
        else "oversold" if _last_rsi_v <= 30
        else "neutral"
    )

    # ── Position summary for selected symbol ──────────────────────────────────
    pos_df = _filter_positions(snap.positions, acct)
    sym_pos = pd.DataFrame()
    if not pos_df.empty and "symbol" in pos_df.columns:
        from src.dashboard.state import classify_portfolio as _cpf  # noqa: PLC0415
        sym_pos = _cpf(pos_df[pos_df["symbol"] == selected_sym].copy())
        sym_pos = _join_tickers(sym_pos, snap.tickers)

    sum_c1, sum_c2, sum_c3, sum_c4 = st.columns(4)
    if not sym_pos.empty:
        n_stk  = int((sym_pos["secType"] == "STK").sum())
        n_opt  = int((sym_pos["secType"] == "OPT").sum())
        stk_mv = float(sym_pos.loc[sym_pos["secType"] == "STK", "marketValue"].sum()) if "marketValue" in sym_pos.columns else 0.0
        opt_mv = float(sym_pos.loc[sym_pos["secType"] == "OPT", "marketValue"].sum()) if "marketValue" in sym_pos.columns else 0.0
        pf_states: dict[str, int] = (
            sym_pos["pf_state"].value_counts().to_dict() if "pf_state" in sym_pos.columns else {}
        )
        sum_c1.metric("Stocks", n_stk, f"{money(stk_mv)} value")
        sum_c2.metric("Options", n_opt, f"{money(opt_mv)} value")
        if pf_states:
            sum_c3.caption("States: " + ", ".join(f"{k} ×{v}" for k, v in pf_states.items()))
    else:
        sum_c1.caption(f"No {selected_sym} positions held.")
    sum_c4.caption(f"BB ±{bb_mult}σ  (VIRGIN_PUT_STD_MULT)")

    # ── Plotly: Candlestick + BB  |  RSI  |  Volume ───────────────────────────
    has_ohlc   = all(c in df_chart.columns for c in ["Open", "High", "Low", "Close"])
    has_volume = "Volume" in df_chart.columns
    n_rows     = 2 + (1 if has_volume else 0)
    row_heights = [0.6, 0.25] + ([0.15] if has_volume else [])
    sp_titles   = [
        f"{selected_sym} is at ${_last_close_v:,.2f} and is {_condition_str} (RSI {_last_rsi_v:.0f})",
        "RSI (14)",
    ] + (["Volume"] if has_volume else [])

    fig = make_subplots(
        rows=n_rows, cols=1,
        shared_xaxes=True,
        row_heights=row_heights,
        vertical_spacing=0.04,
        subplot_titles=sp_titles,
    )
    x = df_chart.index

    # Row 1 — price
    if has_ohlc:
        fig.add_trace(
            go.Candlestick(
                x=x,
                open=df_chart["Open"], high=df_chart["High"],
                low=df_chart["Low"],   close=df_chart["Close"],
                name=selected_sym,
                increasing_line_color="#22c55e",
                decreasing_line_color="#ef4444",
                showlegend=False,
                hoverlabel={
                    "bgcolor":     "#1e2130",
                    "font_color":  "#f1f5f9",
                    "bordercolor": "#475569",
                },
            ),
            row=1, col=1,
        )
    else:
        fig.add_trace(
            go.Scatter(x=x, y=close, name="Close", line={"color": "#60a5fa", "width": 1.5}),
            row=1, col=1,
        )

    # Bollinger Bands — shaded ribbon (no hover: the legend already labels them)
    fig.add_trace(
        go.Scatter(
            x=x, y=bb_upper, name=f"BB Upper (SMA20+{bb_mult}σ)",
            line={"color": "rgba(251,191,36,0.7)", "width": 1},
            hoverinfo="skip",
        ),
        row=1, col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=x, y=bb_lower, name=f"BB Lower (SMA20-{bb_mult}σ)",
            line={"color": "rgba(251,191,36,0.7)", "width": 1},
            fill="tonexty",
            fillcolor="rgba(251,191,36,0.10)",
            hoverinfo="skip",
        ),
        row=1, col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=x, y=bb_mid, name="SMA 20",
            line={"color": "rgba(251,191,36,1.0)", "width": 1, "dash": "dot"},
            hoverinfo="skip",
        ),
        row=1, col=1,
    )

    # Row 2 — RSI
    fig.add_trace(
        go.Scatter(x=x, y=rsi_vals, name="RSI 14", line={"color": "#a78bfa", "width": 1.5}),
        row=2, col=1,
    )
    for lvl, clr, fill_y0, fill_y1, opacity in [
        (70, "#ef4444", 70, 100, 0.06),
        (30, "#22c55e",  0,  30, 0.06),
    ]:
        fig.add_hline(y=lvl, line_dash="dash", line_color=clr, line_width=1, row=2, col=1)
        fig.add_hrect(y0=fill_y0, y1=fill_y1, fillcolor=clr, opacity=opacity, row=2, col=1)
    fig.add_hline(y=50, line_dash="dot", line_color="rgba(128,128,128,0.4)", line_width=1, row=2, col=1)

    # Row 3 — Volume (colour matches daily candle direction)
    if has_volume:
        vol_colors = [
            "#22c55e" if i == 0 or close.iloc[i] >= close.iloc[i - 1] else "#ef4444"
            for i in range(len(close))
        ]
        fig.add_trace(
            go.Bar(
                x=x, y=df_chart["Volume"],
                name="Volume",
                marker_color=vol_colors,
                showlegend=False,
            ),
            row=n_rows, col=1,
        )

    # ── Strike lines for held options in this symbol ──────────────────────────
    # One horizontal line per option position: colour by right+direction,
    # annotated with right, strike, DTE, state, 1σ move, and margin.
    _STRIKE_COLORS = {
        ("C", "short"): "#92400e",   # brown for short call
        ("P", "short"): "#92400e",   # brown for short put
        ("C", "long"):  "#3b82f6",   # blue for long call
        ("P", "long"):  "#3b82f6",   # blue for long put
    }
    if not sym_pos.empty and "secType" in sym_pos.columns:
        _opt_rows = sym_pos[sym_pos["secType"] == "OPT"]
        _price_lo = float(df_chart["Low"].min())  if "Low"  in df_chart.columns else 0.0
        _price_hi = float(df_chart["High"].max()) if "High" in df_chart.columns else float("inf")

        for _, _opt in _opt_rows.iterrows():
            _strike = float(_opt.get("strike", 0) or 0)
            # Skip strikes way outside the visible price range
            if _strike <= 0 or not (_price_lo * 0.3 <= _strike <= _price_hi * 1.7):
                continue

            _right    = str(_opt.get("right", ""))
            _expiry_s = str(_opt.get("expiry", ""))
            _dte_v    = int(_dte_series(pd.Series([_expiry_s])).iloc[0] or 0) if _expiry_s else 0
            _state_s  = str(_opt.get("pf_state", ""))
            _pos_qty  = float(_opt.get("position", 0) or 0)
            _direction = "short" if _pos_qty < 0 else "long"
            _qty_lbl  = f"{'−' if _pos_qty < 0 else '+'}{int(abs(_pos_qty))}"

            # 1σ move estimate from joined tickers
            _iv_v  = float(_opt.get("iv",            float("nan")))
            _und_v = float(_opt.get("underlying_px", float("nan")))
            # σ distance: how many standard deviations is the strike from current price
            _1sigma = (
                _und_v * _iv_v * (_dte_v / 252) ** 0.5
                if not (pd.isna(_iv_v) or pd.isna(_und_v) or _dte_v <= 0)
                else float("nan")
            )
            _std_s = (
                f"{abs(_strike - _und_v) / _1sigma:.1f}σ OTM"
                if not pd.isna(_1sigma) and _1sigma > 0
                else "σ=?"
            )

            # Margin from what-if or estimate column
            _m_val = next(
                (float(_opt[mc]) for mc in ["margin_init", "margin_est"]
                 if mc in _opt.index and _opt[mc] == _opt[mc]),
                float("nan"),
            )
            _margin_s = f"M=${_m_val:,.0f}" if not pd.isna(_m_val) else ""

            _lc        = _STRIKE_COLORS.get((_right, _direction), "#9ca3af")
            _ld        = "dot"   # dotted for all option strike lines
            _right_name = "Put" if _right == "P" else "Call"
            _qty_abs   = int(abs(_pos_qty))
            _qty_sign  = "−" if _pos_qty < 0 else "+"
            _label = (
                f"{_qty_sign}{_qty_abs} × {_right_name} {_strike:,.0f}  {_dte_v}d"
                f"  {_state_s}  {_std_s}"
                + (f"  {_margin_s}" if _margin_s else "")
            )

            fig.add_hline(
                y=_strike,
                line_dash=_ld,
                line_color=_lc,
                line_width=0.8,
                annotation_text=_label,
                annotation_position="top left",
                annotation_font_size=9,
                annotation_font_color="#e2e8f0",       # white — always readable
                annotation_bgcolor="rgba(10,10,10,0.88)",
                annotation_bordercolor=_lc,
                row=1, col=1,
            )

    # ── Force dark tooltip on every trace (layout-level hoverlabel is ignored
    #    by Scatter/Candlestick in many Plotly versions) ──────────────────────
    _hl = {"bgcolor": "#1e2130", "font": {"color": "#f1f5f9", "size": 11}, "bordercolor": "#475569"}
    fig.update_traces(hoverlabel=_hl)

    # ── Layout & tooltip colours ──────────────────────────────────────────────
    fig.update_layout(
        height=700,
        showlegend=True,
        legend={
            "orientation": "h", "yanchor": "bottom", "y": 1.02, "xanchor": "right", "x": 1,
            "bgcolor": "rgba(14,17,23,0.82)",
            "font": {"color": "#e2e8f0"},
        },
        margin={"l": 10, "r": 10, "t": 60, "b": 10},
        xaxis_rangeslider_visible=False,
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        # Fix white-on-white tooltip: force dark background with light text
        hoverlabel={
            "bgcolor":     "#1e2130",
            "font_color":  "#f1f5f9",
            "bordercolor": "#475569",
            "font_size":   11,
        },
    )
    fig.update_yaxes(range=[0, 100], tickvals=[0, 30, 50, 70, 100], row=2, col=1)

    st.plotly_chart(fig, width="stretch")

    # ── Per-symbol position detail table ──────────────────────
    if not sym_pos.empty:
        st.markdown(f"**{selected_sym} — positions**")
        disp_cols = [
            "account", "secType", "right", "strike", "expiry",
            "position", "avgCost", "marketPrice", "marketValue",
            "unrealizedPNL", "delta", "theta",
        ]
        if "pf_state" in sym_pos.columns:
            disp_cols.append("pf_state")
        view = sym_pos[[c for c in disp_cols if c in sym_pos.columns]].copy()
        st.dataframe(
            _banded(view),
            hide_index=True,
            width="stretch",
            column_config={
                "strike":        st.column_config.NumberColumn("Strike",     format="$%,.1f"),
                "avgCost":       st.column_config.NumberColumn("Avg Cost",   format="$%,.2f"),
                "marketPrice":   st.column_config.NumberColumn("Mkt Px",     format="$%,.2f"),
                "marketValue":   st.column_config.NumberColumn("Mkt Val",    format="$%,.0f"),
                "unrealizedPNL": st.column_config.NumberColumn("Unreal P&L", format="$%,.0f"),
                "delta":         st.column_config.NumberColumn("Δ Delta",   format="%.3f"),
                "theta":         st.column_config.NumberColumn("Θ Theta",   format="%.3f"),
                "pf_state":      st.column_config.TextColumn("State"),
            },
        )


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

header()
kpi_strip()

tabs = st.tabs(["Positions", "Orders", "Analysis", "Diagnostics"])
with tabs[0]:
    render_positions()
    st.divider()
    render_risk()
with tabs[1]:
    _ord_col, _cfg_col = st.columns([3, 1])
    with _ord_col:
        render_orders()
    with _cfg_col:
        render_config_panel()
with tabs[2]:
    render_analysis()
with tabs[3]:
    render_diagnostics()
