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

    class SuppressError200Filter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            return "Error 200" not in record.getMessage()

    handler.addFilter(SuppressError200Filter())
    root.addHandler(handler)

    # Suppress noisy third-party loggers from reaching stdout.
    # ib_async: errors go to ib_async.log via setup_ib_logging in derive.py;
    # propagate=False prevents them from also flooding stdout/derive_progress.log.
    for _noisy in ("urllib3", "asyncio"):
        logging.getLogger(_noisy).setLevel(logging.WARNING)
    _ib = logging.getLogger("ib_async")
    _ib.setLevel(logging.WARNING)
    _ib.propagate = False
    if not _ib.handlers:
        _ib.addHandler(logging.NullHandler())


def setup_ib_logging(path: Path, level: int = logging.ERROR) -> None:
    """Configure ib_async logger to write to a file directly and prevent propagation.

    This ensures that error messages go to the designated file and do not fall back
    to logging.lastResort (which prints unformatted messages to stderr/console).
    """
    ib_logger = logging.getLogger("ib_async")
    ib_logger.setLevel(level)
    ib_logger.propagate = False

    # Ensure parent directories exist
    path.parent.mkdir(parents=True, exist_ok=True)

    # Create file handler
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))

    # Clean up existing handlers on ib_async (like NullHandler) to avoid duplicates/conflicts
    for h in list(ib_logger.handlers):
        ib_logger.removeHandler(h)

    ib_logger.addHandler(handler)

