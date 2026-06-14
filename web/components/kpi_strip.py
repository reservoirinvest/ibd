"""Live KPI strip — NLV, cushion, day P&L, net Greeks (dollar) from risk.py."""

from __future__ import annotations

from nicegui import ui

from src.dashboard.formatting import money, pct, signed_money
from src.dashboard.ib_client import Snapshot
from src.dashboard.risk import account_kpis, greek_dollar_sums

from .. import theme


def _card(label: str, value: str, *, accent: str = theme.TEXT, sub: str = "") -> None:
    with ui.column().classes("gap-0 items-center grow").style(
        f"background:{theme.PANEL}; border:1px solid {theme.BORDER}; "
        "padding:10px 8px; min-width:130px;"
    ):
        ui.label(label).classes("qo-label")
        ui.label(value).classes("qo-mono").style(
            f"color:{accent}; font-size:1.25rem; font-weight:700; line-height:1.2;"
        )
        if sub:
            ui.label(sub).style(f"color:{theme.MUTED}; font-size:0.62rem;")


@ui.refreshable
def kpi_strip(snap: Snapshot, *, min_cushion: float = 0.20) -> None:
    """Account + Greek KPIs. Refreshed by the page timer."""
    kpis = account_kpis(snap, min_cushion=min_cushion)
    greeks = greek_dollar_sums(snap.positions, snap.tickers)

    cushion = float(kpis["cushion"])
    cushion_accent = theme.RUST_FG if kpis["cushion_breach"] else theme.GREEN_FG
    day_pnl = float(kpis["day_pnl"])
    pnl_accent = theme.GREEN_FG if day_pnl >= 0 else theme.RUST_FG
    theta = greeks["theta_$"]
    theta_accent = theme.GREEN_FG if theta >= 0 else theme.RUST_FG

    n_pos = 0 if snap.positions is None or snap.positions.empty else len(snap.positions)

    with ui.row().classes("w-full no-wrap gap-2").style("padding:10px 14px;"):
        _card("NET LIQ VALUE", money(kpis["nlv"]), accent=theme.GOLD)
        _card(
            "CUSHION", pct(cushion), accent=cushion_accent,
            sub=f"min {pct(min_cushion)}",
        )
        _card("EXCESS LIQ", money(kpis["excess_liquidity"]), accent=theme.TEXT)
        _card("DAY P&L", signed_money(day_pnl), accent=pnl_accent)
        _card("NET DELTA $", signed_money(greeks["delta_$"]), accent=theme.GOLD)
        _card("DAILY THETA $", signed_money(theta), accent=theta_accent)
        _card("POSITIONS", str(n_pos), accent=theme.TEXT)
