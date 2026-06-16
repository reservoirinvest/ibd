"""Broker-agnostic backtest scoring (ported from ibd)."""

from .score import BacktestScore, score_from_trades

__all__ = ["BacktestScore", "score_from_trades"]
