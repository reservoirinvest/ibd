"""log_utils.py — Shared logging setup for batch scripts.

Usage in any batch script:
    import argparse
    from src.log_utils import setup_logging

    p = argparse.ArgumentParser()
    p.add_argument("--debug", action="store_true")
    args = p.parse_args()
    setup_logging("my_script", debug=args.debug)
"""

from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger


def setup_logging(log_name: str, *, debug: bool = False, log_dir: Path | None = None) -> None:
    """Configure loguru sinks.

    Terminal: INFO+ normally; DEBUG when debug=True.
    File:     DEBUG always, at log/<log_name>.log, rotated every 2 days.
    """
    logger.remove()

    logger.add(
        sys.stderr,
        level="DEBUG" if debug else "INFO",
        colorize=True,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}",
    )

    if log_dir is None:
        from pyprojroot import here
        log_dir = here() / "log"

    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    logger.add(
        str(log_dir / f"{log_name}.log"),
        level="DEBUG",
        rotation="2 days",
        encoding="utf-8",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}",
    )
