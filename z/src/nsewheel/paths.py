"""Project path helpers shared across modules."""

from __future__ import annotations

from pathlib import Path

# z/ project root (this file lives at src/nsewheel/paths.py)
ROOT = Path(__file__).resolve().parents[2]

CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
MASTER_DIR = DATA_DIR / "master"
LOG_DIR = ROOT / "log"


def ensure_dirs() -> None:
    """Create the gitignored runtime directories if missing."""
    for d in (DATA_DIR, MASTER_DIR, LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)
