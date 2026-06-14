"""Top alert bar — gold-accented session header (mirrors the SVG top bar)."""

from __future__ import annotations

from datetime import datetime, timezone

from nicegui import ui

from src.dashboard.ib_client import Snapshot

from .. import theme


@ui.refreshable
def alert_bar(snap: Snapshot) -> None:
    """Connection + session strip. Refreshed by the page timer."""
    connected = snap.connected
    if connected:
        dot, status, color = "⬤", "DATA FEED — LIVE", theme.GREEN_FG
    else:
        dot, status, color = "⬤", "DATA FEED — DISCONNECTED", theme.RUST_FG

    as_of = snap.as_of.astimezone() if snap.as_of else None
    clock = as_of.strftime("%H:%M:%S") if as_of else "--:--:--"
    today = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")

    with ui.row().classes("w-full items-center no-wrap gap-0").style(
        f"background:{theme.PANEL}; border-left:4px solid {theme.GOLD}; "
        f"border-bottom:1px solid {theme.BORDER}; padding:8px 18px;"
    ):
        with ui.column().classes("gap-0"):
            ui.label("QUANTITATIVE OPS · RISK MONITOR").classes("qo-label")
            ui.label(f"{dot} {status}").style(
                f"color:{color}; font-weight:700; font-size:0.82rem;"
            )
        ui.space()
        with ui.column().classes("gap-0 items-end"):
            ui.label(f"SESSION   {today}").classes("qo-label")
            ui.label(f"{clock} LOCAL").classes("qo-mono").style(
                f"color:{theme.TEXT}; font-weight:700; font-size:0.78rem;"
            )
