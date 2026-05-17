from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> None:
    app = Path(__file__).resolve().parent.parent / "app.py"
    sys.exit(
        subprocess.run(
            [sys.executable, "-m", "streamlit", "run", str(app), "--server.address=127.0.0.1"],
        ).returncode
    )
