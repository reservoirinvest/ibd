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
import time
from datetime import datetime
from pathlib import Path

import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
import yaml
from loguru import logger
from plotly.subplots import make_subplots
from pyprojroot import here as _here

# pyrefly: ignore [missing-import]
from src.dashboard.formatting import money, pct, signed_money
# pyrefly: ignore [missing-import]
from src.dashboard.ib_client import get_client
# pyrefly: ignore [missing-import]
from src.dashboard.llm_query import query_data, query_data_deepseek, query_data_gemini
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

# Apply minimal CSS to position navigation and allow native Streamlit dark/light mode
st.markdown(
    """
    <style>
    /* Nav row (header + radio + account selector) — fixed at top.
       :has() is supported Chrome 105+, Safari 15.4+, Firefox 121+, Edge 105+. */
    [data-testid="stHorizontalBlock"]:has([data-testid="stRadio"]) {
        position: fixed !important;
        top: 0 !important;
        left: 0 !important;
        right: 14rem !important;
        z-index: 999999 !important;
        background-color: var(--background-color) !important;
        padding: 0 0.5rem !important;
        margin: 0 !important;
        align-items: center !important;
    }
    /* Style radio as tab pills */
    [data-testid="stRadio"] > div {
        gap: 0.5rem;
    }
    [data-testid="stRadio"] label {
        padding: 0.25rem 1rem;
        border-radius: 999px;
        background-color: transparent;
        border: 1px solid transparent;
        transition: all 0.2s ease;
    }
    [data-testid="stRadio"] label:hover {
        background-color: var(--secondary-background-color);
    }
    [data-testid="stRadio"] label[data-checked="true"] {
        background-color: var(--secondary-background-color);
        border: 1px solid var(--text-color);
        box-shadow: 0 1px 3px rgba(0,0,0,0.1);
        font-weight: 600;
    }
    /* Opaque background for all expander content */
    [data-testid="stExpanderDetails"] {
        background-color: var(--secondary-background-color) !important;
        backdrop-filter: none !important;
    }
    /* Fixed band (nav → status bar → KPI+AI) — JS adds .kpi-bar-fixed and sets top dynamically */
    .kpi-bar-fixed {
        position: fixed !important;
        top: 2.5rem;           /* fallback — JS overrides with actual nav height */
        left: 0 !important;
        right: 14rem !important;
        z-index: 999998 !important;
        background-color: var(--background-color) !important;
        padding: 0 0.5rem !important;
        border-bottom: 1px solid rgba(128, 128, 128, 0.15) !important;
    }
    /* Ask AI column: allow natural height (answer expander drives it) */
    .ask-ai-col { overflow: visible !important; }
    /* Compact status bar inside left column of the fixed band */
    .hdr-bar { font-size: 0.72rem; line-height: 1.5; padding: 3px 0; }
    .hdr-title { font-weight: 700; font-size: 0.86rem; }
    .hdr-cur { color: #22c55e; font-weight: 700; font-size: 0.86rem; }
    .hdr-item { opacity: 0.8; }
    /* Push main content below nav + KPI/AI bar (JS refines this dynamically) */
    section[data-testid="stMain"] > div.block-container {
        padding-top: 7rem !important;
    }
    /* Analysis symbol selectbox — narrow to ~10 chars */
    .sym-sel-narrow [data-baseweb="select"] {
        min-width: 0 !important;
        width: 10ch !important;
    }
    /* KPI compact banded table */
    .kpi-tbl { width: 100%; border-collapse: collapse; font-size: 0.84rem; }
    .kpi-tbl td { padding: 3px 6px; white-space: nowrap; }
    .kpi-lbl { opacity: 0.6; font-size: 0.74rem; }
    .kpi-val { font-weight: 600; font-size: 0.92rem; text-align: right; }
    .kpi-row-a { background-color: rgba(128,128,128,0.04); }
    .kpi-row-b { background-color: rgba(128,128,128,0.1); }
    .kpi-breach { color: #ef4444 !important; }
    /* Left padding on the second/third label — visual gap between pairs */
    .kpi-lbl2 { padding-left: 1.2rem !important; }
    .kpi-lbl3 { padding-left: 1.2rem !important; }
    /* Tooltip trigger (?) inside KPI table — superscript, muted */
    .kpi-help { cursor: help; opacity: 0.45; font-size: 0.6rem; vertical-align: super; margin-left: 1px; }
    </style>
    """,
    unsafe_allow_html=True,
)

settings = get_settings()

# st.cache_resource ensures start() is called at most once per Streamlit server
# process — survives hot-reload reruns and concurrent multi-session calls.
@st.cache_resource(show_spinner=False)
def _start_ib_client():
    c = get_client()
    c.start(settings)
    return c

client = _start_ib_client()

# ---------------------------------------------------------------------------
# Account selector — build label → account-number mapping from settings
# ---------------------------------------------------------------------------
_US = settings.us_account.get_secret_value()
_SG = settings.sg_account.get_secret_value()

_DATA_DIR    = _here() / "data"
_MASTER_DIR  = _DATA_DIR / "master"
_CFG_PATH    = _here() / "config" / "snp_config.yml"
_DERIVE_LOG    = _here() / "log" / "derive_progress.log"
_OHLC_LOG      = _here() / "log" / "ohlc_progress.log"
_EXECUTE_LOG   = _here() / "log" / "execute.log"
_DASHBOARD_LOG = _here() / "log" / "dashboard.log"

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


def _filter_positions(positions: pd.DataFrame, account: str) -> pd.DataFrame:
    """Filter a positions DataFrame to the selected account (no-op for ALL)."""
    if not account or positions.empty or "account" not in positions.columns:
        return positions
    return positions[positions["account"] == account].reset_index(drop=True)


_EVEN_BG = "background-color: rgba(128, 128, 128, 0.05)"   # subtle overlay for both modes
_ODD_BG  = "background-color: transparent"
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


def _pkl_age(name: str, *, path: Path | None = None) -> str:
    """Human-readable age of a file, e.g. '3m ago'. Pass path= to override _DATA_DIR/name."""
    p = path or (_DATA_DIR / name)
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

# Single registry: (session_key, yaml_key, cast_fn, default)
# All three config helpers (init/save/dirty) are driven by this list —
# adding a new config key requires only one entry here.
_CFG_KEYS: list[tuple[str, str, type, object]] = [
    ("cfg_cover_me",         "COVER_ME",             bool,  True),
    ("cfg_cover_min_dte",    "COVER_MIN_DTE",         int,   4),
    ("cfg_cover_std_mult",   "COVER_STD_MULT",        float, 0.65),
    ("cfg_covxpmult",        "COVXPMULT",             float, 1.2),
    ("cfg_sow_nakeds",       "SOW_NAKEDS",            bool,  True),
    ("cfg_virgin_dte",       "VIRGIN_DTE",            int,   5),
    ("cfg_virgin_call_std",  "VIRGIN_CALL_STD_MULT",  float, 3.8),
    ("cfg_virgin_put_std",   "VIRGIN_PUT_STD_MULT",   float, 1.2),
    ("cfg_nakedxpmult",      "NAKEDXPMULT",           float, 4.95),
    ("cfg_minnaked",         "MINNAKEDOPTPRICE",      float, 2.5),
    ("cfg_virgin_qty_mult",  "VIRGIN_QTY_MULT",       float, 0.055),
    ("cfg_protect_me",       "PROTECT_ME",            bool,  False),
    ("cfg_protect_dte",      "PROTECT_DTE",           int,   35),
    ("cfg_protection_strip", "PROTECTION_STRIP",      int,   5),
    ("cfg_reap_me",          "REAP_ME",               bool,  True),
    ("cfg_reapratio",        "REAPRATIO",             float, 0.025),
    ("cfg_minreapdte",       "MINREAPDTE",            int,   1),
    ("cfg_max_dte",          "MAX_DTE",               int,   50),
    ("cfg_mincushion",       "MINCUSHION",            float, 0.2),
]


def _init_cfg_state() -> None:
    """Seed session_state from snp_config.yml exactly once per browser session."""
    if st.session_state.get("_cfg_inited"):
        return
    try:
        cfg = yaml.safe_load(_CFG_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        cfg = {}
    for sk, yk, cast, default in _CFG_KEYS:
        st.session_state.setdefault(sk, cast(cfg.get(yk, default)))
    st.session_state["_cfg_inited"] = True


def _save_cfg() -> None:
    """Write session_state config values back to snp_config.yml (comments not preserved)."""
    try:
        cfg = yaml.safe_load(_CFG_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        cfg = {}
    changed: list[str] = []
    for sk, yk, cast, _ in _CFG_KEYS:
        new_val = cast(st.session_state[sk])
        old_val = cast(cfg.get(yk, new_val))
        if new_val != old_val:
            changed.append(f"{yk}={new_val!r}")
        cfg[yk] = new_val
    _CFG_PATH.write_text(
        yaml.dump(cfg, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )
    if changed:
        logger.info("Config saved — changed: {}", ", ".join(changed))
    else:
        logger.debug("Config saved (no changes)")


def _cfg_dirty() -> bool:
    """Return True if any config session_state value differs from the current YAML file."""
    if not st.session_state.get("_cfg_inited"):
        return False
    try:
        cfg = yaml.safe_load(_CFG_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        return True
    return any(
        cast(st.session_state.get(sk, default)) != cast(cfg.get(yk, default))
        for sk, yk, cast, default in _CFG_KEYS
    )


def _force_reload_cfg() -> None:
    """Unconditionally re-read snp_config.yml → session_state (overwrites existing values)."""
    try:
        cfg = yaml.safe_load(_CFG_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        cfg = {}
    for sk, yk, cast, default in _CFG_KEYS:
        st.session_state[sk] = cast(cfg.get(yk, default))
    st.session_state["_cfg_inited"] = True


def _itm_mask_vec(df: pd.DataFrame) -> list[bool]:
    """Boolean list: True for in-the-money options (call: strike < und_px; put: strike > und_px)."""
    if df.empty:
        return []
    is_opt = (df.get("secType", pd.Series("", index=df.index)) == "OPT")
    und    = df.get("underlying_px", pd.Series(float("nan"), index=df.index)).fillna(float("nan"))
    strike = df.get("strike",        pd.Series(float("nan"), index=df.index)).fillna(float("nan"))
    right  = df.get("right",         pd.Series("",           index=df.index))
    call_itm = is_opt & (right == "C") & (strike < und)
    put_itm  = is_opt & (right == "P") & (strike > und)
    return (call_itm | put_itm).fillna(False).tolist()


def _capture_exit(proc: subprocess.Popen | None, key: str) -> None:
    """Store a subprocess exit code in session_state exactly once."""
    if proc is not None and proc.poll() is not None and key not in st.session_state:
        rc = proc.poll()
        st.session_state[key] = rc
        logger.info("Subprocess exited: key={} rc={} pid={}", key, rc, proc.pid)


def _sub_env() -> dict[str, str]:
    """Build subprocess environment with project root on PYTHONPATH.

    Scripts in src/ use 'from src.X import ...' which requires the project root
    on sys.path. Python adds the script's own directory to sys.path[0], not the root,
    so we pass PYTHONPATH explicitly.
    """
    root = str(_here())
    existing = os.environ.get("PYTHONPATH", "")
    pythonpath = f"{root}{os.pathsep}{existing}" if existing else root
    return {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONPATH": pythonpath}


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
_TQDM_PCT_RE = re.compile(r":\s+(\d+)%\|")
_ANSI_RE     = re.compile(r"\x1b\[[0-9;]*[mGKHF]")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


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


def _ohlc_progress() -> tuple[float, str]:
    """Parse ohlc_progress.log tqdm lines → (progress 0–1, latest bar label)."""
    lines = _ohlc_log_lines(30)
    if not lines:
        return 0.01, "Initialising…"
    last_pct = 0.0
    last_label = ""
    for ln in lines:
        m_label = _TQDM_BAR_RE.match(_strip_ansi(ln))
        m_pct   = _TQDM_PCT_RE.search(_strip_ansi(ln))
        if m_label and m_pct:
            last_label = m_label.group(1).strip()
            last_pct   = int(m_pct.group(1)) / 100.0
    return max(last_pct, 0.01), last_label or "Fetching OHLCs…"


def _render_log_expander(label: str, log_path: Path, *, expanded: bool = False) -> None:
    """Render a collapsible st.expander containing the full text of *log_path*."""
    with st.expander(label, expanded=expanded):
        if log_path.exists():
            try:
                st.code(log_path.read_text(encoding="utf-8", errors="replace"), language=None)
            except Exception as e:
                st.warning(f"Could not read log: {e}")


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



def _drop_withstand(excess: float, delta_abs: float) -> str:
    """Format a market-drop withstand % string."""
    if delta_abs < 1:
        return "N/A"
    v = excess / delta_abs * 100
    return f"{v:.1f}%" if v < 200 else ">200%"


@st.fragment(run_every=2.0)
def header() -> None:
    """Compact status bar — rendered in the nav row (left of tabs)."""
    snap = client.snapshot()
    acct = _selected_account()
    positions_filt = _filter_positions(snap.positions, acct)

    if client.is_frozen():
        st_html = "🧊 <b>FROZEN</b>"
    elif snap.connected:
        st_html = "🟢 <b>LIVE</b>"
    else:
        st_html = "🔴 <b>DISC.</b>"

    as_of = snap.as_of.strftime('%H:%M:%S') if snap.as_of else '—'
    pos_n = len(positions_filt)

    st.markdown(
        f'<div class="hdr-bar">'
        f'<span class="hdr-title">IB Monitor</span>'
        f'&nbsp;&nbsp;{st_html}&nbsp;&nbsp;'
        f'<span class="hdr-cur">{settings.currency}</span>'
        f'<br>'
        f'<span class="hdr-item">as_of:&nbsp;{as_of}</span>'
        f'&nbsp;&bull;&nbsp;<span class="hdr-item">port:&nbsp;{settings.ib_port}&nbsp;cid:&nbsp;{settings.ib_client_id}</span>'
        f'&nbsp;&bull;&nbsp;<span class="hdr-item">pos:&nbsp;{pos_n}</span>'
        f'</div>',
        unsafe_allow_html=True,
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
    opt_val = float(av.get("OptionMarketValue", _D("0")) or 0)

    # Drop withstand — goes into row 1 col 3 (in line with NLV)
    if snap.account_values and not snap.positions.empty:
        delta_sel = g["delta_$"]
        if len(_REAL_ACCOUNTS) > 1 and _US and not acct:
            pos_us   = _filter_positions(snap.positions, _US)
            k_us     = account_kpis(snap, account=_US)
            g_us     = greek_dollar_sums(pos_us, snap.tickers)
            delta_us = g_us["delta_$"]
            _dr_r1_lbl = "US+SG drop"
            _dr_r1_val = "N/A" if delta_sel <= 0 else _drop_withstand(k["excess_liquidity"], abs(delta_sel))
            _dr_r1_tip = "US+SG drop withstand = Total Excess Liquidity ÷ |Total Dollar Delta| × 100%. Combined US+SG portfolio."
            _dr_r4_lbl = "US drop"
            _dr_r4_val = "N/A" if delta_us  <= 0 else _drop_withstand(k_us["excess_liquidity"],  abs(delta_us))
            _dr_r4_tip = "US drop withstand = US Excess Liquidity ÷ |US Dollar Delta| × 100%. Approximate % broad market decline before US account margin call (delta-only, linear)."
        else:
            _dr_r1_lbl = "Drop withstand"
            _dr_r1_val = "N/A" if delta_sel <= 0 else _drop_withstand(k["excess_liquidity"], abs(delta_sel))
            _dr_r1_tip = "Drop withstand = Excess Liquidity ÷ |Dollar Delta| × 100%. Approximate % broad market decline before margin call (delta-only, linear). N/A when net-short or delta near zero."
            _dr_r4_lbl = ""
            _dr_r4_val = ""
            _dr_r4_tip = ""
    else:
        _dr_r1_lbl = "Drop withstand"
        _dr_r1_val = "—"
        _dr_r1_tip = "Drop withstand = Excess Liquidity ÷ |Dollar Delta| × 100%. Waiting for account data."
        _dr_r4_lbl = ""
        _dr_r4_val = ""
        _dr_r4_tip = ""

    def _lbl(text: str, tip: str) -> str:
        safe_tip = tip.replace('"', '&quot;')
        return f'<span title="{safe_tip}">{text}&nbsp;<span class="kpi-help">?</span></span>'

    c_cls = ' class="kpi-breach"' if k["cushion_breach"] else ""
    min_c_pct = f"{settings.min_cushion:.0%}"
    _dr_lbl1 = _lbl(_dr_r1_lbl, _dr_r1_tip) if _dr_r1_lbl else ""
    _dr_lbl4 = _lbl(_dr_r4_lbl, _dr_r4_tip) if _dr_r4_lbl else ""

    # Layout: col1 = NLV/Cushion, col2 = risk metrics, col3 = greeks
    rows: list[tuple[str, str, str, str, str, str, str, str, str]] = [
        (
            _lbl("NLV", "Net Liquidation Value: total portfolio value including cash, stocks and options at current market prices."),
            money(k["nlv"]), "",
            _dr_lbl1, _dr_r1_val, "",
            _lbl("&#x3A3;&#x394; ($)", "Portfolio Dollar Delta: P&amp;L change for a 1-point broad market move. Sum of position × delta × 100 × underlying price across all positions."),
            signed_money(g["delta_$"]), "",
        ),
        (
            _lbl(f"Cushion (min {min_c_pct})", f"Margin cushion = Excess Liquidity ÷ NLV. Alert threshold: {min_c_pct}. Breach turns red."),
            pct(k["cushion"]), c_cls,
            _lbl("Excess Liq", "Excess Liquidity: funds available above the maintenance margin requirement. Reaching zero triggers a margin call."),
            money(k["excess_liquidity"]), "",
            _lbl("&#x3A3;&#x398; ($/d)", "Portfolio Dollar Theta: daily time decay across all options in dollars. Positive = net premium seller collecting theta."),
            signed_money(g["theta_$"]), "",
        ),
        (
            _dr_lbl4, _dr_r4_val, "",
            _lbl("Opt Value", "Option Market Value: total mark-to-market value of all option positions."),
            money(opt_val), "",
            _lbl("&#x3A3;&#x3B3; ($)", "Portfolio Dollar Gamma: rate of change of dollar delta per 1-point move. Positive gamma means delta grows in your favour as the market moves."),
            signed_money(g["gamma_$"]), "",
        ),
        (
            "", "", "",
            _lbl("Maint Margin", "Maintenance Margin Requirement: minimum equity you must hold to keep current positions open."),
            money(k["maint_margin"]), "",
            _lbl("&#x3A3;&#x3BD; ($)", "Portfolio Dollar Vega: P&amp;L sensitivity to a 1% rise in implied volatility across all options."),
            signed_money(g["vega_$"]), "",
        ),
    ]

    html_parts = ['<table class="kpi-tbl">']
    for i, (l1, v1, cls1, l2, v2, cls2, l3, v3, cls3) in enumerate(rows):
        rc = "kpi-row-a" if i % 2 == 0 else "kpi-row-b"
        html_parts.append(
            f'<tr class="{rc}">'
            f'<td class="kpi-lbl">{l1}</td><td class="kpi-val"{cls1}>{v1}</td>'
            f'<td class="kpi-lbl kpi-lbl2">{l2}</td><td class="kpi-val"{cls2}>{v2}</td>'
            f'<td class="kpi-lbl kpi-lbl3">{l3}</td><td class="kpi-val"{cls3}>{v3}</td>'
            f'</tr>'
        )
    html_parts.append('</table>')
    st.markdown("\n".join(html_parts), unsafe_allow_html=True)




@st.fragment(run_every=3.0)
def render_orders() -> None:
    snap = client.snapshot()
    acct = _selected_account()
    _ok = account_kpis(snap, min_cushion=settings.min_cushion, account=acct)

    # ── Subprocess state ─────────────────────────────────────────────────────
    proc: subprocess.Popen | None       = st.session_state.get("derive_proc")
    ohlc_proc: subprocess.Popen | None  = st.session_state.get("ohlc_proc")
    exec_proc: subprocess.Popen | None  = st.session_state.get("execute_proc")
    frozen     = client.is_frozen()
    # OHLC runs without freezing (IBKR fallback uses CID=12, never conflicts with dashboard CID=10)
    _ohlc_running = ohlc_proc is not None and ohlc_proc.poll() is None
    frozen_for = st.session_state.get("frozen_for", "")   # "derive" | "ohlc" | "execute" | ""

    # Auto-unfreeze when each subprocess finishes (derive + execute only; ohlc runs without freeze)
    def _auto_unfreeze(tag: str, proc_key: str) -> None:
        proc_ = st.session_state.get(proc_key)
        if frozen and proc_ is not None and proc_.poll() is not None and frozen_for == tag:
            st.session_state[f"_{tag}_exit"] = proc_.poll()  # capture before clearing proc
            client.unfreeze()
            st.session_state[proc_key] = None
            st.session_state.pop("frozen_for", None)
            st.rerun()

    _auto_unfreeze("derive",  "derive_proc")
    _auto_unfreeze("execute", "execute_proc")

    # Capture exit codes the moment each process ends
    _capture_exit(proc,      "_derive_exit")
    _capture_exit(ohlc_proc, "_ohlc_exit")
    _capture_exit(exec_proc, "_execute_exit")

    # ── Imports needed by Generate OHLCs ──────────────────────────────────────
    from src.dashboard.ohlc import (   # noqa: PLC0415  (local import inside fragment)
        OHLC_PATH,
        get_sp500_symbols,
        write_symbol_list,
    )

    # ── Action buttons — single row: generate/fetch/execute/clear ────────────────
    gen_col, ohlc_col, exec_col, clr_col, _btn_spacer = st.columns([2, 2, 2, 2, 2])

    # ── Generate Orders ────────────────────────────────────────────────────────
    with gen_col:
        if st.button(
            "⚙️ Generate Orders",
            disabled=frozen,
            width="stretch",
            help="Freezes the dashboard (releases CID), runs derive.py, then reconnects. "
                 "Takes 2–5 min. Last-known data stays visible during the freeze.",
        ):
            _DERIVE_LOG.parent.mkdir(parents=True, exist_ok=True)
            log_fh = open(_DERIVE_LOG, "w", encoding="utf-8")  # noqa: SIM115
            _env = _sub_env()
            st.session_state.pop("_derive_exit", None)
            st.session_state["frozen_for"] = "derive"
            client.freeze()          # freeze BEFORE Popen so derive.py can claim CID=10
            new_proc = subprocess.Popen(
                [sys.executable, str(_here() / "src" / "derive.py")],
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                env=_env,
            )
            st.session_state["derive_proc"] = new_proc
            logger.info("derive.py started pid={}", new_proc.pid)
            # No st.rerun() — fragment run_every=3.0 picks up frozen state automatically
        # Last-derive timestamp sits right under the button
        if "_derive_exit" not in st.session_state and not frozen:
            ages = [_pkl_age(n) for n in ["df_cov.pkl", "df_nkd.pkl", "df_reap.pkl"]]
            age_str = ages[0] if len(set(ages)) == 1 else " | ".join(ages)
            st.caption(f"Last: {age_str}")

    # ── Generate OHLCs ────────────────────────────────────────────────────────
    with ohlc_col:
        if st.button(
            "📊 Generate OHLCs",
            disabled=frozen or _ohlc_running,
            width="stretch",
            help=(
                "Fetch / update 1.5 yr daily OHLC for S&P500 weekly underlyings + "
                "portfolio positions. Runs in background; dashboard stays connected."
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

            # No freeze needed: primary fetch is yfinance; IBKR fallback uses CID=12
            # (separate from dashboard CID=10 — no conflict).
            _OHLC_LOG.parent.mkdir(parents=True, exist_ok=True)
            _ohlc_log_fh = open(_OHLC_LOG, "w", encoding="utf-8")   # noqa: SIM115
            _env = _sub_env()
            st.session_state.pop("_ohlc_exit", None)
            _ohlc_new_proc = subprocess.Popen(
                [sys.executable, str(_here() / "src" / "fetch_ohlc.py")],
                stdout=_ohlc_log_fh,
                stderr=subprocess.STDOUT,
                env=_env,
            )
            st.session_state["ohlc_proc"] = _ohlc_new_proc
            logger.info("fetch_ohlc.py started pid={}", _ohlc_new_proc.pid)
            # No st.rerun() — fragment run_every=3.0 polls process state automatically
        # Last-OHLC timestamp sits right under the button
        if not _ohlc_running and "_ohlc_exit" not in st.session_state:
            st.caption(f"Last: {_pkl_age('', path=OHLC_PATH)}")

    # ── Execute Orders ────────────────────────────────────────────────────────
    with exec_col:
        # Execute Orders button with confirmation dialog
        @st.dialog("⚠️ Confirm Order Execution", width="small")
        def _confirm_execute():
            st.markdown(
                "This will execute all orders from the Suggested Orders section. "
                "**This action is irreversible.** Are you sure?"
            )
            col1, col2 = st.columns(2)
            with col1:
                if st.button("✅ Execute", width="stretch", use_container_width=True):
                    st.session_state["_exec_confirmed"] = True
                    st.rerun()
            with col2:
                if st.button("❌ Cancel", width="stretch", use_container_width=True):
                    st.session_state.pop("_execute_exit", None)
                    st.session_state.pop("_exec_confirmed", None)
                    st.rerun()

        if st.button(
            "▶️ Execute Orders",
            disabled=frozen,
            width="stretch",
            help="Execute all orders from the Suggested Orders section. "
                 "Freezes the dashboard, runs execute.py, then reconnects. "
                 "⚠️ This is IRREVERSIBLE.",
        ):
            _confirm_execute()

        # Check if user confirmed and execute
        if st.session_state.get("_exec_confirmed"):
            st.session_state.pop("_exec_confirmed", None)
            _EXECUTE_LOG.parent.mkdir(parents=True, exist_ok=True)
            log_fh = open(_EXECUTE_LOG, "w", encoding="utf-8")  # noqa: SIM115
            _env = _sub_env()
            st.session_state.pop("_execute_exit", None)
            st.session_state["frozen_for"] = "execute"
            client.freeze()
            exec_proc = subprocess.Popen(
                [sys.executable, str(_here() / "src" / "execute.py")],
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                env=_env,
            )
            st.session_state["execute_proc"] = exec_proc
            logger.info("execute.py started pid={}", exec_proc.pid)

    # ── Clear Data ─────────────────────────────────────────────────────────────
    with clr_col:
        if st.button(
            "🗑️ Clear Data",
            width="stretch",
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

    # ── Status row (derive + OHLC + execute) ────────────────────────────────────
    if frozen or _ohlc_running or "_derive_exit" in st.session_state or "_ohlc_exit" in st.session_state or "_execute_exit" in st.session_state:
        gen_status_col, ohlc_status_col, exec_status_col, _st_spacer = st.columns([2.5, 2.5, 2.5, 2.5])
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
            if _ohlc_running:
                _op, _ol = _ohlc_progress()
                st.progress(_op, text=f"⏳ {_ol}")
            elif "_ohlc_exit" in st.session_state:
                rc = st.session_state["_ohlc_exit"]
                if rc == 0:
                    st.success("✅ OHLCs up to date")
                else:
                    st.error(f"❌ OHLC fetch failed (exit {rc})")
        with exec_status_col:
            if frozen and frozen_for == "execute":
                st.progress(0.5, text="⏳ Executing orders…")
            elif "_execute_exit" in st.session_state:
                rc = st.session_state["_execute_exit"]
                if rc == 0:
                    st.success("✅ Orders executed")
                else:
                    st.error(f"❌ Order execution failed (exit {rc})")

    # ── Scrollable log (live during run; collapsible after) ───────────────────
    if _ohlc_running:
        _ohlc_live = _ohlc_log_lines(30)
        if _ohlc_live:
            st.code("\n".join(_strip_ansi(ln) for ln in _ohlc_live), language=None)
    if frozen and frozen_for == "execute":
        _exec_log = _here() / "log" / "execute.log"
        if _exec_log.exists():
            try:
                _exec_live = _exec_log.read_text(encoding="utf-8", errors="replace").splitlines()[-30:]
                if _exec_live:
                    st.code("\n".join(_exec_live), language=None)
            except Exception:
                pass
    elif frozen:
        log_lines = _derive_log_lines(35)
        if log_lines:
            st.code("\n".join(_strip_ansi(ln) for ln in log_lines), language=None)
    elif "_derive_exit" in st.session_state:
        rc = st.session_state["_derive_exit"]
        _render_log_expander("📋 derive.py log", _DERIVE_LOG, expanded=rc != 0)

    # Post-run OHLC log — collapsible after freeze ends
    if "_ohlc_exit" in st.session_state:
        rc = st.session_state["_ohlc_exit"]
        _render_log_expander("📋 OHLC log", _OHLC_LOG, expanded=rc != 0)

    # Post-run Execute log — collapsible after freeze ends
    if "_execute_exit" in st.session_state:
        rc = st.session_state["_execute_exit"]
        _render_log_expander("📋 execute.py log", _EXECUTE_LOG, expanded=rc != 0)

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
    if _f3.button("✕ Clear", key="ord_clear_filter", width="stretch",
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
        if _raw_nkd.empty and _ok["cushion_breach"]:
            st.info(
                "No sow suggestions as cushion is less. "
                "Adjust MINCUSHION and rerun generate, if sow is needed."
            )
        elif _raw_nkd.empty:
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
    _cfg_hdr, _cfg_btn = st.columns([3, 1])
    with _cfg_hdr:
        st.markdown("#### ⚙️ Config")
    with _cfg_btn:
        if st.button("🔄 Get Config", help="Force re-read from snp_config.yml", width="stretch"):
            _force_reload_cfg()
            st.rerun()
    st.caption("Changes apply to next derive run. Comments in YAML are not preserved on save.")

    # ── GENERAL (top — most-used risk limits) ───────────────────────────────
    st.number_input("MINCUSHION", min_value=0.0, max_value=1.0, step=0.01, format="%.2f",
                    key="cfg_mincushion",
                    help="Minimum excess-liquidity / NLV cushion (triggers alert below this)")
    st.number_input("MAX_DTE", min_value=1, step=1,
                    key="cfg_max_dte",
                    help="Maximum days to expiry for new option entries")

    st.divider()

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

    if st.button("💾 Save Config", type="primary", width="stretch",
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
        ("OptionMarketValue",  "Opt Value",      False),
        ("StockMarketValue",   "Stock Value",    False),
        ("AccruedDividend",    "Dividend",       False),
        ("InitMarginReq",      "Init Margin",    False),
        ("AvailableFunds",     "Avail Funds",    False),
        ("BuyingPower",        "Buying Power",   False),
        ("Leverage-S",         "Leverage-S",     True),   # ratio e.g. 3.0×
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

        def _fmt_val(v) -> str:
            try:
                f = float(v)
                if f == round(f, 0):
                    return f"{int(round(f)):,}"
                return str(v)
            except (ValueError, TypeError):
                return str(v)

        def _av_rows(a: str, vals: dict) -> list[dict]:
            return [
                {"account": a, "tag": k, "value": _fmt_val(v)}
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
    _ERR_KW = ("error", "failed", "fatal", "exception", "traceback", "missing", "critical")
    for log_path, label in [(_OHLC_LOG, "ohlc"), (_DERIVE_LOG, "derive"), (_DASHBOARD_LOG, "dashboard")]:
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

@st.cache_data(ttl=120, show_spinner=False)
def _cached_ohlc() -> dict:
    """Load the OHLC pickle store, cached for 120 s to avoid re-reading on every 60 s tick."""
    from src.dashboard.ohlc import load_ohlc  # noqa: PLC0415
    return load_ohlc()


def _sync_analysis_to_pos_filter() -> None:
    pass  # callback kept for selectbox; Positions tab removed


@st.fragment(run_every=60.0)
def render_analysis() -> None:
    """Cover/Protect gaps + OHLC chart browser."""
    snap = client.snapshot()
    acct = _selected_account()

    # ── Positions table (live — with filters + ITM highlighting) ─────────────
    if not snap.positions.empty:
        _pos_data = classify_portfolio(_filter_positions(snap.positions, acct))
        _pos_data = _join_tickers(_pos_data, snap.tickers)
        _pos_data["margin_est"] = position_margin_est(_pos_data)

        with st.expander("📋 Positions", expanded=False):
            _pf_c1, _pf_c2, _pf_c3, _pf_c4, _pf_c5, _pf_c6 = st.columns([2.5, 1, 2, 1, 1, 1])
            _pf_sym = _pf_c1.text_input(
                "Symbol", key="pf_sym", placeholder="exact, e.g. A"
            ).strip().upper()
            _pf_sectype = _pf_c2.selectbox("secType", ["ALL", "STK", "OPT"], key="pf_sectype")
            _all_states = ["ALL"] + sorted(
                _pos_data["pf_state"].dropna().unique().tolist()
                if "pf_state" in _pos_data.columns else []
            )
            _pf_state_sel = _pf_c3.selectbox("State", _all_states, key="pf_f_state")
            # Build DTE choices from OPT rows in the unfiltered position data
            _opt_mask = (_pos_data.get("secType", pd.Series("", index=_pos_data.index)) == "OPT")
            _opt_expiries = (
                _pos_data.loc[_opt_mask, "expiry"].dropna().astype(str).unique().tolist()
                if "expiry" in _pos_data.columns else []
            )
            _dte_int_set = sorted({
                int(v) for v in _dte_series(pd.Series(_opt_expiries)).dropna() if not pd.isna(v)
            })
            _dte_opts = ["ALL"] + [str(d) for d in _dte_int_set]
            _pf_dte_sel = _pf_c4.selectbox("Max DTE", _dte_opts, key="pf_dte_sel")
            _pf_itm_only = _pf_c5.checkbox("ITM only", key="pf_itm_only")
            if _pf_c6.button("✕ Clear", key="pf_clear_filter", width="stretch"):
                for _k in ("pf_sym", "pf_sectype", "pf_f_state", "pf_dte_sel", "pf_itm_only"):
                    st.session_state.pop(_k, None)
                st.rerun()

            # Apply filters
            _pv = _pos_data.copy()
            if _pf_sym:
                _pv = _pv[_pv["symbol"].astype(str).str.upper() == _pf_sym]
            if _pf_sectype != "ALL":
                _pv = _pv[_pv.get("secType", pd.Series("", index=_pv.index)) == _pf_sectype]
            if _pf_state_sel != "ALL":
                _pv = _pv[_pv.get("pf_state", pd.Series("", index=_pv.index)) == _pf_state_sel]
            if _pf_dte_sel != "ALL":
                _dte_max_val = int(_pf_dte_sel)
                _dte_col = _dte_series(
                    _pv.get("expiry", pd.Series("", index=_pv.index)).fillna("").astype(str)
                )
                _pv = _pv[_dte_col.isna() | (_dte_col <= _dte_max_val)]
            if _pf_itm_only:
                _pv = _pv[pd.Series(_itm_mask_vec(_pv), index=_pv.index)]
            _pv = _pv.reset_index(drop=True)
            _itm_arr = _itm_mask_vec(_pv)

            # Build display columns: und_px next to strike; dte inserted next to expiry
            _pos_show_cols = [
                "symbol", "secType", "right", "strike", "underlying_px",
                "expiry", "position", "marketPrice",
                "delta", "gamma", "theta", "vega", "margin_est", "pf_state",
            ]
            _pv_show = _pv[[c for c in _pos_show_cols if c in _pv.columns]].copy()
            # Insert DTE column immediately after expiry
            if "expiry" in _pv_show.columns:
                _pv_show.insert(
                    _pv_show.columns.get_loc("expiry") + 1, "dte",
                    _dte_series(_pv_show["expiry"].fillna("").astype(str)).round(0).values,
                )
            # For STK rows underlying price equals market price
            if all(c in _pv_show.columns for c in ("underlying_px", "marketPrice", "secType")):
                _stk_rows = _pv_show["secType"] == "STK"
                _pv_show.loc[_stk_rows, "underlying_px"] = _pv_show.loc[_stk_rows, "marketPrice"]
            if "position" in _pv_show.columns:
                _pv_show["position"] = _pv_show["position"].fillna(0).astype(int)

            if _pv_show.empty:
                st.info("No positions match the current filter.")
            else:
                st.dataframe(
                    _banded(_pv_show, itm_mask=_itm_arr),
                    hide_index=True,
                    width="stretch",
                    column_config={
                        "strike":        st.column_config.NumberColumn("Strike",  format="%.1f"),
                        "underlying_px": st.column_config.NumberColumn("Und Px",  format="$%,.2f"),
                        "expiry":        st.column_config.TextColumn("Expiry"),
                        "dte":           st.column_config.NumberColumn("DTE",     format="%.0f"),
                        "position":      st.column_config.NumberColumn("Qty",     format="%.0f"),
                        "marketPrice":   st.column_config.NumberColumn("Mkt Px",  format="$%,.2f"),
                        "delta":         st.column_config.NumberColumn("Δ",       format="%.3f"),
                        "gamma":         st.column_config.NumberColumn("Γ",       format="%.4f"),
                        "theta":         st.column_config.NumberColumn("Θ",       format="%.3f"),
                        "vega":          st.column_config.NumberColumn("ν",       format="%.3f"),
                        "margin_est":    st.column_config.NumberColumn("Margin",  format="$%,.0f"),
                        "pf_state":      st.column_config.TextColumn("State"),
                    },
                )

        # ── Cover / Protect gaps ─────────────────────────────────────────────
        # protect_me=True always so Protect Strike / Value Protected columns are present
        gaps = cover_protect_gaps(
            _pos_data, snap.tickers,
            protect_me=True,
            cover_std_mult=settings.cover_std_mult,
            max_dte=settings.max_dte,
        )
        _gap_header = "🔍 Cover / Protect gaps"
        if not settings.protect_me:
            _gap_header += " — cover only (PROTECT_ME=False)"
        with st.expander(_gap_header, expanded=False):
            if gaps.empty:
                st.success("No gaps — all stocks covered and protected.")
            else:
                _gaps_show = gaps.copy()
                if "shares" in _gaps_show.columns:
                    _gaps_show["shares"] = _gaps_show["shares"].fillna(0).astype(int)
                # Merge unrealizedPNL from live positions (sum across stock + options)
                if "unrealizedPNL" in _pos_data.columns:
                    _pnl_agg = (
                        _pos_data.groupby("symbol")["unrealizedPNL"]
                        .sum()
                        .reset_index()
                    )
                    _gaps_show = _gaps_show.merge(_pnl_agg, on="symbol", how="left")
                # Needs filter
                _gap_needs_opts = ["ALL"] + sorted(_gaps_show["needs"].dropna().unique().tolist())
                _gnf_col, _ = st.columns([2, 6])
                _gap_needs_sel = _gnf_col.selectbox(
                    "Needs", _gap_needs_opts, key="gap_needs_sel",
                )
                if _gap_needs_sel != "ALL":
                    _gaps_show = _gaps_show[_gaps_show["needs"] == _gap_needs_sel]
                # Column order: cover_strike | mkt_px | protect_strike either side
                _gap_cols = [
                    "symbol", "shares", "avg_cost",
                    "cover_strike", "mkt_px", "protect_strike",
                    "gain_if_called", "max_downside", "unrealizedPNL", "needs",
                ]
                _gaps_show = _gaps_show[[c for c in _gap_cols if c in _gaps_show.columns]]
                _gap_col_cfg: dict = {
                    "shares": st.column_config.NumberColumn("Shares", format="%d"),
                    "avg_cost": st.column_config.NumberColumn("Avg Cost", format="$%.2f"),
                    "cover_strike": st.column_config.NumberColumn(
                        "Cover Strike", format="$%.1f",
                        help=(
                            f"Target call strike: max(avgCost, mkt_px + "
                            f"{settings.cover_std_mult}×IV×√(DTE/252)), DTE={settings.max_dte}. "
                            "IV sourced from existing option tickers, default 30%."
                        ),
                    ),
                    "mkt_px": st.column_config.NumberColumn("Mkt Px", format="$%.2f"),
                    "protect_strike": st.column_config.TextColumn(
                        "Target Protect Strike",
                        help=(
                            "~XXX.X = target put strike (mkt_px − cover_std_mult×σ) when no protection. "
                            "Existing strike(s) shown when long option held."
                        ),
                    ),
                    "gain_if_called": st.column_config.NumberColumn(
                        "Gain if Called", format="$%,.0f",
                        help="Capital gain vs avg cost if the stock is called away at the cover strike.",
                    ),
                    "max_downside": st.column_config.NumberColumn(
                        "Value Protected", format="$%,.0f",
                        help="Total cost basis at risk (avg cost × shares) if the position falls to zero.",
                    ),
                    "unrealizedPNL": st.column_config.NumberColumn(
                        "Unrealized P&L", format="$%,.0f",
                        help="Sum of unrealized P&L across all positions (stock + options) for this symbol.",
                    ),
                }
                st.dataframe(_banded(_gaps_show), hide_index=True, width="stretch", column_config=_gap_col_cfg)
        st.divider()

    # ── Load OHLC store ───────────────────────────────────────────────────────
    ohlc_store = _cached_ohlc()
    if not ohlc_store:
        st.info(
            "No OHLC data yet. Click **📊 Generate OHLCs** in the Orders tab "
            "to build the store (requires data/symbols.pkl from build.py first)."
        )
        return

    all_symbols = sorted(ohlc_store.keys())

    st.caption(f"{len(all_symbols)} symbols in OHLC store")

    # ── Portfolio Treemap ─────────────────────────────────────────────────────
    pos_df = _filter_positions(snap.positions, acct)
    if not pos_df.empty:
        df_tree = pos_df.copy()
        if "margin_init" not in df_tree.columns:
            df_tree["margin_est"] = position_margin_est(df_tree)
            m_col = "margin_est"
        else:
            m_col = "margin_init"
            
        if "unrealizedPNL" in df_tree.columns and m_col in df_tree.columns:
            df_tree["unrealizedPNL"] = df_tree["unrealizedPNL"].fillna(0)
            df_tree[m_col] = df_tree[m_col].fillna(0)
            
            tree_agg = df_tree.groupby("symbol").agg({
                "unrealizedPNL": "sum",
                m_col: "sum",
            }).reset_index()
            
            tree_agg = tree_agg[tree_agg[m_col] > 0]
            if not tree_agg.empty:
                fig_tree = px.treemap(
                    tree_agg,
                    path=["symbol"],
                    values=m_col,
                    color="unrealizedPNL",
                    color_continuous_scale="RdYlGn",
                    color_continuous_midpoint=0,
                )
                fig_tree.update_traces(
                    marker=dict(line=dict(color='rgba(0,0,0,0)')),
                    hovertemplate='<b>%{label}</b><br>Margin: $%{value:,.0f}<br>P&L: $%{color:,.0f}<extra></extra>'
                )
                fig_tree.update_layout(
                    margin=dict(t=10, l=10, r=10, b=10),
                    paper_bgcolor="rgba(0,0,0,0)",
                    plot_bgcolor="rgba(0,0,0,0)",
                    height=320,
                )
                st.plotly_chart(fig_tree, width="stretch")
            else:
                st.info("No positions with margin > 0.")

    # ── Chart symbol selector ─────────────────────────────────────────────────
    # Guard stale session_state (e.g. symbol removed from OHLC store after a refresh)
    _cur_sym = st.session_state.get("analysis_chart_sym")
    if _cur_sym not in all_symbols:
        st.session_state["analysis_chart_sym"] = all_symbols[0] if all_symbols else None

    _sym_col, _ = st.columns([1, 9])
    with _sym_col:
        st.markdown('<div class="sym-sel-narrow">', unsafe_allow_html=True)
        selected_sym: str | None = st.selectbox(
            "Symbol",
            all_symbols,
            key="analysis_chart_sym",
            on_change=_sync_analysis_to_pos_filter,
            label_visibility="collapsed",
        )
        st.markdown('</div>', unsafe_allow_html=True)
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
        sym_pos["delta_$"] = position_delta_dollars(sym_pos)

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

    # Bollinger Bands first (behind candles)
    fig.add_trace(
        go.Scatter(
            x=x, y=bb_upper, name=f"BB Upper (SMA20+{bb_mult}σ)",
            line={"color": "rgba(251,191,36,0.7)", "width": 1},
            hovertemplate="BB Upper: %{y:,.2f}<extra></extra>",
        ),
        row=1, col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=x, y=bb_lower, name=f"BB Lower (SMA20-{bb_mult}σ)",
            line={"color": "rgba(251,191,36,0.7)", "width": 1},
            fill="tonexty",
            fillcolor="rgba(251,191,36,0.10)",
            hoverinfo="skip",   # fill area must not obscure candle hover
        ),
        row=1, col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=x, y=bb_mid, name="SMA 20",
            line={"color": "rgba(251,191,36,1.0)", "width": 1, "dash": "dot"},
            hovertemplate="SMA 20: %{y:,.2f}<extra></extra>",
        ),
        row=1, col=1,
    )
    # Separator line in unified hover between BB/SMA rows and OHLC row
    fig.add_trace(
        go.Scatter(
            x=x, y=[None] * len(x),
            name="", mode="none", showlegend=False,
            hovertemplate="──────────────<extra></extra>",
        ),
        row=1, col=1,
    )

    # Row 1 — price (rendered on top of BB)
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
                hovertemplate=(
                    "<b style='color:#1e2130'>" + selected_sym + "</b><br>"
                    "O: %{open:,.2f}  H: %{high:,.2f}<br>"
                    "L: %{low:,.2f}  C: %{close:,.2f}"
                    "<extra></extra>"
                ),
            ),
            row=1, col=1,
        )
    else:
        fig.add_trace(
            go.Scatter(x=x, y=close, name="Close", line={"color": "#60a5fa", "width": 1.5}),
            row=1, col=1,
        )
    
    # Add horizontal line for current price
    fig.add_hline(
        y=_last_close_v,
        line_color="#3b82f6", # blue
        line_width=1,
        line_dash="solid",
        row=1, col=1,
        annotation_text=f"Px: ${_last_close_v:,.2f}",
        annotation_position="top left",
        annotation_font_color="#3b82f6",
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
                annotation_font_color="#1e2130",       # dark — always readable
                annotation_bgcolor="rgba(255,255,255,0.88)",
                annotation_bordercolor=_lc,
                row=1, col=1,
            )

    # ── Force dark tooltip on every trace (layout-level hoverlabel is ignored
    #    by Scatter/Candlestick in many Plotly versions) ──────────────────────
    _hl = {"bgcolor": "#ffffff", "font": {"color": "#1e2130", "size": 11}, "bordercolor": "#cbd5e1"}
    fig.update_traces(hoverlabel=_hl)

    # ── Layout & tooltip colours ──────────────────────────────────────────────
    fig.update_layout(
        height=700,
        showlegend=True,
        hovermode="x unified",
        legend={
            "orientation": "h", "yanchor": "bottom", "y": 1.02, "xanchor": "right", "x": 1,
            "bgcolor": "rgba(248, 249, 250, 0.82)",
            "font": {"color": "#1e2130"},
        },
        margin={"l": 10, "r": 10, "t": 60, "b": 10},
        xaxis_rangeslider_visible=False,
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        hoverlabel={
            "bgcolor":     "#ffffff",
            "font_color":  "#1e2130",
            "bordercolor": "#cbd5e1",
            "font_size":   11,
        },
        hoverdistance=50,
    )
    fig.update_xaxes(showspikes=False)
    fig.update_yaxes(showspikes=False)
    fig.update_yaxes(range=[0, 100], tickvals=[0, 30, 50, 70, 100], row=2, col=1)

    st.plotly_chart(fig, width="stretch")

    # ── Per-symbol position detail table (always rendered) ──────────────────────
    st.markdown(f"**{selected_sym} — positions**")
    if sym_pos.empty:
        st.caption(f"No positions held for {selected_sym}.")
    else:
        sym_pos = sym_pos.copy()
        # Add DTE column
        if "expiry" in sym_pos.columns:
            sym_pos["dte"] = _dte_series(sym_pos["expiry"]).fillna(0).astype(int)
        # Add margin column
        if "margin_init" in sym_pos.columns:
            _sym_mcol = "margin_init"
            _sym_mlabel = "Margin"
        else:
            sym_pos["margin_est"] = position_margin_est(sym_pos)
            _sym_mcol = "margin_est"
            _sym_mlabel = "Margin*"
        # ITM mask for row shading
        _s_und = sym_pos["underlying_px"] if "underlying_px" in sym_pos.columns else pd.Series(float("nan"), index=sym_pos.index)
        _s_str = sym_pos["strike"] if "strike" in sym_pos.columns else pd.Series(float("nan"), index=sym_pos.index)
        _s_rgt = sym_pos["right"] if "right" in sym_pos.columns else pd.Series("", index=sym_pos.index)
        _s_itm = (
            ((sym_pos["secType"] == "OPT") & (_s_rgt == "C") & (_s_und > _s_str))
            | ((sym_pos["secType"] == "OPT") & (_s_rgt == "P") & (_s_und < _s_str))
        ).to_numpy(dtype=bool)

        disp_cols = [
            "symbol", "secType", "underlying_px", "right", "strike", "expiry", "dte",
            "position", "pf_state", "avgCost", "marketPrice", "marketValue",
            "unrealizedPNL", "delta", "theta", "vega", "iv", "delta_$",
        ]
        if _sym_mcol in sym_pos.columns:
            disp_cols.append(_sym_mcol)
        sym_view = sym_pos[[c for c in disp_cols if c in sym_pos.columns]].reset_index(drop=True)
        # STK rows: underlying_px is always None — fill with marketPrice (stock IS the underlying)
        if "secType" in sym_view.columns and "marketPrice" in sym_view.columns and "underlying_px" in sym_view.columns:
            _stk = sym_view["secType"] == "STK"
            sym_view.loc[_stk, "underlying_px"] = sym_view.loc[_stk, "marketPrice"]
        st.dataframe(
            _banded(sym_view, _s_itm),
            hide_index=True,
            width="stretch",
            column_config={
                "symbol":        st.column_config.TextColumn("Symbol"),
                "underlying_px": st.column_config.NumberColumn("Underlying", format="$%.2f"),
                "right":         st.column_config.TextColumn("C/P"),
                "dte":           st.column_config.NumberColumn("DTE",        format="%.0f"),
                "strike":        st.column_config.NumberColumn("Strike",     format="$%,.1f"),
                "avgCost":       st.column_config.NumberColumn("Avg Cost",   format="$%,.2f"),
                "marketPrice":   st.column_config.NumberColumn("Mkt Px",     format="$%,.2f"),
                "marketValue":   st.column_config.NumberColumn("Mkt Val",    format="$%,.0f"),
                "unrealizedPNL": st.column_config.NumberColumn("Unreal P&L", format="$%,.0f"),
                "delta":         st.column_config.NumberColumn("Δ Delta",    format="%.3f"),
                "theta":         st.column_config.NumberColumn("Θ Theta",    format="%.3f"),
                "vega":          st.column_config.NumberColumn("ν Vega",     format="%.3f"),
                "iv":            st.column_config.NumberColumn("IV",         format="%.3f"),
                "delta_$":       st.column_config.NumberColumn("Delta $",    format="$%,.0f"),
                _sym_mcol:       st.column_config.NumberColumn(_sym_mlabel,  format="$%,.0f"),
                "pf_state":      st.column_config.TextColumn("State"),
            },
        )


_PROVIDER_HINTS: dict[str, tuple[str, str]] = {
    "DeepSeek": ("platform.deepseek.com",          "https://platform.deepseek.com/"),
    "Gemini":   ("aistudio.google.com/app/apikey", "https://aistudio.google.com/app/apikey"),
    "Claude":   ("console.anthropic.com",          "https://console.anthropic.com"),
}


def _build_live_context() -> dict:
    """Build LLM context from the live dashboard snapshot."""
    snap = client.snapshot()
    acct = _selected_account()
    positions = _filter_positions(snap.positions, acct)
    context: dict = {}
    if not positions.empty:
        cols = [c for c in ("symbol", "secType", "position", "marketPrice",
                             "marketValue", "delta", "theta", "vega") if c in positions.columns]
        context["positions"] = positions[cols]
    g = greek_dollar_sums(positions, snap.tickers) if not positions.empty else {}
    if g:
        context["greeks"] = {k: round(v, 2) for k, v in g.items() if isinstance(v, float)}
    av = _select_account_values(snap, acct)
    if av:
        context["metrics"] = {k: str(v) for k, v in av.items()}
    return context


def _render_llm_chat() -> None:
    """Compact Ask AI dock: always visible, one row of controls + cached response."""
    p_col, q_col, c_col = st.columns([2, 5, 1])

    provider = p_col.selectbox(
        "Provider",
        list(_PROVIDER_HINTS),
        key="llm_provider",
        label_visibility="collapsed",
    )

    _qver = st.session_state.get("llm_q_ver", 0)
    question: str = q_col.text_input(
        "Ask",
        key=f"llm_q_{_qver}",
        placeholder="Ask about your portfolio…",
        label_visibility="collapsed",
    )

    if c_col.button("✕", key="llm_clr", help="Clear", use_container_width=True):
        st.session_state["llm_q_ver"] = _qver + 1
        st.session_state.pop("llm_response", None)
        st.session_state.pop("llm_last_q", None)
        st.session_state.pop("llm_last_prov", None)
        st.rerun()

    # Only query when the question or provider actually changes (not on every fragment tick)
    if question and (
        question != st.session_state.get("llm_last_q")
        or provider != st.session_state.get("llm_last_prov")
    ):
        with st.spinner(f"{provider}…"):
            try:
                context = _build_live_context()
                if provider == "Claude":
                    resp = query_data(question, context)
                elif provider == "Gemini":
                    resp = query_data_gemini(question, context)
                else:
                    resp = query_data_deepseek(question, context)
                st.session_state["llm_response"] = resp
            except Exception as e:
                st.session_state["llm_response"] = f"⚠️ {e}"
            st.session_state["llm_last_q"] = question
            st.session_state["llm_last_prov"] = provider

    if cached := st.session_state.get("llm_response"):
        with st.expander("Answer", expanded=True):
            with st.container(height=120, border=False):
                # Escape $ so Streamlit doesn't treat currency/options as LaTeX delimiters
                safe = cached.replace("$", r"\$")
                st.markdown(safe)


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

# Nav row: [header | tabs | account selector | spacer] — all fixed at top via CSS :has([stRadio])
# Account selector placed right after tabs so native Streamlit Deploy/burger stay visible on far right.
_hdr_c, _nav_c1, _acct_c, _spacer_c = st.columns([2, 3, 1, 2])
with _hdr_c:
    header()
with _nav_c1:
    nav = st.radio(
        "Navigation",
        ["Analysis", "Orders", "Diagnostics"],
        horizontal=True,
        label_visibility="collapsed",
    )
with _acct_c:
    if len(_REAL_ACCOUNTS) > 1:
        st.selectbox(
            "Account",
            list(_ACCOUNT_OPTIONS.keys()),
            key="acct_sel",
            label_visibility="collapsed",
        )
    elif _REAL_ACCOUNTS:
        st.caption(next(iter(_REAL_ACCOUNTS)))
# _spacer_c intentionally empty — preserves right-side gap for native Streamlit controls

# Second fixed band: [KPI table | Ask AI]
# JS below finds this block via the Ask AI input placeholder and pins it below the nav row.
_kpi_c, _ai_c = st.columns([3, 7])
with _kpi_c:
    kpi_strip()
with _ai_c:
    _render_llm_chat()

st.markdown(
    """
    <script>
    (function () {
        const PH = 'Ask about your portfolio…';
        function applyFix() {
            const inp = document.querySelector('input[placeholder="' + PH + '"]');
            if (!inp) return;
            const bar = inp.closest('[data-testid="stHorizontalBlock"]');
            if (!bar) return;
            if (!bar.classList.contains('kpi-bar-fixed')) bar.classList.add('kpi-bar-fixed');
            const col = inp.closest('[data-testid="stColumn"]');
            if (col && !col.classList.contains('ask-ai-col')) col.classList.add('ask-ai-col');
            /* Dynamically set main-content padding to match actual nav+bar heights */
            const nav = document.querySelector('[data-testid="stHorizontalBlock"]:has([data-testid="stRadio"])');
            const main = document.querySelector('section[data-testid="stMain"] > div.block-container');
            if (nav && main) {
                const navH = nav.getBoundingClientRect().height;
                bar.style.top = navH + 'px';
                const h = navH + bar.getBoundingClientRect().height;
                main.style.paddingTop = Math.max(h + 6, 80) + 'px';
            }
        }
        applyFix();
        let _t;
        new MutationObserver(function () { clearTimeout(_t); _t = setTimeout(applyFix, 50); })
            .observe(document.body, { childList: true, subtree: true });
    })();
    </script>
    """,
    unsafe_allow_html=True,
)

# Log tab and account navigation changes (fires once per actual change per session).
_prev_nav = st.session_state.get("_prev_nav")
if _prev_nav is not None and _prev_nav != nav:
    logger.info("Tab navigation: {} → {}", _prev_nav, nav)
st.session_state["_prev_nav"] = nav

_prev_acct = st.session_state.get("_prev_acct_sel")
_curr_acct = st.session_state.get("acct_sel")
if _prev_acct is not None and _prev_acct != _curr_acct:
    logger.info("Account selector: {} → {}", _prev_acct, _curr_acct)
st.session_state["_prev_acct_sel"] = _curr_acct

if nav == "Analysis":
    render_analysis()
elif nav == "Orders":
    _ord_col, _cfg_col = st.columns([3, 1])
    with _ord_col:
        render_orders()
    with _cfg_col:
        render_config_panel()
elif nav == "Diagnostics":
    render_diagnostics()
