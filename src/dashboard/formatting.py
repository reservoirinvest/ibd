"""Tiny formatting helpers — keep ALL display logic here."""

from __future__ import annotations

import math
from decimal import Decimal


def money(x: float | Decimal | None, *, dp: int = 0) -> str:
    if x is None:
        return "—"
    f = float(x)
    if math.isnan(f) or math.isinf(f):
        return "—"
    sign = "-" if f < 0 else ""
    f = abs(f)
    return f"{sign}${f:,.{dp}f}"


def pct(x: float | None, *, dp: int = 1) -> str:
    if x is None:
        return "—"
    if math.isnan(x) or math.isinf(x):
        return "—"
    return f"{x * 100:.{dp}f}%"


def num(x: float | None, *, dp: int = 2) -> str:
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return "—"
    return f"{x:,.{dp}f}"


def signed_money(x: float | Decimal | None, *, dp: int = 0) -> str:
    if x is None:
        return "—"
    f = float(x)
    if math.isnan(f):
        return "—"
    return f"{'+' if f >= 0 else '-'}${abs(f):,.{dp}f}"


def state_color(state: str) -> str:
    """Hex color for a symbol/portfolio state — used by Plotly + st."""
    return {
        "zen": "#22c55e",
        "virgin": "#3b82f6",
        "covering": "#22c55e",
        "protecting": "#22c55e",
        "straddled": "#a855f7",
        "unprotected": "#f59e0b",
        "uncovered": "#f59e0b",
        "unreaped": "#ef4444",
        "exposed": "#ef4444",
        "orphaned": "#ef4444",
        "sowed": "#3b82f6",
        "unknown": "#6b7280",
    }.get(state, "#6b7280")
