"""log_utils.py — Shared logging setup for batch scripts."""
from __future__ import annotations

import logging
import sys
from pathlib import Path


_FMT  = "%(asctime)s | %(levelname)-8s | %(message)s"
_DATE = "%H:%M:%S"


def setup_logging(log_name: str, *, debug: bool = False, log_dir: Path | None = None) -> None:
    """Configure stdlib logging — stdout only, no file handler.

    When run as a subprocess, app.py captures stdout+stderr to a single log file.
    """
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    root.handlers.clear()

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(logging.DEBUG if debug else logging.INFO)
    handler.setFormatter(logging.Formatter(_FMT, datefmt=_DATE))
    root.addHandler(handler)

    # Suppress noisy third-party loggers
    for _noisy in ("ib_async", "urllib3", "asyncio"):
        logging.getLogger(_noisy).setLevel(logging.WARNING)
