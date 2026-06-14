"""Quant-ops terminal palette + global theme for the NiceGUI dashboard.

Colours are lifted from `quant_ops_alert_widget.svg` (dark-navy / gold). The
state-badge map adapts the live `pf_state` taxonomy (see src/dashboard/state.py)
to that palette so position rows read the same as the reference widget.

Pure data + one `apply_theme()` side-effect — no IBKR / Streamlit coupling.
"""

from __future__ import annotations

from nicegui import ui

# ---------------------------------------------------------------------------
# Core palette (from the SVG)
# ---------------------------------------------------------------------------
BG = "#07162C"          # root background
PANEL = "#0E294E"       # raised panel / even rows
PANEL_ALT = "#07162C"   # odd rows (same as bg — zebra against PANEL)
FOOTER = "#0A192F"
GOLD = "#D4AF37"        # primary accent / alerts / KPIs
BORDER = "#1E56A0"      # hairline dividers + bar outlines
MUTED = "#5A6B82"       # labels, secondary text
TEXT = "#F8F9FA"        # primary text

# Semantic accents
GREEN = "#0F6E56"
GREEN_FG = "#9FE1CB"
PURPLE = "#3C3489"
PURPLE_FG = "#CECBF6"
BLUE = "#1E56A0"
BLUE_FG = "#F8F9FA"
RUST = "#993C1D"
RUST_FG = "#F5C4B3"
AMBER = "#412402"
AMBER_FG = "#FAC775"
RESOLVED = "#04342C"
RESOLVED_FG = "#5DCAA5"

# ---------------------------------------------------------------------------
# pf_state → (badge_bg, badge_fg, label) — drives Position State Matrix badges
# ---------------------------------------------------------------------------
STATE_BADGE: dict[str, tuple[str, str, str]] = {
    "sowed":       (BLUE, BLUE_FG, "SOWN"),
    "covering":    (GREEN, GREEN_FG, "COVERS"),
    "protecting":  (PURPLE, PURPLE_FG, "PROTECTS"),
    "zen":         (GREEN, GREEN_FG, "ZEN"),
    "straddled":   (PURPLE, PURPLE_FG, "STRADDLE"),
    "uncovered":   (AMBER, AMBER_FG, "UNCOVERED"),
    "unprotected": (AMBER, AMBER_FG, "UNPROT"),
    "exposed":     (RUST, RUST_FG, "EXPOSED"),
    "orphaned":    (RUST, RUST_FG, "ORPHAN"),
    "unknown":     (MUTED, TEXT, "—"),
}


def state_badge(state: str) -> tuple[str, str, str]:
    """(bg, fg, label) for a pf_state — falls back to the muted 'unknown' chip."""
    return STATE_BADGE.get(state, STATE_BADGE["unknown"])


# ---------------------------------------------------------------------------
# Alert-log severity → (panel_bg, accent_bar, fg)
# ---------------------------------------------------------------------------
ALERT_STYLE: dict[str, tuple[str, str, str]] = {
    "critical": ("#4A1B0C", GOLD, RUST_FG),
    "warn":     (AMBER, "#EF9F27", AMBER_FG),
    "info":     ("#042C53", "#378ADD", "#B5D4F4"),
    "resolved": (RESOLVED, "#1D9E75", RESOLVED_FG),
}


def apply_theme() -> None:
    """Install the global dark-navy theme. Call once per page (in the page fn)."""
    ui.colors(
        primary=GOLD,
        secondary=BLUE,
        accent=GREEN,
        dark=BG,
        positive=GREEN_FG,
        negative=RUST_FG,
        warning=AMBER_FG,
        info=BLUE_FG,
    )
    ui.add_head_html(
        f"""
        <style>
            :root {{ --qo-bg: {BG}; --qo-panel: {PANEL}; --qo-gold: {GOLD};
                     --qo-border: {BORDER}; --qo-muted: {MUTED}; --qo-text: {TEXT}; }}
            body {{ background: {BG}; color: {TEXT};
                    font-family: 'Calibri', 'Open Sans', 'Segoe UI', sans-serif; }}
            .nicegui-content {{ padding: 0; }}
            .qo-label {{ color: {MUTED}; font-size: 0.62rem; font-weight: 700;
                         letter-spacing: 0.12em; text-transform: uppercase;
                         font-family: 'Trebuchet MS', Arial, sans-serif; }}
            .qo-panel {{ background: {PANEL}; border: 1px solid {BORDER}; }}
            .qo-mono {{ font-variant-numeric: tabular-nums;
                        font-family: 'Trebuchet MS', Arial, sans-serif; }}
        </style>
        """
    )
