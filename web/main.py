"""NiceGUI dashboard entry point — Phase 0 scaffold + live data bridge.

The existing asyncio `IBClient` daemon (src/dashboard/ib_client.py) owns its own
thread + event loop and is fully framework-agnostic. We start it once at app
startup and read `client.snapshot()` from a `ui.timer` — this replaces the
Streamlit fragment timers + MutationObserver pinning entirely.

Run:  uv run python web/main.py        (serves on http://127.0.0.1:8502)
The Streamlit app (port 8501) keeps working in parallel until Phase 4 parity.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow `python web/main.py` to import the `src` package without installation.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from loguru import logger  # noqa: E402
from nicegui import app, ui  # noqa: E402

from src.dashboard.ib_client import Snapshot, get_client  # noqa: E402
from src.dashboard.settings import get_settings  # noqa: E402
from web import theme  # noqa: E402
from web.components.command_center import command_center  # noqa: E402
from web.components.header import alert_bar  # noqa: E402
from web.components.kpi_strip import kpi_strip  # noqa: E402

_PORT = 8502  # 8501 belongs to the Streamlit app; run both side-by-side


@app.on_startup
def _start_ib() -> None:
    """Boot the IBKR daemon once when the web server comes up."""
    settings = get_settings()
    get_client().start(settings)
    logger.info("NiceGUI dashboard startup — IBClient daemon started")


@app.on_shutdown
def _stop_ib() -> None:
    try:
        get_client().stop()
    except Exception as exc:  # noqa: BLE001
        logger.debug("IBClient stop on shutdown: {}", exc)


@ui.page("/")
def dashboard() -> None:
    theme.apply_theme()
    settings = get_settings()
    client = get_client()

    # Static skeleton; refreshable regions fill in live data each tick.
    alert_bar(client.snapshot())
    kpi_strip(client.snapshot(), min_cushion=settings.min_cushion)
    command_center(client.snapshot(), settings)

    def _tick() -> None:
        snap: Snapshot = client.snapshot()
        alert_bar.refresh(snap)
        kpi_strip.refresh(snap, min_cushion=settings.min_cushion)
        command_center.refresh(snap, settings)

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


# `ui.run()` must execute at module import time under NiceGUI's auto-reload model,
# but we keep reload=False so a plain `python web/main.py` works predictably.
if __name__ in {"__main__", "__mp_main__"}:
    main()
