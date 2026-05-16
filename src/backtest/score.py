"""Backtest Expert scoring — adapted from tradermonty/claude-trading-skills.

Scores a strategy on 4 dimensions (0-25 each → 0-100 composite):
  sample_score     — trade count and density
  expectancy_score — profit factor quality
  risk_score       — drawdown severity
  robustness_score — test period length

Verdict thresholds: DEPLOY ≥70, REFINE 40-69, ABANDON <40 or any CRITICAL flag.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd


@dataclass
class BacktestScore:
    symbol: str
    strategy: str
    total_trades: int
    win_rate: float
    profit_factor: float
    max_drawdown_pct: float
    years_tested: float

    sample_score: float = 0.0
    expectancy_score: float = 0.0
    risk_score: float = 0.0
    robustness_score: float = 0.0
    composite: float = 0.0
    red_flags: list[str] = field(default_factory=list)
    verdict: str = "UNKNOWN"

    def compute(self) -> "BacktestScore":
        flags: list[str] = []

        # Sample size (0-25)
        density = self.total_trades / max(self.years_tested, 0.1)
        if self.total_trades < 30:
            flags.append("CRITICAL: Fewer than 30 trades — insufficient sample")
            self.sample_score = 0.0
        elif self.total_trades < 100:
            self.sample_score = 10.0 + min(15.0, density * 0.3)
        else:
            self.sample_score = min(25.0, 15.0 + density * 0.1)

        # Expectancy (0-25)
        if self.profit_factor < 1.0:
            flags.append("CRITICAL: Profit factor < 1.0 (negative expectancy)")
            self.expectancy_score = 0.0
        elif self.profit_factor < 1.5:
            self.expectancy_score = 10.0 + (self.profit_factor - 1.0) * 20.0
        else:
            self.expectancy_score = min(25.0, 20.0 + (self.profit_factor - 1.5) * 10.0)

        # Risk management (0-25)
        if self.max_drawdown_pct > 40.0:
            flags.append("CRITICAL: Max drawdown > 40%")
            self.risk_score = 0.0
        elif self.max_drawdown_pct > 25.0:
            flags.append("WARNING: Max drawdown > 25%")
            self.risk_score = 8.0
        elif self.max_drawdown_pct > 15.0:
            flags.append("INFO: Max drawdown > 15%")
            self.risk_score = 15.0
        else:
            self.risk_score = 25.0

        # Robustness (0-25)
        if self.years_tested < 3.0:
            flags.append("CRITICAL: Test period < 3 years — insufficient history")
            self.robustness_score = 0.0
        elif self.years_tested < 5.0:
            flags.append("WARNING: Test period < 5 years")
            self.robustness_score = 10.0
        else:
            self.robustness_score = min(25.0, 10.0 + (self.years_tested - 5.0) * 3.0)

        self.composite = round(
            self.sample_score + self.expectancy_score + self.risk_score + self.robustness_score, 1
        )
        criticals = [f for f in flags if f.startswith("CRITICAL")]
        self.verdict = "ABANDON" if (criticals or self.composite < 40.0) else (
            "DEPLOY" if self.composite >= 70.0 else "REFINE"
        )
        self.red_flags = flags
        return self


def score_from_trades(
    df: pd.DataFrame,
    symbol: str,
    strategy: str = "options",
) -> BacktestScore:
    """Derive BacktestScore from a normalized Flex trade DataFrame for one symbol."""
    und = "underlyingSymbol" if "underlyingSymbol" in df.columns else "symbol"
    oc = "openCloseIndicator" if "openCloseIndicator" in df.columns else None
    cat = "assetCategory" if "assetCategory" in df.columns else None

    mask = df[und] == symbol
    if cat:
        mask &= df[cat] == "OPT"
    if oc:
        mask &= df[oc] == "C"

    sub = df[mask].copy()
    empty = BacktestScore(
        symbol=symbol, strategy=strategy, total_trades=0,
        win_rate=0.0, profit_factor=0.0, max_drawdown_pct=100.0, years_tested=0.0,
    ).compute()

    if sub.empty or "pnl" not in sub.columns:
        return empty

    pnl = sub["pnl"].dropna()
    n = len(pnl)
    if n == 0:
        return empty

    wins = pnl[pnl > 0]
    losses = pnl[pnl <= 0]
    gross_loss = abs(float(losses.sum())) or 1.0
    win_rate = len(wins) / n
    pf = float(wins.sum()) / gross_loss

    cum = pnl.cumsum()
    peak = cum.cummax()
    dd_pct = abs(float((cum - peak).min()) / float(peak.max()) * 100) if peak.max() > 0 else 0.0

    years = 0.0
    if "dateTime" in sub.columns:
        dt = sub["dateTime"].dropna()
        years = (dt.max() - dt.min()).days / 365.0 if len(dt) >= 2 else 0.0

    return BacktestScore(
        symbol=symbol, strategy=strategy, total_trades=n,
        win_rate=win_rate, profit_factor=round(pf, 3),
        max_drawdown_pct=round(dd_pct, 1), years_tested=round(years, 1),
    ).compute()
