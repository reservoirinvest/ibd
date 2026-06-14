"""Positions panel — live portfolio table, refreshed by the 1 s timer."""

from __future__ import annotations

import math

import pandas as pd
from nicegui import ui

from src.dashboard.formatting import money, signed_money
from src.dashboard.ib_client import Snapshot
from src.dashboard.state import classify_portfolio

from .. import theme


def _section_header(text: str) -> None:
    with ui.row().classes("w-full no-wrap items-center").style("margin-bottom:4px;"):
        ui.label(text).classes("qo-label").style(
            f"color:{theme.GOLD}; font-size:0.72rem; letter-spacing:0.12em;"
        )
        ui.element("div").style(
            f"flex:1; height:1px; background:{theme.BORDER}; margin-left:10px;"
        )


def _col_header(text: str, width: str, *, right: bool = False) -> None:
    align = "right" if right else "left"
    ui.label(text).style(
        f"color:{theme.MUTED}; font-size:0.6rem; width:{width}; text-align:{align}; "
        "letter-spacing:0.05em;"
    )


def _pos_label(row: pd.Series) -> str:
    sym = str(row.get("symbol", ""))
    if row.get("secType") == "OPT":
        strike = row.get("strike") or 0.0
        right = str(row.get("right", "") or "")
        exp = str(row.get("expiry", "") or "")
        exp_short = exp[2:] if len(exp) == 8 else exp  # YYYYMMDD → YYMMDD
        return f"{sym} {exp_short} {strike:g}{right}"
    return sym


@ui.refreshable
def positions_panel(snap: Snapshot) -> None:
    """Live positions table with pf_state badges."""
    with ui.element("div").props('id="positions"').style("height:0; overflow:hidden;"):
        pass

    with ui.column().classes("w-full gap-0").style("padding:6px 14px 10px 14px;"):
        _section_header("POSITIONS")

        positions = snap.positions if snap.positions is not None else pd.DataFrame()

        if positions.empty:
            with ui.element("div").style(
                f"background:{theme.PANEL}; border:1px solid {theme.BORDER}; "
                "padding:16px; text-align:center;"
            ):
                status = "Connecting to IBKR…" if not snap.connected else "No positions held"
                ui.label(status).style(f"color:{theme.MUTED}; font-size:0.8rem;")
            return

        df = classify_portfolio(positions)
        df = df.sort_values("marketValue", key=lambda s: s.abs(), ascending=False)

        n_pos = len(df)
        total_mv = float(df["marketValue"].sum())
        total_upnl = float(df["unrealizedPNL"].sum()) if "unrealizedPNL" in df.columns else float("nan")

        # Summary strip
        with ui.row().classes("w-full no-wrap items-center gap-4").style("margin-bottom:8px;"):
            ui.label(f"{n_pos} positions").style(
                f"color:{theme.MUTED}; font-size:0.72rem; font-family:monospace;"
            )
            ui.label(f"MKT VAL {money(total_mv)}").style(
                f"color:{theme.TEXT}; font-size:0.72rem; font-family:monospace;"
            )
            upnl_color = (
                theme.MUTED if math.isnan(total_upnl) else
                (theme.GREEN_FG if total_upnl >= 0 else theme.RUST_FG)
            )
            ui.label(f"UNREAL P&L {signed_money(total_upnl)}").style(
                f"color:{upnl_color}; font-size:0.72rem; font-family:monospace;"
            )

        # Table
        with ui.element("div").style(
            f"background:{theme.PANEL}; border:1px solid {theme.BORDER}; "
            "overflow-x:auto; width:100%;"
        ):
            # Header row
            with ui.row().classes("w-full no-wrap").style(
                f"background:{theme.PANEL_ALT}; padding:4px 6px; "
                f"border-bottom:1px solid {theme.BORDER}; min-width:700px;"
            ):
                _col_header("SYMBOL",    "35%")
                _col_header("TYPE",      "7%")
                _col_header("QTY",       "8%",  right=True)
                _col_header("LAST",      "10%", right=True)
                _col_header("MKT VAL",   "14%", right=True)
                _col_header("UNREAL P&L","14%", right=True)
                _col_header("STATE",     "12%")

            # Data rows
            with ui.scroll_area().style("max-height:340px; width:100%; min-width:700px;"):
                for i, (_, row) in enumerate(df.iterrows()):
                    rowbg = theme.PANEL if i % 2 == 0 else theme.PANEL_ALT
                    upnl = float(row.get("unrealizedPNL", float("nan")))
                    upnl_col = (
                        theme.MUTED if math.isnan(upnl) else
                        (theme.GREEN_FG if upnl >= 0 else theme.RUST_FG)
                    )
                    mv = float(row.get("marketValue", float("nan")))
                    last = float(row.get("marketPrice", float("nan")))
                    qty = int(row.get("position", 0))
                    sec = str(row.get("secType", ""))
                    pf_state = str(row.get("pf_state", "unknown"))
                    bg, fg, badge = theme.state_badge(pf_state)

                    with ui.row().classes("w-full no-wrap items-center").style(
                        f"background:{rowbg}; padding:3px 6px;"
                    ):
                        ui.label(_pos_label(row)).classes("qo-mono").style(
                            f"color:{theme.TEXT}; font-size:0.72rem; width:35%; "
                            "font-weight:600; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;"
                        )
                        ui.label(sec).classes("qo-mono").style(
                            f"color:{theme.MUTED}; font-size:0.66rem; width:7%;"
                        )
                        ui.label(str(qty)).classes("qo-mono").style(
                            f"color:{theme.TEXT}; font-size:0.72rem; width:8%; text-align:right;"
                        )
                        ui.label(
                            f"${last:.2f}" if not math.isnan(last) else "—"
                        ).classes("qo-mono").style(
                            f"color:{theme.MUTED}; font-size:0.72rem; width:10%; text-align:right;"
                        )
                        ui.label(money(mv)).classes("qo-mono").style(
                            f"color:{theme.TEXT}; font-size:0.72rem; width:14%; text-align:right;"
                        )
                        ui.label(signed_money(upnl)).classes("qo-mono").style(
                            f"color:{upnl_col}; font-size:0.72rem; width:14%; text-align:right;"
                        )
                        with ui.element("div").style("width:12%;"):
                            ui.label(badge).style(
                                f"background:{bg}; color:{fg}; font-size:0.58rem; font-weight:700; "
                                "padding:1px 5px; letter-spacing:0.04em; display:inline-block;"
                            )
