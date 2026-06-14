"""Orders / Actions panel — process buttons with async subprocess streaming.

Each button launches a subprocess, streams its stdout into a shared ui.log widget,
and handles freeze/unfreeze for derive + execute (which require sole IBKR CID ownership).

No @ui.refreshable needed — the button/log structure is built once and stays alive;
only the log content changes (via .push()) and button styles via .props().
"""

from __future__ import annotations

import asyncio
import asyncio.subprocess
import os
import sys
from pathlib import Path

from nicegui import ui

from src.dashboard.ib_client import IBClient
from src.dashboard.ohlc import get_sp500_symbols, write_symbol_list

from .. import theme

_ROOT = Path(__file__).resolve().parent.parent.parent

# One active-process flag per button key — prevents double-launches.
_running: dict[str, bool] = {}


def _sub_env() -> dict[str, str]:
    root = str(_ROOT)
    existing = os.environ.get("PYTHONPATH", "")
    pythonpath = f"{root}{os.pathsep}{existing}" if existing else root
    return {
        **os.environ,
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONPATH": pythonpath,
        "PYTHONUNBUFFERED": "1",
    }


def _section_header(text: str) -> None:
    with ui.row().classes("w-full no-wrap items-center").style("margin-bottom:8px;"):
        ui.label(text).classes("qo-label").style(
            f"color:{theme.GOLD}; font-size:0.72rem; letter-spacing:0.12em;"
        )
        ui.element("div").style(
            f"flex:1; height:1px; background:{theme.BORDER}; margin-left:10px;"
        )


def _proc_button(label: str, icon: str = "", *, color: str = "grey-8") -> ui.button:
    return ui.button(f"{icon} {label}".strip()).props(f'color="{color}" flat no-caps').style(
        f"font-family:monospace; font-size:0.75rem; letter-spacing:0.05em; "
        f"border:1px solid {theme.BORDER}; padding:4px 12px;"
    )


async def _stream(
    cmd: list[str],
    log: ui.log,
    label: str,
    key: str,
    *,
    freeze_client: IBClient | None = None,
    status_btn: ui.button | None = None,
) -> int:
    """Launch cmd, stream stdout→log, handle freeze/unfreeze. Returns exit code."""
    if _running.get(key):
        log.push(f"⚠ {label} already running — ignored")
        return -1
    _running[key] = True
    if status_btn is not None:
        status_btn.props(add='color="warning"')
    if freeze_client is not None:
        freeze_client.freeze()
        log.push(f"🔒 IBKR CID released for {label}")
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=_sub_env(),
            cwd=str(_ROOT),
        )
        log.push(f"▶ {label} started (pid {proc.pid})")
        assert proc.stdout is not None
        while True:
            raw = await proc.stdout.readline()
            if not raw:
                break
            line = raw.decode(errors="replace").rstrip()
            if line:
                log.push(line)
        rc = await proc.wait()
        status = "✅ completed" if rc == 0 else f"❌ failed (exit {rc})"
        log.push(f"── {label} {status} ──")
        return rc
    except Exception as exc:
        log.push(f"❌ {label} error: {exc}")
        return -1
    finally:
        _running[key] = False
        if status_btn is not None:
            status_btn.props(remove='color="warning"')
        if freeze_client is not None:
            freeze_client.unfreeze()
            log.push("🔓 IBKR CID reconnected")


def process_panel(client: IBClient) -> None:
    """Render the Orders / Actions section. Called once at page build."""
    with ui.element("div").props('id="orders"').style("height:0; overflow:hidden;"):
        pass

    with ui.column().classes("w-full gap-0").style("padding:6px 14px 14px 14px;"):
        _section_header("ORDERS / ACTIONS")

        ui.label(
            "Daily flow: Generate OHLCs → Run Backtest → Generate Orders → "
            "review Suggested Orders → Execute Orders"
        ).style(f"color:{theme.MUTED}; font-size:0.68rem; font-family:monospace; margin-bottom:10px;")

        # ── Process log (built first so buttons can reference it in closures) ──
        log = ui.log(max_lines=400).style(
            f"height:260px; width:100%; font-family:monospace; font-size:0.72rem; "
            f"background:{theme.PANEL_ALT}; color:{theme.GREEN_FG}; "
            f"border:1px solid {theme.BORDER}; border-radius:2px; "
            "margin-top:10px;"
        )

        # ── Execute confirmation dialog (built before buttons reference it) ──
        with ui.dialog() as exec_dialog:
            with ui.card().style(
                f"background:{theme.PANEL}; border:2px solid {theme.RUST}; "
                "min-width:340px; padding:20px;"
            ):
                ui.label("⚠ CONFIRM ORDER EXECUTION").style(
                    f"color:{theme.RUST_FG}; font-size:0.9rem; font-weight:700; "
                    "font-family:monospace; margin-bottom:8px;"
                )
                ui.label(
                    "This submits all suggested orders to IBKR.\n"
                    "This action is IRREVERSIBLE."
                ).style(f"color:{theme.TEXT}; font-size:0.8rem; white-space:pre-line;")
                with ui.row().classes("w-full no-wrap gap-3").style("margin-top:14px;"):
                    btn_confirm_yes = ui.button("✅ Execute").props(
                        'color="negative" no-caps'
                    ).style("font-family:monospace;")
                    ui.button("❌ Cancel", on_click=exec_dialog.close).props(
                        'color="grey-7" flat no-caps'
                    ).style("font-family:monospace;")

        # ── Button rows ──────────────────────────────────────────────────────
        with ui.row().classes("no-wrap gap-2").style("margin-bottom:4px;"):
            btn_gen = _proc_button("Generate Orders", "⚙")
            btn_exec = _proc_button("Execute Orders", "▶", color="grey-9")

        with ui.row().classes("no-wrap gap-2 flex-wrap"):
            btn_ohlc     = _proc_button("Generate OHLCs",    "📊")
            btn_bt       = _proc_button("Run Backtest",       "🧪")
            btn_flex     = _proc_button("Refresh Flex",       "🔄")
            btn_weeklies = _proc_button("Identify Weeklies",  "📅")
            ui.button("🗑 Clear log").props('flat no-caps color="grey-7"').style(
                "font-family:monospace; font-size:0.72rem;"
            ).on("click", lambda: log.clear())

        # ── Button handlers ──────────────────────────────────────────────────

        async def _do_generate():
            await _stream(
                [sys.executable, str(_ROOT / "src" / "derive.py")],
                log, "Generate Orders", "derive",
                freeze_client=client, status_btn=btn_gen,
            )

        async def _do_execute():
            exec_dialog.close()
            await _stream(
                [sys.executable, str(_ROOT / "src" / "execute.py")],
                log, "Execute Orders", "execute",
                freeze_client=client, status_btn=btn_exec,
            )

        async def _do_ohlc():
            # Write the symbol list (S&P 500 + portfolio) before launching.
            snap = client.snapshot()
            try:
                sp500 = get_sp500_symbols()
                seen: set[str] = {s["symbol"] for s in sp500}
                extra: list[dict] = []
                if snap.positions is not None and not snap.positions.empty:
                    for _, row in snap.positions.iterrows():
                        sym = str(row.get("symbol", ""))
                        if sym and sym not in seen:
                            extra.append({
                                "symbol":   sym,
                                "exchange": str(row.get("primaryExch", "")) or "SMART",
                                "currency": str(row.get("currency", "")) or "USD",
                            })
                            seen.add(sym)
                write_symbol_list(sp500 + extra)
                log.push(f"ℹ Symbol list updated: {len(sp500) + len(extra)} symbols")
            except Exception as exc:
                log.push(f"⚠ Symbol list update failed: {exc} — proceeding with existing list")
            await _stream(
                [sys.executable, str(_ROOT / "src" / "fetch_ohlc.py")],
                log, "Generate OHLCs", "ohlc",
                status_btn=btn_ohlc,
            )

        async def _do_backtest():
            await _stream(
                [sys.executable, str(_ROOT / "scripts" / "run_backtest.py")],
                log, "Run Backtest", "backtest",
                status_btn=btn_bt,
            )

        async def _do_flex():
            await _stream(
                [sys.executable, str(_ROOT / "scripts" / "update_trades.py")],
                log, "Refresh Flex", "flex",
                status_btn=btn_flex,
            )

        async def _do_weeklies():
            await _stream(
                [sys.executable, str(_ROOT / "scripts" / "update_symbol_categories.py")],
                log, "Identify Weeklies", "weeklies",
                status_btn=btn_weeklies,
            )

        # Wire buttons
        btn_gen.on("click", _do_generate)
        btn_exec.on("click", exec_dialog.open)
        btn_confirm_yes.on("click", _do_execute)
        btn_ohlc.on("click", _do_ohlc)
        btn_bt.on("click", _do_backtest)
        btn_flex.on("click", _do_flex)
        btn_weeklies.on("click", _do_weeklies)
