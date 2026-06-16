"""Display + numeric helpers (INR-aware). Broker-agnostic."""

from __future__ import annotations

import math
from decimal import Decimal


def _na(x) -> bool:
    return x is None or (isinstance(x, float) and math.isnan(x))


def rupees(x, *, dp: int = 0) -> str:
    """Format as ₹1,23,456 (Indian grouping) or — for missing values."""
    if _na(x):
        return "—"
    x = float(x)
    sign = "-" if x < 0 else ""
    s = f"{abs(x):.{dp}f}"
    if "." in s:
        intpart, frac = s.split(".")
        frac = "." + frac
    else:
        intpart, frac = s, ""
    # Indian grouping: last 3 digits, then groups of 2.
    if len(intpart) > 3:
        head, tail = intpart[:-3], intpart[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        intpart = ",".join(groups) + "," + tail
    return f"{sign}₹{intpart}{frac}"


def pct(x, *, dp: int = 1) -> str:
    if _na(x):
        return "—"
    return f"{float(x) * 100:.{dp}f}%"


def round_to_tick(price: float, tick: float = 0.05) -> float:
    """Round a price to the nearest exchange tick (NSE options default 0.05)."""
    if _na(price) or tick <= 0:
        return 0.0
    return float((Decimal(str(price)) / Decimal(str(tick))).quantize(Decimal("1")) * Decimal(str(tick)))
