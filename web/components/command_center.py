"""Command Center — the dense centerpiece panel (Track 2 Phase 1).

Three columns mirroring quant_ops_alert_widget.svg, fed entirely by the
framework-agnostic risk/state helpers:

  LEFT   Position State Matrix  — pf_state badges + per-row dollar delta
  CENTER Risk Exposure          — margin gauges, full Greek$ set, BP / open P&L cards
  RIGHT  Alert Log              — derived from cushion breach + cover/protect gaps + IB errors

A bottom strip carries Recommended Action + System Status.

All pure-read: `command_center(snap, settings)` is `@ui.refreshable` and is
re-rendered by the page's 1 s timer.
"""

from __future__ import annotations

import math
from datetime import datetime

import pandas as pd
from nicegui import ui

from src.dashboard.formatting import money, pct, signed_money
from src.dashboard.ib_client import Snapshot
from src.dashboard.risk import (
    _join_tickers,
    account_kpis,
    cover_protect_gaps,
    greek_dollar_sums,
    position_delta_dollars,
)
from src.dashboard.settings import Settings
from src.dashboard.state import classify_portfolio

from .. import theme

_MATRIX_MAX_ROWS = 60      # cap the scroll list so a huge book stays responsive
_ALERT_MAX = 6             # alert cards shown in the right column
_PANEL_MIN_H = "320px"


# ---------------------------------------------------------------------------
# small building blocks
# ---------------------------------------------------------------------------
def _section_title(text: str) -> None:
    ui.label(text).classes("qo-label").style("margin-bottom:2px;")
    ui.element("div").style(
        f"height:1px; background:{theme.BORDER}; width:100%; margin-bottom:6px;"
    )


def _bar(label: str, value_text: str, frac: float, color: str, *, limit_frac: float | None = None) -> None:
    """A labelled horizontal gauge. frac/limit_frac are 0..1 (clamped)."""
    frac = max(0.0, min(1.0, frac if not math.isnan(frac) else 0.0))
    with ui.row().classes("w-full no-wrap items-center justify-between gap-0").style("margin-top:8px;"):
        ui.label(label).style(f"color:{theme.MUTED}; font-size:0.66rem;")
        ui.label(value_text).classes("qo-mono").style(
            f"color:{color}; font-size:0.74rem; font-weight:700;"
        )
    with ui.element("div").style(
        f"position:relative; width:100%; height:9px; background:{theme.PANEL_ALT}; "
        f"border:1px solid {theme.BORDER}; margin-top:2px;"
    ):
        ui.element("div").style(
            f"position:absolute; left:0; top:0; height:100%; width:{frac * 100:.1f}%; background:{color};"
        )
        if limit_frac is not None:
            lf = max(0.0, min(1.0, limit_frac))
            ui.element("div").style(
                f"position:absolute; left:{lf * 100:.1f}%; top:-2px; height:13px; "
                f"width:1.5px; background:{theme.TEXT};"
            )


def _greek_row(label: str, value_text: str, color: str) -> None:
    with ui.row().classes("w-full no-wrap items-center justify-between gap-0").style("margin-top:5px;"):
        ui.label(label).style(f"color:{theme.MUTED}; font-size:0.66rem;")
        ui.label(value_text).classes("qo-mono").style(
            f"color:{color}; font-size:0.78rem; font-weight:700;"
        )


def _metric_card(label: str, value_text: str, accent: str) -> None:
    with ui.column().classes("gap-0 items-center grow").style(
        f"background:{theme.PANEL_ALT}; border:1px solid {theme.BORDER}; padding:8px 6px;"
    ):
        ui.label(label).classes("qo-label")
        ui.label(value_text).classes("qo-mono").style(
            f"color:{accent}; font-size:1.05rem; font-weight:700;"
        )


def _pos_label(row: pd.Series) -> str:
    sym = str(row.get("symbol", ""))
    if row.get("secType") == "OPT":
        strike = row.get("strike") or 0.0
        right = str(row.get("right", "") or "")
        return f"{sym} {strike:g}{right}"
    qty = row.get("position", 0)
    return f"{sym} ×{qty:g}"


# ---------------------------------------------------------------------------
# columns
# ---------------------------------------------------------------------------
def _state_matrix(df: pd.DataFrame, net_delta: float) -> None:
    _section_title("POSITION STATE MATRIX")
    with ui.row().classes("w-full no-wrap gap-0").style("padding:0 4px 2px 4px;"):
        ui.label("POSITION").style(f"color:{theme.MUTED}; font-size:0.6rem; width:46%;")
        ui.label("STATE").style(f"color:{theme.MUTED}; font-size:0.6rem; width:28%;")
        ui.label("Δ EXPOSURE").style(
            f"color:{theme.MUTED}; font-size:0.6rem; width:26%; text-align:right;"
        )

    with ui.scroll_area().style(f"height:230px; width:100%; background:{theme.PANEL};"):
        if df.empty:
            ui.label("No positions").style(f"color:{theme.MUTED}; padding:8px;")
        else:
            ordered = df.reindex(df["delta_$"].abs().sort_values(ascending=False).index)
            for i, (_, row) in enumerate(ordered.head(_MATRIX_MAX_ROWS).iterrows()):
                bg, fg, badge = theme.state_badge(str(row.get("pf_state", "unknown")))
                d = float(row.get("delta_$", float("nan")))
                d_color = theme.MUTED if math.isnan(d) or abs(d) < 1 else (
                    theme.GREEN_FG if d > 0 else theme.GOLD
                )
                rowbg = theme.PANEL if i % 2 == 0 else theme.PANEL_ALT
                with ui.row().classes("w-full no-wrap items-center gap-0").style(
                    f"background:{rowbg}; padding:3px 4px;"
                ):
                    ui.label(_pos_label(row)).classes("qo-mono").style(
                        f"color:{theme.TEXT}; font-size:0.72rem; width:46%; font-weight:700;"
                    )
                    with ui.element("div").style("width:28%;"):
                        ui.label(badge).style(
                            f"background:{bg}; color:{fg}; font-size:0.58rem; font-weight:700; "
                            "padding:1px 6px; letter-spacing:0.04em;"
                        )
                    ui.label(signed_money(d)).classes("qo-mono").style(
                        f"color:{d_color}; font-size:0.72rem; width:26%; text-align:right;"
                    )

    with ui.row().classes("w-full no-wrap justify-between items-center gap-0").style(
        f"border-top:1px solid {theme.BORDER}; padding-top:4px; margin-top:4px;"
    ):
        ui.label("NET PORTFOLIO DELTA").style(f"color:{theme.MUTED}; font-size:0.66rem;")
        ui.label(signed_money(net_delta)).classes("qo-mono").style(
            f"color:{theme.GOLD}; font-size:0.86rem; font-weight:700;"
        )


def _risk_exposure(kpis: dict, greeks: dict, min_cushion: float) -> None:
    _section_title("RISK EXPOSURE")
    nlv = float(kpis["nlv"]) or 0.0
    init_m = float(kpis["init_margin"])
    maint_m = float(kpis["maint_margin"])
    breach = bool(kpis["cushion_breach"])

    util = (init_m / nlv) if nlv else 0.0
    util_color = theme.RUST_FG if breach else theme.GOLD
    _bar(
        "MARGIN UTILIZATION", pct(util), util,
        util_color, limit_frac=1.0 - min_cushion,
    )
    maint_util = (maint_m / nlv) if nlv else 0.0
    _bar("MAINT MARGIN / NLV", pct(maint_util), maint_util, theme.BLUE_FG)

    theta = greeks["theta_$"]
    vega = greeks["vega_$"]
    _greek_row("NET DELTA $", signed_money(greeks["delta_$"]), theme.GOLD)
    _greek_row(
        "DAILY THETA $", signed_money(theta),
        theme.GREEN_FG if theta >= 0 else theme.RUST_FG,
    )
    _greek_row(
        "VEGA $", signed_money(vega),
        theme.GREEN_FG if vega >= 0 else theme.RUST_FG,
    )
    _greek_row("GAMMA $", signed_money(greeks["gamma_$"]), theme.MUTED)

    with ui.row().classes("w-full no-wrap gap-2").style("margin-top:12px;"):
        _metric_card("BUYING POWER", money(kpis["excess_liquidity"]), theme.TEXT)
        upnl = float(kpis["unrealized_pnl"])
        _metric_card(
            "OPEN P&L", signed_money(upnl),
            theme.GREEN_FG if upnl >= 0 else theme.RUST_FG,
        )


def _build_alerts(snap: Snapshot, kpis: dict, gaps: pd.DataFrame, min_cushion: float) -> list[tuple]:
    """Return [(severity, time_str, line1, line2)] newest-relevant first."""
    now = datetime.now().strftime("%H:%M:%S")
    alerts: list[tuple] = []

    if kpis["cushion_breach"]:
        alerts.append((
            "critical", now,
            f"Cushion {pct(float(kpis['cushion']))} below min {pct(min_cushion)}",
            f"Excess liq {money(kpis['excess_liquidity'])}",
        ))

    if not gaps.empty:
        for _, g in gaps.head(3).iterrows():
            needs = str(g.get("needs", ""))
            alerts.append((
                "warn", now,
                f"{g['symbol']} needs {needs}",
                f"{int(g.get('shares', 0))} sh @ {money(g.get('mkt_px'))}",
            ))

    for ts, code, msg in list(snap.errors)[-3:][::-1]:
        if code in (-1, 200) or code is None:
            sev = "warn" if code == -1 else "info"
        else:
            sev = "info"
        tstr = ts.astimezone().strftime("%H:%M:%S") if hasattr(ts, "astimezone") else now
        alerts.append((sev, tstr, f"IB {code}", str(msg)[:42]))

    if not alerts:
        alerts.append((
            "resolved", now,
            "Book balanced — no gaps",
            "Margin within limits",
        ))
    return alerts[:_ALERT_MAX]


def _alert_log(alerts: list[tuple]) -> None:
    _section_title("ALERT LOG")
    with ui.scroll_area().style("height:282px; width:100%;"):
        for sev, tstr, line1, line2 in alerts:
            bg, accent, fg = theme.ALERT_STYLE.get(sev, theme.ALERT_STYLE["info"])
            with ui.column().classes("gap-0 w-full").style(
                f"background:{bg}; border-left:3px solid {accent}; padding:6px 8px; margin-bottom:6px;"
            ):
                ui.label(f"{sev.upper()}  {tstr}").style(
                    f"color:{accent}; font-size:0.62rem; font-weight:700;"
                )
                ui.label(line1).style(f"color:{fg}; font-size:0.74rem;")
                if line2:
                    ui.label(line2).style(f"color:{fg}; font-size:0.74rem;")


def _recommended_action(kpis: dict, gaps: pd.DataFrame, min_cushion: float) -> str:
    if kpis["cushion_breach"]:
        return (
            f"Reduce risk: close/roll short options or add margin. "
            f"Target cushion ≥ {pct(min_cushion)} (now {pct(float(kpis['cushion']))})."
        )
    if not gaps.empty:
        syms = ", ".join(str(s) for s in gaps["symbol"].head(5))
        return f"Cover/protect gaps on: {syms}. Run derive.py to generate orders."
    return "No action required — positions covered and within margin limits."


def _bottom_strip(snap: Snapshot, action: str, alert_mode: bool) -> None:
    with ui.row().classes("w-full no-wrap gap-2").style("padding:8px 0 0 0;"):
        with ui.column().classes("gap-0 grow").style(
            f"background:{theme.PANEL}; border-left:3px solid {theme.GOLD}; padding:8px 12px;"
        ):
            ui.label("RECOMMENDED ACTION").classes("qo-label").style(f"color:{theme.GOLD};")
            ui.label(action).style(f"color:{theme.TEXT}; font-size:0.82rem;")
        with ui.column().classes("gap-0").style(
            f"background:{theme.PANEL_ALT}; border:1px solid {theme.BORDER}; "
            "padding:8px 12px; min-width:230px;"
        ):
            ui.label("SYSTEM STATUS").classes("qo-label")
            feed_color = theme.GREEN_FG if snap.connected else theme.RUST_FG
            feed_text = "Data feed: LIVE" if snap.connected else "Data feed: DOWN"
            ui.label(f"⬤ {feed_text}").style(f"color:{feed_color}; font-size:0.74rem;")
            risk_color = theme.GOLD if alert_mode else theme.GREEN_FG
            risk_text = "Risk engine: ALERT MODE" if alert_mode else "Risk engine: NORMAL"
            ui.label(f"⬤ {risk_text}").style(f"color:{risk_color}; font-size:0.74rem;")


# ---------------------------------------------------------------------------
# public
# ---------------------------------------------------------------------------
@ui.refreshable
def command_center(snap: Snapshot, settings: Settings) -> None:
    min_cushion = settings.min_cushion
    positions = snap.positions if snap.positions is not None else pd.DataFrame()

    kpis = account_kpis(snap, min_cushion=min_cushion)
    greeks = greek_dollar_sums(positions, snap.tickers)
    gaps = cover_protect_gaps(
        positions, snap.tickers, protect_me=settings.protect_me, max_dte=settings.max_dte
    )

    if positions.empty:
        df = positions
    else:
        df = classify_portfolio(positions)
        df = _join_tickers(df, snap.tickers)
        df["delta_$"] = position_delta_dollars(df)

    alerts = _build_alerts(snap, kpis, gaps, min_cushion)
    alert_mode = bool(kpis["cushion_breach"]) or not gaps.empty
    action = _recommended_action(kpis, gaps, min_cushion)

    with ui.column().classes("w-full gap-0").style("padding:6px 14px;"):
        with ui.row().classes("w-full no-wrap gap-3 items-stretch"):
            with ui.column().classes("gap-0").style(
                f"width:34%; min-height:{_PANEL_MIN_H}; background:{theme.PANEL}; "
                f"border:1px solid {theme.BORDER}; padding:10px;"
            ):
                _state_matrix(df, greeks["delta_$"])
            with ui.column().classes("gap-0").style(
                f"width:33%; min-height:{_PANEL_MIN_H}; background:{theme.PANEL}; "
                f"border:1px solid {theme.BORDER}; padding:10px;"
            ):
                _risk_exposure(kpis, greeks, min_cushion)
            with ui.column().classes("gap-0").style(
                f"width:33%; min-height:{_PANEL_MIN_H}; background:{theme.PANEL}; "
                f"border:1px solid {theme.BORDER}; padding:10px;"
            ):
                _alert_log(alerts)
        _bottom_strip(snap, action, alert_mode)
