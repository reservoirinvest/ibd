"""IBKR live risk dashboard - Streamlit entrypoint.

Run:
    uv run streamlit run app.py --server.address=127.0.0.1
"""

from __future__ import annotations

import logging as _logging
import os
import pickle
import re
import subprocess
import sys
import time
from datetime import date, datetime
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


# Route INFO+ to a rolling log file; keep terminal clean (WARNING+ only).
# Must run before any module that calls logger.info() at import time.
_LOG_DIR = _here() / "log"
_LOG_DIR.mkdir(exist_ok=True)
logger.remove()
logger.add(sys.stderr, level="WARNING", colorize=True, format="{time:HH:mm:ss} | {level} | {message}")
logger.add(str(_LOG_DIR / "app.log"), level="DEBUG", encoding="utf-8", rotation="50 MB", retention=3)

# Suppress Streamlit's "fragment does not exist anymore" terminal warnings.
# These fire when a run_every fragment's timer triggers while the user is on another tab.
class _SuppressFragmentMissing(_logging.Filter):
    def filter(self, record: _logging.LogRecord) -> bool:
        return "does not exist anymore" not in record.getMessage()

for _ln in ("streamlit", "streamlit.runtime", "streamlit.runtime.scriptrunner"):
    _logging.getLogger(_ln).addFilter(_SuppressFragmentMissing())

st.set_page_config(
    page_title="IB Monitor",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# Claude-inspired styling. Palette comes from .streamlit/config.toml; the rules
# below add serif display type, clay-accented expanders, the action pipeline
# arrows, and keep the KPI / header utility classes. Everything keys off
# Streamlit theme variables so dark mode stays legible.
st.markdown(
    """
    <style>
    /* Hide Deploy button (dark-mode toggle lives in the hamburger menu) */
    [data-testid="stAppDeployButton"] { display: none !important; }

    /* ── Type: serif display for headings + expander titles (Claude feel) ── */
    h1, h2, h3, h4,
    [data-testid="stExpander"] summary p,
    .hdr-title {
        font-family: Georgia, "Tiempos Text", "Times New Roman", serif !important;
        letter-spacing: 0.2px;
    }

    /* ── Expanders: parchment panel, clay left-accent on the header ── */
    [data-testid="stExpander"] {
        border: 1px solid color-mix(in srgb, var(--text-color) 12%, transparent);
        border-radius: 10px;
        overflow: hidden;
        margin-bottom: 0.5rem;
    }
    [data-testid="stExpander"] summary {
        border-left: 3px solid #D97757;  /* clay — literal so it persists in dark mode */
        padding-left: 0.6rem;
    }
    [data-testid="stExpander"] summary:hover { color: #D97757; }
    [data-testid="stExpanderDetails"] {
        background-color: var(--secondary-background-color) !important;
        backdrop-filter: none !important;
    }

    /* ── Action pipeline arrows ── */
    .pipe-arrow {
        display: flex; align-items: center; justify-content: center;
        height: 100%; min-height: 2.4rem;
        font-size: 1.5rem; font-weight: 700;
        color: #D97757; opacity: 0.85;  /* clay — literal so it persists in dark mode */
    }

    /* ── Header status bar ── */
    .hdr-bar   { font-size: 0.72rem; line-height: 1.5; padding: 3px 0; }
    .hdr-title { font-weight: 700; font-size: 1.0rem; }
    .hdr-cur   { color: #1f9d55; font-weight: 700; font-size: 0.86rem; }
    .hdr-item  { opacity: 0.8; }

    /* ── KPI flex band (built by kpi_strip) ── */
    .kpi-band { display: flex; flex-direction: column; }
    .kpi-row  { display: flex; flex-wrap: wrap; padding: 3px 4px; align-items: center; }
    .kpi-row-a { background-color: color-mix(in srgb, var(--text-color) 4%, transparent); }
    .kpi-row-b { background-color: color-mix(in srgb, var(--text-color) 9%, transparent); }
    /* Cells stack label-above-value like st.metric (Performance Dashboard look) */
    .kpi-cell { display: flex; flex-direction: column; align-items: flex-start; gap: 0; flex: 1 1 22%; min-width: 120px; padding: 2px 12px 2px 0; line-height: 1.3; overflow: hidden; }
    @media (min-width: 641px) and (max-width: 900px) { .kpi-cell { flex: 1 1 45%; } }
    .kpi-lbl-stack { display: flex; flex-direction: column; line-height: 1.2; }
    .kpi-lbl { opacity: 0.6; font-size: 0.82rem; white-space: nowrap; }
    .kpi-sub { font-size: 0.68rem; opacity: 0.55; white-space: nowrap; }
    .kpi-val { font-weight: 600; font-size: 1.4rem; white-space: nowrap; }
    .kpi-breach { color: #ef4444 !important; }
    [data-testid="stMetricValue"] { font-size: 1.84rem !important; font-weight: 600 !important; }
    [data-testid="stMetricLabel"] { font-size: 1.0rem !important; opacity: 0.7; }
    [data-testid="stMetricDelta"] { font-size: 0.95rem !important; }

    /* Tooltip (?) badge */
    .kpi-help {
        cursor: help; opacity: 0.65; font-size: 0.72rem; line-height: 1;
        display: inline-flex; align-items: center; justify-content: center;
        width: 1.1em; height: 1.1em;
        border: 1px solid currentColor; border-radius: 50%;
        margin-left: 3px; vertical-align: middle;
    }

    /* Deep-dive symbol selectbox — narrow to ~10 chars */
    .sym-sel-narrow [data-baseweb="select"] { min-width: 0 !important; width: 10ch !important; }

    /* Ask AI answer: wrap prose + scroll wide tables within their block */
    [data-testid="stExpander"] .stMarkdown table { display: block; overflow-x: auto; max-width: 100%; }
    [data-testid="stExpander"] .stMarkdown td,
    [data-testid="stExpander"] .stMarkdown th { white-space: normal !important; word-break: break-word; max-width: 24em; }

    /* Trim the default top padding now that the nav band is gone */
    section[data-testid="stMain"] > div.block-container { padding-top: 2.5rem !important; }

    /* ── Master section banners (Performance / Orders / Config) ── */
    .st-key-btn_master_perf button,
    .st-key-btn_master_orders button,
    .st-key-btn_master_config button {
        justify-content: flex-start !important;
        text-align: left !important;
        font-size: 1.15rem !important;
        font-weight: 700 !important;
        letter-spacing: 0.01em;
        padding: 0.45rem 0.9rem !important;
        margin-top: 0.35rem;
        border: none !important;
        border-left: 4px solid #D97757 !important;
        border-radius: 8px !important;
        background: color-mix(in srgb, #D97757 12%, transparent) !important;
    }
    .st-key-btn_master_perf button:hover,
    .st-key-btn_master_orders button:hover,
    .st-key-btn_master_config button:hover {
        background: color-mix(in srgb, #D97757 20%, transparent) !important;
    }
    /* Force the label (whatever tag Streamlit uses) hard-left — tag-agnostic.
       The label wrapper is itself a flex container, so text-align alone won't
       move it — pin justify-content too, and target nested tags as well. */
    .st-key-btn_master_perf button > *,
    .st-key-btn_master_orders button > *,
    .st-key-btn_master_config button > *,
    .st-key-btn_master_perf button p,
    .st-key-btn_master_orders button p,
    .st-key-btn_master_config button p {
        width: 100% !important;
        text-align: left !important;
        justify-content: flex-start !important;
    }

    /* Keep narrow-column checkbox labels on one line */
    .st-key-syn_chk_wrap label p,
    .st-key-wk_chk_wrap label p { white-space: nowrap !important; }



    /* ── Mobile (≤ 640 px): stack KPI cells two-up ── */
    @media (max-width: 640px) {
        .kpi-cell { flex: 1 1 45%; min-width: 0; flex-direction: column; align-items: flex-start; gap: 0; padding: 2px 4px 2px 0; }
        .kpi-lbl { font-size: 0.78rem; }
        .kpi-val { font-size: 1.2rem; }
    }
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
_CANCEL_LOG    = _here() / "log" / "cancel.log"
_DASHBOARD_LOG = _here() / "log" / "dashboard.log"
_BACKTEST_LOG  = _here() / "log" / "backtest.log"

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

# Default: ALL when both accounts configured, else first real account
if "acct_sel" not in st.session_state:
    default = "ALL" if len(_REAL_ACCOUNTS) > 1 else next(iter(_REAL_ACCOUNTS), "ALL")
    st.session_state["acct_sel"] = default


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
    ("cfg_cov_aged_dte",     "COV_AGED_DTE",          int,   180),
    ("cfg_sow_nakeds",       "SOW_NAKEDS",            bool,  False),
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
    ("cfg_max_file_age",     "MAX_FILE_AGE",          int,   1),
    ("cfg_score_from_focus", "SCORE_FROM_FOCUS",      bool,  False),
    ("cfg_defaultai",        "DEFAULTAI",             str,   "Gemini"),
]

# Hardcoded fallback when FOCUS_DATE is missing from snp_config.yml.
_FOCUS_DATE_FALLBACK = date(2025, 8, 8)


def _cfg_focus_date_default() -> date:
    """FOCUS_DATE from snp_config.yml (read fresh), falling back to the hardcoded date."""
    try:
        cfg = yaml.safe_load(_CFG_PATH.read_text(encoding="utf-8")) or {}
        v = cfg.get("FOCUS_DATE", _FOCUS_DATE_FALLBACK)
    except Exception:
        v = _FOCUS_DATE_FALLBACK
    if isinstance(v, date):
        return v
    try:
        return pd.Timestamp(str(v)).date()
    except Exception:
        return _FOCUS_DATE_FALLBACK


def focus_date() -> pd.Timestamp:
    """Active focus date — the dashboard date-picker value if set, else snp_config.yml."""
    v = st.session_state.get("cfg_focus_date")
    if isinstance(v, date):
        return pd.Timestamp(v)
    return pd.Timestamp(_cfg_focus_date_default())


def score_since():
    """Cut-off Timestamp for trade-history scoring (focus date) when SCORE_FROM_FOCUS
    is enabled, else None (full trade history)."""
    if st.session_state.get("cfg_score_from_focus", False):
        return focus_date()
    return None


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
    st.session_state.setdefault("cfg_focus_date", _cfg_focus_date_default())
    st.session_state.setdefault(
        "_focus_applied",
        (st.session_state["cfg_focus_date"], st.session_state.get("cfg_score_from_focus", False)),
    )
    _ai_raw = cfg.get("AIMODELS", ["Gemini", "DeepSeek"])
    st.session_state.setdefault("cfg_aimodels", _ai_raw if isinstance(_ai_raw, list) else [str(_ai_raw)])
    st.session_state.setdefault("_defaultai_applied", st.session_state.get("cfg_defaultai"))
    st.session_state["_cfg_inited"] = True


def _save_cfg() -> None:
    """Write session_state config values back to snp_config.yml (comments not preserved)."""
    try:
        cfg = yaml.safe_load(_CFG_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        cfg = {}
    changed: list[str] = []
    for sk, yk, cast, default in _CFG_KEYS:
        # fall back to current YAML value if Streamlit cleaned up a hidden widget's key
        _yaml_val = cast(cfg.get(yk, default))
        new_val = cast(st.session_state.get(sk, _yaml_val))
        old_val = _yaml_val
        if new_val != old_val:
            changed.append(f"{yk}={new_val!r}")
        cfg[yk] = new_val
    # FOCUS_DATE — date widget, stored as an ISO string (kept out of _CFG_KEYS).
    _fd = st.session_state.get("cfg_focus_date")
    if isinstance(_fd, date):
        _fd_str = _fd.isoformat()
        if str(cfg.get("FOCUS_DATE", "")) != _fd_str:
            changed.append(f"FOCUS_DATE={_fd_str!r}")
        cfg["FOCUS_DATE"] = _fd_str
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
    if any(
        cast(st.session_state.get(sk, default)) != cast(cfg.get(yk, default))
        for sk, yk, cast, default in _CFG_KEYS
    ):
        return True
    _fd = st.session_state.get("cfg_focus_date")
    if isinstance(_fd, date) and _fd.isoformat() != str(cfg.get("FOCUS_DATE", "")):
        return True
    return False


def _force_reload_cfg() -> None:
    """Unconditionally re-read snp_config.yml → session_state (overwrites existing values)."""
    try:
        cfg = yaml.safe_load(_CFG_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        cfg = {}
    for sk, yk, cast, default in _CFG_KEYS:
        st.session_state[sk] = cast(cfg.get(yk, default))
    st.session_state["cfg_focus_date"] = _cfg_focus_date_default()
    _ai_raw = cfg.get("AIMODELS", ["Gemini", "DeepSeek"])
    st.session_state["cfg_aimodels"] = _ai_raw if isinstance(_ai_raw, list) else [str(_ai_raw)]
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


# ── Single global filter — one set of controls drives every expander ──────────
_FILTER_STATES = [
    "covering", "sowing", "sowed", "reaping", "protecting",
    "orphaned", "zen", "unprotected", "uncovered", "exposed",
]
# Order-table key → the state that table represents (for show/hide on State filter).
_ORDER_TABLE_STATE = {
    "cover": "covering", "monthly_cov": "covering",
    "nkd": "sowing", "reap": "reaping", "protect": "protecting",
}


def _global_filter_active() -> bool:
    """True when any of the global filter controls is set."""
    return bool(
        str(st.session_state.get("flt_symbol", "") or "").strip()
        or st.session_state.get("flt_right")
        or st.session_state.get("flt_state")
        or st.session_state.get("flt_itm")
    )


def apply_global_filter(df: pd.DataFrame, *, use_state: bool = True) -> pd.DataFrame:
    """Apply the global filter (symbol prefix + C/P + state + ITM) to *df*.

    Only dimensions present as columns take effect; ``use_state=False`` skips the
    state column (order tables carry no ``state`` column — they are gated by
    :func:`_order_table_visible` instead).
    """
    if df is None or df.empty:
        return df
    sym     = str(st.session_state.get("flt_symbol", "") or "").strip().upper()
    rights  = st.session_state.get("flt_right") or []
    states  = st.session_state.get("flt_state") or []
    itm_only = bool(st.session_state.get("flt_itm", False))
    out = df
    if sym and "symbol" in out.columns:
        out = out[out["symbol"].astype(str).str.upper() == sym]
    if rights and "right" in out.columns:
        out = out[out["right"].isin(rights)]
    if use_state and states and "state" in out.columns:
        out = out[out["state"].isin(states)]
    if itm_only and "underlying_px" in out.columns:
        _mask = _itm_mask_vec(out)
        if _mask:
            out = out[pd.Series(_mask, index=out.index)]
    return out.reset_index(drop=True)


def _order_table_visible(table_key: str) -> bool:
    """Order tables have no per-row state; hide a table when the active State
    filter excludes the state that table represents."""
    states = st.session_state.get("flt_state") or []
    if not states:
        return True
    return _ORDER_TABLE_STATE.get(table_key) in states


def _clear_global_filter() -> None:
    """Reset every global-filter widget. Used as the Clear button's on_click.
    Assigns explicit cleared values (NOT pop): a callback write is authoritative
    over the widget's frontend-submitted value, so the selectbox/multiselects
    actually clear. pop() leaves them repopulated from the cached frontend value
    on the rerun. Assigning a widget key is legal here because callbacks run
    before the widgets are instantiated this run."""
    st.session_state["flt_symbol"] = ""
    st.session_state["flt_right"] = []
    st.session_state["flt_state"] = []
    st.session_state["flt_itm"] = False


def _clear_positions_filter() -> None:
    """Reset the Positions-only filter widgets (secType / DTE / Weekly only).
    Symbol / State / ITM are cleared from the global Filter bar's Clear.
    Assigns explicit defaults (see _clear_global_filter on why not pop())."""
    st.session_state["pf_sectype"] = "ALL"
    st.session_state["pf_dte_sel"] = "ALL"
    st.session_state["pf_weekly_only"] = False


@st.cache_data(ttl=60, show_spinner=False)
def _symbol_universe_static() -> list[str]:
    """Tradeable symbols from suggested-order pickles + flex trade history.
    Cached (60s) — these only change when the derive / flex pipelines run."""
    syms: set[str] = set()
    for _name in ("df_cov.pkl", "df_nkd.pkl", "df_reap.pkl", "df_protect.pkl"):
        _p = _DATA_DIR / _name
        if _p.exists():
            try:
                _d = pd.read_pickle(_p)
                if "symbol" in _d.columns:
                    syms |= set(_d["symbol"].dropna().astype(str))
            except Exception:
                pass
    _ft = _MASTER_DIR / "flex_trades.pkl"
    if _ft.exists():
        try:
            _d = pd.read_pickle(_ft)
            _col = "underlyingSymbol" if "underlyingSymbol" in _d.columns else "symbol"
            if _col in _d.columns:
                syms |= set(_d[_col].dropna().astype(str))
        except Exception:
            pass
    return sorted({s.strip().upper() for s in syms if s and str(s).strip()})


def _filter_symbol_options() -> list[str]:
    """Every symbol any panel could show: live positions + open orders +
    suggested orders + traded history. Populates the global Symbol dropdown."""
    syms: set[str] = set(_symbol_universe_static())
    try:
        _snap = client.snapshot()
        for _df in (_snap.positions, _snap.orders):
            if _df is not None and not _df.empty and "symbol" in _df.columns:
                syms |= set(_df["symbol"].dropna().astype(str))
    except Exception:
        pass
    return sorted({s.strip().upper() for s in syms if s and str(s).strip()})


def render_filter_bar() -> None:
    """Persistent global filter row — the single control set for all panels."""
    _scope = (
        "Global filter — narrows every panel below: Positions, Open Orders, "
        "Cover / Sow / Reap / Protect, Gaps, Trade Analysis, Deep-Dive. "
        "Empty = show everything."
    )
    c_lbl, c_sym, c_right, c_state, c_itm, c_clr = st.columns([0.9, 3, 1.4, 2.8, 1.1, 1])
    c_lbl.markdown(
        "<div style='padding-top:6px; font-weight:700; color:#D97757;'>🔎 Filter</div>",
        unsafe_allow_html=True,
    )
    # Exact-match dropdown (type-to-search) over every filterable symbol —
    # live positions + open orders + suggested orders + traded history.
    _sym_opts = [""] + _filter_symbol_options()
    # Migrating from the old free-text box: drop a stale value not in the list,
    # else st.selectbox raises on an out-of-range session value.
    if st.session_state.get("flt_symbol") not in _sym_opts:
        st.session_state.pop("flt_symbol", None)
    c_sym.selectbox(
        "Symbol", _sym_opts, key="flt_symbol",
        format_func=lambda s: s or "🔍  All symbols",
        label_visibility="collapsed", help=_scope,
    )
    c_right.multiselect(
        "C/P", ["C", "P"], key="flt_right", placeholder="C / P",
        label_visibility="collapsed",
    )
    c_state.multiselect(
        "State", _FILTER_STATES, key="flt_state", placeholder="State…",
        label_visibility="collapsed",
    )
    c_itm.checkbox("ITM only", key="flt_itm")
    # Clear via on_click callback (runs BEFORE widgets re-instantiate next run).
    # Popping flt_symbol in the button return-branch + st.rerun() is unreliable
    # for st.text_input — the frontend re-submits its cached value, so the Symbol
    # box stays filled. A callback resets the widget keys cleanly.
    c_clr.button(
        "✕ Clear", key="flt_clear", width="stretch", help="Clear all filters",
        on_click=_clear_global_filter,
    )


def _sub_env() -> dict[str, str]:
    """Build subprocess environment with project root on PYTHONPATH.

    Scripts in src/ use 'from src.X import ...' which requires the project root
    on sys.path. Python adds the script's own directory to sys.path[0], not the root,
    so we pass PYTHONPATH explicitly.
    """
    root = str(_here())
    existing = os.environ.get("PYTHONPATH", "")
    pythonpath = f"{root}{os.pathsep}{existing}" if existing else root
    return {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
            "PYTHONPATH": pythonpath, "PYTHONUNBUFFERED": "1"}


def _derive_progress() -> tuple[float, str, list[str]]:
    """Parse derive_progress.log → (progress 0–1, phase label, last 4 output lines).

    Scans the full log for phase markers (not just tail) so early markers
    aren't lost when the log grows large during long qualification loops.
    """
    if not _DERIVE_LOG.exists():
        return 0.0, "Initialising…", []
    try:
        text = _DERIVE_LOG.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return 0.0, "", []
    lines = text.splitlines()
    pct = 0
    label = "Initialising…"
    for marker, p in _DERIVE_PHASES:
        if any(marker in ln for ln in lines):
            pct = p
            label = marker.replace("===", "").strip().title()
    tail = [ln for ln in lines[-6:] if ln.strip() and "===" not in ln][-4:]
    return pct / 100.0, label, tail


_TQDM_BAR_RE      = re.compile(r"^(.+?):\s+\d+%\|")
_TQDM_PCT_RE      = re.compile(r":\s+(\d+)%\|")
_RICH_PROG_RE     = re.compile(r"^(.+?)\s+[━─\-]{3,}\s+(\d+)%")
_BACKTEST_PROG_RE = re.compile(r"\|\s+(Backtest):\s+\d+/\d+")
_ANSI_RE          = re.compile(r"\x1b\[[0-9;]*[mGKHF]")


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
        m = (_TQDM_BAR_RE.match(line)
             or _RICH_PROG_RE.match(_strip_ansi(line))
             or _BACKTEST_PROG_RE.search(line))
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
    """Parse ohlc_progress.log → (progress 0–1, latest bar label).

    Handles both rich non-TTY format ("desc ━━━ 87% SYM") and
    keyword milestones written directly by ohlc.py.
    """
    lines = _ohlc_log_lines(30)
    if not lines:
        return 0.01, "Initialising…"
    full = "\n".join(lines)
    if "OHLC UPDATE COMPLETE" in full:
        return 1.0, "OHLC update complete"
    last_pct = 0.0
    last_label = ""
    for ln in lines:
        stripped = _strip_ansi(ln)
        # Rich non-TTY format: "Fetching OHLC (yfinance) ━━━━━━━━━━ 87% IWDA"
        m = _RICH_PROG_RE.search(stripped)
        if m:
            last_label = m.group(1).strip()
            last_pct = int(m.group(2)) / 100.0
            continue
        # tqdm format (legacy fallback)
        m_label = _TQDM_BAR_RE.match(stripped)
        m_pct   = _TQDM_PCT_RE.search(stripped)
        if m_label and m_pct:
            last_label = m_label.group(1).strip()
            last_pct   = int(m_pct.group(1)) / 100.0
            continue
        # Keyword milestones from ohlc.py log_fh.write() calls
        if "yfinance:" in stripped and "OK," in stripped:
            last_pct = max(last_pct, 0.70)
        elif "After .L retry:" in stripped:
            last_pct = max(last_pct, 0.75)
        elif "IBKR fallback needed:" in stripped:
            last_pct = max(last_pct, 0.80)
        elif "Date range :" in stripped:
            last_pct = max(last_pct, 0.10)
        elif "Symbols :" in stripped:
            last_pct = max(last_pct, 0.05)
    if not last_pct and any(ln.strip() for ln in lines):
        last_pct = 0.30
    return max(last_pct, 0.01), last_label or "Fetching OHLCs…"


def _render_log_expander(
    label: str, log_path: Path, *, expanded: bool = False, lines_fn=None
) -> None:
    """Render a collapsible st.expander containing the full text of *log_path*.

    Pass *lines_fn* (a zero-arg callable returning list[str]) to display
    pre-processed / deduplicated lines instead of the raw file content.
    """
    with st.expander(label, expanded=expanded):
        if log_path.exists():
            try:
                if lines_fn is not None:
                    st.code("\n".join(lines_fn()), language=None)
                else:
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


@st.fragment(run_every=2)
def header() -> None:
    """Compact status bar — title, connection state, port/cid, position count."""
    # One full-app rerun after the FIRST bootstrap so the main page picks up positions.
    # Do NOT reset on later disconnects — a flapping connection would otherwise app-rerun
    # (full-page dim) every ~2 s. The 2 s header + 10 s fragments keep status/positions fresh.
    if client._bootstrapped and not st.session_state.get("_initial_app_rerun_done"):
        st.session_state["_initial_app_rerun_done"] = True
        st.rerun(scope="app")

    snap = client.snapshot()
    acct = _selected_account()
    positions_filt = _filter_positions(snap.positions, acct)

    if client.is_frozen():
        st_html = "🧊 <b>FROZEN</b>"
    elif snap.connected:
        st_html = "🟢 <b>LIVE</b>"
    else:
        st_html = "🔴 <b>DISC.</b>"

    pos_n = len(positions_filt)

    st.markdown(
        f'<div class="hdr-bar">'
        f'<span class="hdr-title">IB Monitor</span>'
        f'&nbsp;&nbsp;{st_html}&nbsp;&nbsp;'
        f'<span class="hdr-cur">{settings.currency}</span>'
        f'<br>'
        f'<span class="hdr-item">port:&nbsp;{settings.ib_port}&nbsp;cid:&nbsp;{settings.ib_client_id}</span>'
        f'&nbsp;&bull;&nbsp;<span class="hdr-item">pos:&nbsp;{pos_n}</span>'
        f'</div>',
        unsafe_allow_html=True,
    )


@st.fragment(run_every=10)
def kpi_strip() -> None:
    snap = client.snapshot()
    acct = _selected_account()
    positions = _filter_positions(snap.positions, acct)
    k = account_kpis(snap, min_cushion=settings.min_cushion, account=acct)
    g = greek_dollar_sums(positions, snap.tickers)
    av = _select_account_values(snap, acct)
    from decimal import Decimal as _D
    opt_val    = float(av.get("OptionMarketValue",  _D("0")) or 0)
    stock_val  = float(av.get("StockMarketValue",   _D("0")) or 0)
    cash_val   = float(av.get("TotalCashBalance",   _D("0")) or 0)
    dividend   = float(av.get("AccruedDividend",    _D("0")) or 0)
    init_mg    = float(av.get("InitMarginReq",      _D("0")) or 0)
    avail_fnds = float(av.get("AvailableFunds",     _D("0")) or 0)
    buy_pow    = float(av.get("BuyingPower",         _D("0")) or 0)
    leverage_s = float(av.get("Leverage-S",          _D("0")) or 0)

    # Warm-start NLV from flex_nav.pkl while IBKR account values are still streaming.
    # Shows ~$X,XXX,XXX (tilde prefix) until all configured accounts have streamed their
    # values — prevents showing a partial US-only sum before SG data arrives in ALL mode.
    _expected_accts = [a for a in (_US, _SG) if a]
    _streaming = set(snap.account_values.keys())
    if acct:
        _all_accounts_ready = acct in _streaming
    else:
        _all_accounts_ready = not _expected_accts or _streaming.issuperset(_expected_accts)
    _display_nlv = k["nlv"]
    _nlv_cached = False
    if _all_accounts_ready and _display_nlv > 0:
        global _LIVE_NAV_TODAY
        _LIVE_NAV_TODAY = {"date": pd.Timestamp.today().normalize(), "nlv": _display_nlv}
    else:
        try:
            _df_nav = pd.read_pickle(_MASTER_DIR / "flex_nav.pkl")
            if not _df_nav.empty and "total" in _df_nav.columns:
                _display_nlv = float(_df_nav["total"].iloc[-1])
                _nlv_cached = True
        except Exception:
            pass

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

    # MTD Interest from flex_cash.pkl
    _mtd_interest = 0.0
    _cash_pkl = _MASTER_DIR / "flex_cash.pkl"
    if _cash_pkl.exists():
        try:
            _df_cash = pd.read_pickle(_cash_pkl)
            if not _df_cash.empty and {"type", "amount", "date"}.issubset(_df_cash.columns):
                _today = pd.Timestamp.today()
                _mtd_mask = (
                    _df_cash["type"].str.contains("Interest", case=False, na=False)
                    & (pd.to_datetime(_df_cash["date"]).dt.year  == _today.year)
                    & (pd.to_datetime(_df_cash["date"]).dt.month == _today.month)
                )
                _mtd_interest = float(_df_cash.loc[_mtd_mask, "amount"].sum())
        except Exception:
            pass

    min_c_pct = f"{settings.min_cushion:.0%}"

    def _lbl(text: str, tip: str) -> str:
        safe_tip = tip.replace('"', '&quot;')
        return f'<span class="kpi-lbl" title="{safe_tip}">{text}&nbsp;<span class="kpi-help">?</span></span>'

    def _lbl_sub(text: str, tip: str, sub: str) -> str:
        safe_tip = tip.replace('"', '&quot;')
        return (
            f'<div class="kpi-lbl-stack">'
            f'<span class="kpi-lbl" title="{safe_tip}">{text}&nbsp;<span class="kpi-help">?</span></span>'
            f'<span class="kpi-sub">{sub}</span>'
            f'</div>'
        )

    def _cell(lbl_html: str, val: str, breach: bool = False) -> str:
        vcls = ' class="kpi-val kpi-breach"' if breach else ' class="kpi-val"'
        return f'<div class="kpi-cell">{lbl_html}<span{vcls}>{val}</span></div>'

    _dr_cell1 = _cell(
        _lbl(_dr_r1_lbl, _dr_r1_tip) if _dr_r1_lbl else "",
        _dr_r1_val,
    )

    # Row 1: NLV | Stock Value | Opt Value | Cash
    row1 = [
        _cell(_lbl("NLV", "Net Liquidation Value: total portfolio value including cash, stocks and options at current market prices."), ("~" + money(_display_nlv)) if _nlv_cached else money(_display_nlv)),
        _cell(_lbl("Stock Value", "Total market value of all stock positions at current prices (StockMarketValue)."), money(stock_val)),
        _cell(_lbl("Opt Value", "Option Market Value: total mark-to-market value of all option positions."), money(opt_val)),
        _cell(_lbl("Cash", "Total cash balance across all currencies (TotalCashBalance)."), money(cash_val)),
    ]
    # Row 2: ΣΔ | ΣΓ | ΣΘ | ΣΝ
    row2 = [
        _cell(_lbl("&#x3A3;&#x394; ($)", "Portfolio Dollar Delta: P&amp;L change for a 1-point broad market move."), signed_money(g["delta_$"])),
        _cell(_lbl("&#x3A3;&#x3B3; ($)", "Portfolio Dollar Gamma: rate of change of dollar delta per 1-point move."), signed_money(g["gamma_$"])),
        _cell(_lbl("&#x3A3;&#x398; ($/d)", "Portfolio Dollar Theta: daily time decay across all options in dollars. Positive = net premium seller collecting theta."), signed_money(g["theta_$"])),
        _cell(_lbl("&#x3A3;&#x3BD; ($)", "Portfolio Dollar Vega: P&amp;L sensitivity to a 1% rise in implied volatility across all options."), signed_money(g["vega_$"])),
    ]
    # Row 3: US+SG drop | US drop | Cushion | Dividend
    row3 = [
        _dr_cell1,
        _cell(_lbl(_dr_r4_lbl, _dr_r4_tip) if _dr_r4_lbl else _lbl("US drop", ""), _dr_r4_val if _dr_r4_lbl else "—"),
        _cell(_lbl_sub("Cushion", f"Margin cushion = Excess Liquidity ÷ NLV. Alert threshold: {min_c_pct}. Breach turns red.", f"min {min_c_pct}"), pct(k["cushion"]), k["cushion_breach"]),
        _cell(_lbl("Dividend", "Accrued dividends not yet received (AccruedDividend)."), money(dividend)),
    ]
    # Row 4: Init Margin | Maint Margin | Leverage | MTD Interest
    row4 = [
        _cell(_lbl("Init Margin", "Initial Margin Requirement: equity needed to open current positions (InitMarginReq)."), money(init_mg)),
        _cell(_lbl("Maint Margin", "Maintenance Margin Requirement: minimum equity you must hold to keep current positions open."), money(k["maint_margin"])),
        _cell(_lbl("Leverage-S", "Short leverage: portfolio exposure ÷ equity. Higher = more leveraged (Leverage-S)."), f"{leverage_s:.2f}×"),
        _cell(_lbl("MTD Interest", "Month-to-date interest income from flex_cash.pkl (current calendar month)."), signed_money(_mtd_interest)),
    ]
    # Row 5: Buying Power | Avail Funds | Excess Liq
    row5 = [
        _cell(_lbl("Buying Power", "Maximum new position size without adding more funds (BuyingPower)."), money(buy_pow)),
        _cell(_lbl("Avail Funds", "Funds available for trading above the maintenance margin (AvailableFunds)."), money(avail_fnds)),
        _cell(_lbl("Excess Liq", "Excess Liquidity: funds available above the maintenance margin requirement."), money(k["excess_liquidity"])),
    ]

    all_rows = [row1, row2, row3, row4, row5]

    html_parts = ['<div class="kpi-band">']
    for i, row_cells in enumerate(all_rows):
        rc = "kpi-row-a" if i % 2 == 0 else "kpi-row-b"
        html_parts.append(f'<div class="kpi-row {rc}">{"".join(row_cells)}</div>')
    html_parts.append('</div>')
    st.markdown("\n".join(html_parts), unsafe_allow_html=True)

    _n_ohlc, _n_weekly = _cached_ohlc_stats()
    if _n_ohlc:
        st.caption(f"{_n_ohlc} symbols in OHLC store — {_n_weekly} weekly S&P 500")

    # Detect OHLC / auto-trades subprocess completion from a ticking fragment so
    # render_analysis (no run_every) doesn't need user interaction to show status.
    _kpi_ohlc_p = st.session_state.get("ohlc_proc")
    if _kpi_ohlc_p is not None and _kpi_ohlc_p.poll() is not None:
        _capture_exit(_kpi_ohlc_p, "_ohlc_exit")
        st.session_state.pop("ohlc_proc", None)
        st.rerun(scope="app")
    _kpi_trades_p = st.session_state.get("trades_proc")
    if _kpi_trades_p is not None and _kpi_trades_p.poll() is not None:
        _capture_exit(_kpi_trades_p, "_trades_exit")
        st.session_state.pop("trades_proc", None)
        st.rerun(scope="app")


# Content-only (no run_every): the 10 s refresh timer lives on the parent
# _master_orders fragment. Putting the timer here too makes the inner fragment
# tick independently of the master banner — so it re-renders (and "expands back")
# even after the banner is collapsed. Keep the timer on the parent only.
def render_orders() -> None:
    snap = client.snapshot()
    acct = _selected_account()
    _ok = account_kpis(snap, min_cushion=settings.min_cushion, account=acct)

    # ── Auto-delete stale order pickles (MAX_FILE_AGE check) ──────────────────
    try:
        _yml = yaml.safe_load(_CFG_PATH.read_text(encoding="utf-8")) or {}
        _max_age_days: int = int(_yml.get("MAX_FILE_AGE", 1))
    except Exception:
        _max_age_days = 1
    if _max_age_days > 0:
        _max_age_secs = _max_age_days * 86400
        for _stale_name in ("df_cov.pkl", "df_nkd.pkl", "df_reap.pkl", "df_protect.pkl"):
            _stale_p = _DATA_DIR / _stale_name
            if _stale_p.exists():
                _age_secs = (datetime.now() - datetime.fromtimestamp(_stale_p.stat().st_mtime)).total_seconds()
                if _age_secs > _max_age_secs:
                    try:
                        _stale_p.unlink()
                        logger.info("Auto-deleted stale {} (age {:.1f}h > {}d)",
                                    _stale_name, _age_secs / 3600, _max_age_days)
                    except Exception:
                        pass

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

    _raw_cov          = _load_pkl("df_cov.pkl")
    _raw_monthly_cov  = _load_pkl("df_monthly_cov.pkl")
    _raw_nkd          = _load_pkl("df_nkd.pkl")
    _raw_reap         = _load_pkl("df_reap.pkl")

    _cov_summary: dict = {}
    try:
        _cov_sum_path = _DATA_DIR / "cover_summary.json"
        if _cov_sum_path.exists():
            import json as _jcs
            _cov_summary = _jcs.loads(_cov_sum_path.read_text(encoding="utf-8"))
    except Exception:
        pass

    _hv_fallback_set: set[str] = set()
    try:
        _bld_sum_path = _DATA_DIR / "build_summary.json"
        if _bld_sum_path.exists():
            import json as _jbs
            _bld = _jbs.loads(_bld_sum_path.read_text(encoding="utf-8"))
            _hv_fallback_set = set(_bld.get("hv_fallback", {}).get("symbols", []))
    except Exception:
        pass
    try:
        _yml_protect = (yaml.safe_load(_CFG_PATH.read_text(encoding="utf-8")) or {}).get(
            "PROTECT_ME", False
        )
    except Exception:
        _yml_protect = False
    _raw_prot = _load_pkl("df_protect.pkl") if _yml_protect else pd.DataFrame()

    # Order tables are driven by the single global filter bar (symbol + C/P + ITM).
    # State is handled per-table via _order_table_visible() since order pickles
    # carry no per-row state column.
    def _ord_filt(df: pd.DataFrame) -> pd.DataFrame:
        return apply_global_filter(df, use_state=False)

    df_cov         = _ord_filt(_raw_cov)         if _order_table_visible("cover")       else _raw_cov.iloc[0:0]
    df_monthly_cov = _ord_filt(_raw_monthly_cov) if _order_table_visible("monthly_cov") else _raw_monthly_cov.iloc[0:0]
    df_nkd         = _ord_filt(_raw_nkd)         if _order_table_visible("nkd")         else _raw_nkd.iloc[0:0]
    df_reap_pkl    = _ord_filt(_raw_reap)        if _order_table_visible("reap")        else _raw_reap.iloc[0:0]
    df_prot        = _ord_filt(_raw_prot)        if _order_table_visible("protect")     else _raw_prot.iloc[0:0]

    # ── Open Orders ─────────────────────────────────────────────────────────
    orders = snap.orders
    if acct and not orders.empty and "account" in orders.columns:
        orders = orders[orders["account"] == acct].reset_index(drop=True)
    orders = _ord_filt(orders)
    _n_open = len(orders)
    _open_label = f"🗂 Open Orders — {_n_open}" if _n_open else "🗂 Open Orders"
    with st.expander(_open_label, expanded=False):
        if orders.empty:
            st.info("No open orders." if not _global_filter_active() else "No open orders match filter.")
        else:
            cols_show = [
                "symbol", "secType", "right", "strike", "expiry",
                "action", "qty", "filled", "remaining",
                "orderType", "lmtPrice", "status", "state",
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
                    "state":     st.column_config.TextColumn("State",
                        help="Classified role: covering, sowing, protecting, reaping, de-orphaning, straddling"),
                },
            )

    # ── Suggested Orders ─────────────────────────────────────────────────────
    st.markdown("##### Suggested Orders")
    st.caption("Generated by derive.py — click **Generate Orders** to refresh.")

    # Cover
    n_cov = len(df_cov)
    cov_reward = _exp_cover_reward(df_cov)
    _cov_hv = int(df_cov["symbol"].isin(_hv_fallback_set).sum()) if not df_cov.empty else 0
    _cov_hv_sfx = f" (used hv for {_cov_hv} symbols)" if _cov_hv else ""
    cov_label = (
        f"📈 Cover — {n_cov} orders · ${cov_reward:,.0f} expected if called{_cov_hv_sfx}"
        if n_cov else "📈 Cover — 0 orders"
    )
    with st.expander(cov_label, expanded=n_cov > 0):
        _cov_processed = _cov_summary.get("processed", 0)
        _cov_generated = _cov_summary.get("generated", 0)
        _cov_estimated = _cov_summary.get("estimated_prices", 0)
        _cov_missed = max(0, _cov_processed - _cov_generated)
        if _cov_missed > 0:
            st.caption(
                f"⚠ {_cov_missed} of {_cov_processed} uncovered/exposed position(s) "
                "have no cover order — check qualify / strike filter / market hours."
            )
        if _cov_estimated > 0:
            st.caption(
                f"ℹ {_cov_estimated} order(s) use a theoretical price estimate "
                "(no live market data — rerun Generate during market hours for live prices)."
            )
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
                    "qty":      st.column_config.NumberColumn("Qty",         format="%d"),
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

    # Monthly Covered Calls
    n_mc = len(df_monthly_cov)
    mc_reward = _exp_cover_reward(df_monthly_cov)
    mc_label = (
        f"📅 Monthly CC — {n_mc} orders · ${mc_reward:,.0f} profit if called"
        if n_mc else "📅 Monthly CC — 0 orders"
    )
    with st.expander(mc_label, expanded=n_mc > 0):
        if _raw_monthly_cov.empty:
            st.info(
                "No monthly CC suggestions — run Generate Orders, or no monthly-only "
                "uncovered positions, or symbol_categories.pkl missing (run Identify Weeklies)."
            )
        elif df_monthly_cov.empty:
            st.info("No monthly CC orders match the current filter.")
        else:
            mc_cols = ["symbol", "right", "strike", "expiry", "dte", "qty",
                       "undPrice", "sdev", "avgCost", "price", "xPrice", "margin"]
            st.dataframe(
                _banded(df_monthly_cov[[c for c in mc_cols if c in df_monthly_cov.columns]].copy()),
                hide_index=True, width="stretch",
                column_config={
                    "qty":      st.column_config.NumberColumn("Qty",         format="%d"),
                    "strike":   st.column_config.NumberColumn("Strike",      format="%.1f"),
                    "undPrice": st.column_config.NumberColumn("Und Px",      format="$%,.2f"),
                    "sdev":     st.column_config.NumberColumn("1σ Move",     format="$%,.2f",
                        help="1-sigma expected underlying move = undPrice × IV × √(DTE/365)"),
                    "avgCost":  st.column_config.NumberColumn("Avg Cost",    format="$%,.2f"),
                    "price":    st.column_config.NumberColumn("Mkt Px",      format="$%,.2f"),
                    "xPrice":   st.column_config.NumberColumn("Expected Px", format="$%,.2f",
                        help="Target execution price = mkt px × COVXPMULT. Strike ≥ avgCost → breakeven or better if called."),
                    "margin":   st.column_config.NumberColumn("Margin",      format="$%,.0f"),
                },
            )

    # Sow (Nakeds) — optional DEPLOY-only filter (synthetic OR trade-history DEPLOY, plus REFINE overrides)
    import json as _sow_json  # noqa: PLC0415
    from src.backtest.score import score_from_trades as _sft  # noqa: PLC0415
    from src.backtest.synthetic import BACKTEST_RESULTS_PATH as _BT_RES  # noqa: PLC0415
    _deploy_only = st.session_state.get("ord_sow_deploy_only", False)
    _df_nkd_disp = df_nkd.copy()
    _syn_deploy_syms: set[str] = set()
    _trade_deploy_syms: set[str] = set()
    _refine_override_syms: set[str] = set()
    if _deploy_only:
        try:
            if _BT_RES.exists():
                _bt_verd = pd.read_pickle(_BT_RES)
                _syn_deploy_syms = set(_bt_verd.loc[_bt_verd["verdict"] == "DEPLOY", "symbol"].dropna())
            _flex_pkl = Path("data/master/flex_trades.pkl")
            if _flex_pkl.exists():
                _flex_df = pd.read_pickle(_flex_pkl)
                for _sym in df_nkd["symbol"].dropna().unique():
                    _bs = _sft(_flex_df, _sym, since=score_since())
                    if _bs.total_trades > 0 and _bs.verdict == "DEPLOY":
                        _trade_deploy_syms.add(_sym)
            _ovr_path = Path("data/symbol_overrides.json")
            if _ovr_path.exists():
                _ovr_data = _sow_json.loads(_ovr_path.read_text(encoding="utf-8"))
                _refine_override_syms = set(_ovr_data.get("VIRGIN_PUT_STD_MULT", {}).keys())
            _deploy_syms = _syn_deploy_syms | _trade_deploy_syms
            _allowed = _deploy_syms | _refine_override_syms
            if _allowed:
                _df_nkd_disp = _df_nkd_disp[_df_nkd_disp["symbol"].isin(_allowed)]
        except Exception:
            pass

    n_nkd = len(_df_nkd_disp)
    nkd_premium = _exp_premium(_df_nkd_disp)
    _sow_hv = int(_df_nkd_disp["symbol"].isin(_hv_fallback_set).sum()) if not _df_nkd_disp.empty else 0
    _sow_hv_sfx = f" (used hv for {_sow_hv} symbols)" if _sow_hv else ""
    nkd_label = (
        f"🌱 Sow — {n_nkd} orders · ${nkd_premium:,.0f} expected premium{_sow_hv_sfx}"
        if n_nkd else "🌱 Sow — 0 orders"
    )
    with st.expander(nkd_label, expanded=n_nkd > 0):
        _deploy_syms_all = _syn_deploy_syms | _trade_deploy_syms
        if st.session_state.get("ord_sow_deploy_only", False):
            if _deploy_syms_all or _refine_override_syms:
                st.caption(
                    f"Filter: {len(_syn_deploy_syms)} syn-DEPLOY"
                    f" + {len(_trade_deploy_syms)} trade-DEPLOY"
                    f" + {len(_refine_override_syms)} REFINE-override"
                )
            else:
                st.caption("No backtest results — all symbols shown")
        else:
            st.caption("Filter: off — all symbols shown")
        if _raw_nkd.empty:
            _skip_path = _DATA_DIR / "sow_skip.json"
            _skip_reason: dict = {}
            try:
                if _skip_path.exists():
                    import json as _j
                    _skip_reason = _j.loads(_skip_path.read_text(encoding="utf-8"))
            except Exception:
                pass
            if _skip_reason.get("reason") == "cushion":
                _act = _skip_reason.get("actual", 0)
                _req = _skip_reason.get("required", settings.min_cushion)
                st.info(
                    f"No sow orders — last Generate skipped sow because cushion was "
                    f"**{_act:.1%}** < MINCUSHION **{_req:.1%}**. "
                    "Free up margin or lower MINCUSHION in config and rerun Generate."
                )
            elif _ok["cushion_breach"]:
                st.info(
                    f"No sow suggestions — cushion {_ok['cushion']:.1%} < MINCUSHION "
                    f"{settings.min_cushion:.1%}. Free up margin or lower MINCUSHION in config."
                )
            else:
                st.info("No sow suggestions — run Generate Orders or no virgin/orphaned positions.")
        elif _df_nkd_disp.empty:
            st.info("No sow orders match the current filter.")
        else:
            n_cols = ["symbol", "right", "strike", "expiry", "dte", "qty",
                      "undPrice", "vy", "sdev", "price", "xPrice", "margin"]
            st.dataframe(
                _banded(_df_nkd_disp[[c for c in n_cols if c in _df_nkd_disp.columns]].copy()),
                hide_index=True, width="stretch",
                column_config={
                    "qty":      st.column_config.NumberColumn("Qty",         format="%d"),
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

    # REFINE Overrides — sits directly under Sow (drives sow σ per symbol)
    render_refine_overrides()

    # Reap
    n_reap = len(df_reap_pkl)
    reap_cost = _exp_premium(df_reap_pkl)
    _reap_hv = int(df_reap_pkl["symbol"].isin(_hv_fallback_set).sum()) if not df_reap_pkl.empty else 0
    _reap_hv_sfx = f" (used hv for {_reap_hv} symbols)" if _reap_hv else ""
    reap_label = (
        f"🌾 Reap — {n_reap} orders · ${reap_cost:,.0f} to close{_reap_hv_sfx}"
        if n_reap else "🌾 Reap — 0 orders"
    )
    with st.expander(reap_label, expanded=n_reap > 0):
        if _raw_reap.empty:
            st.info("No reap suggestions - run Generate Orders or nothing to reap")
        elif df_reap_pkl.empty:
            st.info("No reap orders match the current filter.")
        else:
            r_cols = ["symbol", "right", "strike", "expiry", "dte", "qty",
                      "undPrice", "avgCost", "optPrice", "xPrice"]
            st.dataframe(
                _banded(df_reap_pkl[[c for c in r_cols if c in df_reap_pkl.columns]].copy()),
                hide_index=True, width="stretch",
                column_config={
                    "qty":      st.column_config.NumberColumn("Qty",         format="%d"),
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
    with st.expander(prot_label, expanded=n_prot > 0):
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
                    "qty":        st.column_config.NumberColumn("Qty",         format="%d"),
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


def _sel_all_ovr() -> None:
    _nb = st.session_state["_ref_ovr_base"].copy()
    _nb["Use"] = True
    st.session_state["_ref_ovr_base"] = _nb
    st.session_state.pop("sym_override_editor", None)


def _clr_all_ovr() -> None:
    _nb = st.session_state["_ref_ovr_base"].copy()
    _nb["Use"] = False
    st.session_state["_ref_ovr_base"] = _nb
    st.session_state.pop("sym_override_editor", None)


def _apply_all_ovr() -> None:
    _val = round(float(st.session_state.get("ovr_all_val", 2.0)), 2)
    _nb = st.session_state["_ref_ovr_base"].copy()
    _nb["Override σ"] = _val
    st.session_state["_ref_ovr_base"] = _nb
    st.session_state.pop("sym_override_editor", None)


def render_refine_overrides() -> None:
    """Per-symbol VIRGIN_PUT_STD_MULT overrides for REFINE symbols.

    Plain st.expander (matches the Sow / Reap / Protect siblings) rendered inside
    render_orders directly under Sow. Like the siblings it passes a constant
    `expanded=` so Streamlit preserves the user's open/close toggle across the 10 s
    timer (the old popped `_ref_ovr_expanded` flag was what forced it to collapse).

    Saves to data/symbol_overrides.json; picked up by derive.py on next run.
    """
    import json as _json

    from src.backtest.synthetic import BACKTEST_RESULTS_PATH as _BT_RES

    _OVERRIDE_PATH = Path("data/symbol_overrides.json")

    if not _BT_RES.exists():
        with st.expander("🔧 REFINE Overrides — no backtest results", expanded=False, key="exp_refine_overrides"):
            st.info("No backtest results — run Backtest to see REFINE symbols.")
        return

    # Rebuild base DF when backtest file changes or after a save
    _mtime = _BT_RES.stat().st_mtime
    if (
        st.session_state.get("_ref_ovr_mtime") != _mtime
        or "_ref_ovr_base" not in st.session_state
    ):
        _bt = pd.read_pickle(_BT_RES)
        _refine = (
            _bt[_bt["verdict"] == "REFINE"][
                ["symbol", "csp_win_rate", "csp_pf", "put_std_mult_opt"]
            ]
            .sort_values("symbol")
            .reset_index(drop=True)
            .copy()
        )

        # Suggested σ = max(grid-optimal, config) — never suggest tighter than global setting
        from src.build import load_config as _load_cfg
        _cfg_put_mult = float(_load_cfg("SNP").get("VIRGIN_PUT_STD_MULT", 1.0))
        _refine["put_std_mult_opt"] = _refine["put_std_mult_opt"].apply(
            lambda v: max(float(v), _cfg_put_mult)
        )

        _saved: dict[str, float] = {}
        if _OVERRIDE_PATH.exists():
            try:
                _saved = _json.loads(
                    _OVERRIDE_PATH.read_text(encoding="utf-8")
                ).get("VIRGIN_PUT_STD_MULT", {})
            except Exception:
                pass

        _refine["Use"] = _refine["symbol"].isin(_saved)
        _refine["Override σ"] = _refine.apply(
            lambda r: float(_saved.get(r["symbol"], r["put_std_mult_opt"])), axis=1
        )
        _refine["csp_win_rate"] = (_refine["csp_win_rate"] * 100).round(1)
        st.session_state["_ref_ovr_base"] = (
            _refine.rename(
                columns={
                    "csp_win_rate": "CSP Win%",
                    "csp_pf": "CSP PF",
                    "put_std_mult_opt": "Suggested σ",
                }
            )[["Use", "symbol", "CSP Win%", "CSP PF", "Suggested σ", "Override σ"]]
            # Lowest Override σ first (ascending) — surfaces symbols whose override
            # still needs raising at the top.
            .sort_values("Override σ", ascending=True, kind="stable")
            .reset_index(drop=True)
        )
        st.session_state["_ref_ovr_mtime"] = _mtime

    _base = st.session_state["_ref_ovr_base"]
    _n_sym = len(_base)
    _n_active = int(_base["Use"].sum())
    _lbl = (
        f"🔧 REFINE Overrides — {_n_active} active · {_n_sym} REFINE symbols"
        if _n_sym
        else "🔧 REFINE Overrides — no REFINE symbols"
    )

    # Constant `expanded=` (never a popped flag) → Streamlit preserves the user's
    # open/close toggle across render_orders' 10 s reruns, exactly like the siblings.
    with st.expander(_lbl, expanded=False, key="exp_refine_overrides"):
        if _n_sym == 0:
            st.info("No REFINE symbols in current backtest results.")
            return

        if "_ref_ovr_saved_n" in st.session_state:
            _saved_n = st.session_state.pop("_ref_ovr_saved_n")
            st.toast(f"💾 Saved {_saved_n} override(s) → derive.py will use them next run")

        st.caption(
            "Per-symbol put-strike width override. "
            "**Override σ** replaces the global VIRGIN_PUT_STD_MULT for that symbol in the next derive run. "
            "Tick **Use** → **Save** → then run **Generate Orders** to apply. "
            "**Suggested σ** = max(grid-optimal, config) — never tighter than global setting."
        )

        # Select All | Change all overrides to: [value] ↵ | Clear All  (one row);
        # st.columns(vertical_alignment="center") centers each cell against the input.
        with st.container(key="ref_ovr_ctrls"):
            _sa_col, _lbl_col, _inp_col, _go_col, _ca_col, _sp_col = st.columns(
                [1.1, 1.7, 0.9, 0.5, 1.1, 1.4], vertical_alignment="center"
            )
            with _sa_col:
                st.button("☑ Select All", key="btn_sel_all_ovr", width="stretch",
                          on_click=_sel_all_ovr)
            with _lbl_col:
                # Fixed-height flex box centres the text vertically against the
                # ~2.5rem-tall input/buttons (CSS keyed off st.container doesn't
                # apply in this Streamlit build, so keep the alignment self-contained).
                st.markdown(
                    "<div style='display:flex; align-items:center; justify-content:flex-end; "
                    "height:2.5rem; white-space:nowrap; font-weight:600;'>"
                    "Change all overrides to:</div>",
                    unsafe_allow_html=True,
                )
            with _inp_col:
                st.number_input(
                    "Change all overrides to",
                    value=2.00, step=0.25, min_value=0.1, max_value=5.0,
                    key="ovr_all_val", label_visibility="collapsed",
                )
            with _go_col:
                st.button("↵", key="btn_apply_all_ovr", width="stretch",
                          help="Set every Override σ to this value",
                          on_click=_apply_all_ovr)
            with _ca_col:
                st.button("☐ Clear All", key="btn_clr_all_ovr", width="stretch",
                          on_click=_clr_all_ovr)

        _edited = st.data_editor(
            _base,
            hide_index=True,
            width="stretch",
            key="sym_override_editor",
            column_config={
                "Use": st.column_config.CheckboxColumn(
                    "Use", width="small",
                    help="Apply this override in the next Generate Orders run",
                ),
                "symbol": st.column_config.TextColumn(
                    "Symbol", disabled=True, width="small",
                ),
                "CSP Win%": st.column_config.NumberColumn(
                    "Win%", format="%.1f", disabled=True, width="small",
                    help="CSP win rate at global config σ",
                ),
                "CSP PF": st.column_config.NumberColumn(
                    "PF", format="%.3f", disabled=True, width="small",
                    help="CSP profit factor at global config σ",
                ),
                "Suggested σ": st.column_config.NumberColumn(
                    "Suggested σ", format="%.2f", disabled=True, width="small",
                    help="max(grid-optimal, config VIRGIN_PUT_STD_MULT). "
                    "Higher = further OTM (safer, less premium).",
                ),
                "Override σ": st.column_config.NumberColumn(
                    "Override σ", format="%.2f", min_value=0.1, max_value=5.0,
                    width="small",
                    help="Your per-symbol VIRGIN_PUT_STD_MULT. "
                    "Strike = undPrice − (Override σ × sdev).",
                ),
            },
        )

        _save_col, _info_col = st.columns([1, 3])
        with _save_col:
            if st.button("💾 Save Overrides", type="primary", key="btn_save_overrides"):
                _active_rows = _edited[_edited["Use"].astype(bool)]
                _new_map: dict[str, float] = {
                    row["symbol"]: round(float(row["Override σ"]), 2)
                    for _, row in _active_rows.iterrows()
                }
                _existing: dict = {}
                if _OVERRIDE_PATH.exists():
                    try:
                        _existing = _json.loads(
                            _OVERRIDE_PATH.read_text(encoding="utf-8")
                        )
                    except Exception:
                        pass
                _existing["VIRGIN_PUT_STD_MULT"] = _new_map
                _OVERRIDE_PATH.parent.mkdir(parents=True, exist_ok=True)
                _OVERRIDE_PATH.write_text(
                    _json.dumps(_existing, indent=2), encoding="utf-8"
                )
                # Clear cached base so next render reflects saved state
                st.session_state.pop("_ref_ovr_mtime", None)
                st.session_state.pop("_ref_ovr_base", None)
                st.session_state.pop("sym_override_editor", None)
                st.session_state["_ref_ovr_saved_n"] = len(_new_map)
                st.rerun(scope="fragment")
        with _info_col:
            if _n_active:
                _active_syms = list(_base.loc[_base["Use"], "symbol"])
                st.caption(f"Active: {', '.join(_active_syms)}")


@st.fragment
def render_config_panel() -> None:
    """Interactive editor for snp_config.yml.

    Fragment: toggle/number interactions only rerun this panel, not the full page
    or other fragments.  This prevents accidental triggering of the unfreeze logic
    in render_orders when the user edits config while derive is running.
    """
    _init_cfg_state()

    # Load backtest suggestions (cached by file mtime so only recomputes on new results)
    _bt_rec: dict = {}
    try:
        from src.backtest.synthetic import BACKTEST_RESULTS_PATH as _BT_RES_PATH, suggest_config as _sug_fn
        if _BT_RES_PATH.exists():
            _mtime = _BT_RES_PATH.stat().st_mtime
            _cached_rec = st.session_state.get("_bt_cfg_rec")
            if _cached_rec is None or _cached_rec[0] != _mtime:
                _raw_sug = _sug_fn(pd.read_pickle(_BT_RES_PATH))
                _raw_sug.pop("_meta", None)
                st.session_state["_bt_cfg_rec"] = (_mtime, _raw_sug)
            _bt_rec = st.session_state["_bt_cfg_rec"][1]
    except Exception:
        pass

    def _lbl(name: str, key: str | None = None) -> str:
        """Return label with inline backtest recommendation when available."""
        k = key or name
        return f"{name}  [Recommend: {_bt_rec[k]}]" if k in _bt_rec else name

    _cfg_btn, _ = st.columns([2, 4])
    with _cfg_btn:
        if st.button("🔄 Get Config", help="Force re-read from snp_config.yml", width="stretch"):
            _force_reload_cfg()
            st.rerun(scope="fragment")
    st.caption("Changes apply to next derive run. Comments in YAML are not preserved on save.")

    # ── DELETE helpers (dialog shown on confirmation button click) ──────────
    def _delete_pkl(fname: str, key: str) -> None:
        """Delete a single order pickle and toast the result."""
        p = _DATA_DIR / fname
        try:
            p.unlink(missing_ok=True)
            st.session_state[key] = True
            st.toast(f"Deleted {fname}")
        except Exception as exc:
            st.toast(f"Could not delete {fname}: {exc}", icon="⚠️")

    @st.dialog("⚠️ Confirm Delete", width="small")
    def _confirm_delete(fname: str, label: str, session_key: str) -> None:
        st.markdown(f"Delete **`data/{fname}`** ({label} orders from last derive run)?")
        c1, c2 = st.columns(2)
        with c1:
            if st.button("🗑️ Delete", width="stretch"):
                _delete_pkl(fname, session_key)
                st.rerun()
        with c2:
            if st.button("❌ Cancel", width="stretch"):
                st.rerun()

    def _del_btn(fname: str, label: str, key: str, done_key: str) -> None:
        """Render the Del button + clear its one-shot 'done' flag (call inside a card)."""
        if (_DATA_DIR / fname).exists():
            if st.button("🗑 Del", key=key, width="stretch",
                         help=f"Delete residual {fname} from last derive run"):
                _confirm_delete(fname, label, done_key)
        st.session_state.pop(done_key, None)

    # ── Card builders ────────────────────────────────────────────────────────
    def _card_risk_focus() -> None:
        with st.container(border=True):
            st.markdown("**🎯 Risk & Focus**")
            a, b = st.columns(2)
            with a:
                st.number_input("MINCUSHION", min_value=0.0, max_value=1.0, step=0.01,
                                format="%.2f", key="cfg_mincushion",
                                help="Minimum excess-liquidity / NLV cushion (triggers alert below this)")
            with b:
                st.number_input("MAX_DTE", min_value=1, step=1, key="cfg_max_dte",
                                help="Maximum days to expiry for new option entries")
            _fd_col, _sf_col = st.columns([1, 1.4], vertical_alignment="bottom")
            with _fd_col:
                st.date_input(
                    "FOCUS_DATE", key="cfg_focus_date", format="YYYY-MM-DD",
                    help="Anchors the Performance Dashboard display window + reference NAV. "
                         "When 'Score from focus date' is on, also the trade-history cut-off "
                         "for ABANDON/REFINE scoring. Synthetic OHLC backtest is unaffected "
                         "(always full history).",
                )
            with _sf_col:
                st.toggle(
                    "Score from focus date", key="cfg_score_from_focus",
                    help="Restrict trade-history ABANDON/REFINE/DEPLOY scoring to trades on or "
                         "after FOCUS_DATE — excludes pre-focus mistakes. Off = full history.",
                )
            st.number_input(
                "MAX_FILE_AGE (days)", min_value=0, step=1, key="cfg_max_file_age",
                help="Order pickles older than this many days are auto-deleted on next dashboard load. "
                     "0 = never auto-delete.",
            )
            _age_pkls = [
                ("df_cov.pkl", "Cover"), ("df_nkd.pkl", "Nakeds"),
                ("df_protect.pkl", "Protect"), ("df_reap.pkl", "Reap"),
            ]
            st.caption("File ages — " + "  |  ".join(
                f"{lbl}: {_pkl_age(f)}" for f, lbl in _age_pkls))

    def _card_cover() -> None:
        with st.container(border=True):
            st.markdown("**🛡 Cover**")
            _t, _d = st.columns([3, 1])
            with _t:
                st.toggle("COVER_ME", key="cfg_cover_me")
            with _d:
                _del_btn("df_cov.pkl", "Cover", "btn_del_cov", "_del_cov_done")
            if st.session_state["cfg_cover_me"]:
                a, b = st.columns(2)
                with a:
                    st.number_input("COVER_MIN_DTE", min_value=0, step=1,
                                    key="cfg_cover_min_dte",
                                    help="Minimum days to expiry for covered call/put candidates")
                with b:
                    st.number_input(_lbl("COVER_STD_MULT"), min_value=0.0, step=0.05,
                                    format="%.2f", key="cfg_cover_std_mult",
                                    help="Strike distance in units of 1σ above/below spot")
                c, d = st.columns(2)
                with c:
                    st.number_input("COVXPMULT", min_value=0.0, step=0.05, format="%.2f",
                                    key="cfg_covxpmult",
                                    help="Multiplier on market price for execution limit")
                with d:
                    st.number_input("COV_AGED_DTE", min_value=1, step=1,
                                    key="cfg_cov_aged_dte",
                                    help="Stocks held longer than this many days since assignment use vol-based price only (income over cost-recovery). Default: 180.")

    def _card_sow() -> None:
        with st.container(border=True):
            st.markdown("**🌱 Sow**")
            _t, _d = st.columns([3, 1])
            with _t:
                st.toggle("SOW_NAKEDS", key="cfg_sow_nakeds")
            with _d:
                _del_btn("df_nkd.pkl", "Nakeds", "btn_del_nkd", "_del_nkd_done")
            if not st.session_state["cfg_sow_nakeds"]:
                return
            a, b = st.columns(2)
            with a:
                st.number_input("VIRGIN_DTE", min_value=0, step=1, key="cfg_virgin_dte",
                                help="Target DTE for naked put entries")
            with b:
                st.number_input("VIRGIN_CALL_STD_MULT", min_value=0.0, step=0.1,
                                format="%.2f", key="cfg_virgin_call_std",
                                help="σ OTM for virgin call strikes")
            c, d = st.columns(2)
            with c:
                st.number_input(_lbl("VIRGIN_PUT_STD_MULT"), min_value=0.0, step=0.1,
                                format="%.2f", key="cfg_virgin_put_std",
                                help="σ OTM for virgin put strikes")
            with d:
                st.number_input("NAKEDXPMULT", min_value=0.0, step=0.05, format="%.2f",
                                key="cfg_nakedxpmult",
                                help="Multiplier on market price for naked execution limit")
            _vqm_help = "Fraction of NLV per symbol allocated to naked puts"
            try:
                from src.backtest.synthetic import BACKTEST_RESULTS_PATH as _vqm_bt_path  # noqa: PLC0415
                import json as _vqm_json  # noqa: PLC0415
                if _vqm_bt_path.exists():
                    _vqm_bt = pd.read_pickle(_vqm_bt_path)
                    _vqm_n_deploy = int((_vqm_bt["verdict"] == "DEPLOY").sum())
                    _vqm_n_refine_ovr = 0
                    _vqm_ovr_path = Path("data/symbol_overrides.json")
                    if _vqm_ovr_path.exists():
                        _vqm_n_refine_ovr = len(
                            _vqm_json.loads(_vqm_ovr_path.read_text(encoding="utf-8"))
                            .get("VIRGIN_PUT_STD_MULT", {})
                        )
                    _vqm_n = _vqm_n_deploy + _vqm_n_refine_ovr
                    if _vqm_n > 0:
                        _vqm_rec = round(1.0 / _vqm_n, 3)
                        _vqm_help += (
                            f"\n\n**Recommended: {_vqm_rec:.3f}** "
                            f"(1 ÷ {_vqm_n} sow symbols: "
                            f"{_vqm_n_deploy} DEPLOY + {_vqm_n_refine_ovr} REFINE-override)"
                        )
            except Exception:
                pass
            e, f = st.columns(2)
            with e:
                st.number_input(_lbl("MINNAKEDOPTPRICE $", "MINNAKEDOPTPRICE"),
                                min_value=0.0, step=0.25, format="%.2f", key="cfg_minnaked",
                                help="Minimum option price to write a naked put")
            with f:
                st.number_input("VIRGIN_QTY_MULT", min_value=0.0, step=0.005, format="%.3f",
                                key="cfg_virgin_qty_mult", help=_vqm_help)

    def _card_protect() -> None:
        with st.container(border=True):
            st.markdown("**🛟 Protect**")
            _t, _d = st.columns([3, 1])
            with _t:
                st.toggle("PROTECT_ME", key="cfg_protect_me")
            with _d:
                _del_btn("df_protect.pkl", "Protect", "btn_del_prot", "_del_prot_done")
            if st.session_state["cfg_protect_me"]:
                a, b = st.columns(2)
                with a:
                    st.number_input("PROTECT_DTE", min_value=0, step=1,
                                    key="cfg_protect_dte",
                                    help="Target DTE for protective put/call purchases")
                with b:
                    st.number_input("PROTECTION_STRIP", min_value=1, step=1,
                                    key="cfg_protection_strip",
                                    help="Number of OTM strikes to evaluate for protection")

    def _card_reap() -> None:
        with st.container(border=True):
            st.markdown("**🌾 Reap**")
            _t, _d = st.columns([3, 1])
            with _t:
                st.toggle("REAP_ME", key="cfg_reap_me")
            with _d:
                _del_btn("df_reap.pkl", "Reap", "btn_del_reap", "_del_reap_done")
            if st.session_state["cfg_reap_me"]:
                a, b = st.columns(2)
                with a:
                    st.number_input(_lbl("REAPRATIO"), min_value=0.001, step=0.005,
                                    format="%.3f", key="cfg_reapratio",
                                    help="Close short option when price ≤ REAPRATIO × avgCost")
                with b:
                    st.number_input("MINREAPDTE", min_value=0, step=1, key="cfg_minreapdte",
                                    help="Do not reap at or below this DTE")

    def _card_ai() -> None:
        with st.container(border=True):
            st.markdown("**🤖 AI**")
            _ai_models = st.session_state.get("cfg_aimodels", ["Gemini", "DeepSeek"])
            _valid_ai = [m for m in _ai_models if m in _PROVIDER_HINTS]
            if _valid_ai:
                st.selectbox(
                    "DEFAULTAI", _valid_ai, key="cfg_defaultai",
                    help="Default AI provider in the Ask AI dock. Options come from AIMODELS in snp_config.yml.",
                )
            else:
                st.caption("No valid AI providers configured.")

    # ── Card grid (st.columns stack to one column on narrow / mobile) ─────────
    _r1c1, _r1c2 = st.columns(2)
    with _r1c1:
        _card_risk_focus()
    with _r1c2:
        _card_cover()
    _r2c1, _r2c2 = st.columns(2)
    with _r2c1:
        _card_sow()
    with _r2c2:
        _card_protect()
    _r3c1, _r3c2 = st.columns(2)
    with _r3c1:
        _card_reap()
    with _r3c2:
        _card_ai()

    # Focus-date / toggle change must FULL-rerun so the Performance Dashboard +
    # trade-history scores (other fragments) update live.
    _focus_state = (
        st.session_state.get("cfg_focus_date"),
        st.session_state.get("cfg_score_from_focus"),
    )
    if st.session_state.get("_focus_applied") != _focus_state:
        st.session_state["_focus_applied"] = _focus_state
        st.rerun()

    # DEFAULTAI change must FULL-rerun so the Ask AI dock (a separate fragment)
    # re-seeds its provider selection to the newly chosen default.
    if st.session_state.get("_defaultai_applied") != st.session_state.get("cfg_defaultai"):
        st.session_state["_defaultai_applied"] = st.session_state.get("cfg_defaultai")
        st.rerun()

    st.divider()

    if st.button("💾 Save Config", type="primary", width="stretch",
                 disabled=not _cfg_dirty()):
        try:
            _save_cfg()
            st.success("Saved ✓")
        except Exception as e:
            st.error(f"Save failed: {e}")


# ---------------------------------------------------------------------------
# Analysis tab
# ---------------------------------------------------------------------------

@st.cache_data(ttl=600, show_spinner=False)
def _cached_ohlc() -> dict:
    """Load the OHLC pickle store, cached for 10 min — OHLC data is daily, no need to reload often."""
    from src.dashboard.ohlc import load_ohlc  # noqa: PLC0415
    return load_ohlc()


@st.cache_data(ttl=300, show_spinner=False)
def _cached_ohlc_stats() -> tuple[int, int]:
    """Return (total OHLC symbols, weekly-eligible S&P 500 symbols in OHLC store)."""
    ohlc = _cached_ohlc()
    total = len(ohlc)
    try:
        cats_path = _MASTER_DIR / "symbol_categories.pkl"
        if cats_path.exists():
            cats = pd.read_pickle(cats_path)
            weekly_syms = set(cats.loc[cats["is_weekly"], "symbol"])
            weekly_in_ohlc = sum(1 for s in ohlc if s in weekly_syms)
        else:
            weekly_in_ohlc = 0
    except Exception:
        weekly_in_ohlc = 0
    return total, weekly_in_ohlc


def _render_bt_status() -> None:
    """Backtest progress + results display.

    Plain helper (not a fragment): called from render_actions, whose 5 s timer
    drives the refresh. backtest_proc polling happens in render_actions, so this
    only renders. (A nested run_every fragment caused "fragment … does not exist"
    warnings on the parent's full reruns.)
    """
    from src.backtest.synthetic import BACKTEST_RESULTS_PATH as _BT_RESULTS

    _running = st.session_state.get("backtest_proc") is not None

    if _running:
        st.info("⏳ Backtest running… (first run may take ~5 min to fetch OHLC)")
        _render_log_expander("📋 Backtest log", _BACKTEST_LOG, expanded=True, lines_fn=lambda: _log_lines(_BACKTEST_LOG, 500))
    elif "_bt_exit" in st.session_state:
        _bt_rc = st.session_state["_bt_exit"]
        if _bt_rc == 0:
            st.success("✅ Backtest complete")
            _render_log_expander("📋 Backtest log", _BACKTEST_LOG, expanded=False, lines_fn=lambda: _log_lines(_BACKTEST_LOG, 500))
            if _BT_RESULTS.exists():
                try:
                    from src.backtest.synthetic import suggest_config
                    _bt_df = pd.read_pickle(_BT_RESULTS)
                    _bt_sug = suggest_config(_bt_df)
                    _bt_meta = _bt_sug.pop("_meta", {})
                    _d_pct = _bt_meta.get("deploy_pct", 0)
                    st.caption(
                        f"{_bt_meta.get('n_symbols', 0)} symbols — "
                        f"**{_bt_meta.get('n_deploy', 0)} DEPLOY** / "
                        f"{_bt_meta.get('n_refine', 0)} REFINE / "
                        f"{_bt_meta.get('n_abandon', 0)} ABANDON "
                        f"({_d_pct:.0f}% deploy rate)"
                    )
                    _rec_parts = "  ·  ".join(f"**{k}** {v}" for k, v in _bt_sug.items())
                    st.caption(f"Suggested → {_rec_parts}")
                    st.caption("Recommendations shown next to parameter names in Config panel (Orders tab).")
                except Exception as _be:
                    st.warning(f"Could not load backtest results: {_be}")
        else:
            st.error(f"❌ Backtest failed (exit {_bt_rc})")
            _render_log_expander("📋 Backtest log", _BACKTEST_LOG, expanded=True, lines_fn=lambda: _log_lines(_BACKTEST_LOG, 500))


@st.fragment
def render_analysis() -> None:
    """Cover/Protect gaps + OHLC chart browser."""
    from src.flex.analyze import symbol_performance
    from src.flex.parse import mask_accounts, normalize

    snap = client.snapshot()
    acct = _selected_account()

    # ── Persist Analysis tab state across tab switches ────────────────────────
    # Streamlit cleans up widget-backed keys when the tab is not rendered.
    # Shadow keys (_ana_*) are manually set so they survive navigation.
    # Bidirectional sync: save on each render; restore when returning.
    _ANA_PERSIST: list[tuple[str, object]] = [
        ("pf_sectype",        "ALL"),
        ("pf_dte_sel",        "ALL"),
        ("pf_weekly_only",    False),
        ("scr_sym",           ""),
        ("scr_strat",         "ALL"),
        ("gap_needs_sel",     "ALL"),
        ("analysis_chart_sym", None),
        ("perf_date_start",   None),
        ("perf_date_end",     None),
        ("exp_ana_actions",   False),
        ("exp_ana_positions", False),
        ("exp_ana_cover",        False),
        ("exp_ana_treemap",      False),
        ("exp_ana_pnl",          False),
        ("pnl_verdict_filter",   "All"),
        ("pnl_syn_check",        True),
        ("pnl_sym_filter",       ""),
        ("exp_ana_chart",        False),
        ("exp_ana_perf",         True),
    ]
    for _wk, _wdefault in _ANA_PERSIST:
        _sk = f"_ana_{_wk}"
        if _wk in st.session_state:
            st.session_state[_sk] = st.session_state[_wk]   # save current
        elif _sk in st.session_state:
            st.session_state[_wk] = st.session_state[_sk]   # restore on re-entry

    # ── Flex data paths needed by the data panels ────────────────────────────
    flex_path = _MASTER_DIR / "flex_trades.pkl"
    cash_path = _MASTER_DIR / "flex_cash.pkl"
    nav_path  = _MASTER_DIR / "flex_nav.pkl"
    _acct_map = {a: lbl for lbl, a in (("US", _US), ("SG", _SG)) if a}

    # ── Positions table (live — with filters + ITM highlighting) ─────────────
    _pos_data = pd.DataFrame()
    if not snap.positions.empty:
        _pos_data = classify_portfolio(_filter_positions(snap.positions, acct))
        _pos_data = _join_tickers(_pos_data, snap.tickers)
        _pos_data["margin_est"] = position_margin_est(_pos_data)
        # Prefer real IBKR what-if margins when available, fall back to Reg-T estimate
        _m_col = "margin_init" if "margin_init" in _pos_data.columns else "margin_est"

        # Sort: per-symbol CC (short C) → STK → protective put (long P) → others
        if {"secType", "right", "position", "symbol"} <= set(_pos_data.columns):
            _cc_key  = (_pos_data["secType"].eq("OPT") & _pos_data["right"].eq("C") & (_pos_data["position"] < 0)).map({True: 0, False: 999})
            _stk_key = _pos_data["secType"].eq("STK").map({True: 1, False: 999})
            _pp_key  = (_pos_data["secType"].eq("OPT") & _pos_data["right"].eq("P") & (_pos_data["position"] > 0)).map({True: 2, False: 999})
            _row_ord = pd.concat([_cc_key, _stk_key, _pp_key], axis=1).min(axis=1)
            _pos_data = (
                _pos_data.assign(_row_ord=_row_ord)
                .sort_values(["symbol", "_row_ord"])
                .drop(columns="_row_ord")
                .reset_index(drop=True)
            )

        # Apply the single global filter (symbol / C/P / ITM) so Positions + Gaps
        # honour the top filter bar. State is matched against the pf_state column.
        _pos_data = apply_global_filter(_pos_data, use_state=False)
        _g_states = st.session_state.get("flt_state") or []
        if _g_states and "pf_state" in _pos_data.columns:
            _pos_data = _pos_data[_pos_data["pf_state"].isin(_g_states)].reset_index(drop=True)

    # Render Positions expander unconditionally to avoid layout shifts at startup
    with st.expander("📋 Positions", expanded=False, key="exp_ana_positions"):
        if not client._bootstrapped:
            st.info("Waiting for live positions to load (bootstrap in progress)...")
        elif snap.positions.empty:
            st.info("No positions held in the selected account.")
        else:
            # Positions-only controls. Symbol / State / ITM live in the global
            # Filter bar (already applied to _pos_data above) — not duplicated here.
            _pf_c2, _pf_c4, _pf_c5, _pf_c6 = st.columns([1.3, 1.3, 1.4, 1])
            _pf_sectype = _pf_c2.selectbox("secType", ["ALL", "STK", "OPT"], key="pf_sectype")
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
            _pf_dte_sel = _pf_c4.selectbox("DTE", _dte_opts, key="pf_dte_sel")
            _pf_weekly_only = _pf_c5.checkbox("Weekly only", key="pf_weekly_only")
            _pf_c6.button(
                "✕ Clear", key="pf_clear_filter", width="stretch",
                on_click=_clear_positions_filter,
            )

            # Load monthly-only list once for weekly filter
            _monthly_only_set: set[str] = set()
            if _pf_weekly_only:
                _sc_pkl = _MASTER_DIR / "symbol_categories.pkl"
                if _sc_pkl.exists():
                    try:
                        _sc_df = pd.read_pickle(_sc_pkl)
                        if "symbol" in _sc_df.columns and "is_weekly" in _sc_df.columns:
                            _monthly_only_set = set(
                                _sc_df.loc[~_sc_df["is_weekly"], "symbol"].dropna()
                            )
                    except Exception:
                        pass

            # Apply Positions-only filters (Symbol / State / ITM already applied
            # globally to _pos_data above).
            _pv = _pos_data.copy()
            if _pf_sectype != "ALL":
                _pv = _pv[_pv.get("secType", pd.Series("", index=_pv.index)) == _pf_sectype]
            if _pf_dte_sel != "ALL":
                _dte_max_val = int(_pf_dte_sel)
                _dte_col = _dte_series(
                    _pv.get("expiry", pd.Series("", index=_pv.index)).fillna("").astype(str)
                )
                _pv = _pv[_dte_col.isna() | (_dte_col <= _dte_max_val)]
            if _pf_weekly_only and _monthly_only_set and "symbol" in _pv.columns:
                _pv = _pv[~_pv["symbol"].isin(_monthly_only_set)]
            _pv = _pv.reset_index(drop=True)
            _itm_arr = _itm_mask_vec(_pv)

            # Build display columns: und_px next to strike; dte inserted next to expiry
            _pos_show_cols = [
                "symbol", "secType", "right", "strike", "underlying_px",
                "expiry", "position", "marketPrice",
                "delta", "gamma", "theta", "vega", _m_col, "unrealizedPNL", "pf_state",
            ]
            _pv_show = _pv[[c for c in _pos_show_cols if c in _pv.columns]].copy()
            # Insert DTE column immediately after expiry
            if "expiry" in _pv_show.columns:
                _pv_show.insert(
                    _pv_show.columns.get_loc("expiry") + 1, "dte",
                    _dte_series(_pv_show["expiry"].fillna("").astype(str)).round(0).values,
                )
            # For STK rows: underlying price = market price; blank option-specific cols
            if "secType" in _pv_show.columns:
                _stk_rows = _pv_show["secType"] == "STK"
                if all(c in _pv_show.columns for c in ("underlying_px", "marketPrice")):
                    _pv_show.loc[_stk_rows, "underlying_px"] = _pv_show.loc[_stk_rows, "marketPrice"]
                if "right" in _pv_show.columns:
                    _pv_show.loc[_stk_rows, "right"] = ""   # empty string renders blank; None renders "None"
                if "strike" in _pv_show.columns:
                    _pv_show.loc[_stk_rows, "strike"] = None
                if "dte" in _pv_show.columns:
                    _pv_show.loc[_stk_rows, "dte"] = None
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
                        "symbol":        st.column_config.TextColumn("Symbol",       help="Ticker symbol"),
                        "secType":       st.column_config.TextColumn("Type",         help="Security type: STK=stock, OPT=option"),
                        "right":         st.column_config.TextColumn("C/P",          help="Option right: C=Call, P=Put"),
                        "strike":        st.column_config.NumberColumn("Strike",      format="%.1f",    help="Option strike price"),
                        "underlying_px": st.column_config.NumberColumn("Und Px",      format="$%,.2f",  help="Current underlying stock price"),
                        "expiry":        st.column_config.TextColumn("Expiry",        help="Option expiration date"),
                        "dte":           st.column_config.NumberColumn("DTE",         format="%.0f",    help="Calendar days to expiration"),
                        "position":      st.column_config.NumberColumn("Qty",         format="%d",      help="Shares (STK) or contracts (OPT); negative = short"),
                        "marketPrice":   st.column_config.NumberColumn("Mkt Px",      format="$%,.2f",  help="Current market price of the position"),
                        "delta":         st.column_config.NumberColumn("Δ",           format="%.3f",    help="Price change per $1 move in the underlying"),
                        "gamma":         st.column_config.NumberColumn("Γ",           format="%.4f",    help="Rate of change of delta per $1 move in underlying"),
                        "theta":         st.column_config.NumberColumn("Θ",           format="%.3f",    help="Daily time decay in option value (negative = losing value each day)"),
                        "vega":          st.column_config.NumberColumn("ν",           format="%.3f",    help="Sensitivity to a 1% change in implied volatility"),
                        _m_col:          st.column_config.NumberColumn("Margin",      format="$%,.0f",  help="IBKR what-if margin (margin_init) when available, else Reg-T estimate"),
                        "unrealizedPNL": st.column_config.NumberColumn("Unreal P&L",  format="$%,.0f",  help="Unrealized profit/loss at current market price"),
                        "pf_state":      st.column_config.TextColumn("State",         help="Portfolio state: CC=covered call, PP=protective put, NP=naked put, etc."),
                    },
                )

    # ── Performance Dashboard ─────────────────────────────────────────────────
    _render_perf_chart(
        flex_path=flex_path,
        ohlc_path=_MASTER_DIR / "ohlc.pkl",
        cash_path=cash_path,
        nav_path=nav_path,
    )

    # ── Symbol Deep-Dive (fragment: symbol changes isolated here) ────────────
    _render_symbol_deep_dive()

    # ── Gaps ──────────────────────────────────────────────────────────────────
    _prot_status = "enabled" if settings.protect_me else "disabled"
    with st.expander(f"⚠️ Gaps [PROTECT {_prot_status}]", expanded=False, key="exp_ana_cover"):
        if not client._bootstrapped:
            st.info("Waiting for live positions to load (bootstrap in progress)...")
        elif snap.positions.empty:
            st.info("No positions held in the selected account.")
        else:
            # protect_me=True always so Protect Strike / Value Protected columns are present
            gaps = cover_protect_gaps(
                _pos_data, snap.tickers,
                protect_me=True,
                cover_std_mult=settings.cover_std_mult,
                max_dte=settings.max_dte,
            )
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
                    "symbol":   st.column_config.TextColumn("Symbol",   help="Ticker symbol"),
                    "shares":   st.column_config.NumberColumn("Shares",  format="%d",      help="Number of shares held"),
                    "avg_cost": st.column_config.NumberColumn("Avg Cost", format="$%.2f",  help="Average cost basis per share"),
                    "cover_strike": st.column_config.NumberColumn(
                        "Cover Strike", format="$%.1f",
                        help=(
                            f"Target call strike: max(avgCost, mkt_px + "
                            f"{settings.cover_std_mult}×IV×√(DTE/252)), DTE={settings.max_dte}. "
                            "IV sourced from existing option tickers, default 30%."
                        ),
                    ),
                    "mkt_px": st.column_config.NumberColumn("Mkt Px", format="$%.2f", help="Current market price of the underlying stock"),
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



    # ── Load flex trade data (needed for Options P&L by Underlying) ──────────
    perf = pd.DataFrame()
    _flex_loaded = flex_path.exists()
    if _flex_loaded:
        df_all = pd.read_pickle(flex_path)
        if df_all.empty or "pnl" not in df_all.columns:
            if not df_all.empty:
                df_all = normalize(df_all)
        _has_raw_ids = (
            _acct_map and "accountId" in df_all.columns
            and df_all["accountId"].isin(set(_acct_map.keys())).any()
        )
        if _has_raw_ids:
            df_all = mask_accounts(df_all, _acct_map)
            df_all.to_pickle(flex_path)
        perf = symbol_performance(df_all)
        _unds_path = _DATA_DIR / "df_unds.pkl"
        _unds = pd.read_pickle(_unds_path).set_index("symbol") if _unds_path.exists() else pd.DataFrame()
        _pos_map: dict[str, str] = {}
        if not snap.positions.empty:
            for _sym_k, _grp in _filter_positions(snap.positions, acct).groupby("symbol"):
                _parts = []
                _stk = _grp[_grp["secType"] == "STK"]
                if not _stk.empty:
                    _qty = int(_stk["position"].sum())
                    _parts.append(f"STK {_qty:+d}")
                _opt = _grp[_grp["secType"] == "OPT"]
                for _, _orow in _opt.iterrows():
                    _r = _orow.get("right", "?")
                    _q = int(_orow["position"])
                    _parts.append(f"{_r} {_q:+d}")
                if _parts:
                    _pos_map[str(_sym_k)] = "  ".join(_parts)

        if not perf.empty:
            _extra: list[dict] = []
            for _sym in perf["symbol"]:
                _u = _unds.loc[_sym] if _sym in _unds.index else None
                _extra.append({
                    "current_price": round(float(_u["price"]), 2) if _u is not None and pd.notna(_u["price"]) else None,
                    "hv":  round(float(_u["hv"]) * 100, 1)  if _u is not None and pd.notna(_u["hv"])  else None,
                    "iv":  round(float(_u["iv"]) * 100, 1)  if _u is not None and pd.notna(_u["iv"])  else None,
                    "position": _pos_map.get(_sym, ""),
                    "margin": round(float(_u["margin"]), 0) if _u is not None and pd.notna(_u["margin"]) else None,
                })
            _extra_df = pd.DataFrame(_extra)
            _sym_idx = perf.columns.get_loc("symbol") + 1
            for _col in reversed(["current_price", "hv", "iv", "position", "margin"]):
                perf.insert(_sym_idx, _col, _extra_df[_col].values)

            # Backtest score + verdict — one call per symbol, fast (in-memory mask)
            from src.backtest.score import score_from_trades as _score_fn  # noqa: PLC0415
            from src.backtest.synthetic import BACKTEST_RESULTS_PATH as _SYN_RES  # noqa: PLC0415
            _bt_scores: list[int | None] = []
            _bt_verdicts: list[str | None] = []
            _score_since = score_since()
            for _sym in perf["symbol"]:
                _bs = _score_fn(df_all, _sym, since=_score_since)
                if _bs.total_trades == 0:
                    _bt_scores.append(None)
                    _bt_verdicts.append(None)
                else:
                    _bt_scores.append(int(_bs.composite))
                    _bt_verdicts.append(_bs.verdict)

            # Synthetic backtest verdict — join from backtest_results.pkl if available
            _syn_vmap: dict[str, str] = {}
            if _SYN_RES.exists():
                try:
                    _syn_raw = pd.read_pickle(_SYN_RES)
                    if not _syn_raw.empty and {"symbol", "verdict"} <= set(_syn_raw.columns):
                        _syn_vmap = dict(zip(_syn_raw["symbol"], _syn_raw["verdict"]))
                except Exception:
                    pass
            _syn_verdicts_col = [_syn_vmap.get(s) for s in perf["symbol"]]

            _mg_idx = perf.columns.get_loc("margin") + 1
            perf.insert(_mg_idx,     "syn_verdict", _syn_verdicts_col)
            perf.insert(_mg_idx + 1, "score",       _bt_scores)
            perf.insert(_mg_idx + 2, "verdict",     _bt_verdicts)

    # ── Trade Analysis: Synthetic Backtest + Historical Trade P&L ───────────
    from src.backtest.synthetic import BACKTEST_RESULTS_PATH as _SYN_PATH  # noqa: PLC0415
    with st.expander("📊 Trade Analysis", expanded=False, key="exp_ana_pnl"):

        # Shared filter controls
        _fv_col, _fsyn_col, _fsym_col, _fwk_col, _fsp = st.columns([2, 1.4, 2, 1.6, 3.0])
        _vf = _fv_col.selectbox(
            "Verdict", ["All", "DEPLOY", "REFINE", "ABANDON", "INSUFFICIENT_DATA"],
            key="pnl_verdict_filter",
        )
        with _fsyn_col.container(key="syn_chk_wrap"):
            _use_syn = st.checkbox(
                "Synthetic",
                key="pnl_syn_check",
                help=(
                    "Controls which verdict column the filter applies to in the "
                    "Historical Trade P&L table below.\n"
                    "ON — filter on the Synthetic backtest Rating column.\n"
                    "OFF — filter on your personal Trade Rating column.\n"
                    "The Synthetic Backtest table below always filters on its own Rating."
                ),
            )
        _sf = _fsym_col.text_input("Symbol", key="pnl_sym_filter", placeholder="e.g. AAPL")
        with _fwk_col.container(key="wk_chk_wrap"):
            _wk_only = st.checkbox(
                "Weeklies Only",
                key="pnl_weekly_only",
                help="Show only symbols explicitly classified as weekly in symbol_categories.pkl. "
                     "Drops monthly-only AND uncategorized symbols (delisted/renamed tickers, ETFs, "
                     "share-class variants not in the current S&P 500).",
            )

        # Symbols explicitly marked weekly — the *only* ones kept when "Weeklies Only"
        # is ticked. Keeping (not just dropping monthly-only) also removes uncategorized
        # symbols absent from symbol_categories.pkl. _wk_only_active is False when the
        # categories file is missing/malformed, so the checkbox degrades to a no-op
        # rather than blanking the whole table.
        _weekly_set: set[str] = set()
        _wk_only_active = False
        if _wk_only:
            _sc_pkl = _MASTER_DIR / "symbol_categories.pkl"
            if _sc_pkl.exists():
                try:
                    _sc_df = pd.read_pickle(_sc_pkl)
                    if {"symbol", "is_weekly"} <= set(_sc_df.columns):
                        _weekly_set = set(_sc_df.loc[_sc_df["is_weekly"], "symbol"].dropna())
                        _wk_only_active = True
                except Exception:
                    pass

        # Match count next to the Verdict selector (e.g. 20/277) — respects Weeklies Only
        _vcount_col = "syn_verdict" if _use_syn else "verdict"
        if not perf.empty and _vcount_col in perf.columns:
            _count_base = perf[perf["symbol"].isin(_weekly_set)] if _wk_only_active else perf
            _v_total = len(_count_base)
            _v_match = _v_total if _vf == "All" else int((_count_base[_vcount_col] == _vf).sum())
            _fv_col.caption(f"**{_v_match}/{_v_total}** symbols match")

        _vcolors = {
            "DEPLOY": "color: #22c55e; font-weight: 600",
            "REFINE": "color: #f59e0b; font-weight: 600",
            "ABANDON": "color: #ef4444; font-weight: 600",
        }

        # ── Historical Trade P&L table (first) ───────────────────────────────
        st.markdown("##### 📊 Historical Trade P&L")
        if perf.empty:
            st.info("No closed options trades in the Flex data.")
        else:
            _verdict_col_for_filter = "syn_verdict" if _use_syn else "verdict"
            _perf_disp = perf.copy()
            if _wk_only_active:
                _perf_disp = _perf_disp[_perf_disp["symbol"].isin(_weekly_set)]
            if _vf != "All" and _verdict_col_for_filter in _perf_disp.columns:
                _perf_disp = _perf_disp[_perf_disp[_verdict_col_for_filter] == _vf]
            _gsym = str(st.session_state.get("flt_symbol", "") or "").strip().upper()
            if _gsym:  # global filter bar takes precedence
                _perf_disp = _perf_disp[_perf_disp["symbol"].astype(str).str.upper() == _gsym]
            elif _sf.strip():
                _perf_disp = _perf_disp[_perf_disp["symbol"].str.contains(_sf.strip().upper(), na=False)]
            _perf_disp = _perf_disp.sort_values("symbol")
            def _vstyle(v):
                return _vcolors.get(v, "")
            _vcols = [c for c in ["syn_verdict", "verdict"] if c in _perf_disp.columns]
            _pstyled = _banded(_perf_disp).format({
                "current_price": "${:,.2f}",
                "hv":            "{:.1f}%",
                "iv":            "{:.1f}%",
                "margin":        "${:,.0f}",
                "score":         "{:.0f}",
                "win_rate":      "{:.0%}",
                "profit_factor": "{:.2f}",
                "avg_win":       "${:,.0f}",
                "avg_loss":      "${:,.0f}",
                "total_pnl":     "${:,.0f}",
            }, na_rep="—")
            for _vc in _vcols:
                _pstyled = _pstyled.map(_vstyle, subset=[_vc])
            st.dataframe(
                _pstyled,
                width="stretch",
                hide_index=True,
                column_config={
                    "symbol":        st.column_config.TextColumn("Symbol",       help="Underlying ticker"),
                    "current_price": st.column_config.NumberColumn("Price",      help="Last close from df_unds (yfinance)"),
                    "hv":            st.column_config.TextColumn("HV %",         help="20-day historical volatility (annualised) from df_unds"),
                    "iv":            st.column_config.TextColumn("IV %",         help="Implied volatility from df_unds"),
                    "position":      st.column_config.TextColumn("Position",     help="Current live position: STK ±shares, P/C ±contracts"),
                    "margin":        st.column_config.TextColumn("Margin",       help="Estimated margin per contract from df_unds"),
                    "syn_verdict":   st.column_config.TextColumn("Rating",       help="Synthetic backtest rating (DEPLOY ≥70 · REFINE 40–69 · ABANDON <40). Blank = monthly-only or backtest not yet run."),
                    "score":         st.column_config.NumberColumn("Score /100", help="Personal trade history composite score 0–100. Populated only for symbols with ≥10 closed OPT trades."),
                    "verdict":       st.column_config.TextColumn("Trade Rating", help="Personal trade history rating (DEPLOY ≥70 · REFINE 40–69 · ABANDON <40). Blank if fewer than 10 trades."),
                    "trades":        st.column_config.NumberColumn("Trades",     help="Closed option contracts in your Flex history"),
                    "win_rate":      st.column_config.TextColumn("Win %",        help="% of closed OPT trades with positive P&L"),
                    "profit_factor": st.column_config.TextColumn("PF",           help="Gross profit ÷ gross loss on your own trades. ≥1.5 = strong edge"),
                    "avg_win":       st.column_config.TextColumn("Avg Win",      help="Average P&L on your winning trades"),
                    "avg_loss":      st.column_config.TextColumn("Avg Loss",     help="Average P&L on your losing trades (negative)"),
                    "total_pnl":     st.column_config.TextColumn("Total P&L",    help="Sum of all realised P&L for this symbol"),
                },
            )

        # ── Synthetic Backtest table (second) ────────────────────────────────
        st.divider()
        st.markdown("##### 🧪 Synthetic Backtest")
        if not _SYN_PATH.exists():
            st.info("No results yet — run the backtest via 🛠 Actions → 🧪 Run Backtest.")
        else:
            try:
                _syn = pd.read_pickle(_SYN_PATH)
                _syn_disp = _syn.copy()
                if _vf != "All" and _use_syn:
                    _syn_disp = _syn_disp[_syn_disp["verdict"] == _vf]
                # Global Filter bar symbol takes precedence (exact match), as in the
                # Historical P&L table above; fall back to the local Symbol field.
                _gsym = str(st.session_state.get("flt_symbol", "") or "").strip().upper()
                if _gsym:
                    _syn_disp = _syn_disp[_syn_disp["symbol"].astype(str).str.upper() == _gsym]
                elif _sf.strip():
                    _syn_disp = _syn_disp[_syn_disp["symbol"].str.contains(_sf.strip().upper(), na=False)]
                st.caption(
                    f"{len(_syn_disp)} of {len(_syn)} symbols · last run {_pkl_age('', path=_SYN_PATH)}"
                )
                _cols_show = [c for c in [
                    "symbol", "verdict", "composite",
                    "cover_std_mult_opt", "cc_pf", "cc_win_rate", "cc_max_dd",
                    "put_std_mult_opt", "csp_pf", "csp_win_rate",
                    "years_tested",
                ] if c in _syn_disp.columns]
                _syn_disp = _syn_disp[_cols_show].sort_values("symbol")
                st.dataframe(
                    _syn_disp.style
                    .map(lambda v: _vcolors.get(v, ""),
                         subset=["verdict"] if "verdict" in _syn_disp.columns else [])
                    .format({
                        "composite":          lambda v: f"{v:,.0f}" if pd.notna(v) else "—",
                        "cc_pf":              lambda v: ("99.9*" if v >= 99.9 else f"{v:,.2f}") if pd.notna(v) else "—",
                        "csp_pf":             lambda v: ("99.9*" if v >= 99.9 else f"{v:,.2f}") if pd.notna(v) else "—",
                        "cc_win_rate":        lambda v: f"{v:.0%}" if pd.notna(v) else "—",
                        "csp_win_rate":       lambda v: f"{v:.0%}" if pd.notna(v) else "—",
                        "cc_max_dd":          lambda v: f"{v:,.1f}%" if pd.notna(v) else "—",
                        "cover_std_mult_opt": lambda v: f"{v:.2f}" if pd.notna(v) else "—",
                        "put_std_mult_opt":   lambda v: f"{v:.2f}" if pd.notna(v) else "—",
                        "years_tested":       lambda v: f"{v:.1f}" if pd.notna(v) else "—",
                    }, na_rep="—"),
                    column_config={
                        "symbol":             st.column_config.TextColumn("Symbol"),
                        "verdict":            st.column_config.TextColumn("Rating", help="Wheel-appropriate verdict based on CSP leg (the real assignment risk). DEPLOY: CSP win%>=70 & PF>=1.0 & 3+yr. REFINE: CSP win%>=55 & PF>=0.85. ABANDON: below thresholds. CC assignment is a designed wheel exit, not a loss — so CC performance does not drive the verdict."),
                        "composite":          st.column_config.NumberColumn("Score /100", help="BacktestExpert composite score (CC leg, 0–100). Reference only — structural ceiling is ~74 with 5y monthly data. Use Rating for the actionable verdict."),
                        "cover_std_mult_opt": st.column_config.TextColumn("CC σ best", help="Grid-optimal COVER_STD_MULT for this symbol (best PF from grid [0.30…1.50]). Compare against your config value to see how far you are from the historical optimum."),
                        "cc_pf":              st.column_config.TextColumn("CC PF",     help="Covered-call profit factor at your configured COVER_STD_MULT. ≥1.5 good · ≥2.0 strong. 99.9* = no losing cycles (capped)."),
                        "cc_win_rate":        st.column_config.TextColumn("CC Win%",   help="Fraction of monthly CC cycles that were profitable at your configured COVER_STD_MULT."),
                        "cc_max_dd":          st.column_config.TextColumn("CC MaxDD",  help="Max drawdown as % of peak cumulative CC P&L at your configured COVER_STD_MULT. >30% signals adverse months despite positive PF."),
                        "put_std_mult_opt":   st.column_config.TextColumn("CSP σ best", help="Grid-optimal VIRGIN_PUT_STD_MULT for this symbol (best PF from grid [0.50…2.00]). Compare against your config value."),
                        "csp_pf":             st.column_config.TextColumn("CSP PF",    help="Cash-secured-put profit factor at your configured VIRGIN_PUT_STD_MULT. 99.9* = no losing cycles (capped)."),
                        "csp_win_rate":       st.column_config.TextColumn("CSP Win%",  help="Fraction of monthly CSP cycles that expired OTM or were profitable at your configured VIRGIN_PUT_STD_MULT."),
                        "years_tested":       st.column_config.TextColumn("Years",     help="Years of daily OHLC used (up to 5y from yfinance). Shorter = less reliable."),
                        "n_cycles":           st.column_config.NumberColumn("Cycles",  help="Monthly cycles simulated (~years×12). Each = sell ~35 days before 3rd-Friday expiry."),
                    },
                    width="stretch", hide_index=True,
                )
            except Exception as _se:
                st.warning(f"Could not load synthetic backtest results: {_se}")


@st.fragment
def _render_symbol_deep_dive() -> None:
    """Symbol Deep-Dive: backtest stats + OHLC chart + positions/orders table.

    Single fragment so symbol changes don't rerender the surrounding analysis.
    """
    from src.backtest.score import score_from_trades as _sft  # noqa: PLC0415
    from src.flex.analyze import (  # noqa: PLC0415
        dte_distribution as _dte_dist,
        strategy_recommendation as _strat_rec,
        symbol_performance as _sym_perf,
    )
    from src.flex.parse import normalize as _norm  # noqa: PLC0415

    ohlc_store = _cached_ohlc()
    all_symbols = sorted(ohlc_store.keys())
    snap = client.snapshot()
    acct = _selected_account()

    with st.expander("🔍 Deep-Dive", expanded=False, key="exp_ana_chart"):
        # Guard stale session_state (e.g. symbol removed from OHLC store after a refresh)
        _cur_sym = st.session_state.get("analysis_chart_sym")
        # Global filter bar drives the deep-dive symbol when a symbol is set.
        _gsym = str(st.session_state.get("flt_symbol", "") or "").strip().upper()
        if _gsym:
            _match = next((s for s in all_symbols if s.upper() == _gsym), None)
            if _match:
                st.session_state["analysis_chart_sym"] = _match
                _cur_sym = _match
        if _cur_sym not in all_symbols:
            _default_sym = "GOOG" if "GOOG" in all_symbols else (all_symbols[0] if all_symbols else None)
            st.session_state["analysis_chart_sym"] = _default_sym

        # ── Margin & P&L Map ──────────────────────────────────────────────────────
        pos_df = _filter_positions(snap.positions, acct)
        clicked_sym = None
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
                        custom_data=["symbol"],
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
                    event = st.plotly_chart(fig_tree, on_select="rerun", key="treemap_pnl_map", width="stretch")
                    if event and "selection" in event and event["selection"]["points"]:
                        try:
                            pt = event["selection"]["points"][0]
                            candidate = pt.get("label")
                            if not candidate or candidate not in all_symbols:
                                cdata = pt.get("customdata")
                                if cdata and isinstance(cdata, list) and len(cdata) > 0:
                                    candidate = cdata[0]
                            if not candidate or candidate not in all_symbols:
                                pidx = pt.get("point_index")
                                if pidx is not None and 0 <= pidx < len(tree_agg):
                                    candidate = tree_agg.iloc[pidx]["symbol"]
                            
                            if candidate in all_symbols:
                                clicked_sym = candidate
                        except Exception as e:
                            import logging
                            logging.getLogger("ibd").warning(f"Error parsing treemap selection event: {e}, event={event}")
                else:
                    st.info("No positions with margin > 0.")
        else:
            st.info("No positions to display.")

        # Check if selection changed to prevent feedback loop
        last_clicked = st.session_state.get("last_treemap_clicked_sym")
        if clicked_sym != last_clicked:
            st.session_state["last_treemap_clicked_sym"] = clicked_sym
            if clicked_sym and clicked_sym in all_symbols:
                st.session_state["analysis_chart_sym"] = clicked_sym

        st.divider()

        _sym_col, _ = st.columns([1, 9])
        with _sym_col:
            st.markdown('<div class="sym-sel-narrow">', unsafe_allow_html=True)
            selected_sym: str | None = st.selectbox(
                "Symbol",
                all_symbols,
                key="analysis_chart_sym",
                label_visibility="collapsed",
            )
            st.markdown('</div>', unsafe_allow_html=True)
        if not selected_sym or selected_sym not in ohlc_store:
            return

        # ── Backtest stats from flex trade history ────────────────────────────
        _flex_path = _MASTER_DIR / "flex_trades.pkl"
        _unds_path = _DATA_DIR / "df_unds.pkl"
        _vd = {"DEPLOY": "🟢", "REFINE": "🟡", "ABANDON": "🔴"}
        if _flex_path.exists():
            _df_all = pd.read_pickle(_flex_path)
            if not _df_all.empty and "pnl" not in _df_all.columns:
                _df_all = _norm(_df_all)
            _perf = _sym_perf(_df_all)
            try:
                _unds_raw = pd.read_pickle(_unds_path) if _unds_path.exists() else pd.DataFrame()
                _unds = _unds_raw.set_index("symbol") if "symbol" in _unds_raw.columns else pd.DataFrame()
            except Exception:
                _unds = pd.DataFrame()
            _av = _select_account_values(snap, acct)
            _nlv = float(_av.get("NetLiquidation", 0)) if _av else 0.0
            _qty_mult = st.session_state.get("cfg_virgin_qty_mult", 0.055)

            if not _perf.empty and selected_sym in _perf["symbol"].values:
                _dte_df = _dte_dist(_df_all)
                _rec = _strat_rec(_perf, _dte_df, selected_sym)
                _sel_margin = (
                    float(_unds.loc[selected_sym, "margin"])
                    if not _unds.empty and selected_sym in _unds.index
                    and pd.notna(_unds.loc[selected_sym, "margin"])
                    else 0.0
                )
                if _nlv > 0 and _sel_margin > 0 and _qty_mult > 0:
                    _sug_qty = max(1, int(_qty_mult * _nlv / _sel_margin))
                    _rec += (
                        f"\nSuggested qty: {_sug_qty} contract(s)"
                        f"  (VIRGIN_QTY_MULT {_qty_mult} × NLV ${_nlv:,.0f} ÷ margin ${_sel_margin:,.0f})"
                    )
                _, _rec_col = st.columns([1, 3])
                with _rec_col:
                    st.code(_rec)
                _score = _sft(_df_all, selected_sym, since=score_since())
                _s1, _s2, _s3, _s4, _s5 = st.columns(5)
                _s1.metric("Score", f"{_score.composite:.0f}/100",
                           f"{_vd.get(_score.verdict, '')} {_score.verdict}",
                           help="Composite backtest quality 0–100. DEPLOY ≥70, REFINE 40–69, ABANDON <40.")
                _s2.metric("Trades", _score.total_trades,
                           help="Closed option contracts used in the backtest.")
                _s3.metric("Win Rate", f"{_score.win_rate:.0%}",
                           help="% of closed trades with positive realised P&L.")
                _s4.metric("Profit Factor", f"{_score.profit_factor:.2f}",
                           help="Gross profit ÷ gross loss. <1.0 = losing edge, 1.5+ = strong.")
                _s5.metric("Years Tested", f"{_score.years_tested:.1f}",
                           help="Date span of trade history. <3 years = insufficient for robust scoring.")
            else:
                st.caption(f"No trade history for {selected_sym} in flex_trades.pkl.")

        st.divider()

        # ── OHLC candlestick chart ────────────────────────────────────────────
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
                hoverinfo="skip",
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

        fig.add_hline(
            y=_last_close_v,
            line_color="#3b82f6",
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
        _STRIKE_COLORS = {
            ("C", "short"): "#92400e",
            ("P", "short"): "#92400e",
            ("C", "long"):  "#3b82f6",
            ("P", "long"):  "#3b82f6",
        }
        if not sym_pos.empty and "secType" in sym_pos.columns:
            _opt_rows = sym_pos[sym_pos["secType"] == "OPT"]
            _price_lo = float(df_chart["Low"].min())  if "Low"  in df_chart.columns else 0.0
            _price_hi = float(df_chart["High"].max()) if "High" in df_chart.columns else float("inf")

            for _, _opt in _opt_rows.iterrows():
                _strike = float(_opt.get("strike", 0) or 0)
                if _strike <= 0 or not (_price_lo * 0.3 <= _strike <= _price_hi * 1.7):
                    continue

                _right    = str(_opt.get("right", ""))
                _expiry_s = str(_opt.get("expiry", ""))
                _dte_v    = int(_dte_series(pd.Series([_expiry_s])).iloc[0] or 0) if _expiry_s else 0
                _state_s  = str(_opt.get("pf_state", ""))
                _pos_qty  = float(_opt.get("position", 0) or 0)
                _direction = "short" if _pos_qty < 0 else "long"

                _iv_v  = float(_opt.get("iv",            float("nan")))
                _und_v = float(_opt.get("underlying_px", float("nan")))
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

                _m_val = next(
                    (float(_opt[mc]) for mc in ["margin_init", "margin_est"]
                     if mc in _opt.index and _opt[mc] == _opt[mc]),
                    float("nan"),
                )
                _margin_s = f"M=${_m_val:,.0f}" if not pd.isna(_m_val) else ""

                _lc         = _STRIKE_COLORS.get((_right, _direction), "#9ca3af")
                _right_name = "Put" if _right == "P" else "Call"
                _qty_abs    = int(abs(_pos_qty))
                _qty_sign   = "−" if _pos_qty < 0 else "+"
                _label = (
                    f"{_qty_sign}{_qty_abs} × {_right_name} {_strike:,.0f}  {_dte_v}d"
                    f"  {_state_s}  {_std_s}"
                    + (f"  {_margin_s}" if _margin_s else "")
                )

                fig.add_hline(
                    y=_strike,
                    line_dash="dot",
                    line_color=_lc,
                    line_width=0.8,
                    annotation_text=_label,
                    annotation_position="top left",
                    annotation_font_size=9,
                    annotation_font_color="#1e2130",
                    annotation_bgcolor="rgba(255,255,255,0.88)",
                    annotation_bordercolor=_lc,
                    row=1, col=1,
                )

        # ── Open order strike lines (yellow) ─────────────────────────────────
        _all_orders = snap.orders
        if not _all_orders.empty and "symbol" in _all_orders.columns:
            _sym_ords = _all_orders[_all_orders["symbol"] == selected_sym]
            if "secType" in _sym_ords.columns:
                _sym_ords = _sym_ords[_sym_ords["secType"] == "OPT"]

            # Track strike levels already claimed by position annotations (all "top left")
            # and by earlier order annotations, to steer new annotations to a free side.
            _top_strikes: list[float] = []
            if not sym_pos.empty and "secType" in sym_pos.columns:
                for _, _r in sym_pos[sym_pos["secType"] == "OPT"].iterrows():
                    _s = float(_r.get("strike", 0) or 0)
                    if _s > 0:
                        _top_strikes.append(_s)
            _bot_strikes: list[float] = []

            def _strike_near(val: float, others: list[float], pct: float = 0.02) -> bool:
                return any(abs(val - o) / max(abs(o), 1.0) < pct for o in others)

            for _, _ord in _sym_ords.iterrows():
                _ord_strike = float(_ord.get("strike", 0) or 0)
                if _ord_strike <= 0:
                    continue
                _ord_action  = str(_ord.get("action", ""))
                _ord_right   = str(_ord.get("right", ""))
                _ord_expiry  = str(_ord.get("expiry", ""))
                _ord_qty     = _ord.get("remaining", _ord.get("qty", ""))
                _ord_lmt_raw = _ord.get("lmtPrice", None)
                try:
                    _ord_lmt = float(_ord_lmt_raw)
                    _lmt_s = f" @${_ord_lmt:.2f}" if not pd.isna(_ord_lmt) else ""
                except (TypeError, ValueError):
                    _lmt_s = ""
                _right_name = "Put" if _ord_right == "P" else "Call" if _ord_right == "C" else _ord_right
                _ord_label = (
                    f"ORDER {_ord_action} {_ord_qty}×{_right_name}"
                    f" {_ord_strike:,.0f}  {_ord_expiry}{_lmt_s}"
                )
                # Use "top left" unless a position or prior order already sits there;
                # fall back to "bottom left" (and track it too to avoid order-on-order clash).
                if _strike_near(_ord_strike, _top_strikes):
                    _ord_annot_pos = "bottom left"
                    _bot_strikes.append(_ord_strike)
                else:
                    _ord_annot_pos = "top left"
                    _top_strikes.append(_ord_strike)
                # Place label at horizontal centre of the chart (x=0.5 paper)
                # so it doesn't clash with the left-edge blue price label.
                # annotation_position still controls the top/bottom yanchor;
                # the explicit annotation_x / xanchor override the left-edge x.
                fig.add_hline(
                    y=_ord_strike,
                    line_dash="solid",
                    line_color="#fbbf24",
                    line_width=1.5,
                    annotation_text=_ord_label,
                    annotation_position=_ord_annot_pos,
                    annotation_x=0.5,
                    annotation_xanchor="center",
                    annotation_font_size=9,
                    annotation_font_color="#1e2130",
                    annotation_bgcolor="#fbbf24",
                    annotation_bordercolor="#fbbf24",
                    row=1, col=1,
                )

        _hl = {"bgcolor": "#ffffff", "font": {"color": "#1e2130", "size": 11}, "bordercolor": "#cbd5e1"}
        fig.update_traces(hoverlabel=_hl)

        fig.update_layout(
            height=700,
            showlegend=True,
            hovermode="x unified",
            legend={
                "orientation": "h", "yanchor": "bottom", "y": 1.02, "xanchor": "left", "x": 0,
                "bgcolor": "rgba(0,0,0,0)",
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

        # ── Per-symbol combined table (Pf / Suggested Orders / Open Orders) ────────
        import numpy as _np  # noqa: PLC0415

        # Suggested-order rows from each strategy pickle
        _SO_PICKLES = [
            ("df_cov.pkl",     "SELL"),
            ("df_nkd.pkl",     "SELL"),
            ("df_reap.pkl",    "BUY"),
            ("df_protect.pkl", "BUY"),
        ]
        _so_frames: list[pd.DataFrame] = []
        for _pkl_name, _so_action in _SO_PICKLES:
            _raw_so = _load_pkl(_pkl_name)
            if _raw_so.empty or "symbol" not in _raw_so.columns:
                continue
            _fsym = _raw_so[_raw_so["symbol"] == selected_sym].copy()
            if _fsym.empty:
                continue
            _fsym["source"] = "SOrd"
            if "action" not in _fsym.columns:
                _fsym["action"] = _so_action
            if "secType" not in _fsym.columns:
                _fsym["secType"] = "OPT"
            # Use qty (contracts to trade) as position; df_reap has both qty and position
            if "qty" in _fsym.columns:
                _fsym["position"] = _fsym["qty"]
            elif "position" not in _fsym.columns:
                _fsym["position"] = 0
            _fsym["lmt_px"] = _fsym["xPrice"] if "xPrice" in _fsym.columns else float("nan")
            if "dte" not in _fsym.columns and "expiry" in _fsym.columns:
                _fsym["dte"] = _dte_series(_fsym["expiry"]).fillna(0).astype(int)
            if "margin" not in _fsym.columns:
                _fsym["margin"] = _fsym["margin_init"] if "margin_init" in _fsym.columns else float("nan")
            _so_frames.append(_fsym)

        # Open-order rows from snap
        _ord_frame: pd.DataFrame = pd.DataFrame()
        if not snap.orders.empty and "symbol" in snap.orders.columns:
            _o_sym = snap.orders[snap.orders["symbol"] == selected_sym].copy()
            if not _o_sym.empty:
                _o_sym["source"] = "Ord"
                _o_sym["position"] = _o_sym["remaining"] if "remaining" in _o_sym.columns else 0
                _o_sym["lmt_px"] = _o_sym["lmtPrice"] if "lmtPrice" in _o_sym.columns else float("nan")
                _o_sym["pf_state"] = ""
                _o_sym["margin"] = float("nan")
                if "dte" not in _o_sym.columns and "expiry" in _o_sym.columns:
                    _o_sym["dte"] = _dte_series(_o_sym["expiry"]).fillna(0).astype(int)
                _ord_frame = _o_sym

        # Portfolio rows
        _pf_len = 0
        if not sym_pos.empty:
            sym_pos = sym_pos.copy()
            sym_pos["source"] = "Pf"
            sym_pos["action"] = ""
            sym_pos["lmt_px"] = float("nan")
            if "expiry" in sym_pos.columns:
                sym_pos["dte"] = _dte_series(sym_pos["expiry"]).fillna(0).astype(int)
            if "margin_init" in sym_pos.columns:
                sym_pos["margin"] = sym_pos["margin_init"]
                _sym_mlabel = "Margin"
            else:
                sym_pos["margin"] = position_margin_est(sym_pos)
                _sym_mlabel = "Margin*"
            _pf_len = len(sym_pos)
        else:
            _sym_mlabel = "Margin"

        _all_frames: list[pd.DataFrame] = []
        if not sym_pos.empty:
            _all_frames.append(sym_pos)
        _all_frames.extend(_so_frames)
        if not _ord_frame.empty:
            _all_frames.append(_ord_frame)

        st.markdown(f"**{selected_sym} — positions & orders**")
        if not _all_frames:
            st.caption(f"No positions, suggested orders, or open orders for {selected_sym}.")
        else:
            combined = pd.concat(_all_frames, ignore_index=True, sort=False)

            # ITM highlight mask — Pf rows only (amber tint)
            if _pf_len > 0:
                _pf_part = combined.iloc[:_pf_len]
                _c_und = _pf_part["underlying_px"] if "underlying_px" in _pf_part.columns else pd.Series(float("nan"), index=_pf_part.index)
                _c_str = _pf_part["strike"] if "strike" in _pf_part.columns else pd.Series(float("nan"), index=_pf_part.index)
                _c_rgt = _pf_part["right"] if "right" in _pf_part.columns else pd.Series("", index=_pf_part.index)
                _pf_itm = (
                    ((_pf_part["secType"] == "OPT") & (_c_rgt == "C") & (_c_und > _c_str))
                    | ((_pf_part["secType"] == "OPT") & (_c_rgt == "P") & (_c_und < _c_str))
                ).to_numpy(dtype=bool)
                _s_itm = _np.concatenate([_pf_itm, _np.zeros(len(combined) - _pf_len, dtype=bool)])
            else:
                _s_itm = _np.zeros(len(combined), dtype=bool)

            # Clean up STK rows
            if "secType" in combined.columns:
                _stk = combined["secType"] == "STK"
                if all(c in combined.columns for c in ("underlying_px", "marketPrice")):
                    combined.loc[_stk, "underlying_px"] = combined.loc[_stk, "marketPrice"]
                for _col in ("right", "strike", "dte"):
                    if _col in combined.columns:
                        combined.loc[_stk, _col] = None if _col != "right" else ""
            if "position" in combined.columns:
                combined["position"] = combined["position"].fillna(0).astype(int)

            disp_cols = [
                "symbol", "source", "secType", "underlying_px", "right", "strike", "expiry", "dte",
                "position", "action", "pf_state", "lmt_px",
                "avgCost", "marketPrice", "marketValue",
                "unrealizedPNL", "delta", "theta", "vega", "iv", "delta_$", "margin",
            ]
            sym_view = combined[[c for c in disp_cols if c in combined.columns]].reset_index(drop=True)
            st.dataframe(
                _banded(sym_view, _s_itm),
                hide_index=True,
                width="stretch",
                column_config={
                    "symbol":        st.column_config.TextColumn("Symbol",      help="Ticker symbol"),
                    "source":        st.column_config.TextColumn("Source",      help="Pf=Portfolio position  ·  SOrd=Suggested Order  ·  Ord=Open IBKR order"),
                    "secType":       st.column_config.TextColumn("Type",        help="Security type: STK=stock, OPT=option"),
                    "underlying_px": st.column_config.NumberColumn("Underlying",format="$%.2f",   help="Current underlying stock price"),
                    "right":         st.column_config.TextColumn("C/P",         help="C=Call, P=Put"),
                    "position":      st.column_config.NumberColumn("Qty",       format="%d",      help="Contracts held (Pf) or to trade (SO/O); negative = short"),
                    "dte":           st.column_config.NumberColumn("DTE",       format="%.0f",    help="Calendar days to expiration"),
                    "strike":        st.column_config.NumberColumn("Strike",    format="$%,.1f",  help="Option strike price"),
                    "expiry":        st.column_config.TextColumn("Expiry",      help="Option expiration date"),
                    "action":        st.column_config.TextColumn("Action",      help="BUY or SELL (SO and O rows only)"),
                    "pf_state":      st.column_config.TextColumn("State",       help="Portfolio state: CC=covered call, PP=protective put, NP=naked put, etc."),
                    "lmt_px":        st.column_config.NumberColumn("Limit Px",  format="$%,.2f",  help="Target execution price: xPrice for SO; lmtPrice for O"),
                    "avgCost":       st.column_config.NumberColumn("Avg Cost",  format="$%,.2f",  help="Average cost basis per share/contract (for options: premium collected or paid)"),
                    "marketPrice":   st.column_config.NumberColumn("Mkt Px",    format="$%,.2f",  help="Current market price"),
                    "marketValue":   st.column_config.NumberColumn("Mkt Val",   format="$%,.0f",  help="Total market value = price × qty × multiplier"),
                    "unrealizedPNL": st.column_config.NumberColumn("Unreal P&L",format="$%,.0f", help="Unrealized profit/loss at current market price"),
                    "delta":         st.column_config.NumberColumn("Δ Delta",   format="%.3f",    help="Price change per $1 move in the underlying"),
                    "theta":         st.column_config.NumberColumn("Θ Theta",   format="%.3f",    help="Daily time decay in option value"),
                    "vega":          st.column_config.NumberColumn("ν Vega",    format="%.3f",    help="Sensitivity to a 1% change in implied volatility"),
                    "iv":            st.column_config.NumberColumn("IV",        format="%.3f",    help="Implied volatility of the option (decimal, e.g. 0.25 = 25%)"),
                    "delta_$":       st.column_config.NumberColumn("Delta $",   format="$%,.0f",  help="Dollar delta = delta × qty × underlying_px × 100"),
                    "margin":        st.column_config.NumberColumn(_sym_mlabel, format="$%,.0f",  help="IBKR what-if margin (Pf) or Reg-T estimate (SO)"),
                },
            )


_PROVIDER_HINTS: dict[str, tuple[str, str]] = {
    "DeepSeek": ("platform.deepseek.com",          "https://platform.deepseek.com/"),
    "Gemini":   ("aistudio.google.com/app/apikey", "https://aistudio.google.com/app/apikey"),
    "Claude":   ("console.anthropic.com",          "https://console.anthropic.com"),
}
_LLM_MAX_HISTORY = 5  # rolling window: oldest turn dropped when full; 5 turns ≈ 3 000 extra tokens


def _fmt_date_col(s: pd.Series, fallback: str = "?") -> pd.Series:
    """Format a date/datetime Series to 'YYYY-MM-DD' strings."""
    if pd.api.types.is_datetime64_any_dtype(s):
        return s.dt.strftime("%Y-%m-%d").fillna(fallback)
    return s.astype(str).str[:10].fillna(fallback)


_PERF_CHART_DEFAULT_NAV = 632_507
# Wired to FOCUS_DATE (snp_config.yml / dashboard date picker). Re-evaluated each
# script rerun, so changing the focus date moves the display window + NAV anchor.
_DEFAULT_PERF_START     = focus_date()   # default display window start
_PERF_CHART_REF_DATE    = focus_date()   # anchor for reference NAV cards


def _render_perf_chart(
    flex_path: Path, ohlc_path: Path, cash_path: Path, nav_path: Path
) -> None:
    """Cumulative performance vs. SPY/QQQ benchmark — shown above Trade History & Backtest."""
    import math

    with st.expander("📈 Performance Dashboard", expanded=True, key="exp_ana_perf"):

        # ── Guards ────────────────────────────────────────────────────────
        if not flex_path.exists() or not ohlc_path.exists():
            st.info(
                "Requires flex_trades.pkl and ohlc.pkl — "
                "update trades (🔄 Update Trades) and run OHLC update first."
            )
            return
        try:
            df_flex: pd.DataFrame = pd.read_pickle(flex_path)
            ohlc: dict = pd.read_pickle(ohlc_path)
        except Exception:
            st.warning("Could not load performance data — pickle read error.")
            return

        _required = {"dateTime", "pnl", "assetCategory", "openCloseIndicator"}
        if df_flex.empty or not _required.issubset(df_flex.columns):
            st.info("No trade data yet — click 🔄 Update Trades.")
            return

        spy_df = ohlc.get("SPY")
        if spy_df is None or spy_df.empty or "Close" not in spy_df.columns:
            st.warning("SPY OHLC data missing — run OHLC Update to enable this chart.")
            return

        # ── Load consolidated NAV (full history, unclipped) ───────────────
        _nav_series_full = pd.Series(dtype=float)
        if nav_path.exists():
            try:
                _df_nav = pd.read_pickle(nav_path)
                if not _df_nav.empty and {"reportDate", "total"}.issubset(_df_nav.columns):
                    _nav_series_full = _df_nav.set_index("reportDate")["total"].sort_index()
            except Exception:
                pass

        # ── Patch today with live NLV when Flex data is stale ────────────
        # Reads _LIVE_NAV_TODAY written by kpi_strip — no extra snapshot() call needed.
        _today_ts = pd.Timestamp.today().normalize()
        if (
            len(_nav_series_full) >= 1
            and _nav_series_full.index.max() < _today_ts
            and _LIVE_NAV_TODAY.get("date") == _today_ts
            and _LIVE_NAV_TODAY.get("nlv", 0) > 0
        ):
            _nav_series_full = pd.concat([
                _nav_series_full,
                pd.Series({_today_ts: _LIVE_NAV_TODAY["nlv"]}),
            ]).sort_index()

        _have_nav = len(_nav_series_full) >= 2

        # ── Load USD cash flows for TWR adjustment ─────────────────────────
        _cf_daily = pd.Series(dtype=float)
        if cash_path.exists():
            try:
                _df_cash_raw = pd.read_pickle(cash_path)
                if not _df_cash_raw.empty and {"type", "amount", "date", "currency"}.issubset(
                    _df_cash_raw.columns
                ):
                    _usd_dw = _df_cash_raw[
                        _df_cash_raw["type"].str.contains("Deposit|Withdraw", case=False, na=False)
                        & (_df_cash_raw["currency"] == "USD")
                        & (_df_cash_raw["amount"].abs() >= 500.0)
                    ].copy()
                    _usd_dw["date"] = pd.to_datetime(_usd_dw["date"])
                    _cf_daily = _usd_dw.groupby("date")["amount"].sum()
            except Exception:
                pass

        # ── OPT P&L series (full history) ─────────────────────────────────
        closed_opt = df_flex[
            (df_flex["openCloseIndicator"] == "C") &
            (df_flex["assetCategory"] == "OPT")
        ].copy()
        if closed_opt.empty:
            st.info("No closed option trades found in flex_trades.pkl.")
            return
        closed_opt["_date"] = pd.to_datetime(closed_opt["dateTime"]).dt.normalize()
        daily_pnl: pd.Series = closed_opt.groupby("_date")["pnl"].sum().sort_index()

        # ── t0 = earliest data (trades or NAV) — no floor applied ─────────
        t0_trades = daily_pnl.index.min()
        t0_nav    = _nav_series_full.index.min() if _have_nav else t0_trades
        t0        = min(t0_trades, t0_nav)
        _today    = pd.Timestamp.today().normalize()

        # ── Period shortcut buttons ───────────────────────────────────────────
        # Buttons set widget keys directly in session_state then rerun — this
        # is the only reliable way to force date_input to show the new value.
        # Using a staging key + value= can be silently ignored by Streamlit's
        # widget reconciliation when the key already exists in session state.
        _default_start = max(_DEFAULT_PERF_START.date(), t0.date())
        if "perf_date_start" not in st.session_state:
            st.session_state["perf_date_start"] = _default_start
        if "perf_date_end" not in st.session_state:
            st.session_state["perf_date_end"] = _today.date()

        _eff_start = st.session_state["perf_date_start"]
        _eff_end   = st.session_state["perf_date_end"]

        _pb = st.columns(8)
        _period_presets = [
            ("Focus", _default_start),
            ("MTD",   max(_today.replace(day=1).date(), t0.date())),
            ("1M",    max((_today - pd.DateOffset(months=1)).date(), t0.date())),
            ("3M",    max((_today - pd.DateOffset(months=3)).date(), t0.date())),
            ("YTD",   max(_today.replace(month=1, day=1).date(), t0.date())),
            ("1Y",    max((_today - pd.DateOffset(years=1)).date(), t0.date())),
            ("3Y",    max((_today - pd.DateOffset(years=3)).date(), t0.date())),
            ("All",   t0.date()),
        ]
        _active_preset_lbl = next(
            (_plbl for _plbl, _pstart in _period_presets
             if _eff_start == _pstart and _eff_end == _today.date()),
            None,
        )
        for _pcol, (_plbl, _pstart) in zip(_pb, _period_presets):
            if _pcol.button(
                _plbl, key=f"perf_period_{_plbl}", width="stretch",
                type="primary" if _plbl == _active_preset_lbl else "secondary",
            ):
                st.session_state["perf_date_start"] = _pstart
                st.session_state["perf_date_end"]   = _today.date()
                st.rerun()

        # ── Controls: date pickers — no value= so Streamlit uses session_state
        # exclusively (avoids double-set conflict with _ANA_PERSIST restores) ──
        _c1, _c2, _c3, _c4, _c5 = st.columns([2, 2, 2, 1, 1])
        _d_start = _c4.date_input(
            "From",
            min_value=t0.date(),
            max_value=_today.date(),
            key="perf_date_start",
        )
        _d_end = _c5.date_input(
            "To",
            min_value=t0.date(),
            max_value=_today.date(),
            key="perf_date_end",
        )
        if _d_start >= _d_end:
            st.warning("'From' date must be earlier than 'To' date.")
            return
        _d_start_ts = pd.Timestamp(_d_start)
        _d_end_ts   = pd.Timestamp(_d_end)

        _d_start_lbl = _d_start.strftime("%d-%b-%Y")
        _d_end_lbl   = _d_end.strftime("%d-%b-%Y")

        # ── Full-period bdays for OPT P&L proxy (computed first — needed for back-calc) ──
        # Include today even if weekend/holiday so live NAV appears on the chart.
        _bdays_core = pd.bdate_range(start=t0, end=_today)
        bdays = (
            _bdays_core.append(pd.DatetimeIndex([_today_ts]))
            if _today_ts not in _bdays_core else _bdays_core
        )
        cum_pnl = daily_pnl.reindex(bdays, fill_value=0.0).cumsum()
        _cum_before_start = cum_pnl[cum_pnl.index <= _d_start_ts]
        _cum_at_start    = float(_cum_before_start.iloc[-1]) if not _cum_before_start.empty else 0.0

        if _have_nav:
            _avail_before = _nav_series_full[_nav_series_full.index <= _d_start_ts]
            if not _avail_before.empty:
                starting_capital = float(_avail_before.iloc[-1])
                _start_nav_lbl   = _d_start_lbl
            else:
                # t0 predates flex_nav data; back-calculate NAV at t0 so the OPT P&L
                # proxy is anchored to the correct baseline rather than the first
                # available NAV (which may be years later and much larger).
                _first_nav_ts       = _nav_series_full.index[0]
                _first_nav_val      = float(_nav_series_full.iloc[0])
                _cum_at_first_cands = cum_pnl[cum_pnl.index <= _first_nav_ts]
                _cum_at_first       = (
                    float(_cum_at_first_cands.iloc[-1])
                    if not _cum_at_first_cands.empty else _cum_at_start
                )
                starting_capital = _first_nav_val - (_cum_at_first - _cum_at_start)
                _start_nav_lbl   = f"{_first_nav_ts.strftime('%d-%b-%Y')} (est.)"
            _avail_at_end = _nav_series_full[_nav_series_full.index <= _d_end_ts]
            _ending_nav   = float(_avail_at_end.iloc[-1]) if not _avail_at_end.empty else None
            _period_gain  = (_ending_nav - starting_capital) if _ending_nav is not None else None
            _period_ret   = (
                ((_ending_nav / starting_capital - 1) * 100)
                if (_ending_nav is not None and starting_capital > 0) else None
            )
            _c1.metric(
                f"Consolidated NAV ({_start_nav_lbl})",
                f"${starting_capital:,.0f}",
                help="Flex consolidated NAV (US + SG) at the 'From' date.",
            )
            if _ending_nav is not None:
                _c2.metric(
                    f"Consolidated NAV ({_d_end_lbl})",
                    f"${_ending_nav:,.0f}",
                    delta=f"{_period_ret:+.1f}% vs {_d_start_lbl}" if _period_ret is not None else None,
                )
            if _period_gain is not None:
                _c3.metric(
                    "Period Gain / Loss",
                    f"${_period_gain:+,.0f}",
                    help=f"Consolidated NAV change from {_d_start_lbl} to {_d_end_lbl}.",
                )
        else:
            starting_capital = _c1.number_input(
                "NAV at Period Start ($)",
                min_value=10_000, max_value=100_000_000,
                value=st.session_state.get("perf_start_capital", _PERF_CHART_DEFAULT_NAV),
                step=1_000, format="%d", key="perf_start_capital",
                help="Consolidated NAV at the display start date — base for OPT P&L % chart.",
            )

        options_nav_full = starting_capital + (cum_pnl - _cum_at_start)

        # NAV reindexed to bdays for bar chart
        _nav_bdays_full = (
            _nav_series_full.reindex(bdays, method="ffill")
            if _have_nav else pd.Series(dtype=float)
        )

        # ── Raw benchmark closes (reindex to bdays) ────────────────────────
        def _raw_closes(df_ohlc: pd.DataFrame) -> pd.Series:
            closes = df_ohlc["Close"].sort_index()
            return closes[closes.index >= t0].reindex(bdays, method="ffill").dropna()

        spy_closes  = _raw_closes(spy_df)
        qqq_df      = ohlc.get("QQQ")
        qqq_closes  = (
            _raw_closes(qqq_df)
            if qqq_df is not None and "Close" in qqq_df.columns
            else pd.Series(dtype=float)
        )

        # ── Cash markers (all events from t0 onwards) ─────────────────────
        _dep_events: pd.DataFrame = pd.DataFrame()
        if cash_path.exists():
            try:
                _df_cash = pd.read_pickle(cash_path)
                if not _df_cash.empty and {"type", "amount", "date"}.issubset(_df_cash.columns):
                    _dw = _df_cash[
                        _df_cash["type"].str.contains("Deposit|Withdraw", case=False, na=False)
                        & (_df_cash["amount"].abs() >= 1.0)
                        & (_df_cash["date"] >= t0)
                    ].copy()
                    if not _dw.empty:
                        # Deduplicate same (amount, currency) within the same Mon-Sun week —
                        # IBKR sometimes reports a deposit on both transaction date and settle date.
                        _dw_dates = pd.to_datetime(_dw["date"], errors="coerce")
                        _dw["_week"] = _dw_dates - pd.to_timedelta(_dw_dates.dt.dayofweek, unit="D")
                        _fkey = [c for c in ("accountId", "amount", "currency", "description") if c in _dw.columns]
                        _dw = (
                            _dw.sort_values("date")
                            .drop_duplicates(subset=_fkey + ["_week"], keep="last")
                            .drop(columns=["_week"])
                        )
                        _dep_events = _dw[["date", "amount", "currency"]].copy()
            except Exception:
                pass

        # ── TWR: strips out USD deposit effects from the Consolidated % line ─
        # Consecutive NAV changes adjusted for any USD cash flows in the interval.
        # SGD deposits are not converted (no FX rates) and remain in the raw NAV.
        def _compute_twr(nav_s: pd.Series, cf: pd.Series, start_ts, end_ts) -> pd.Series:
            import numpy as np  # app.py has no module-level numpy import
            nav = nav_s[(nav_s.index >= start_ts) & (nav_s.index <= end_ts)]
            if len(nav) < 2:
                return pd.Series(dtype=float)

            # Vectorised cash-flow alignment via numpy searchsorted (O(N log M)).
            cf_sorted = cf.sort_index() if not cf.empty else pd.Series(dtype=float)
            cf_dates  = cf_sorted.index.values
            cf_values = cf_sorted.values
            cf_cumsum = np.cumsum(cf_values) if len(cf_values) else np.array([], dtype=float)

            nav_dates  = nav.index.values
            nav_values = nav.values.astype(float)

            cf_cum_at_nav = np.zeros(len(nav_dates))
            if len(cf_cumsum):
                cf_idx = np.searchsorted(cf_dates, nav_dates, side="right")
                valid  = cf_idx > 0
                cf_cum_at_nav[valid] = cf_cumsum[cf_idx[valid] - 1]
            cf_in_periods = cf_cum_at_nav[1:] - cf_cum_at_nav[:-1]

            n = len(nav_values)
            cum_perf   = np.zeros(n)
            cumulative = 1.0
            prev_val   = nav_values[0]
            for i in range(1, n):
                denom = prev_val + cf_in_periods[i - 1]
                if denom > 0:
                    cumulative *= nav_values[i] / denom
                cum_perf[i] = (cumulative - 1.0) * 100.0
                prev_val = nav_values[i]

            s = pd.Series(cum_perf, index=nav.index)
            _twr_bdays = pd.bdate_range(start_ts, end_ts)
            if end_ts not in _twr_bdays:
                _twr_bdays = _twr_bdays.append(pd.DatetimeIndex([end_ts]))
            return s.reindex(_twr_bdays, method="ffill").dropna()

        # ── Clip & rebase (for OPT P&L, SPY, QQQ — no deposit adjustment) ─
        def _clip_rebase(series: pd.Series) -> pd.Series:
            # Right-clip only; rebase at _d_start_ts so pre-start data is visible when
            # zooming out with 1Y/3Y/All without leaving an empty gap.
            s = series[series.index <= _d_end_ts]
            if len(s) < 2:
                return pd.Series(dtype=float)
            _base_cands = s[s.index <= _d_start_ts]
            if _base_cands.empty:
                return pd.Series(dtype=float)
            base = float(_base_cands.iloc[-1])
            if base == 0:
                return pd.Series(dtype=float)
            return (s / base - 1.0) * 100.0

        # NAV TWR: compute from t0 (full history) then reanchor to 0% at _d_start_ts
        if _have_nav:
            _nav_twr_raw = _compute_twr(_nav_series_full, _cf_daily, t0, _d_end_ts)
            if not _nav_twr_raw.empty:
                _anchor_cands = _nav_twr_raw[_nav_twr_raw.index <= _d_start_ts]
                if not _anchor_cands.empty:
                    _anchor_pct = float(_anchor_cands.iloc[-1])
                    nav_index = (
                        (1.0 + _nav_twr_raw / 100.0) / (1.0 + _anchor_pct / 100.0) - 1.0
                    ) * 100.0
                else:
                    nav_index = _nav_twr_raw
            else:
                nav_index = _nav_twr_raw
        else:
            nav_index = pd.Series(dtype=float)

        opt_index = _clip_rebase(options_nav_full)
        spy_index = _clip_rebase(spy_closes)
        qqq_index = _clip_rebase(qqq_closes) if not qqq_closes.empty else pd.Series(dtype=float)

        if opt_index.empty and nav_index.empty:
            st.info("No data in the selected date range.")
            return

        # Full-history dollar series for bars and hover customdata (right-clip only)
        _nav_display = (
            _nav_bdays_full[_nav_bdays_full.index <= _d_end_ts]
            if _have_nav else pd.Series(dtype=float)
        )
        _opt_display = options_nav_full[options_nav_full.index <= _d_end_ts]

        # ── Metrics (period-clipped to [_d_start_ts, _d_end_ts] so cards stay correct) ──
        _primary_pct = nav_index if not nav_index.empty else opt_index
        _primary_nav = _nav_display if (_have_nav and not _nav_display.empty) else _opt_display

        # Period-only slices for metric cards — traces extend further but metrics stay pinned
        _opt_period  = options_nav_full[
            (options_nav_full.index >= _d_start_ts) & (options_nav_full.index <= _d_end_ts)
        ]
        _pct_period  = _primary_pct[_primary_pct.index >= _d_start_ts] if not _primary_pct.empty else pd.Series(dtype=float)
        _nav_period  = _primary_nav[_primary_nav.index >= _d_start_ts] if not _primary_nav.empty else pd.Series(dtype=float)

        display_pnl = (
            float(_opt_period.iloc[-1] - _opt_period.iloc[0])
            if len(_opt_period) >= 2 else 0.0
        )
        _consolidated_ret = float(_primary_pct.iloc[-1]) if not _primary_pct.empty else 0.0
        spy_ret_pct = float(spy_index.iloc[-1]) if not spy_index.empty else None
        alpha_pp    = (_consolidated_ret - spy_ret_pct) if spy_ret_pct is not None else None

        # Max drawdown with peak→trough dates — computed on period only
        max_dd, _dd_period = 0.0, ""
        if not _pct_period.empty:
            _pct1    = 1.0 + _pct_period / 100.0
            _peak_tw = _pct1.cummax()
            _dd_tw   = (_pct1 / _peak_tw - 1.0) * 100.0
            max_dd   = float(_dd_tw.min())
            _dd_trough = _dd_tw.idxmin()
            _to_trough = _pct1[:_dd_trough]
            _dd_peak   = _to_trough.idxmax() if len(_to_trough) > 0 else _dd_trough
            _dd_period = f"{_dd_peak.strftime('%b %d, %Y')} → {_dd_trough.strftime('%b %d, %Y')}"

        _daily_ret = (
            _nav_period.pct_change()
            .replace([float("inf"), float("-inf")], float("nan"))
            .dropna()
        )
        sharpe = (
            float(_daily_ret.mean() / _daily_ret.std() * math.sqrt(252))
            if len(_daily_ret) >= 2 and _daily_ret.std() > 0
            else 0.0
        )

        _m1, _m2, _m3, _m4, _m5 = st.columns(5)
        _m1.metric(
            "Realized OPT P&L", f"${display_pnl:,.0f}",
            help=f"Closed OPT trade P&L for {_d_start} → {_d_end}.",
        )
        _m2.metric(
            "Consolidated TWR" if _have_nav else "Portfolio Return",
            f"{_consolidated_ret:+.1f}%",
            help=(
                f"Time-weighted return of consolidated NAV (USD deposits stripped). "
                f"SGD deposits are not FX-adjusted. Zeroed at {_d_start}."
                if _have_nav
                else f"Closed OPT P&L % return zeroed at {_d_start}. Update Trades to load Flex NAV."
            ),
        )
        _m3.metric(
            "Alpha vs SPY",
            f"{alpha_pp:+.1f} pp" if alpha_pp is not None else "N/A",
            help="Consolidated TWR minus SPY price return over the displayed period.",
        )
        _m4.metric(
            "Max Drawdown", f"{max_dd:.1f}%",
            delta=_dd_period if _dd_period else None,
            delta_color="off",
            help="Worst peak-to-trough drawdown in the primary return series. Delta shows the drawdown window.",
        )
        _m5.metric(
            "Sharpe Ratio", f"{sharpe:.2f}",
            help="Annualised Sharpe ratio (daily NAV returns × √252, no risk-free rate).",
        )

        # ── Chart ─────────────────────────────────────────────────────────
        # Y-axes: Consolidated NAV ($) on LEFT (primary), % return on RIGHT (secondary).
        fig = go.Figure()

        # NAV $ bars on PRIMARY (left) axis
        _bar_y     = _nav_display if (_have_nav and not _nav_display.empty) else _opt_display
        _bar_label = "Consolidated NAV ($)" if _have_nav else "OPT NAV ($)"
        if not _bar_y.empty:
            fig.add_trace(go.Bar(
                x=_bar_y.index, y=_bar_y.values,
                name=_bar_label,
                marker_color="rgba(148,163,184,0.22)",
                yaxis="y",
                hoverinfo="skip",
            ))

        # All % lines on SECONDARY (right) axis
        if _have_nav and not nav_index.empty:
            _nav_equiv = _nav_display.reindex(nav_index.index, method="ffill").values
            _nav_text  = [f"{v:+.2f}%" for v in nav_index.values]
            fig.add_trace(go.Scatter(
                x=nav_index.index, y=nav_index.values,
                customdata=_nav_equiv, text=_nav_text,
                name="Consolidated", yaxis="y2",
                line=dict(color="#a78bfa", width=2.5),
                hovertemplate="%{text}  $%{customdata:,.0f}<extra>Consolidated</extra>",
            ))

        if not spy_index.empty:
            spy_equiv = ((spy_index / 100.0 + 1.0) * starting_capital).values
            _spy_text = [f"{v:+.2f}%" for v in spy_index.values]
            fig.add_trace(go.Scatter(
                x=spy_index.index, y=spy_index.values,
                customdata=spy_equiv, text=_spy_text,
                name="SPY", yaxis="y2",
                line=dict(color="#34d399", width=1.5),
                hovertemplate="%{text}  $%{customdata:,.0f}<extra>SPY</extra>",
            ))
        if not qqq_index.empty:
            qqq_equiv = ((qqq_index / 100.0 + 1.0) * starting_capital).values
            _qqq_text = [f"{v:+.2f}%" for v in qqq_index.values]
            fig.add_trace(go.Scatter(
                x=qqq_index.index, y=qqq_index.values,
                customdata=qqq_equiv, text=_qqq_text,
                name="QQQ", yaxis="y2",
                line=dict(color="#fbbf24", width=1.5, dash="dot"),
                hovertemplate="%{text}  $%{customdata:,.0f}<extra>QQQ</extra>",
            ))

        if not opt_index.empty:
            _opt_equiv = _opt_display.reindex(opt_index.index, method="ffill").values
            _opt_text  = [f"{v:+.2f}%" for v in opt_index.values]
            fig.add_trace(go.Scatter(
                x=opt_index.index, y=opt_index.values,
                customdata=_opt_equiv, text=_opt_text,
                name="OPT P&L", yaxis="y2",
                line=dict(color="#60a5fa", width=1.5, dash="dash" if _have_nav else "solid"),
                hovertemplate="%{text}  $%{customdata:,.0f}<extra>OPT P&L</extra>",
            ))

        # Cash markers on secondary % axis (at 0%) — full history so they show when zoomed out
        if not _dep_events.empty:
            _dve  = _dep_events[_dep_events["date"] <= _d_end_ts]
            _deps = _dve[_dve["amount"] > 0]
            _wits = _dve[_dve["amount"] < 0]
            def _agg_cash_markers(df: pd.DataFrame) -> pd.DataFrame:
                cur_col = df.get("currency", pd.Series([""] * len(df)))
                df = df.copy()
                df["currency"] = cur_col.values
                by_date_cur = (
                    df.groupby(["date", "currency"], sort=True)["amount"]
                    .sum()
                    .reset_index()
                )
                def _fmt(grp):
                    parts = [f"{row.currency} {abs(row.amount):,.0f}" for row in grp.itertuples()]
                    return " / ".join(parts)
                result = by_date_cur.groupby("date", sort=True).apply(_fmt).reset_index()
                result.columns = ["date", "_text"]
                return result

            if not _deps.empty:
                _deps_g = _agg_cash_markers(_deps)
                fig.add_trace(go.Scatter(
                    x=_deps_g["date"], y=[0.0] * len(_deps_g), mode="markers",
                    marker=dict(symbol="triangle-up", size=11,
                                color="#22c55e", line=dict(width=1, color="#166534")),
                    name="Deposit", text=_deps_g["_text"].values, yaxis="y2",
                    hovertemplate="%{text}<extra>Deposit</extra>",
                ))
            if not _wits.empty:
                _wits_g = _agg_cash_markers(_wits)
                fig.add_trace(go.Scatter(
                    x=_wits_g["date"], y=[0.0] * len(_wits_g), mode="markers",
                    marker=dict(symbol="triangle-down", size=11,
                                color="#ef4444", line=dict(width=1, color="#991b1b")),
                    name="Withdrawal", text=_wits_g["_text"].values, yaxis="y2",
                    hovertemplate="%{text}<extra>Withdrawal</extra>",
                ))

        # ── Y-axis ranges: computed from visible window so period buttons rescale ──
        def _windowed_range(
            series_list: list[pd.Series],
            extra_vals: list[float] | None = None,
            pad: float = 0.08,
        ) -> list[float] | None:
            vals: list[float] = list(extra_vals or [])
            for _s in series_list:
                if _s.empty:
                    continue
                _w = _s[(_s.index >= _d_start_ts) & (_s.index <= _d_end_ts)]
                if not _w.empty:
                    vals.extend(_w.dropna().tolist())
            if not vals:
                return None
            _lo, _hi = min(vals), max(vals)
            _span = (_hi - _lo) or 1.0
            return [_lo - _span * pad, _hi + _span * pad]

        _y1_range = _windowed_range([_bar_y])

        # Align y2 (%) so 0% sits at the same axis fraction as the starting NAV
        # bar on y1. With y2_lo = -f·S and y2_hi = (1−f)·S, the zero-line
        # overlaps the top of the first bar, and % lines visually track bar growth.
        _y2_range = None
        if _y1_range is not None and _have_nav:
            _y1_lo, _y1_hi = _y1_range
            _y1_span = _y1_hi - _y1_lo
            if _y1_span > 0:
                _f = (starting_capital - _y1_lo) / _y1_span
                if 0.01 < _f < 0.99:
                    _y2_pts = [0.0]
                    for _s2 in [nav_index, opt_index, spy_index, qqq_index]:
                        if _s2.empty:
                            continue
                        _w2 = _s2[(_s2.index >= _d_start_ts) & (_s2.index <= _d_end_ts)]
                        _y2_pts.extend(_w2.dropna().tolist())
                    _lo2, _hi2 = min(_y2_pts), max(_y2_pts)
                    _spans2 = []
                    if _f > 0 and _lo2 < 0:
                        _spans2.append(-_lo2 / _f)
                    if _f < 1 and _hi2 > 0:
                        _spans2.append(_hi2 / (1.0 - _f))
                    if _spans2:
                        _S2 = max(_spans2) * 1.08
                        _y2_range = [-_f * _S2, (1.0 - _f) * _S2]
        if _y2_range is None:
            _y2_range = _windowed_range(
                [nav_index, opt_index, spy_index, qqq_index],
                extra_vals=[0.0],
            )

        _y1_title = "Consolidated NAV ($)" if _have_nav else "OPT NAV ($)"
        fig.update_layout(
            height=400,
            hovermode="x unified",
            plot_bgcolor="rgba(0,0,0,0)",
            paper_bgcolor="rgba(0,0,0,0)",
            hoverlabel={"bgcolor": "#ffffff", "font_color": "#1e2130",
                        "bordercolor": "#cbd5e1", "font_size": 11},
            legend={"orientation": "h", "yanchor": "bottom", "y": 1.02,
                    "xanchor": "center", "x": 0.5,
                    "bgcolor": "rgba(0,0,0,0)"},
            margin={"l": 10, "r": 60, "t": 40, "b": 10},
            yaxis=dict(
                title=dict(text=_y1_title, font=dict(color="rgba(148,163,184,0.6)")),
                tickformat="$,.0f",
                tickfont=dict(color="rgba(148,163,184,0.6)"),
                showgrid=False,
                **({} if _y1_range is None else {"range": _y1_range}),
            ),
            yaxis2=dict(
                overlaying="y", side="right",
                ticksuffix="%", tickformat=".1f",
                zeroline=True, zerolinecolor="rgba(148,163,184,0.5)", zerolinewidth=1,
                showgrid=True,
                **({} if _y2_range is None else {"range": _y2_range}),
            ),
        )
        # range= always bounded to real data dates → no blank future months.
        # autorange=False prevents Plotly from padding beyond _d_end_ts.
        fig.update_xaxes(
            range=[str(_d_start_ts.date()), str(_d_end_ts.date())],
            autorange=False,
            rangeslider=dict(visible=False),
            hoverformat="%b %d, %Y",
        )
        st.plotly_chart(fig, width="stretch")
        st.caption(
            "Consolidated: time-weighted return — USD deposits stripped; SGD deposits not FX-adjusted. "
            "SPY/QQQ: price return (their TWR is unaffected by personal deposits). "
            "Markers: ▲ deposit  ▼ withdrawal."
        )


def _build_history_context() -> dict:
    """Summarise flex_trades.pkl into compact LLM-friendly tables.

    Returns a dict with keys 'global_stats' and 'per_symbol'.
    Cached in session_state for 5 minutes so repeated questions don't reload.
    """
    pkl = _MASTER_DIR / "flex_trades.pkl"
    if not pkl.exists():
        return {}

    _CACHE, _TS = "llm_hist_cache", "llm_hist_ts"
    _pkl_mtime = pkl.stat().st_mtime
    if (
        _CACHE in st.session_state
        and time.time() - st.session_state.get(_TS, 0) < 300
        and st.session_state.get("llm_hist_pkl_mtime") == _pkl_mtime
    ):
        return st.session_state[_CACHE]

    df = pd.read_pickle(pkl)
    if df.empty or "pnl" not in df.columns:
        return {}

    sym_col = "underlyingSymbol" if "underlyingSymbol" in df.columns else "symbol"

    # Closed trades with realised P&L
    closed = df[
        (df.get("openCloseIndicator", pd.Series(dtype=str)) == "C")
        & (df["pnl"] != 0)
    ] if "openCloseIndicator" in df.columns else df[df["pnl"] != 0]

    n = len(closed)
    wins = int((closed["pnl"] > 0).sum())
    total_pnl = closed["pnl"].sum()
    gross_win = closed.loc[closed["pnl"] > 0, "pnl"].sum()
    gross_loss = abs(closed.loc[closed["pnl"] < 0, "pnl"].sum())
    pf = round(gross_win / gross_loss, 2) if gross_loss > 0 else 0.0

    best_row = closed.loc[closed["pnl"].idxmax()] if n else None
    worst_row = closed.loc[closed["pnl"].idxmin()] if n else None
    def _trade_label(row) -> str:
        sym = row.get(sym_col, row.get("symbol", "?")) if isinstance(row, pd.Series) else "?"
        return f"{sym} ${row['pnl']:,.0f}"

    date_min = df["dateTime"].min() if "dateTime" in df.columns else None
    date_max = df["dateTime"].max() if "dateTime" in df.columns else None

    total_commissions = (
        int(df["ibCommission"].sum()) if "ibCommission" in df.columns else None
    )

    global_stats = {
        "date_range": f"{date_min.date() if date_min is not pd.NaT else '?'} to {date_max.date() if date_max is not pd.NaT else '?'}",
        "total_rows_all": len(df),
        "closed_pnl_trades": n,
        "win_rate_pct": round(wins / n * 100, 1) if n else 0.0,
        "profit_factor": pf,
        "total_pnl_usd": int(total_pnl),
        "best_trade": _trade_label(best_row) if best_row is not None else "N/A",
        "worst_trade": _trade_label(worst_row) if worst_row is not None else "N/A",
    }
    if total_commissions is not None:
        global_stats["total_commissions_usd"] = total_commissions

    # All closed OPT trades. STK assignment trades excluded (assetCategory == "OPT") — their
    # inclusion would halve the apparent win rate for wheel symbols.
    _all_closed_opt = (
        df[(df["openCloseIndicator"] == "C") & (df["assetCategory"] == "OPT")]
        if all(c in df.columns for c in ("openCloseIndicator", "assetCategory"))
        else closed
    )
    opens = df[df["openCloseIndicator"] == "O"] if "openCloseIndicator" in df.columns else df

    # Patch pnl=0 close rows: IBKR zeroes the option pnl at assignment and absorbs the premium
    # into the stock cost basis instead. Recover the opening-leg premium so per_symbol and
    # trade_log show accurate wheel-cycle P&L.
    _join_keys = [c for c in [sym_col, "putCall", "strike", "expiry"] if c in df.columns]
    if (
        _join_keys
        and {"tradePrice", "quantity"}.issubset(opens.columns)
        and not opens.empty
        and (_all_closed_opt["pnl"] == 0).any()
    ):
        _op = opens.copy()
        _mult = _op["multiplier"] if "multiplier" in _op.columns else 100
        _sign = (
            _op["buySell"].map({"SELL": 1.0, "BUY": -1.0}).fillna(0.0)
            if "buySell" in _op.columns else 1.0
        )
        _op["_prem"] = _sign * _op["tradePrice"].abs() * _op["quantity"].abs() * _mult
        _prem_map = _op.groupby(_join_keys, as_index=False)["_prem"].sum()
        _zero_mask = _all_closed_opt["pnl"] == 0
        _all_closed_opt = _all_closed_opt.copy()
        _merged = _all_closed_opt.loc[_zero_mask, _join_keys].merge(
            _prem_map, on=_join_keys, how="left"
        )
        _all_closed_opt.loc[_zero_mask, "pnl"] = _merged["_prem"].fillna(0.0).values

    per_symbol: list[dict] = []
    for sym, grp in _all_closed_opt.groupby(sym_col):
        if not sym or pd.isna(sym):
            continue
        t = len(grp)
        w = int((grp["pnl"] > 0).sum())
        sym_pnl = int(grp["pnl"].sum())
        best_t = int(grp["pnl"].max())
        worst_t = int(grp["pnl"].min())

        # Strategy fingerprint from opening legs on this underlying
        strats: list[str] = []
        if not opens.empty and "buySell" in opens.columns and "putCall" in opens.columns:
            sym_opens = opens[opens[sym_col] == sym]
            if ((sym_opens["buySell"] == "SELL") & (sym_opens["putCall"] == "P")).any():
                strats.append("CSP")
            if ((sym_opens["buySell"] == "SELL") & (sym_opens["putCall"] == "C")).any():
                strats.append("CC")
            if ((sym_opens["buySell"] == "BUY") & (sym_opens["putCall"] == "P")).any():
                strats.append("LP")
            if ((sym_opens["buySell"] == "BUY") & (sym_opens["putCall"] == "C")).any():
                strats.append("LC")

        per_symbol.append({
            "sym": sym,
            "n": t,
            "wr%": round(w / t * 100) if t else 0,
            "pnl": sym_pnl,
            "best": best_t,
            "worst": worst_t,
            "strat": "+".join(strats) if strats else "?",
        })

    per_symbol.sort(key=lambda r: r["pnl"], reverse=True)

    # Backtest scores — run BacktestExpert scoring for all symbols with ≥10 closed OPT trades.
    # Uses same df / symbol filter as score_from_trades() so numbers match the Deep-Dive panel.
    from src.backtest.score import score_from_trades as _score_fn
    _score_since = score_since()
    backtest_scores: list[dict] = []
    for _r in sorted((r for r in per_symbol if r["n"] >= 10), key=lambda r: r["n"], reverse=True)[:80]:
        _bs = _score_fn(df, _r["sym"], since=_score_since)
        backtest_scores.append({
            "sym":    _r["sym"],
            "score":  _bs.composite,
            "verdict": _bs.verdict,
            "n":      _bs.total_trades,
            "wr%":    round(_bs.win_rate * 100),
            "pf":     _bs.profit_factor,
            "yrs":    _bs.years_tested,
            "sample": _bs.sample_score,
            "expect": _bs.expectancy_score,
            "risk":   _bs.risk_score,
            "robust": _bs.robustness_score,
            "flags":  "; ".join(_bs.red_flags) if _bs.red_flags else "",
        })

    # Trade log — same _all_closed_opt source, newest-first for context-window priority.
    # Prefer dateTime (always populated) over tradeDate (often NaT in Activity query XMLs).
    _date_col = next(
        (c for c in ("dateTime", "tradeDate") if c in _all_closed_opt.columns and _all_closed_opt[c].notna().any()),
        None,
    )
    trade_log: list[dict] = []
    if _date_col:
        _tlog = _all_closed_opt.sort_values(_date_col, ascending=False, na_position="last")
        _date_strs = _fmt_date_col(_tlog[_date_col], "?")
        _exp_strs = (
            _fmt_date_col(_tlog["expiry"], "") if "expiry" in _tlog.columns
            else pd.Series("", index=_tlog.index)
        )
        for (_, row), date_s, exp_s in zip(_tlog.iterrows(), _date_strs, _exp_strs):
            _pc = str(row.get("putCall", ""))
            _sk = row.get("strike")
            _pnl = row.get("pnl")
            trade_log.append({
                "date":   date_s,
                "sym":    str(row.get(sym_col, row.get("symbol", "?"))),
                "strike": (_pc + str(round(float(_sk), 1))) if pd.notna(_sk) else _pc,
                "expiry": exp_s,
                "qty":    int(row["quantity"]) if pd.notna(row.get("quantity")) else "",
                "pnl":    int(_pnl) if pd.notna(_pnl) else 0,
            })

    result = {"global_stats": global_stats, "per_symbol": per_symbol, "trade_log": trade_log, "backtest_scores": backtest_scores}
    st.session_state[_CACHE] = result
    st.session_state[_TS] = time.time()
    st.session_state["llm_hist_pkl_mtime"] = _pkl_mtime
    return result


def _build_ohlc_context(focus_symbols: set[str]) -> dict:
    """Compute per-symbol OHLC summary stats for LLM context.

    Covers focus_symbols (current positions + top-traded history symbols).
    Cached in session_state for 5 minutes.
    Returns dict with key 'ohlc_stats': list[dict].
    """
    pkl = _MASTER_DIR / "ohlc.pkl"
    if not pkl.exists() or not focus_symbols:
        return {}

    _CACHE, _TS = "llm_ohlc_cache", "llm_ohlc_ts"
    if (
        _CACHE in st.session_state
        and time.time() - st.session_state.get(_TS, 0) < 300
    ):
        return st.session_state[_CACHE]

    ohlc: dict = pd.read_pickle(pkl)
    rows: list[dict] = []

    for sym in sorted(focus_symbols):
        df = ohlc.get(sym)
        if df is None or df.empty or "Close" not in df.columns:
            continue
        closes = df["Close"].dropna()
        if len(closes) < 2:
            continue

        last = round(float(closes.iloc[-1]), 2)
        lo = float(closes.min())
        hi = float(closes.max())
        pos52 = round((last - lo) / (hi - lo) * 100) if hi > lo else 50

        ret20 = round((last / float(closes.iloc[-20]) - 1) * 100, 1) if len(closes) >= 20 else None
        ret90 = round((last / float(closes.iloc[-90]) - 1) * 100, 1) if len(closes) >= 90 else None

        # 20-day annualised HV
        if len(closes) >= 21:
            import numpy as _np
            log_ret = _np.log(closes.iloc[-21:] / closes.iloc[-21:].shift(1)).dropna()
            hv20 = round(float(log_ret.std() * (252 ** 0.5)), 2)
        else:
            hv20 = None

        # MA trend
        ma50 = float(closes.iloc[-50:].mean()) if len(closes) >= 50 else None
        ma200 = float(closes.iloc[-200:].mean()) if len(closes) >= 200 else None
        if ma50 and ma200:
            trend = "UP" if last > ma50 > ma200 else ("DN" if last < ma50 < ma200 else "MX")
        elif ma50:
            trend = "UP" if last > ma50 else "DN"
        else:
            trend = "?"

        rows.append({
            "sym": sym,
            "price": last,
            "r20d": f"{ret20:+.1f}" if ret20 is not None else "?",
            "r90d": f"{ret90:+.1f}" if ret90 is not None else "?",
            "pos52w": pos52,
            "trend": trend,
            "hv20": hv20 if hv20 is not None else "?",
        })

    cutoff = pd.Timestamp.now() - pd.DateOffset(months=24)
    monthly: dict[str, list[tuple[str, float]]] = {}
    for sym in sorted(focus_symbols):
        df = ohlc.get(sym)
        if df is None or df.empty or "Close" not in df.columns:
            continue
        closes = df["Close"].dropna()
        try:
            mo = closes[closes.index >= cutoff].resample("ME").last().dropna()
        except Exception:
            continue
        if mo.empty:
            continue
        monthly[sym] = [(str(dt)[:7], round(float(v), 2)) for dt, v in mo.items()]

    result: dict = {"ohlc_stats": rows}
    if monthly:
        result["ohlc_price_history"] = monthly
    st.session_state[_CACHE] = result
    st.session_state[_TS] = time.time()
    return result


_PF_PICKLE = _MASTER_DIR.parent / "df_pf.pkl"   # data/df_pf.pkl — auto-saved snapshot

# Live NAV for today, written by kpi_strip once all accounts are streaming.
# All consumers (perf chart, LLM context) read from here — single source of truth.
_LIVE_NAV_TODAY: dict = {}  # {"date": pd.Timestamp, "nlv": float}


def _build_live_context() -> dict:
    """Build LLM context from the live dashboard snapshot, trade history, and OHLC stats."""
    snap = client.snapshot()
    acct = _selected_account()
    positions = _filter_positions(snap.positions, acct)
    context: dict = {}

    # Weekly vs monthly-only map {symbol: is_weekly} — loaded once, used to tag each
    # position row (below) AND build the symbol_categories key. Per-row tagging stops
    # the AI hallucinating membership of the 257-symbol monthly list (e.g. wrongly
    # calling BA / AMZN monthly-only).
    _cat_is_weekly: dict[str, bool] = {}
    _sym_cat_pkl = _MASTER_DIR / "symbol_categories.pkl"
    if _sym_cat_pkl.exists():
        try:
            _sym_cat_df = pd.read_pickle(_sym_cat_pkl)
            if not _sym_cat_df.empty and {"symbol", "is_weekly"}.issubset(_sym_cat_df.columns):
                _cat_is_weekly = {
                    str(_s): bool(_w)
                    for _s, _w in zip(_sym_cat_df["symbol"], _sym_cat_df["is_weekly"])
                }
        except Exception:
            pass

    # ── Positions: live → auto-save; empty → load cached pickle with staleness warning ──
    _pos_is_live = not positions.empty
    _pos_as_of: str | None = None

    if _pos_is_live:
        # Persist snapshot so future queries can fall back if dashboard goes offline
        try:
            _now = datetime.now()
            _pf_meta = {"positions": positions, "as_of": _now}
            with open(_PF_PICKLE, "wb") as _fh:
                pickle.dump(_pf_meta, _fh)
            _pos_as_of = _now.strftime("%Y-%m-%d %H:%M")
        except Exception:
            pass
    else:
        # Live snapshot empty — fall back to last saved pickle
        try:
            with open(_PF_PICKLE, "rb") as _fh:
                _pf_meta = pickle.load(_fh)
            positions = _pf_meta.get("positions", pd.DataFrame())
            _saved_at: datetime = _pf_meta.get("as_of", datetime.min)
            _pos_as_of = _saved_at.strftime("%Y-%m-%d %H:%M")
        except Exception:
            positions = pd.DataFrame()

    # Always send positions key so the AI knows exactly what is held (or that nothing is)
    if not positions.empty:
        cols = [c for c in (
            "symbol", "secType", "right", "strike", "expiry",
            "position", "marketPrice", "marketValue", "avgCost",
            "delta", "theta", "vega",
        ) if c in positions.columns]
        _pos_ctx = positions[cols].copy()
        # Authoritative per-row expiry classification — the AI must read this column
        # directly instead of inferring monthly/weekly from any symbol list.
        if _cat_is_weekly:
            _pos_ctx["expiry_class"] = [
                ("weekly" if _cat_is_weekly[_s] else "monthly_only")
                if _s in _cat_is_weekly else "unclassified"
                for _s in _pos_ctx["symbol"].astype(str)
            ]
        context["positions"] = _pos_ctx
    context["positions_is_live"] = _pos_is_live
    if _pos_as_of:
        context["positions_as_of"] = _pos_as_of

    g = greek_dollar_sums(positions, snap.tickers) if not positions.empty else {}
    if g:
        context["greeks"] = {k: round(v, 2) for k, v in g.items() if isinstance(v, float)}
    av = _select_account_values(snap, acct)
    if av:
        kpis = account_kpis(snap, min_cushion=settings.min_cushion, account=acct)
        metrics = {k: str(v) for k, v in av.items()}
        # IBKR's raw "Cushion" tag uses a different formula and reads as ~0.01.
        # Replace with the dashboard's definition: ExcessLiquidity / NLV (shown as % in the KPI strip).
        metrics["Cushion"] = (
            f"{kpis['cushion']:.1%} = ExcessLiquidity / NLV"
            f" (alert threshold {settings.min_cushion:.0%};"
            f" {'BREACH' if kpis['cushion_breach'] else 'OK'})"
        )
        context["metrics"] = metrics

    hist = _build_history_context()
    context.update(hist)

    # OHLC for current position underlyings + top-50 most-traded history symbols
    pos_syms: set[str] = set()
    if not positions.empty and "symbol" in positions.columns:
        pos_syms = set(positions["symbol"].dropna().unique())
    hist_syms: set[str] = set()
    if "per_symbol" in hist:
        by_trades = sorted(hist["per_symbol"], key=lambda r: r["n"], reverse=True)
        hist_syms = {r["sym"] for r in by_trades[:50]}
    context.update(_build_ohlc_context(pos_syms | hist_syms))

    # Suggested Orders — load pickles so the LLM can reason about them
    _live_conids: set = (
        set(positions["conId"]) if not positions.empty and "conId" in positions.columns else set()
    )
    for ctx_key, pkl_name in (
        ("orders_cover",   "df_cov.pkl"),
        ("orders_sow",     "df_nkd.pkl"),
        ("orders_reap",    "df_reap.pkl"),
        ("orders_protect", "df_protect.pkl"),
    ):
        df_ord = _load_pkl(pkl_name)
        if ctx_key == "orders_reap" and _live_conids and "conId" in df_ord.columns:
            # Guard against stale derive.py output: only keep options still in live positions
            df_ord = df_ord[df_ord["conId"].isin(_live_conids)]
        if not df_ord.empty:
            context[ctx_key] = df_ord

    # Uncreated orders — reasons from last derive.py run (written to derive_uncreated.json)
    _unc_path = _here() / "data" / "derive_uncreated.json"
    if _unc_path.exists():
        try:
            import json as _junc
            _unc = _junc.loads(_unc_path.read_text(encoding="utf-8"))
            if _unc:
                context["uncreated_orders"] = _unc
        except Exception:
            pass

    # Live open orders (filtered to selected account)
    live_orders = snap.orders
    if acct and not live_orders.empty and "account" in live_orders.columns:
        live_orders = live_orders[live_orders["account"] == acct].reset_index(drop=True)
    if not live_orders.empty:
        context["open_orders"] = live_orders

    # Consolidated NAV — flex_nav.pkl (EquitySummaryByReportDateInBase, both accounts summed)
    _nav_pkl = _MASTER_DIR / "flex_nav.pkl"
    if _nav_pkl.exists():
        try:
            _df_nav = pd.read_pickle(_nav_pkl)
            if not _df_nav.empty and {"reportDate", "total"}.issubset(_df_nav.columns):
                _nav_ts = _df_nav.set_index("reportDate")["total"].sort_index()
                _nav_ts = _nav_ts[_nav_ts > 0]
                # Patch today's NAV from _LIVE_NAV_TODAY (written by kpi_strip).
                _today_ts = pd.Timestamp.today().normalize()
                if (
                    len(_nav_ts) >= 1
                    and _nav_ts.index.max() < _today_ts
                    and _LIVE_NAV_TODAY.get("date") == _today_ts
                    and _LIVE_NAV_TODAY.get("nlv", 0) > 0
                ):
                    _nav_ts = pd.concat(
                        [_nav_ts, pd.Series({_today_ts: _LIVE_NAV_TODAY["nlv"]})]
                    ).sort_index()
                if len(_nav_ts) >= 2:
                    _jan25      = pd.Timestamp("2025-01-01")
                    _ytd_start  = pd.Timestamp(f"{_today_ts.year}-01-01")
                    _current    = float(_nav_ts.iloc[-1])
                    _at_jan25   = float(_nav_ts[_nav_ts.index >= _jan25].iloc[0]) if (_nav_ts.index >= _jan25).any() else None
                    _at_ytd     = float(_nav_ts[_nav_ts.index >= _ytd_start].iloc[0]) if (_nav_ts.index >= _ytd_start).any() else None
                    # Month-end NAV — full history (needed for multi-year performance questions)
                    _nav_df  = pd.DataFrame({"date": _nav_ts.index, "nav": _nav_ts.values})
                    _nav_df["ym"] = _nav_df["date"].dt.to_period("M")
                    _monthly_df  = (
                        _nav_df
                        .groupby("ym", sort=True)
                        .last()
                        .reset_index()
                    )
                    import math as _math
                    _daily_ret_full = (
                        _nav_ts.pct_change()
                        .replace([float("inf"), float("-inf")], float("nan"))
                        .dropna()
                    )
                    _sharpe_full = (
                        round(float(_daily_ret_full.mean() / _daily_ret_full.std() * _math.sqrt(252)), 2)
                        if len(_daily_ret_full) >= 2 and _daily_ret_full.std() > 0 else None
                    )
                    _twr_full = round(float(_nav_ts.iloc[-1] / _nav_ts.iloc[0] - 1) * 100, 2)
                    _norm_full = _nav_ts / _nav_ts.iloc[0]
                    _max_dd_full = round(float((_norm_full / _norm_full.cummax() - 1.0).min() * 100), 2)

                    _nav_2025 = _nav_ts[_nav_ts.index >= pd.Timestamp("2025-01-01")]
                    _sharpe_2025 = _max_dd_2025 = None
                    if len(_nav_2025) >= 2:
                        _ret_2025 = (
                            _nav_2025.pct_change()
                            .replace([float("inf"), float("-inf")], float("nan"))
                            .dropna()
                        )
                        _sharpe_2025 = (
                            round(float(_ret_2025.mean() / _ret_2025.std() * _math.sqrt(252)), 2)
                            if len(_ret_2025) >= 2 and _ret_2025.std() > 0 else None
                        )
                        _norm_2025 = _nav_2025 / _nav_2025.iloc[0]
                        _max_dd_2025 = round(float((_norm_2025 / _norm_2025.cummax() - 1.0).min() * 100), 2)

                    context["nav_summary"] = {
                        "current":                        _current,
                        "current_date":                   str(_nav_ts.index[-1].date()),
                        "ytd_return_pct":                 round((_current / _at_ytd  - 1) * 100, 2) if _at_ytd  else None,
                        "since_jan2025_pct":              round((_current / _at_jan25 - 1) * 100, 2) if _at_jan25 else None,
                        "twr_full_pct":                   _twr_full,
                        "sharpe_full":                    _sharpe_full,
                        "max_drawdown_full_pct":          _max_dd_full,
                        "sharpe_since_jan2025":           _sharpe_2025,
                        "max_drawdown_since_jan2025_pct": _max_dd_2025,
                        "monthly":                        [
                            (str(r.date.date()), int(round(r.nav)))
                            for _, r in _monthly_df.iterrows()
                        ],
                    }
        except Exception:
            pass

    # Cash transactions — flex_cash.pkl (deposits/withdrawals, dividends, interest)
    _cash_pkl = _MASTER_DIR / "flex_cash.pkl"
    if _cash_pkl.exists():
        try:
            _df_cash = pd.read_pickle(_cash_pkl)
            if not _df_cash.empty and {"type", "amount", "date"}.issubset(_df_cash.columns):
                _df_cash = _df_cash.copy()
                _df_cash["_year"] = pd.to_datetime(_df_cash["date"]).dt.year
                _two_yrs_ago = pd.Timestamp.today() - pd.DateOffset(years=2)
                # Deposits / withdrawals (last 2 years, exclude sub-$1 noise)
                _dw = _df_cash[
                    _df_cash["type"].str.contains("Deposit|Withdraw", case=False, na=False)
                    & (_df_cash["amount"].abs() >= 1.0)
                    & (_df_cash["date"] >= _two_yrs_ago)
                ].sort_values("date")
                # Dividends and interest aggregated by year
                _div_by_yr = (
                    _df_cash[_df_cash["type"].str.contains("Dividend", case=False, na=False)]
                    .groupby("_year")["amount"].sum().round(0).astype(int).to_dict()
                )
                _int_by_yr = (
                    _df_cash[_df_cash["type"].str.contains("Interest", case=False, na=False)]
                    .groupby("_year")["amount"].sum().round(0).astype(int).to_dict()
                )
                context["cash_summary"] = {
                    "recent_dw": [
                        {
                            "date":     str(r["date"].date()) if hasattr(r["date"], "date") else str(r["date"]),
                            "amount":   round(float(r["amount"]), 2),
                            "currency": str(r.get("currency", "")),
                        }
                        for _, r in _dw.iterrows()
                    ],
                    "dividends_by_year": _div_by_yr,
                    "interest_by_year":  _int_by_yr,
                }
        except Exception:
            pass

    # SPY / QQQ monthly closes from 2020 (benchmark comparison for Ask AI — full history for
    # multi-year return questions like "compare to SPY since 2022")
    try:
        _ohlc_store = _cached_ohlc()
        _bench_start_ts = pd.Timestamp("2020-01-01")
        _bench_data: dict[str, list[tuple[str, float]]] = {}
        for _bsym in ("SPY", "QQQ"):
            _bdf = _ohlc_store.get(_bsym)
            if _bdf is not None and not _bdf.empty and "Close" in _bdf.columns:
                _bcloses = _bdf["Close"].dropna()
                _bcloses = _bcloses[_bcloses.index >= _bench_start_ts]
                if not _bcloses.empty:
                    _bmo = _bcloses.resample("ME").last().dropna()
                    _bench_data[_bsym] = [
                        (str(dt)[:7], round(float(v), 2)) for dt, v in _bmo.items()
                    ]
        if _bench_data:
            context["benchmark_prices"] = _bench_data
    except Exception:
        pass

    # Synthetic backtest results — wheel-appropriate verdict per weekly symbol
    from src.backtest.synthetic import BACKTEST_RESULTS_PATH as _SYN_RES_PATH  # noqa: PLC0415
    _SYN_CACHE, _SYN_TS = "llm_syn_cache", "llm_syn_ts"
    if (
        _SYN_CACHE in st.session_state
        and time.time() - st.session_state.get(_SYN_TS, 0) < 300
    ):
        if st.session_state[_SYN_CACHE]:
            context["synthetic_backtest"] = st.session_state[_SYN_CACHE]
    elif _SYN_RES_PATH.exists():
        try:
            _syn_df = pd.read_pickle(_SYN_RES_PATH)
            if not _syn_df.empty and "symbol" in _syn_df.columns:
                _syn_rows = []
                for _, _sr in _syn_df.iterrows():
                    if str(_sr.get("verdict", "")) == "INSUFFICIENT_DATA":
                        continue
                    _syn_rows.append({
                        "sym":    _sr["symbol"],
                        "verdict": _sr.get("verdict", ""),
                        "score":  _sr.get("composite"),
                        "cc_pf":  _sr.get("cc_pf"),
                        "cc_wr":  round(float(_sr.get("cc_win_rate") or 0) * 100),
                        "csp_pf": _sr.get("csp_pf"),
                        "csp_wr": round(float(_sr.get("csp_win_rate") or 0) * 100),
                        "years":  _sr.get("years_tested"),
                    })
                st.session_state[_SYN_CACHE] = _syn_rows
                st.session_state[_SYN_TS] = time.time()
                if _syn_rows:
                    context["synthetic_backtest"] = _syn_rows
        except Exception:
            st.session_state[_SYN_CACHE] = []

    # Symbol categories — weekly vs monthly-only (reuses the map loaded at the top).
    if _cat_is_weekly:
        context["symbol_categories"] = {
            "monthly_only": sorted(s for s, w in _cat_is_weekly.items() if not w),
            "weekly": sorted(s for s, w in _cat_is_weekly.items() if w),
        }

    return context


@st.fragment
def _render_llm_chat() -> None:
    """Compact Ask AI dock — a fragment so the copy/history toggles rerun only this
    section, not the whole page (which would re-pull the live IBKR snapshot)."""
    _ai_models = st.session_state.get("cfg_aimodels", list(_PROVIDER_HINTS))
    _valid_prov = [m for m in _ai_models if m in _PROVIDER_HINTS] or list(_PROVIDER_HINTS)

    # Follow Config's DEFAULTAI: (re)seed the dock's provider whenever DEFAULTAI
    # changes, without clobbering a manual in-dock selection made between changes.
    # (Assignment is before the selectbox below, so it sets the widget's default.)
    _def = st.session_state.get("cfg_defaultai", _valid_prov[0])
    _def = _def if _def in _valid_prov else _valid_prov[0]
    if st.session_state.get("_llm_defaultai_applied") != _def:
        st.session_state["_llm_defaultai_applied"] = _def
        st.session_state["llm_provider"] = _def

    _prov_w = max(len(p) for p in _valid_prov)  # chars in longest provider name
    p_col, q_col, s_col, c_col = st.columns([_prov_w * 0.13, 8 - _prov_w * 0.13 - 1.0, 0.5, 0.5])

    provider = p_col.selectbox(
        "Provider",
        _valid_prov,
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

    # ▶ send button: forces re-query even when question/provider unchanged (e.g. after model switch)
    _submit_ver = st.session_state.get("llm_submit_ver", 0)
    if s_col.button("▶", key="llm_send", help="Submit", width="stretch"):
        st.session_state["llm_submit_ver"] = _submit_ver + 1
        st.rerun(scope="fragment")

    if c_col.button("✕", key="llm_clr", help="Clear", width="stretch"):
        st.session_state["llm_q_ver"] = _qver + 1
        st.session_state.pop("llm_response", None)
        st.session_state.pop("llm_last_q", None)
        st.session_state.pop("llm_last_prov", None)
        st.session_state.pop("llm_submit_ver", None)
        st.rerun(scope="fragment")

    # Query when question changes (Enter in text box) OR ▶ was explicitly clicked
    _forced = _submit_ver != st.session_state.get("llm_last_submit_ver", 0)
    if question and (question != st.session_state.get("llm_last_q") or _forced):
        _prev_hist: list[dict] = st.session_state.get("llm_history", [])
        with st.spinner(f"{provider}…"):
            try:
                context = _build_live_context()
                if provider == "Claude":
                    resp = query_data(question, context, history=_prev_hist)
                elif provider == "Gemini":
                    resp = query_data_gemini(question, context, history=_prev_hist)
                else:
                    resp = query_data_deepseek(question, context, history=_prev_hist)
                st.session_state["llm_response"] = resp
                st.session_state["llm_history"] = (_prev_hist + [{"q": question, "a": resp}])[-_LLM_MAX_HISTORY:]
            except Exception as e:
                st.session_state["llm_response"] = f"⚠️ {e}"
            st.session_state["llm_last_q"] = question
            st.session_state["llm_last_prov"] = provider
            st.session_state["llm_last_submit_ver"] = _submit_ver

    if cached := st.session_state.get("llm_response"):
        _last_q = st.session_state.get("llm_last_q", "")
        with st.expander("Answer", expanded=True):
            with st.container(height=250, border=False):
                # Escape $ so Streamlit doesn't treat currency/options as LaTeX delimiters
                safe = cached.replace("$", r"\$")
                # Downgrade headers (H1→H4, H2→H5, H3→H6) so they fit the compact container
                safe = re.sub(
                    r"^(#{1,3})\s",
                    lambda m: "#" * min(6, len(m.group(1)) + 3) + " ",
                    safe,
                    flags=re.MULTILINE,
                )
                st.markdown(safe)
            _btn_copy, _btn_hist = st.columns(2)
            if _btn_copy.button("📋", key="llm_copy_btn", help="Show plain text for copying"):
                st.session_state["llm_copy_open"] = not st.session_state.get("llm_copy_open", False)
            _hist_len = len(st.session_state.get("llm_history", []))
            _turns_left = _LLM_MAX_HISTORY - _hist_len
            if _btn_hist.button(
                f"💬 {_turns_left}",
                key="llm_hist_clr_btn",
                help=(
                    f"{_hist_len}/{_LLM_MAX_HISTORY} turns stored — "
                    f"{_turns_left} slot{'s' if _turns_left != 1 else ''} remaining. "
                    "Click to clear conversation history."
                ),
            ):
                st.session_state["llm_history"] = []
                st.rerun(scope="fragment")
        if st.session_state.get("llm_copy_open", False):
            _hist = st.session_state.get("llm_history", [])
            if _hist:
                _parts = [f"Q: {h['q']}\n\nA: {h['a']}" for h in _hist]
                st.code("\n\n---\n\n".join(_parts), language=None)
            else:
                st.code(f"Q: {_last_q}\n\nA: {cached}", language=None)


# (render_history removed — all content moved to render_analysis)


@st.fragment(run_every=5)
def render_actions() -> None:
    """Single daily-action pipeline: OHLCs → Flex → Backtest → Generate → Execute.

    Owns all action subprocess state (derive / execute / ohlc / backtest / trades)
    and the freeze state machine. Reuses the verbatim handler logic that previously
    lived inside render_orders (Order Actions) and render_analysis (Actions).
    """
    from src.dashboard.ohlc import OHLC_PATH, get_sp500_symbols, write_symbol_list  # noqa: PLC0415
    from src.backtest.synthetic import BACKTEST_RESULTS_PATH as _BT_RESULTS  # noqa: PLC0415

    snap = client.snapshot()
    nav_path = _MASTER_DIR / "flex_nav.pkl"
    _api_ready = bool(
        settings.token.get_secret_value() and settings.trades_flexid.get_secret_value()
    )

    # ── derive / execute / cancel subprocess + freeze state ─────────────────
    proc: subprocess.Popen | None      = st.session_state.get("derive_proc")
    exec_proc: subprocess.Popen | None = st.session_state.get("execute_proc")
    frozen     = client.is_frozen()
    frozen_for = st.session_state.get("frozen_for", "")   # "derive" | "execute" | "cancel" | ""

    def _auto_unfreeze(tag: str, proc_key: str) -> None:
        proc_ = st.session_state.get(proc_key)
        if frozen and proc_ is not None and proc_.poll() is not None and frozen_for == tag:
            st.session_state[f"_{tag}_exit"] = proc_.poll()
            client.unfreeze()
            st.session_state[proc_key] = None
            st.session_state.pop("frozen_for", None)
            st.rerun()

    _auto_unfreeze("derive",  "derive_proc")
    _auto_unfreeze("execute", "execute_proc")
    _auto_unfreeze("cancel",  "cancel_proc")

    # Fallback: derive.py logged COMPLETE but hasn't exited in 60s → kill it.
    if frozen and frozen_for == "derive" and proc is not None and proc.poll() is None:
        _pct_chk, _, _ = _derive_progress()
        if _pct_chk >= 1.0:
            _since = st.session_state.get("_derive_complete_since")
            if _since is None:
                st.session_state["_derive_complete_since"] = time.monotonic()
            elif time.monotonic() - _since > 60.0:
                try:
                    proc.terminate()
                except Exception:
                    pass
                try:
                    _kill_rc = proc.wait(timeout=3)
                except Exception:
                    _kill_rc = 1
                st.session_state["_derive_exit"] = _kill_rc
                client.unfreeze()
                st.session_state["derive_proc"] = None
                st.session_state.pop("frozen_for", None)
                st.session_state.pop("_derive_complete_since", None)
                st.rerun()
    else:
        st.session_state.pop("_derive_complete_since", None)

    _capture_exit(proc,      "_derive_exit")
    _capture_exit(exec_proc, "_execute_exit")

    # ── ohlc / trades / backtest subprocess state ────────────────────────────
    ohlc_proc: subprocess.Popen | None = st.session_state.get("ohlc_proc")
    _ohlc_running = ohlc_proc is not None and ohlc_proc.poll() is None
    _capture_exit(ohlc_proc, "_ohlc_exit")

    def _launch_flex_refresh(year: int | None = None) -> None:
        cmd = [sys.executable, str(_here() / "scripts" / "update_trades.py")]
        if year is not None:
            cmd += ["--year", str(year)]
        _ut_log_fh = open(_here() / "log" / "update_trades.log", "w", encoding="utf-8")  # noqa: SIM115
        _ut_proc = subprocess.Popen(cmd, stdout=_ut_log_fh, stderr=subprocess.STDOUT, env=_sub_env())
        st.session_state["trades_proc"] = _ut_proc
        logger.info("update_trades.py started pid={} year={}", _ut_proc.pid, year)

    if (
        st.session_state.get("_ohlc_exit") == 0
        and st.session_state.get("_auto_update_trades_pending")
        and not st.session_state.get("trades_proc")
    ):
        st.session_state.pop("_auto_update_trades_pending", None)
        _launch_flex_refresh()

    _trades_proc: subprocess.Popen | None = st.session_state.get("trades_proc")
    if _trades_proc is not None and _trades_proc.poll() is not None:
        st.session_state["_trades_exit"] = _trades_proc.poll()
        st.session_state["trades_proc"] = None
        st.rerun()

    _bt_running = st.session_state.get("backtest_proc") is not None
    _bt_proc: subprocess.Popen | None = st.session_state.get("backtest_proc")
    if _bt_proc is not None and _bt_proc.poll() is not None:
        st.session_state["_bt_exit"] = _bt_proc.poll()
        st.session_state.pop("backtest_proc", None)
        _bt_running = False

    with st.expander("⚡ Actions", expanded=False, key="exp_actions"):
        st.caption(
            "Daily flow — left to right.  Generate OHLCs auto-runs Refresh Flex; "
            "set REFINE Overrides (below) before Execute Orders."
        )

        _c1, _a1, _c2, _a2, _c3, _a3, _c4, _a4, _c5 = st.columns(
            [3, 0.35, 3, 0.35, 3, 0.35, 3.3, 0.35, 3]
        )
        for _a in (_a1, _a2, _a3, _a4):
            _a.markdown("<div class='pipe-arrow'>→</div>", unsafe_allow_html=True)

        # 1) Generate OHLCs ──────────────────────────────────────────────────
        with _c1:
            if st.button(
                "📊 Generate OHLCs",
                type="primary" if _ohlc_running else "secondary",
                width="stretch",
                help="Fetch / update 1.5 yr daily OHLC for S&P500 weekly underlyings + "
                     "portfolio positions. Runs in background; Refresh Flex runs after.",
            ) and not _ohlc_running:
                sp500_specs = get_sp500_symbols()
                seen: set[str] = {s["symbol"] for s in sp500_specs}
                # Load option-chain symbols so we can skip non-optionable positions
                # (London-listed ETFs: VWRA, CNDX, IGLN — no chain, no yfinance data)
                _chain_syms: set[str] = set()
                try:
                    _cdf = pd.read_pickle(_here() / "data" / "df_chains.pkl")
                    if "symbol" in _cdf.columns:
                        _chain_syms = set(_cdf["symbol"].dropna().astype(str))
                except Exception:
                    pass
                port_specs: list[dict[str, str]] = []
                if not snap.positions.empty:
                    for _, _pos in snap.positions.iterrows():
                        _sym = str(_pos.get("symbol", ""))
                        if not _sym or _sym in seen:
                            continue
                        # Skip symbols with no options chain (London ETFs, non-US ETFs)
                        if _chain_syms and _sym not in _chain_syms:
                            continue
                        port_specs.append({
                            "symbol":   _sym,
                            "exchange": str(_pos.get("primaryExch", "")) or "SMART",
                            "currency": str(_pos.get("currency", "")) or "USD",
                        })
                        seen.add(_sym)
                write_symbol_list(sp500_specs + port_specs)
                _OHLC_LOG.parent.mkdir(parents=True, exist_ok=True)
                _ohlc_log_fh = open(_OHLC_LOG, "w", encoding="utf-8")   # noqa: SIM115
                st.session_state.pop("_ohlc_exit", None)
                st.session_state["_auto_update_trades_pending"] = True
                _ohlc_new_proc = subprocess.Popen(
                    [sys.executable, str(_here() / "src" / "fetch_ohlc.py")],
                    stdout=_ohlc_log_fh, stderr=subprocess.STDOUT, env=_sub_env(),
                )
                st.session_state["ohlc_proc"] = _ohlc_new_proc
                logger.info("fetch_ohlc.py started pid={}", _ohlc_new_proc.pid)
                st.rerun()
            if not _ohlc_running and "_ohlc_exit" not in st.session_state:
                st.caption(f"Last: {_pkl_age('', path=OHLC_PATH)}")

        # 2) Refresh Flex ────────────────────────────────────────────────────
        with _c2:
            _flex_running = st.session_state.get("trades_proc") is not None
            if st.button(
                "🔄 Refresh Flex",
                key="btn_refresh_flex",
                type="primary" if _flex_running else "secondary",
                width="stretch",
                help="Download the Flex statement for the selected year and refresh all "
                     "three pickles (trades, cash, NAV). Runs in background.",
            ) and not _flex_running:
                st.session_state.pop("_trades_exit", None)
                if st.session_state.get("chk_flex_xml_only"):
                    _rf_cmd = [sys.executable, str(_here() / "scripts" / "update_trades.py"), "--xml-only"]
                    _rf_log_fh = open(_here() / "log" / "update_trades.log", "w", encoding="utf-8")  # noqa: SIM115
                    _rf_proc = subprocess.Popen(_rf_cmd, stdout=_rf_log_fh, stderr=subprocess.STDOUT, env=_sub_env())
                    st.session_state["trades_proc"] = _rf_proc
                    logger.info("update_trades.py --xml-only started pid={}", _rf_proc.pid)
                else:
                    _launch_flex_refresh(year=int(st.session_state.get("ni_flex_year", pd.Timestamp.today().year)))
                st.rerun()
            _rfy, _rfx = st.columns([1, 1])
            _rfy.number_input(
                "Year", min_value=2010, max_value=pd.Timestamp.today().year,
                value=pd.Timestamp.today().year, step=1, key="ni_flex_year",
                help="Year for the downloaded XML.",
            )
            _rfx.checkbox("XML only", key="chk_flex_xml_only",
                          help="Skip API download — re-parse existing *.xml files only.")
            if not _flex_running and "_trades_exit" not in st.session_state:
                _rf_warn = "⚠ TOKEN / TRADES_FLEXID not set" if not _api_ready else ""
                st.caption(_rf_warn or f"flex_nav.pkl — {_pkl_age('', path=nav_path)}")

        # 3) Run Backtest ────────────────────────────────────────────────────
        with _c3:
            if st.button(
                "🧪 Run Backtest",
                key="btn_run_backtest",
                type="primary" if _bt_running else "secondary",
                width="stretch",
                help="Synthetic wheel-strategy backtest on 5-year daily OHLC. Outputs "
                     "per-symbol DEPLOY/REFINE/ABANDON verdicts + suggested config.",
            ) and not _bt_running:
                _BACKTEST_LOG.parent.mkdir(parents=True, exist_ok=True)
                _bt_log_fh = open(_BACKTEST_LOG, "w", encoding="utf-8")  # noqa: SIM115
                st.session_state.pop("_bt_exit", None)
                _bt_cmd = [sys.executable, str(_here() / "scripts" / "run_backtest.py")]
                if st.session_state.get("chk_bt_refresh_ohlc"):
                    _bt_cmd.append("--refresh-ohlc")
                _bt_new_proc = subprocess.Popen(
                    _bt_cmd, stdout=_bt_log_fh, stderr=subprocess.STDOUT, env=_sub_env(),
                )
                st.session_state["backtest_proc"] = _bt_new_proc
                logger.info("run_backtest.py started pid={}", _bt_new_proc.pid)
                st.rerun()
            st.checkbox(
                "Force-refresh OHLC", key="chk_bt_refresh_ohlc", disabled=_bt_running,
                help="Re-fetch 5-year daily OHLC from yfinance for all symbols.",
            )
            if not _bt_running and "_bt_exit" not in st.session_state:
                st.caption(f"Last: {_pkl_age('', path=_BT_RESULTS)}")

        # 4) Generate Orders ─────────────────────────────────────────────────
        with _c4:
            if st.button(
                "⚙️ Generate Orders",
                type="primary" if frozen else "secondary",
                width="stretch",
                help="Freezes the dashboard (releases CID), runs derive.py, then reconnects. "
                     "Takes 2–5 min. Last-known data stays visible during the freeze.",
            ) and not frozen:
                _DERIVE_LOG.parent.mkdir(parents=True, exist_ok=True)
                log_fh = open(_DERIVE_LOG, "w", encoding="utf-8")  # noqa: SIM115
                st.session_state.pop("_derive_exit", None)
                st.session_state.pop("_derive_summary", None)
                st.session_state["frozen_for"] = "derive"
                client.freeze()
                new_proc = subprocess.Popen(
                    [sys.executable, str(_here() / "src" / "derive.py")],
                    stdout=log_fh, stderr=None, env=_sub_env(),
                )
                st.session_state["derive_proc"] = new_proc
                logger.info("derive.py started pid={}", new_proc.pid)
                st.rerun()
            st.checkbox(
                "Sow: DEPLOY only", key="ord_sow_deploy_only", value=False,
                help="When ON, Sow shows symbols rated DEPLOY in synthetic backtest OR trade "
                     "history, plus REFINE symbols with active overrides.",
            )
            if "_derive_exit" not in st.session_state and not frozen:
                ages = [_pkl_age(n) for n in ["df_cov.pkl", "df_nkd.pkl", "df_reap.pkl"]]
                age_str = ages[0] if len(set(ages)) == 1 else " | ".join(ages)
                st.caption(f"Last: {age_str}")

        # 5) Execute Orders ──────────────────────────────────────────────────
        with _c5:
            @st.dialog("⚠️ Confirm Order Execution", width="small")
            def _confirm_execute():
                st.markdown(
                    "This will execute all orders from the Suggested Orders section. "
                    "**This action is irreversible.** Are you sure?"
                )
                _ce1, _ce2 = st.columns(2)
                with _ce1:
                    if st.button("✅ Execute", width="stretch"):
                        st.session_state["_exec_confirmed"] = True
                        st.rerun()
                with _ce2:
                    if st.button("❌ Cancel", width="stretch"):
                        st.session_state.pop("_execute_exit", None)
                        st.session_state.pop("_exec_confirmed", None)
                        st.rerun()

            if st.button(
                "▶️ Execute Orders",
                type="primary" if (frozen and frozen_for == "execute") else "secondary",
                width="stretch",
                help="Execute all orders from the Suggested Orders section. Freezes the "
                     "dashboard, runs execute.py, then reconnects. ⚠️ This is IRREVERSIBLE.",
            ) and not frozen:
                _confirm_execute()

            if st.session_state.get("_exec_confirmed"):
                st.session_state.pop("_exec_confirmed", None)
                _EXECUTE_LOG.parent.mkdir(parents=True, exist_ok=True)
                log_fh = open(_EXECUTE_LOG, "w", encoding="utf-8")  # noqa: SIM115
                st.session_state.pop("_execute_exit", None)
                st.session_state["frozen_for"] = "execute"
                client.freeze()
                exec_new_proc = subprocess.Popen(
                    [sys.executable, str(_here() / "src" / "execute.py")],
                    stdout=log_fh, stderr=None, env=_sub_env(),
                )
                st.session_state["execute_proc"] = exec_new_proc
                logger.info("execute.py started pid={}", exec_new_proc.pid)

        # ── Cancel / Clear row ───────────────────────────────────────────────
        _CLEAR_PROTECTED = {"backtest_results.pkl", "backtest_ohlc.pkl", "symbol_overrides.json"}

        @st.dialog("⚠️ Confirm Clear Data", width="small")
        def _confirm_clear(files: list[str]):
            st.markdown(
                "The following derived files in `data/` will be **permanently deleted**. "
                "Backtest results, REFINE overrides, and OHLC cache are **kept**."
            )
            st.markdown("\n".join(f"- `{f}`" for f in files))
            _cc1, _cc2 = st.columns(2)
            with _cc1:
                if st.button("🗑️ Delete", width="stretch"):
                    st.session_state["_clear_confirmed"] = True
                    st.rerun()
            with _cc2:
                if st.button("❌ Cancel", width="stretch"):
                    st.rerun()

        @st.dialog("⚠️ Confirm Cancel Sell Orders", width="small")
        def _confirm_cancel_sells():
            st.markdown(
                "This will cancel all open **SELL option** orders. "
                "BUY orders and stock positions are unaffected. Are you sure?"
            )
            _cs1, _cs2 = st.columns(2)
            with _cs1:
                if st.button("🚫 Cancel Sells", width="stretch"):
                    st.session_state["_cancel_confirmed"] = True
                    st.session_state["_cancel_mode"] = "sells"
                    st.rerun()
            with _cs2:
                if st.button("❌ Back", width="stretch"):
                    st.rerun()

        @st.dialog("⚠️ Confirm Cancel ALL Orders", width="small")
        def _confirm_cancel_all():
            st.markdown(
                "This will call **reqGlobalCancel** and cancel **ALL** open orders. "
                "Are you sure?"
            )
            _ca1, _ca2 = st.columns(2)
            with _ca1:
                if st.button("🛑 Cancel All", width="stretch"):
                    st.session_state["_cancel_confirmed"] = True
                    st.session_state["_cancel_mode"] = "all"
                    st.rerun()
            with _ca2:
                if st.button("❌ Back", width="stretch"):
                    st.rerun()

        @st.dialog("⏻ Shutdown IB Monitor?", width="small")
        def _confirm_shutdown():
            st.markdown("Disconnect from IBKR and stop the dashboard server?")
            _sd1, _sd2 = st.columns(2)
            with _sd1:
                if st.button("⏻ Shutdown", width="stretch", type="primary"):
                    st.session_state["_shutdown_confirmed"] = True
                    st.rerun()
            with _sd2:
                if st.button("❌ Back", width="stretch"):
                    st.rerun()

        _ccol1, _ccol2, _ccol3, _ccol_spacer, _ccol_sd = st.columns([3, 3, 3, 4, 3])
        with _ccol1:
            if st.button(
                "🚫 Cancel Sell Orders",
                type="primary" if (frozen and frozen_for == "cancel"
                                   and st.session_state.get("_cancel_mode") == "sells") else "secondary",
                width="stretch",
                help="Cancel all open SELL option orders. BUY orders are unaffected.",
            ) and not frozen:
                _confirm_cancel_sells()
        with _ccol2:
            if st.button(
                "🛑 Cancel All Orders",
                type="primary" if (frozen and frozen_for == "cancel"
                                   and st.session_state.get("_cancel_mode") == "all") else "secondary",
                width="stretch",
                help="Send reqGlobalCancel — cancels ALL open orders.",
            ) and not frozen:
                _confirm_cancel_all()
        with _ccol3:
            if st.button(
                "🗑️ Clear Data",
                width="stretch",
                type="secondary",
                help="Delete derived pkl files for a fresh start (e.g. malformed chains). "
                     "Keeps backtest results, REFINE overrides, OHLC cache, and data/master/.",
            ) and not frozen:
                _files_to_clear = sorted(
                    p.name for p in _DATA_DIR.iterdir()
                    if p.is_file() and p.name not in _CLEAR_PROTECTED
                )
                if _files_to_clear:
                    _confirm_clear(_files_to_clear)
                else:
                    st.toast("No files to clear.")
        with _ccol_sd:
            if st.button(
                "⏻ Shutdown",
                width="stretch",
                help="Disconnect from IBKR and stop the server.",
            ) and not frozen:
                _confirm_shutdown()

        if st.session_state.pop("_shutdown_confirmed", None):
            client.stop()
            os._exit(0)

        if st.session_state.get("_cancel_confirmed"):
            _cmode = st.session_state.pop("_cancel_mode", "sells")
            st.session_state.pop("_cancel_confirmed", None)
            _CANCEL_LOG.parent.mkdir(parents=True, exist_ok=True)
            _cancel_log_fh = open(_CANCEL_LOG, "w", encoding="utf-8")  # noqa: SIM115
            st.session_state.pop("_cancel_exit", None)
            st.session_state["_cancel_mode"] = _cmode
            st.session_state["frozen_for"] = "cancel"
            client.freeze()
            cancel_new_proc = subprocess.Popen(
                [sys.executable, str(_here() / "src" / "cancel.py"), "--mode", _cmode],
                stdout=_cancel_log_fh, stderr=subprocess.STDOUT, env=_sub_env(),
            )
            st.session_state["cancel_proc"] = cancel_new_proc
            logger.info("cancel.py started pid={} mode={}", cancel_new_proc.pid, _cmode)

        # ── Status + logs (full width, below the pipeline row) ───────────────
        if frozen or "_derive_exit" in st.session_state or "_execute_exit" in st.session_state or "_cancel_exit" in st.session_state:
            gen_status_col, exec_status_col, cancel_status_col, _st_spacer = st.columns([2.5, 2.5, 2.5, 2.5])
            with gen_status_col:
                if frozen and frozen_for == "derive":
                    _pct, phase, _ = _derive_progress()
                    st.progress(max(_pct, 0.01), text=f"⏳ {phase}")
                elif "_derive_exit" in st.session_state:
                    rc = st.session_state["_derive_exit"]
                    if rc == 0:
                        if "_derive_summary" not in st.session_state:
                            _nc = len(_load_pkl("df_cov.pkl"))
                            _nn = len(_load_pkl("df_nkd.pkl"))
                            _nr = len(_load_pkl("df_reap.pkl"))
                            _np = len(_load_pkl("df_protect.pkl"))
                            st.session_state["_derive_summary"] = (
                                ([f"{_nc} covers"] if _nc else [])
                                + ([f"{_nn} nakeds"] if _nn else [])
                                + ([f"{_nr} reaps"] if _nr else [])
                                + ([f"{_np} protects"] if _np else [])
                            )
                        _parts = st.session_state["_derive_summary"]
                        if _parts:
                            st.success(f"✅ {', '.join(_parts)}")
                        else:
                            st.warning("⚠️ derive.py ran OK — no orders generated (check log)")
                    else:
                        st.error(f"❌ Generate Orders failed (exit {rc})")
            with exec_status_col:
                if frozen and frozen_for == "execute":
                    st.progress(0.5, text="⏳ Executing orders…")
                elif "_execute_exit" in st.session_state:
                    rc = st.session_state["_execute_exit"]
                    if rc == 0:
                        st.success("✅ Orders executed")
                    else:
                        st.error(f"❌ Order execution failed (exit {rc})")
            with cancel_status_col:
                if frozen and frozen_for == "cancel":
                    _cmode_lbl = st.session_state.get("_cancel_mode", "")
                    st.progress(0.5, text=f"⏳ Cancelling {'sell' if _cmode_lbl == 'sells' else 'all'} orders…")
                elif "_cancel_exit" in st.session_state:
                    rc = st.session_state["_cancel_exit"]
                    if rc == 0:
                        st.success("✅ Cancel completed")
                    else:
                        st.error(f"❌ Cancel failed (exit {rc})")

        if frozen and frozen_for == "execute":
            _exec_log = _here() / "log" / "execute.log"
            if _exec_log.exists():
                try:
                    _exec_live = _exec_log.read_text(encoding="utf-8", errors="replace").splitlines()[-30:]
                    if _exec_live:
                        st.code("\n".join(_exec_live), language=None)
                except Exception:
                    pass
        elif frozen and frozen_for == "cancel":
            if _CANCEL_LOG.exists():
                try:
                    _cancel_live = _CANCEL_LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-20:]
                    if _cancel_live:
                        st.code("\n".join(_cancel_live), language=None)
                except Exception:
                    pass
        elif frozen:
            log_lines = _derive_log_lines(35)
            if log_lines:
                st.code("\n".join(_strip_ansi(ln) for ln in log_lines), language=None)
        elif "_derive_exit" in st.session_state:
            rc = st.session_state["_derive_exit"]
            _render_log_expander("📋 derive.py log", _DERIVE_LOG, expanded=rc != 0)

        if "_execute_exit" in st.session_state:
            rc = st.session_state["_execute_exit"]
            _render_log_expander("📋 execute.py log", _EXECUTE_LOG, expanded=rc != 0)

        if "_cancel_exit" in st.session_state:
            rc = st.session_state["_cancel_exit"]
            _render_log_expander("📋 cancel.py log", _CANCEL_LOG, expanded=rc != 0)

        if any(k in st.session_state for k in (
            "_derive_exit", "_ohlc_exit", "_trades_exit", "_execute_exit", "_bt_exit", "_cancel_exit"
        )):
            _, _clr_col, _ = st.columns([8, 2, 8])
            with _clr_col:
                if st.button("🗑 Clear logs", key="btn_clear_logs", use_container_width=True):
                    for _k in ("_derive_exit", "_derive_summary", "_ohlc_exit",
                               "_trades_exit", "_execute_exit", "_bt_exit", "_cancel_exit"):
                        st.session_state.pop(_k, None)
                    st.session_state.pop("_cancel_mode", None)
                    for _lf in (_DERIVE_LOG, _OHLC_LOG, _EXECUTE_LOG, _CANCEL_LOG, _BACKTEST_LOG,
                                _here() / "log" / "update_trades.log"):
                        try:
                            if _lf.exists():
                                _lf.write_text("", encoding="utf-8")
                        except Exception:
                            pass
                    st.rerun()

        # OHLC status
        if _ohlc_running:
            _op, _ol = _ohlc_progress()
            st.progress(_op, text=f"⏳ {_ol}")
        elif "_ohlc_exit" in st.session_state:
            _ohlc_rc = st.session_state["_ohlc_exit"]
            if _ohlc_rc == 0:
                st.success("✅ OHLCs up to date")
            else:
                st.error(f"❌ OHLC fetch failed (exit {_ohlc_rc})")
        if _ohlc_running:
            _ohlc_live = _ohlc_log_lines(30)
            if _ohlc_live:
                st.code("\n".join(_strip_ansi(ln) for ln in _ohlc_live), language=None)
        if "_ohlc_exit" in st.session_state:
            _render_log_expander("📋 OHLC log", _OHLC_LOG, expanded=st.session_state["_ohlc_exit"] != 0)

        # Refresh Flex status
        if st.session_state.get("trades_proc") is not None:
            st.info("⏳ Refresh Flex running in background…")
        elif "_trades_exit" in st.session_state:
            _ut_rc = st.session_state["_trades_exit"]
            if _ut_rc == 0:
                st.success("✅ Refresh Flex completed")
            else:
                st.error(f"❌ Refresh Flex failed (exit {_ut_rc})")

        # Backtest status / results (render_actions' 5 s timer drives the refresh)
        _render_bt_status()

        if st.session_state.get("_clear_confirmed"):
            st.session_state.pop("_clear_confirmed", None)
            _cleared, _locked = [], []
            for _p in sorted(_DATA_DIR.iterdir()):
                if not _p.is_file() or _p.name in _CLEAR_PROTECTED:
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


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

# Seed config session_state from snp_config.yml at page top (one-shot, guarded).
# Must run here — not only inside render_config_panel — because Config is collapsed
# by default, and the Ask AI dock needs cfg_defaultai / cfg_aimodels to pick the
# right default provider even when the user never opens Config.
_init_cfg_state()

# Header bar: title | account selector | refresh
# (Theme switching is handled by Streamlit's burger menu → Settings → Appearance.)
_hdr_c, _spacer_c, _acct_c, _refresh_c = st.columns([4, 4.7, 1.6, 0.7])
with _hdr_c:
    header()
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
with _refresh_c:
    if st.button("↺", key="btn_nav_refresh", help="Reload all dashboard data"):
        st.session_state.pop("_ref_ovr_base", None)
        st.session_state.pop("_ref_ovr_mtime", None)
        st.session_state.pop("sym_override_editor", None)
        st.rerun()

# Log account-selector changes (fires once per actual change per session).
_prev_acct = st.session_state.get("_prev_acct_sel")
_curr_acct = st.session_state.get("acct_sel")
if _prev_acct is not None and _prev_acct != _curr_acct:
    logger.info("Account selector: {} → {}", _prev_acct, _curr_acct)
st.session_state["_prev_acct_sel"] = _curr_acct

# ── KPIs ────────────────────────────────────────────────────────────────────
with st.expander("\U0001F4CA KPIs", expanded=True, key="exp_kpis"):
    kpi_strip()

# ── Actions pipeline ─────────────────────────────────────────────────────────
render_actions()

# ── Ask AI ───────────────────────────────────────────────────────────────────
with st.expander("\U0001F916 Ask AI", expanded=False, key="exp_ask_ai"):
    _render_llm_chat()

# ── Single global filter — drives every panel below ──────────────────────────
render_filter_bar()

# ── Content: master collapsible sections (Performance / Orders / Config) ──────
# Streamlit forbids expander-in-expander, so master sections are button-toggled
# banners (st-key-btn_master_* styled in CSS) that show/hide their sub-expanders.
def _toggle_master(sk: str) -> None:
    st.session_state[sk] = not st.session_state.get(sk, False)


def _master_banner(title: str, key: str, *, default: bool = True) -> bool:
    """Draw the clay banner toggle button; return whether the section is open.

    The button is rendered inside a per-section fragment (below), so clicking it
    reruns only that section — not the whole dashboard.
    """
    sk = f"_master_{key}"
    if sk not in st.session_state:
        st.session_state[sk] = default
    is_open = st.session_state[sk]
    arrow = "▾" if is_open else "▸"
    # on_click toggles BEFORE the implicit (fragment-scoped) rerun → arrow +
    # content update in a single refresh; no explicit st.rerun() needed.
    st.button(
        f"{arrow}  {title}", key=f"btn{sk}", width="stretch",
        on_click=_toggle_master, args=(sk,),
    )
    return st.session_state[sk]


# Each master is its own fragment, so opening/closing it reruns only that
# section instead of the entire page. Header (2 s) and KPIs (10 s) keep their
# own timers, so they stay fresh without a page-wide rerun on toggle. Config
# *saves* still call st.rerun() (full app) so other fragments pick up new YAML.
@st.fragment
def _master_perf() -> None:
    # Performance: Positions, Performance Dashboard, Deep-Dive, Gaps, Trade Analysis
    if _master_banner("📊 Performance", "perf", default=True):
        render_analysis()


@st.fragment(run_every=10)
def _master_orders() -> None:
    # Orders: Open Orders + Suggested Orders (Cover / Monthly CC / Sow → REFINE / Reap / Protect)
    # run_every lives here (not on render_orders) so the 10 s refresh pauses while
    # the banner is collapsed — otherwise the inner fragment ticks on its own and
    # re-expands the order tables.
    if _master_banner("🗂 Orders", "orders", default=True):
        render_orders()


@st.fragment
def _master_config() -> None:
    # Config — last and separate
    if _master_banner("⚙️ Config", "config", default=False):
        render_config_panel()


_master_perf()
_master_orders()
_master_config()
