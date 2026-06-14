"""Sticky top navigation bar — scroll-to-anchor links for each page section."""

from __future__ import annotations

from nicegui import ui

from .. import theme

_SECTIONS: list[tuple[str, str]] = [
    ("Command Center", "cmd-center"),
    ("Positions",      "positions"),
    ("Orders",         "orders"),
    ("Performance",    "performance"),
    ("Ask AI",         "ask-ai"),
]


def nav_bar() -> None:
    """Render a sticky top bar with logo + anchor links. Called once at page build."""
    with ui.element("div").style(
        f"position:sticky; top:0; z-index:1000; "
        f"background:{theme.BG}; border-bottom:2px solid {theme.GOLD}; "
        "padding:7px 16px;"
    ):
        with ui.row().classes("w-full no-wrap items-center").style("gap:0;"):
            ui.label("⬡ QUANT OPS").style(
                f"color:{theme.GOLD}; font-size:0.85rem; font-weight:700; "
                "font-family:monospace; letter-spacing:0.12em; margin-right:20px;"
            )
            ui.element("div").style(
                f"width:1px; height:18px; background:{theme.BORDER}; margin-right:20px;"
            )
            for label, anchor in _SECTIONS:
                ui.html(
                    f'<a href="#{anchor}" style="'
                    f"color:{theme.MUTED}; font-size:0.76rem; font-family:monospace; "
                    f"text-decoration:none; letter-spacing:0.06em; padding:4px 10px; "
                    f"border-radius:3px; transition:color 0.15s;"
                    f'" onmouseover="this.style.color=\'{theme.TEXT}\'" '
                    f'onmouseout="this.style.color=\'{theme.MUTED}\'">{label}</a>'
                )
