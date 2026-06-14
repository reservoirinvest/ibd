"""NiceGUI dashboard entry point — Phase 2: single scrolling page + process buttons.

Page structure (scroll-to-anchor):
  nav_bar (sticky)
  #cmd-center   → command_center (refreshed 1s)
  #positions    → positions_panel (refreshed 1s)
  #orders       → process_panel (static; log streams live)
  #performance  → placeholder (Phase 3)
  #ask-ai       → placeholder (Phase 3)

Run:  uv run python web/main.py        (serves on http://127.0.0.1:8502)
The Streamlit app (port 8501) keeps working in parallel until Phase 4 parity.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from loguru import logger  # noqa: E402
from nicegui import app, ui  # noqa: E402

from src.dashboard.ib_client import get_client  # noqa: E402
from src.dashboard.settings import get_settings  # noqa: E402
from web import theme  # noqa: E402
from web.components.command_center import command_center  # noqa: E402
from web.components.header import alert_bar  # noqa: E402
from web.components.kpi_strip import kpi_strip  # noqa: E402
from web.components.nav_bar import nav_bar  # noqa: E402
from web.components.positions_panel import positions_panel  # noqa: E402
from web.components.process_panel import process_panel  # noqa: E402

_PORT = 8502


@app.on_startup
def _start_ib() -> None:
    settings = get_settings()
    get_client().start(settings)
    logger.info("NiceGUI dashboard startup — IBClient daemon started")


@app.on_shutdown
def _stop_ib() -> None:
    try:
        get_client().stop()
    except Exception as exc:  # noqa: BLE001
        logger.debug("IBClient stop on shutdown: {}", exc)


def _placeholder_section(anchor: str, title: str, note: str) -> None:
    with ui.element("div").props(f'id="{anchor}"').style("height:0; overflow:hidden;"):
        pass
    with ui.column().classes("w-full gap-0").style("padding:6px 14px 14px 14px;"):
        with ui.row().classes("w-full no-wrap items-center").style("margin-bottom:8px;"):
            ui.label(title).classes("qo-label").style(
                f"color:{theme.GOLD}; font-size:0.72rem; letter-spacing:0.12em;"
            )
            ui.element("div").style(
                f"flex:1; height:1px; background:{theme.BORDER}; margin-left:10px;"
            )
        with ui.element("div").style(
            f"background:{theme.PANEL}; border:1px solid {theme.BORDER}; "
            "padding:24px; text-align:center;"
        ):
            ui.label(note).style(f"color:{theme.MUTED}; font-size:0.8rem; font-family:monospace;")


@ui.page("/")
def dashboard() -> None:
    theme.apply_theme()
    settings = get_settings()
    client = get_client()

    # Sticky nav — built once, stays at top
    nav_bar()

    # Alert bar + KPI strip — always visible below nav
    with ui.element("div").props('id="cmd-center"').style("height:0; overflow:hidden;"):
        pass
    alert_bar(client.snapshot())
    kpi_strip(client.snapshot(), min_cushion=settings.min_cushion)

    # Command center — dense 3-column panel
    command_center(client.snapshot(), settings)

    # Positions — live portfolio table
    positions_panel(client.snapshot())

    # Orders / Actions — process buttons + subprocess log
    process_panel(client)

    # Phase-3 placeholders
    _placeholder_section(
        "performance", "PERFORMANCE",
        "Cumulative performance chart, P&L bars, drawdown — coming in Phase 3.",
    )
    _placeholder_section(
        "ask-ai", "ASK AI",
        "LLM portfolio analysis dock — coming in Phase 3.",
    )

    # 1-second timer refreshes live-data regions
    def _tick() -> None:
        snap = client.snapshot()
        alert_bar.refresh(snap)
        kpi_strip.refresh(snap, min_cushion=settings.min_cushion)
        command_center.refresh(snap, settings)
        positions_panel.refresh(snap)

    ui.timer(1.0, _tick)


def main() -> None:
    ui.run(
        host="127.0.0.1",
        port=_PORT,
        title="Quant Ops — Risk Monitor",
        dark=True,
        reload=False,
        show=False,
        favicon="🟡",
    )


if __name__ in {"__main__", "__mp_main__"}:
    main()
